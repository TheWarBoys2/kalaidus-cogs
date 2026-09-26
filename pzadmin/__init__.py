from redbot.core.bot import Red

from .pzadmin import PZAdmin

__red_end_user_data_statement__ = (
    "This cog stores which channels belong to which server and which show its status. Mod requests send the requester's "
    "Discord username and ID to your PZAdmin, which keeps them with the request."
)


async def setup(bot: Red) -> None:
    await bot.add_cog(PZAdmin(bot))
