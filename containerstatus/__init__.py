from redbot.core.bot import Red

from .containerstatus import ContainerStatus

__red_end_user_data_statement__ = "This cog stores no data about users."


async def setup(bot: Red) -> None:
    await bot.add_cog(ContainerStatus(bot))
