import asyncio
import logging
from typing import Tuple

import aiohttp
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

log = logging.getLogger("red.kalaidus.foundry")

API_PATH = "/api/olivetin.api.v1.OliveTinApiService/StartActionAndWait"
MAX_OUTPUT = 1800


class Foundry(commands.Cog):
    """Foundry VTT maintenance tasks, run through OliveTin."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1262570561, force_registration=True)
        self.config.register_global(
            base_url=None,
            fixperms_action="fix-gmjj-perms",
            timeout=330,
        )
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        return  # no user data stored

    # ---------- OliveTin call ----------

    async def _run_action(self, action_id: str) -> Tuple[bool, str]:
        base_url = await self.config.base_url()
        tokens = await self.bot.get_shared_api_tokens("olivetin")
        api_key = tokens.get("api_key")

        if not base_url or not api_key:
            return False, "Not configured. Owner: set [p]foundryset url and the olivetin api_key."

        timeout = aiohttp.ClientTimeout(total=await self.config.timeout())
        url = base_url.rstrip("/") + API_PATH

        try:
            async with self._session.post(
                url,
                json={"actionId": action_id},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    return False, f"OliveTin returned HTTP {resp.status}: {body}"
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return False, "Timed out waiting for OliveTin."
        except aiohttp.ClientError as e:
            return False, f"Couldn't reach OliveTin: {e}"

        entry = data.get("logEntry") or {}
        if entry.get("blocked"):
            return False, "OliveTin blocked the action (rate limit or concurrency)."
        if entry.get("timedOut"):
            return False, "The action timed out on the server."

        output = (entry.get("output") or "").strip() or "(no output)"
        if len(output) > MAX_OUTPUT:
            output = "..." + output[-MAX_OUTPUT:]
        return entry.get("exitCode") == 0, output

    # ---------- user commands ----------

    @commands.hybrid_group(name="foundry")
    @commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def foundry(self, ctx: commands.Context) -> None:
        """Foundry VTT maintenance tasks."""

    @foundry.command(name="fixperms")
    @commands.cooldown(1, 60, commands.BucketType.guild)
    async def foundry_fixperms(self, ctx: commands.Context) -> None:
        """Fix file permissions on the gmjj Foundry instance."""
        if self._lock.locked():
            await ctx.send("A Foundry task is already running - hang on a moment.")
            return

        async with self._lock:
            log.info("fixperms requested by %s (%s)", ctx.author, ctx.author.id)
            async with ctx.typing():
                ok, output = await self._run_action(await self.config.fixperms_action())

        status = "✅ Permissions fixed." if ok else "❌ Something went wrong."
        await ctx.send(f"{status}\n{box(output)}")
        log.info("fixperms result for %s: %s", ctx.author.id, "ok" if ok else "failed")

    # ---------- owner settings ----------

    @commands.group(name="foundryset")
    @commands.is_owner()
    async def foundryset(self, ctx: commands.Context) -> None:
        """Configure the Foundry cog."""

    @foundryset.command(name="url")
    async def foundryset_url(self, ctx: commands.Context, url: str) -> None:
        """Set the OliveTin base URL, e.g. http://192.168.0.103:1337"""
        await self.config.base_url.set(url.rstrip("/"))
        await ctx.send(f"OliveTin URL set to `{url.rstrip('/')}`.")

    @foundryset.command(name="action")
    async def foundryset_action(self, ctx: commands.Context, action_id: str) -> None:
        """Set the OliveTin action ID used by fixperms."""
        await self.config.fixperms_action.set(action_id)
        await ctx.send(f"fixperms will run OliveTin action `{action_id}`.")

    @foundryset.command(name="show")
    async def foundryset_show(self, ctx: commands.Context) -> None:
        """Show current settings (never shows the API key)."""
        tokens = await self.bot.get_shared_api_tokens("olivetin")
        await ctx.send(
            box(
                f"URL:      {await self.config.base_url()}\n"
                f"Action:   {await self.config.fixperms_action()}\n"
                f"Timeout:  {await self.config.timeout()}s\n"
                f"API key:  {'set' if tokens.get('api_key') else 'NOT SET'}"
            )
        )
