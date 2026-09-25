from redbot.core.bot import Red

from .rolegate import RoleGate

__red_end_user_data_statement__ = (
    "This cog stores Discord user IDs for pending role requests only. "
    "They are removed once a request is approved or denied."
)


async def setup(bot: Red) -> None:
    await bot.add_cog(RoleGate(bot))
