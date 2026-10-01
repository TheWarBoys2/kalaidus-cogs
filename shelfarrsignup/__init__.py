from redbot.core.bot import Red

from .shelfarrsignup import ShelfarrSignup

__red_end_user_data_statement__ = (
    "This cog stores Discord user IDs with the Shelfarr username they asked for, while a request is "
    "pending and after an account is created, so the same person can't request twice. "
    "Passwords are never stored."
)


async def setup(bot: Red) -> None:
    await bot.add_cog(ShelfarrSignup(bot))
