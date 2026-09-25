from redbot.core.bot import Red

from .foundry import Foundry

__red_end_user_data_statement__ = "This cog does not store any end user data."


async def setup(bot: Red) -> None:
    await bot.add_cog(Foundry(bot))
