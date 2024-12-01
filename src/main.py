from typing import List
from dotenv import load_dotenv

load_dotenv()
from os import environ
from qdrant_client import AsyncQdrantClient, models
import backoff
from aiohttp import ClientResponseError
import disnake
from disnake.ext import commands
from aiohttp_client_cache.session import CachedSession
from aiohttp_client_cache import RedisBackend
from rich.console import Console

console = Console()

cache = RedisBackend(
    cache_name="wpa-recommendations-aiohttp-cache",
    address=environ["REDIS_URL"],
    expire_after=43200,  # 12 hours
)

qdrant = AsyncQdrantClient(
    url=environ["QDRANT_HOST"],
    port=int(environ["QDRANT_PORT"]),
    grpc_port=int(environ["QDRANT_GRPC_PORT"]),
    prefer_grpc=True,
    api_key=(environ["QDRANT_API_KEY"]),
)
COLLECTION_NAME = "wpa_recommendations"

intents = disnake.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("r!"),
    intents=intents,
    case_insensitive=True,
)
bot.load_extension("jishaku")

headers = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36"
}


class NotFoundError(Exception): ...


class SearchCog(commands.Cog):
    """Search Cog."""

    def __init__(self, bot: commands.Bot):
        """Initialize."""
        self.bot = bot

    @backoff.on_exception(backoff.expo, ClientResponseError, max_time=15)
    async def retrieve_list_stories(
        self, list_id: int, cache_break: bool = False
    ) -> List[int]:
        """Retrieve the list of stories on a Wattpad list."""
        async with CachedSession(
            headers=headers, cache=None if cache_break else cache
        ) as session:
            async with session.get(
                f"https://www.wattpad.com/api/v3/lists/{list_id}?fields=stories(id)"
            ) as response:
                data = await response.json()

                if response.status == 400:
                    match data.get("error_code"):
                        case 1011:  # List not found
                            raise NotFoundError()
                response.raise_for_status()

        return [int(story["id"]) for story in data["stories"]]

    @backoff.on_exception(backoff.expo, ClientResponseError, max_time=15)
    async def retrieve_story(self, story_id: int) -> dict:
        """Retrieve Story metadata."""
        async with CachedSession(headers=headers, cache=cache) as session:
            async with session.get(
                f"https://www.wattpad.com/api/v3/stories/{story_id}?fields=title,voteCount,language(name)"
            ) as response:
                data = await response.json()
                if response.status == 400:
                    match data.get("error_code"):
                        case 1017:  # Story not found
                            raise NotFoundError()
                response.raise_for_status()

        return data

    @commands.slash_command(
        name="recommend",
        description="Recommend stories",
    )
    async def text_slash_cmd(
        self,
        interaction: disnake.GuildCommandInteraction,
        list_url: str = commands.Param(
            name="list_url", description="Wattpad list to base recommendations off of."
        ),
        refresh: bool = commands.Param(
            default=False, description="Refresh the list, update the local copy."
        ),
    ):
        await interaction.response.defer()

        try:
            story_ids = await self.retrieve_list_stories(
                int(list_url.split("/")[-1].split("-")[0]), cache_break=refresh
            )
        except NotFoundError:
            await interaction.send("List not found")
            return

        valid_story_ids = [
            int(record.id)
            for record in await qdrant.retrieve(COLLECTION_NAME, ids=story_ids)
        ]

        recommendations = [
            (point.id, int(point.score * 100))
            for point in (
                await qdrant.query_points(
                    collection_name=COLLECTION_NAME,
                    query=models.RecommendQuery(
                        recommend=models.RecommendInput(
                            positive=valid_story_ids,  # type: ignore
                            strategy=models.RecommendStrategy.AVERAGE_VECTOR,
                        )
                    ),
                    limit=5,
                )
            ).points
        ]

        recommendations_text = ""
        for recommendation, score in recommendations:
            try:
                metadata = await self.retrieve_story(int(recommendation))
            except NotFoundError:
                continue

            title = f"[{metadata['title'][:25]}{'...' if len(metadata['title'])>25 else ''}](https://wattpad.com/story/{recommendation})"
            language = metadata["language"]["name"].capitalize()
            recommendations_text = (
                recommendations_text
                + f"• {score}% Match: {title} [{language}] [{metadata['voteCount']:,} Vote{'s' if int(metadata['voteCount']) != 1 else ''}]\n"
            )
        embed = disnake.Embed(
            title="Recommendations",
            description=f"I found {len(valid_story_ids)} of {len(story_ids)} stories in my dataset. ({(len(valid_story_ids)/len(story_ids))*100:.0f}% found)",
            color=disnake.Color.dark_green(),
        )
        embed.add_field(
            name=f"Results ({len(recommendations)})", value=recommendations_text
        )
        embed.set_footer(
            text=f"Requested by {interaction.author.global_name} | Some results may have been removed from Wattpad"
        )

        await interaction.send(embed=embed)


bot.add_cog(SearchCog(bot))


@bot.event
async def on_ready():
    console.print(f"Logged in as {bot.user} (ID: {bot.user.id})\n------")


if __name__ == "__main__":
    bot.run(environ["TOKEN"])
