import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

log = logging.getLogger("red.kalaidus.pzadmin")

API_PREFIX = "/api/v1"
READ_TIMEOUT = 10
RESTART_TIMEOUT = 360  # PZAdmin: "a restart or stop can take a few minutes to answer"
AUTOCOMPLETE_TIMEOUT = 2.5  # Discord wants an autocomplete answer within 3s
CACHE_TTL = 60
CONFIRM_TIMEOUT = 30
MAX_REASON = 200
MAX_EMBED_DESC = 4000
MAX_FIELD = 1000
NO_MENTIONS = discord.AllowedMentions.none()

STATE_ICONS = {
    "online": "\N{LARGE GREEN CIRCLE}",
    "offline": "\N{LARGE RED CIRCLE}",
    "restarting": "\N{LARGE YELLOW CIRCLE}",
    "deploying": "\N{LARGE YELLOW CIRCLE}",
    "stopped": "\N{MEDIUM BLACK CIRCLE}",
    "unknown": "\N{MEDIUM WHITE CIRCLE}",
}


class PZAdminError(Exception):
    """An error with a message that is safe to show in Discord."""


def _icon(server: Dict[str, Any]) -> str:
    return STATE_ICONS.get(server.get("state") or "unknown", STATE_ICONS["unknown"])


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _timestamp(value: Optional[str]) -> str:
    dt = _parse_time(value)
    return discord.utils.format_dt(dt, "R") if dt else "never"


# ---------- confirmation ----------


class ConfirmView(discord.ui.View):
    """Confirm / Cancel buttons that only the invoker can press."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.author_id = author_id
        self.value: Optional[bool] = None
        self.interaction: Optional[discord.Interaction] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, value: bool) -> None:
        self.value = value
        self.interaction = interaction
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, False)


# ---------- cog ----------


class PZAdmin(commands.Cog):
    """Project Zomboid servers, through the PZAdmin API."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1262570563, force_registration=True)
        self.config.register_global(base_url=None)
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: List[Dict[str, Any]] = []
        self._cache_time = 0.0
        self._cache_lock = asyncio.Lock()
        self._restart_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        return  # no user data stored

    # ---------- PZAdmin API ----------

    async def _request(
        self, method: str, path: str, *, json: Optional[dict] = None, timeout: float = READ_TIMEOUT
    ) -> Any:
        base_url = await self.config.base_url()
        tokens = await self.bot.get_shared_api_tokens("pzadmin")
        api_key = tokens.get("api_key")
        if not base_url or not api_key:
            raise PZAdminError(
                "PZAdmin isn't configured yet. The bot owner needs to run `pzadminset url` "
                "and set the key with `set api pzadmin api_key <key>`."
            )

        url = base_url + API_PREFIX + path
        try:
            async with self._session.request(
                method,
                url,
                json=json,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
                if resp.status < 400:
                    return data
                error = data.get("error") if isinstance(data, dict) else None
                raise PZAdminError(self._error_message(resp, error))
        except asyncio.TimeoutError:
            raise PZAdminError(f"PZAdmin didn't answer within {int(timeout)} seconds.") from None
        except aiohttp.ClientError as e:
            log.warning("PZAdmin unreachable at %s: %s", base_url, e)
            raise PZAdminError("Couldn't reach PZAdmin. Is it running?") from None

    @staticmethod
    def _error_message(resp: aiohttp.ClientResponse, error: Optional[str]) -> str:
        status = resp.status
        if status == 401:
            return "PZAdmin rejected the API key."
        if status == 403:
            return "PZAdmin rejected the API key: it doesn't have the access this needs."
        if status == 404:
            return "PZAdmin doesn't know that server (or this key can't see it)."
        if status == 409:
            return "A restart, stop or start is already running for that server."
        if status == 429:
            wait = resp.headers.get("Retry-After")
            return f"PZAdmin is rate limiting us. Try again in {wait} seconds." if wait else (
                "PZAdmin is rate limiting us. Try again shortly."
            )
        if status == 502:
            return "The game server didn't answer PZAdmin over RCON."
        detail = f": {_truncate(error, 200)}" if error else ""
        return f"PZAdmin returned HTTP {status}{detail}"

    async def _fetch_servers(self, timeout: float = READ_TIMEOUT) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/servers", timeout=timeout)
        servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(servers, list):
            raise PZAdminError("PZAdmin sent a server list I couldn't read.")
        self._cache = servers
        self._cache_time = time.monotonic()
        return servers

    async def _servers_cached(self, timeout: float = READ_TIMEOUT) -> List[Dict[str, Any]]:
        async with self._cache_lock:
            if self._cache and time.monotonic() - self._cache_time < CACHE_TTL:
                return self._cache
            return await self._fetch_servers(timeout)

    async def _resolve(self, name: str) -> Dict[str, Any]:
        """Match a name (or ID) against PZAdmin's server list, refreshing once on a miss."""
        wanted = name.strip().casefold()

        def find(servers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            by_id = [s for s in servers if str(s.get("id", "")).casefold() == wanted]
            return by_id or [s for s in servers if str(s.get("name", "")).casefold() == wanted]

        matches = find(await self._servers_cached())
        if not matches:
            async with self._cache_lock:
                matches = find(await self._fetch_servers())
        if not matches:
            raise PZAdminError(f"No server called `{_truncate(name, 60)}`. Try `pz servers`.")
        if len(matches) > 1:
            raise PZAdminError(f"More than one server is called `{_truncate(name, 60)}`.")
        return matches[0]

    async def _server_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        try:
            servers = await self._servers_cached(timeout=AUTOCOMPLETE_TIMEOUT)
        except PZAdminError:
            return []
        current = current.casefold()
        choices = []
        for s in servers:
            name = str(s.get("name") or s.get("id") or "")
            if name and current in name.casefold():
                choices.append(app_commands.Choice(name=_truncate(f"{_icon(s)} {name}", 100), value=name[:100]))
        return choices[:25]

    # ---------- read-only commands ----------

    @commands.hybrid_group(name="pz")
    @commands.guild_only()
    async def pz(self, ctx: commands.Context) -> None:
        """Project Zomboid server info."""

    @pz.command(name="servers")
    async def pz_servers(self, ctx: commands.Context) -> None:
        """List servers, whether they're up and how many are playing."""
        async with ctx.typing():
            try:
                servers = await self._fetch_servers()
            except PZAdminError as e:
                await ctx.send(str(e))
                return

        if not servers:
            await ctx.send("PZAdmin has no servers this key can see.")
            return

        lines = []
        for s in sorted(servers, key=lambda s: str(s.get("name", "")).casefold()):
            name = discord.utils.escape_markdown(str(s.get("name") or s.get("id")))
            state = s.get("state") or "unknown"
            if s.get("online"):
                detail = f"{s.get('playerCount', 0)}/{s.get('maxPlayers', '?')} players"
            else:
                detail = state
            lines.append(f"{_icon(s)} **{name}** - {detail}")
        embed = discord.Embed(
            title="Project Zomboid servers",
            description=_truncate("\n".join(lines), MAX_EMBED_DESC),
            colour=await ctx.embed_colour(),
        )
        await ctx.send(embed=embed)

    @pz.command(name="status")
    @app_commands.describe(server="Server name")
    async def pz_status(self, ctx: commands.Context, *, server: str) -> None:
        """Show details for one server."""
        async with ctx.typing():
            try:
                s = await self._resolve(server)
                s = await self._request("GET", f"/servers/{s['id']}")
            except PZAdminError as e:
                await ctx.send(str(e), allowed_mentions=NO_MENTIONS)
                return

        state = s.get("state") or "unknown"
        embed = discord.Embed(
            title=_truncate(f"{_icon(s)} {s.get('name') or s.get('id')}", 256),
            description=_truncate(s.get("description") or "", MAX_EMBED_DESC) or None,
            colour=await ctx.embed_colour(),
        )
        embed.add_field(name="State", value=state.capitalize())
        embed.add_field(name="Players", value=f"{s.get('playerCount', 0)}/{s.get('maxPlayers', '?')}")
        if s.get("online"):
            embed.add_field(name="Uptime", value=_duration(s.get("uptimeSec")))
        else:
            embed.add_field(name="Last online", value=_timestamp(s.get("lastOnline")))
        if s.get("address"):
            addr = f"{s['address']}:{s['port']}" if s.get("port") else str(s["address"])
            embed.add_field(name="Address", value=f"`{_truncate(addr, 200)}`")
        if s.get("latencyMs") is not None:
            embed.add_field(name="Latency", value=f"{s['latencyMs']} ms")
        mods = f"{s.get('modsEnabled', 0)} enabled"
        missing = s.get("modsMissing") or []
        if missing:
            mods += f", {len(missing)} missing"
        embed.add_field(name="Mods", value=mods)
        backups = f"{s.get('backupCount', 0)}, last {_timestamp(s.get('lastBackup'))}"
        if s.get("backupRunning"):
            backups += " (one running now)"
        embed.add_field(name="Backups", value=backups)
        if s.get("pendingRestartAt"):
            embed.add_field(name="Restart scheduled", value=_timestamp(s.get("pendingRestartAt")))
        if missing:
            names = ", ".join(str(m) for m in missing)
            embed.add_field(name="Missing mods", value=_truncate(names, MAX_FIELD), inline=False)
        embed.set_footer(text="Last checked")
        checked = _parse_time(s.get("lastCheck"))
        if checked:
            embed.timestamp = checked
        await ctx.send(embed=embed)

    @pz.command(name="players")
    @app_commands.describe(server="Server name")
    async def pz_players(self, ctx: commands.Context, *, server: str) -> None:
        """Show who's online on a server."""
        async with ctx.typing():
            try:
                s = await self._resolve(server)
                data = await self._request("GET", f"/servers/{s['id']}/players")
            except PZAdminError as e:
                await ctx.send(str(e), allowed_mentions=NO_MENTIONS)
                return

        players = data.get("players") if isinstance(data, dict) else data
        if not isinstance(players, list):
            await ctx.send("PZAdmin sent a player list I couldn't read.")
            return

        now = datetime.now(timezone.utc)
        online = [p for p in players if isinstance(p, dict) and p.get("online")]
        online.sort(key=lambda p: str(p.get("name", "")).casefold())
        name = str(s.get("name") or s.get("id"))
        if not online:
            await ctx.send(
                f"Nobody is online on **{discord.utils.escape_markdown(name)}**.",
                allowed_mentions=NO_MENTIONS,
            )
            return

        lines = []
        for p in online:
            line = f"\N{BULLET} {discord.utils.escape_markdown(str(p.get('name', '?')))}"
            since = _parse_time(p.get("onlineSince"))
            if since:
                line += f" - {_duration((now - since).total_seconds())}"
            lines.append(line)
        embed = discord.Embed(
            title=_truncate(f"{_icon(s)} {name}: {len(online)} online", 256),
            description=_truncate("\n".join(lines), MAX_EMBED_DESC),
            colour=await ctx.embed_colour(),
        )
        await ctx.send(embed=embed)

    @pz_status.autocomplete("server")
    async def pz_status_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return await self._server_autocomplete(interaction, current)

    @pz_players.autocomplete("server")
    async def pz_players_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return await self._server_autocomplete(interaction, current)

    # ---------- control commands ----------

    @commands.hybrid_group(name="pzctl")
    @commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def pzctl(self, ctx: commands.Context) -> None:
        """Control Project Zomboid servers."""

    @pzctl.command(name="restart")
    @commands.cooldown(1, 60, commands.BucketType.guild)
    @app_commands.describe(server="Server name", reason="Optional reason, shown to players in PZAdmin's announcements")
    async def pzctl_restart(self, ctx: commands.Context, server: str, *, reason: Optional[str] = None) -> None:
        """Gracefully restart a server (warns players, saves, restarts)."""
        who = f"{ctx.author} ({ctx.author.id})"
        await ctx.defer(ephemeral=True)  # slash only; the server lookup can outlast Discord's 3s window
        try:
            s = await self._resolve(server)
        except PZAdminError as e:
            ctx.command.reset_cooldown(ctx)
            log.info("pzctl restart %r by %s: rejected (%s)", server, who, e)
            await ctx.send(str(e), ephemeral=True, allowed_mentions=NO_MENTIONS)
            return

        server_id = str(s["id"])
        name = str(s.get("name") or server_id)
        safe_name = discord.utils.escape_markdown(name)
        if reason:
            reason = reason[:MAX_REASON]
        lock = self._restart_locks[server_id]
        if lock.locked():
            ctx.command.reset_cooldown(ctx)
            log.info("pzctl restart %s by %s: already in progress", name, who)
            await ctx.send(f"A restart of **{safe_name}** is already in progress.", ephemeral=True)
            return

        players = s.get("playerCount", 0)
        prompt = f"Restart **{safe_name}**? {players} player{'s' if players != 1 else ''} online."
        if reason:
            prompt += f"\nReason: {discord.utils.escape_markdown(reason)}"
        view = ConfirmView(ctx.author.id)
        msg = await ctx.send(prompt, view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)
        timed_out = await view.wait()

        if timed_out or not view.value:
            ctx.command.reset_cooldown(ctx)
            text = "Timed out. Nothing was restarted." if timed_out else "Cancelled. Nothing was restarted."
            log.info("pzctl restart %s by %s: %s", name, who, "timed out" if timed_out else "cancelled")
            if view.interaction:
                await view.interaction.response.edit_message(content=text, view=None)
            else:
                try:
                    await msg.edit(content=text, view=None)
                except discord.HTTPException:
                    pass
            return

        if lock.locked():
            await view.interaction.response.edit_message(
                content=f"A restart of **{safe_name}** started meanwhile. Not sending another.", view=None
            )
            log.info("pzctl restart %s by %s: already in progress", name, who)
            return

        async with lock:
            await view.interaction.response.edit_message(
                content=f"Restarting **{safe_name}**. PZAdmin warns players first, so this can take a few minutes.",
                view=None,
            )
            log.info("pzctl restart %s (%s) confirmed by %s", name, server_id, who)
            body: Dict[str, Any] = {"action": "restart"}
            if reason:
                body["reason"] = reason
            try:
                result = await self._request(
                    "POST", f"/servers/{server_id}/lifecycle", json=body, timeout=RESTART_TIMEOUT
                )
            except PZAdminError as e:
                log.warning("pzctl restart %s by %s: failed (%s)", name, who, e)
                await ctx.send(f"\N{CROSS MARK} Restart of **{safe_name}** failed: {e}", allowed_mentions=NO_MENTIONS)
                return

        log.info("pzctl restart %s by %s: ok", name, who)
        text = f"\N{WHITE HEAVY CHECK MARK} **{safe_name}** restarted, requested by {ctx.author.mention}."
        detail = None
        if isinstance(result, dict):
            detail = result.get("response") or result.get("message")
        if detail:
            text += "\n" + box(_truncate(str(detail), 1500))
        await ctx.send(text, allowed_mentions=NO_MENTIONS)

    @pzctl_restart.autocomplete("server")
    async def pzctl_restart_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        return await self._server_autocomplete(interaction, current)

    # ---------- owner settings ----------

    @commands.group(name="pzadminset")
    @commands.is_owner()
    async def pzadminset(self, ctx: commands.Context) -> None:
        """Configure the PZAdmin cog."""

    @pzadminset.command(name="url")
    async def pzadminset_url(self, ctx: commands.Context, url: str) -> None:
        """Set PZAdmin's base URL, e.g. http://192.168.0.50:27815"""
        url = url.strip("<>").rstrip("/")
        if url.endswith(API_PREFIX):
            url = url[: -len(API_PREFIX)]
        if not url.startswith(("http://", "https://")):
            await ctx.send("The URL needs to start with http:// or https://.")
            return
        await self.config.base_url.set(url)
        self._cache, self._cache_time = [], 0.0
        await ctx.send(f"PZAdmin URL set to `{url}`.")

    @pzadminset.command(name="show")
    async def pzadminset_show(self, ctx: commands.Context) -> None:
        """Show current settings and check the connection (never shows the API key)."""
        tokens = await self.bot.get_shared_api_tokens("pzadmin")
        lines = [
            f"URL:      {await self.config.base_url() or 'NOT SET'}",
            f"API key:  {'set' if tokens.get('api_key') else 'NOT SET'}",
        ]
        async with ctx.typing():
            try:
                info = await self._request("GET", "/key")
            except PZAdminError as e:
                lines.append(f"Check:    FAILED - {e}")
            else:
                lines.append("Check:    OK")
                if isinstance(info, dict):
                    for key, value in info.items():
                        if isinstance(value, (list, tuple)):
                            value = ", ".join(str(v) for v in value) or "-"
                        lines.append(f"{str(key)[:12] + ':':<10}{_truncate(str(value), 150)}")
        await ctx.send(box(_truncate("\n".join(lines), 1900)))
