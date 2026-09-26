import asyncio
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

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
MAX_NOTE = 300  # PZAdmin's limit on a mod request note
MAX_REQUESTED_BY = 100
WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
NO_MENTIONS = discord.AllowedMentions.none()

# Status dots in channel names. These match PZAdmin's own "Show 🟢 / 🔴 in the
# channel's name" option, so either can tidy up after the other.
DOT_ONLINE = "\N{LARGE GREEN CIRCLE}"
DOT_OFFLINE = "\N{LARGE RED CIRCLE}"
DOT_RESTARTING = "\N{LARGE ORANGE CIRCLE}"
DOT_PREFIX = re.compile(
    "^(?:\N{LARGE GREEN CIRCLE}|\N{LARGE RED CIRCLE}|\N{LARGE YELLOW CIRCLE}|\N{MEDIUM BLACK CIRCLE}"
    "|\N{MEDIUM WHITE CIRCLE}|\N{LARGE ORANGE CIRCLE})[\\s\\-_|\N{BOX DRAWINGS HEAVY VERTICAL}"
    "\N{KATAKANA MIDDLE DOT}\N{BULLET}]*"
)
DOT_EVERY = 30  # seconds between status checks
DOT_SETTLE = 90  # a new state has to hold this long before the name follows
RENAME_WINDOW = 600  # Discord allows two renames per channel every ten minutes
RENAMES_PER_WINDOW = 2
RENAME_TIMEOUT = 15  # discord.py sleeps through a rate limit; don't let it stall the loop

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

    def __init__(self, message: str, status: Optional[int] = None, data: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.data = data if isinstance(data, dict) else {}


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


def _base_name(name: str) -> str:
    return DOT_PREFIX.sub("", name)


def _with_dot(dot: str, channel: Any) -> str:
    # Text and forum channels can't hold spaces, so they get a hyphen like PZAdmin uses.
    sep = "-" if isinstance(channel, (discord.TextChannel, discord.ForumChannel)) else " "
    return dot + sep + _base_name(channel.name)


def _wanted_dot(server: Dict[str, Any]) -> Optional[str]:
    """The dot a server's state calls for, or None to leave the name as it is.

    PZAdmin reports "restarting" from the moment it saves and quits (after the
    in-game countdown) until the server answers again, and "deploying" while it
    installs one. A state PZAdmin hasn't checked yet keeps the last dot.
    """
    state = server.get("state")
    if state == "online":
        return DOT_ONLINE
    if state in ("offline", "stopped"):
        return DOT_OFFLINE
    if state in ("restarting", "deploying"):
        return DOT_RESTARTING
    return None


class _DotState:
    """What the cog knows about one channel it puts a dot in. Kept in memory."""

    def __init__(self, shown: Optional[str]) -> None:
        self.shown = shown
        self.want: Optional[str] = None
        self.want_since = 0.0
        self.renames: List[float] = []
        self.retry_at = 0.0

    def budget(self, now: float) -> bool:
        self.renames = [t for t in self.renames if now - t < RENAME_WINDOW]
        return len(self.renames) < RENAMES_PER_WINDOW


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
        # channel ID -> {"id": PZAdmin server ID, "name": its name when linked}
        self.config.register_guild(channels={})
        # channel ID -> {"id": PZAdmin server ID, "name": its name} for status dots
        self.config.register_guild(status_channels={})
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: List[Dict[str, Any]] = []
        self._cache_time = 0.0
        self._cache_lock = asyncio.Lock()
        self._restart_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._dots: Dict[int, _DotState] = {}
        self._dot_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        self._dot_task = asyncio.create_task(self._dot_loop())

    async def cog_unload(self) -> None:
        if self._dot_task:
            self._dot_task.cancel()
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
                raise PZAdminError(self._error_message(resp, error), status=resp.status, data=data)
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

    # ---------- mod requests ----------

    @staticmethod
    def _link_key(channel: Any) -> str:
        """Threads (and forum posts) count as their parent channel."""
        if isinstance(channel, discord.Thread) and channel.parent_id:
            return str(channel.parent_id)
        return str(channel.id)

    @pz.command(name="request")
    @commands.cooldown(1, 30, commands.BucketType.user)
    @app_commands.describe(
        workshop="Steam Workshop link or ID of the mod",
        note="Optional note for the admins, e.g. why you want it",
    )
    async def pz_request(self, ctx: commands.Context, workshop: str, *, note: Optional[str] = None) -> None:
        """Request a mod for this channel's server. An admin approves it in PZAdmin."""
        links = await self.config.guild(ctx.guild).channels()
        link = links.get(self._link_key(ctx.channel))
        if not link:
            ctx.command.reset_cooldown(ctx)
            await ctx.send("This channel isn't linked to a server, so I don't know where the mod would go. "
                           "Ask in your server's channel.")
            return

        name = discord.utils.escape_markdown(str(link.get("name") or link["id"]))
        who = _truncate(f"{ctx.author.name} ({ctx.author.id})", MAX_REQUESTED_BY)
        body: Dict[str, Any] = {"workshopId": workshop.strip().strip("<>"), "requestedBy": who}
        if note:
            body["note"] = note.strip()[:MAX_NOTE]

        async with ctx.typing():  # PZAdmin asks Steam about the item, which can take a few seconds
            try:
                data = await self._request("POST", f"/servers/{link['id']}/mod-requests", json=body)
            except PZAdminError as e:
                ctx.command.reset_cooldown(ctx)
                log.info("pz request %r on %s by %s: refused (%s)", body["workshopId"], link["id"], who, e)
                await ctx.send(self._request_error(e, name), allowed_mentions=NO_MENTIONS)
                return

        req = data.get("request") if isinstance(data, dict) else None
        req = req if isinstance(req, dict) else {}
        ws_id = str(req.get("workshopId") or body["workshopId"])
        title = req.get("title") or f"Workshop item {ws_id}"
        log.info("pz request %s on %s by %s: taken", ws_id, link["id"], who)
        embed = discord.Embed(
            title=_truncate(str(title), 256),
            url=WORKSHOP_URL.format(ws_id) if ws_id.isdigit() else None,
            description=f"Requested for **{name}** by {ctx.author.mention}. An admin will approve or reject it.",
            colour=await ctx.embed_colour(),
        )
        mod_ids = req.get("modIds") or []
        if mod_ids:
            embed.add_field(name="Mod IDs", value=_truncate(", ".join(str(m) for m in mod_ids), MAX_FIELD))
        if req.get("note"):
            embed.add_field(name="Note", value=_truncate(str(req["note"]), MAX_FIELD), inline=False)
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @staticmethod
    def _request_error(e: PZAdminError, name: str) -> str:
        if e.status == 409:
            reason = e.data.get("reason")
            if reason == "installed":
                return f"That mod is already on **{name}**."
            if reason == "duplicate":
                existing = e.data.get("request") if isinstance(e.data.get("request"), dict) else {}
                by = existing.get("requestedBy")
                by = f" by {discord.utils.escape_markdown(str(by))}" if by else ""
                return f"That mod has already been requested{by} for **{name}** and is waiting for an admin."
            if reason == "full":
                return f"**{name}** has too many requests waiting already. Try again once an admin has gone through them."
        if e.status == 400:
            detail = str(e.data.get("error") or "")
            if "Steam" in detail or "Project Zomboid" in detail:  # no such item, or not a PZ mod
                return f"That didn't work: {_truncate(detail, 200)}."
            return "That doesn't look like a Workshop mod. Send its Workshop link or numeric ID."
        if e.status == 403:
            return "The bot's PZAdmin key isn't allowed to make mod requests. The bot owner needs to give it the request scope."
        if e.status == 404:
            return f"PZAdmin doesn't know **{name}** any more. The bot owner needs to relink this channel."
        return str(e)

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

    # ---------- status dots in channel names ----------

    async def _dot_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._sync_dots()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Status dots: sync failed")
            await asyncio.sleep(DOT_EVERY)

    async def _sync_dots(self) -> None:
        all_guilds = await self.config.all_guilds()
        wanted = {
            int(channel_id): link
            for data in all_guilds.values()
            for channel_id, link in (data.get("status_channels") or {}).items()
        }
        for channel_id in list(self._dots):
            if channel_id not in wanted:
                del self._dots[channel_id]
        if not wanted:
            return

        try:
            async with self._cache_lock:
                servers = await self._fetch_servers()
        except PZAdminError as e:
            log.debug("Status dots: can't read servers: %s", e)
            return  # an unreachable PZAdmin isn't the same as an offline server
        by_id = {str(s.get("id")): s for s in servers}

        now = time.monotonic()
        for channel_id, link in wanted.items():
            channel = self.bot.get_channel(channel_id)
            server = by_id.get(str(link.get("id")))
            if channel is None or server is None:
                continue  # deleted channel, or a server this key can't see
            ds = self._dots.get(channel_id)
            if ds is None:
                match = DOT_PREFIX.match(channel.name)
                ds = self._dots[channel_id] = _DotState(match.group(0)[:1] if match else None)
            if now < ds.retry_at:
                continue
            dot = _wanted_dot(server)
            if dot is None:
                continue
            if dot != ds.want:
                ds.want, ds.want_since = dot, now
            if ds.want == ds.shown:
                continue
            # The first dot goes up straight away, and so do a restart and the
            # recovery from one, which PZAdmin does on purpose. Anything else
            # has to last before it is worth one of the two renames.
            deliberate = DOT_RESTARTING in (ds.want, ds.shown)
            if ds.shown is not None and not deliberate and now - ds.want_since < DOT_SETTLE:
                continue
            if not ds.budget(now):
                continue
            try:
                await self._rename(channel, _with_dot(ds.want, channel))
            except (discord.HTTPException, asyncio.TimeoutError) as e:
                ds.retry_at = now + RENAME_WINDOW
                log.warning("Status dots: couldn't rename channel %s: %s", channel_id, e)
            else:
                ds.shown = ds.want
                ds.renames.append(now)

    @staticmethod
    async def _rename(channel: Any, name: str) -> None:
        if name == channel.name:
            return
        await asyncio.wait_for(
            channel.edit(name=name[:100], reason="PZAdmin server status"), timeout=RENAME_TIMEOUT
        )

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

    @pzadminset.command(name="link")
    @commands.guild_only()
    async def pzadminset_link(
        self,
        ctx: commands.Context,
        channel: Union[discord.TextChannel, discord.ForumChannel, discord.VoiceChannel],
        *,
        server: str,
    ) -> None:
        """Link a channel to a server, so `pz request` there asks for mods on that server.

        Threads in the channel count too. Linking a channel again replaces its server.
        """
        async with ctx.typing():
            try:
                s = await self._resolve(server)
            except PZAdminError as e:
                await ctx.send(str(e), allowed_mentions=NO_MENTIONS)
                return
        name = str(s.get("name") or s["id"])
        async with self.config.guild(ctx.guild).channels() as links:
            links[str(channel.id)] = {"id": str(s["id"]), "name": name}
        await ctx.send(
            f"{channel.mention} is now linked to **{discord.utils.escape_markdown(name)}**.",
            allowed_mentions=NO_MENTIONS,
        )

    @pzadminset.command(name="unlink")
    @commands.guild_only()
    async def pzadminset_unlink(
        self, ctx: commands.Context, channel: Union[discord.TextChannel, discord.ForumChannel, discord.VoiceChannel]
    ) -> None:
        """Stop a channel taking mod requests."""
        async with self.config.guild(ctx.guild).channels() as links:
            removed = links.pop(str(channel.id), None)
        if removed:
            await ctx.send(f"{channel.mention} is no longer linked to a server.", allowed_mentions=NO_MENTIONS)
        else:
            await ctx.send(f"{channel.mention} wasn't linked to a server.", allowed_mentions=NO_MENTIONS)

    @pzadminset.command(name="links")
    @commands.guild_only()
    async def pzadminset_links(self, ctx: commands.Context) -> None:
        """List which channels take mod requests for which server."""
        links = await self.config.guild(ctx.guild).channels()
        if not links:
            await ctx.send("No channels are linked yet. Use `pzadminset link #channel <server>`.")
            return
        lines = []
        for channel_id, link in links.items():
            channel = ctx.guild.get_channel(int(channel_id))
            where = channel.mention if channel else f"deleted channel {channel_id}"
            lines.append(f"{where} \N{RIGHTWARDS ARROW} {discord.utils.escape_markdown(str(link.get('name') or link['id']))}")
        await ctx.send(_truncate("\n".join(lines), 1900), allowed_mentions=NO_MENTIONS)

    @pzadminset.group(name="dots")
    @commands.guild_only()
    async def pzadminset_dots(self, ctx: commands.Context) -> None:
        """Show a server's status as \N{LARGE GREEN CIRCLE} / \N{LARGE ORANGE CIRCLE} / \N{LARGE RED CIRCLE} at the start of a channel's name.

        \N{LARGE ORANGE CIRCLE} means a restart is in progress.

        Don't use this on a channel where PZAdmin's own "Show \N{LARGE GREEN CIRCLE} / \N{LARGE RED CIRCLE} in the
        channel's name" option is on: both bots would rename it and use up Discord's limit of two renames every
        ten minutes.
        """

    @pzadminset_dots.command(name="add")
    async def pzadminset_dots_add(
        self,
        ctx: commands.Context,
        channel: Union[discord.TextChannel, discord.VoiceChannel, discord.ForumChannel, discord.StageChannel],
        *,
        server: Optional[str] = None,
    ) -> None:
        """Put a status dot in a channel's name. The server defaults to the one the channel is linked to."""
        if server is None:
            link = (await self.config.guild(ctx.guild).channels()).get(str(channel.id))
            if not link:
                await ctx.send(
                    f"{channel.mention} isn't linked to a server. Name one: "
                    f"`pzadminset dots add #channel <server>`.",
                    allowed_mentions=NO_MENTIONS,
                )
                return
            server = link["id"]
        async with ctx.typing():
            try:
                s = await self._resolve(server)
            except PZAdminError as e:
                await ctx.send(str(e), allowed_mentions=NO_MENTIONS)
                return
        name = str(s.get("name") or s["id"])
        async with self.config.guild(ctx.guild).status_channels() as dots:
            dots[str(channel.id)] = {"id": str(s["id"]), "name": name}
        self._dots.pop(channel.id, None)

        lines = [
            f"{channel.mention} will show **{discord.utils.escape_markdown(name)}**'s status in its name. "
            f"It follows within a couple of minutes; Discord only allows two renames every ten minutes, "
            f"so a server that flaps may take longer.",
            "\N{WARNING SIGN} If PZAdmin's own \"Show \N{LARGE GREEN CIRCLE} / \N{LARGE RED CIRCLE} in the channel's "
            "name\" option is on for this channel (Discord page in PZAdmin), turn one of them off. Both bots "
            "renaming the same channel will fight over that limit.",
        ]
        if not channel.permissions_for(ctx.guild.me).manage_channels:
            lines.append("\N{WARNING SIGN} I don't have **Manage Channels** on that channel yet, so I can't rename it.")
        await ctx.send("\n\n".join(lines), allowed_mentions=NO_MENTIONS)

    @pzadminset_dots.command(name="remove")
    async def pzadminset_dots_remove(
        self,
        ctx: commands.Context,
        channel: Union[discord.TextChannel, discord.VoiceChannel, discord.ForumChannel, discord.StageChannel],
    ) -> None:
        """Stop showing a status dot and put the channel's plain name back."""
        async with self.config.guild(ctx.guild).status_channels() as dots:
            removed = dots.pop(str(channel.id), None)
        self._dots.pop(channel.id, None)
        if not removed:
            await ctx.send(f"{channel.mention} doesn't have a status dot from me.", allowed_mentions=NO_MENTIONS)
            return
        try:
            await self._rename(channel, _base_name(channel.name))
        except (discord.HTTPException, asyncio.TimeoutError):
            await ctx.send(
                f"{channel.mention} won't get status dots any more, but Discord wouldn't let me rename it just now. "
                f"Remove the dot by hand or try again in ten minutes.",
                allowed_mentions=NO_MENTIONS,
            )
            return
        await ctx.send(f"{channel.mention} won't get status dots any more.", allowed_mentions=NO_MENTIONS)

    @pzadminset_dots.command(name="list")
    async def pzadminset_dots_list(self, ctx: commands.Context) -> None:
        """List which channels show which server's status."""
        dots = await self.config.guild(ctx.guild).status_channels()
        if not dots:
            await ctx.send("No channels show a status dot. Use `pzadminset dots add #channel [server]`.")
            return
        lines = []
        for channel_id, link in dots.items():
            channel = ctx.guild.get_channel(int(channel_id))
            where = channel.mention if channel else f"deleted channel {channel_id}"
            lines.append(f"{where} \N{RIGHTWARDS ARROW} {discord.utils.escape_markdown(str(link.get('name') or link['id']))}")
        await ctx.send(_truncate("\n".join(lines), 1900), allowed_mentions=NO_MENTIONS)

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
