import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urlencode, urlparse

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify

log = logging.getLogger("red.kalaidus.containerstatus")

TOKEN_SERVICE = "arcane"
DEFAULT_ENVIRONMENT = "0"  # Arcane's own, local environment
REQUEST_TIMEOUT = 10
POLL_EVERY = 30  # seconds between status checks
UNAVAILABLE_AFTER = 3  # failed checks in a row before a card stops claiming anything
RETRY_FORBIDDEN = 600  # a channel the bot can't post in is tried again after this
PAGE_SIZE = 100
MAX_PAGES = 50
MAX_TITLE = 256
MAX_DESCRIPTION = 1000
MAX_LABEL = 80
NO_MENTIONS = discord.AllowedMentions.none()

# The same dots and embed colours as PZAdmin's live status cards.
DOT_UP = "\N{LARGE GREEN CIRCLE}"
DOT_BUSY = "\N{LARGE ORANGE CIRCLE}"
DOT_DOWN = "\N{LARGE RED CIRCLE}"
DOT_UNKNOWN = "\N{MEDIUM WHITE CIRCLE}"
COLOUR_UP = 0x7FA650
COLOUR_BUSY = 0xD38B3A
COLOUR_DOWN = 0xC4553F
COLOUR_UNKNOWN = 0x8A8272
FOOTER = "Updated via Arcane"

# Status dots in channel names, working the same way as the pzadmin cog's.
DOT_PREFIX = re.compile(
    "^(?:\N{LARGE GREEN CIRCLE}|\N{LARGE RED CIRCLE}|\N{LARGE YELLOW CIRCLE}|\N{MEDIUM BLACK CIRCLE}"
    "|\N{MEDIUM WHITE CIRCLE}|\N{LARGE ORANGE CIRCLE})[\\s\\-_|\N{BOX DRAWINGS HEAVY VERTICAL}"
    "\N{KATAKANA MIDDLE DOT}\N{BULLET}]*"
)
DOT_SETTLE = 90  # a new state has to hold this long before the name follows
RENAME_WINDOW = 600  # Discord allows two renames per channel every ten minutes
RENAMES_PER_WINDOW = 2
RENAME_TIMEOUT = 15  # discord.py sleeps through a rate limit; don't let it stall the loop
DotChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.ForumChannel, discord.StageChannel]


class ArcaneError(Exception):
    """An error with a message that is safe to show in Discord."""


class UnknownEnvironment(ArcaneError):
    """Arcane answered, and no single environment matches."""


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Docker writes nanoseconds; Python takes microseconds.
        head, dot, frac = value.replace("Z", "+00:00").partition(".")
        if dot:
            digits = "".join(c for c in frac if c.isdigit())
            frac = digits[:6] + frac[len(digits):]
            value = head + "." + frac
        else:
            value = head
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.year <= 1:
        return None  # Docker's zero time: never started, or never stopped
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _discord_time(dt: datetime) -> str:
    # Each reader's client renders this as "an hour ago" by itself, so a card
    # doesn't need an edit a minute to keep its uptime right.
    return f"<t:{int(dt.timestamp())}:R>"


def _clean_url(url: str) -> Optional[str]:
    url = url.strip().strip("<>")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in url:
        return None
    return url


def wanted_dot(status: Optional[Dict[str, Any]]) -> Optional[str]:
    """The dot a container's status calls for, or None to leave the name alone."""
    if status is None:
        return None  # Arcane can't be read: that isn't the same as down
    if status.get("missing"):
        return DOT_DOWN
    state, health = status.get("state") or "", status.get("health") or ""
    if status.get("running") and state != "paused":
        return DOT_BUSY if health in ("starting", "unhealthy") else DOT_UP
    if state in ("restarting", "paused"):
        return DOT_BUSY
    return DOT_DOWN


def _base_name(name: str) -> str:
    return DOT_PREFIX.sub("", name)


def _with_dot(dot: str, channel: Any) -> str:
    # Text and forum channels can't hold spaces, so they get a hyphen.
    sep = "-" if isinstance(channel, (discord.TextChannel, discord.ForumChannel)) else " "
    return dot + sep + _base_name(channel.name)


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


def render(card: Dict[str, Any], status: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What a card should say, as an embed dict without a timestamp.

    status is None while Arcane can't be reached, {"missing": True} when it has
    no container by that name, and the container's details otherwise.
    """
    lines: List[str] = []
    if status is None:
        colour = COLOUR_UNKNOWN
        lines.append(f"{DOT_UNKNOWN} **Status unavailable**")
        lines.append("Arcane isn't answering, so this card isn't being updated.")
    elif status.get("missing"):
        colour = COLOUR_DOWN
        lines.append(f"{DOT_DOWN} **Not found**")
        lines.append(f"Arcane has no container called `{card['container']}`.")
    else:
        state = status.get("state") or ""
        health = status.get("health") or ""
        started = _parse_time(status.get("startedAt"))
        finished = _parse_time(status.get("finishedAt"))
        if status.get("running") and state != "paused":
            if health == "starting":
                colour, head = COLOUR_BUSY, f"{DOT_BUSY} **Starting**"
            elif health == "unhealthy":
                colour, head = COLOUR_BUSY, f"{DOT_BUSY} **Unhealthy**"
            else:
                colour, head = COLOUR_UP, f"{DOT_UP} **Online**"
            lines.append(head)
            if started:
                lines.append("Up since " + _discord_time(started))
        elif state in ("restarting", "paused"):
            colour = COLOUR_BUSY
            lines.append(f"{DOT_BUSY} **{state.capitalize()}**")
            if state == "restarting" and finished:
                lines.append("Went down " + _discord_time(finished))
        else:
            colour = COLOUR_DOWN
            lines.append(f"{DOT_DOWN} **Offline**")
            if finished:
                lines.append("Last seen " + _discord_time(finished))
    if card.get("link"):
        label = (card.get("link_label") or "").replace("[", "(").replace("]", ")")
        target = f"[{label}]({card['link']})" if label else card["link"]
        lines.append("**Open:** " + target)
    if card.get("description"):
        lines = [card["description"], ""] + lines
    return {
        "title": card.get("title") or card["container"],
        "description": "\n".join(lines),
        "color": colour,
        "footer": {"text": FOOTER},
    }


def _hash(embed: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(embed, sort_keys=True).encode()).hexdigest()[:16]


class ContainerStatus(commands.Cog):
    """Live status cards for Docker containers, read from Arcane."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=3108452291, force_registration=True)
        # cards: card ID -> {channel_id, message_id, container, title,
        # description, link, link_label, environment}. A card without an
        # environment reads the default one.
        # dots: channel ID -> card ID whose status goes at the start of its name
        self.config.register_global(environment=DEFAULT_ENVIRONMENT, cards={}, next_id=1, dots={})
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._shown: Dict[str, str] = {}  # card ID -> hash of what the message says
        self._retry_at: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}  # environment ID -> failed checks in a row
        self._dots: Dict[int, _DotState] = {}

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, **kwargs) -> None:
        return  # no user data stored

    # ---------- Arcane API ----------

    async def _get(self, path: str) -> Any:
        """GET a path under Arcane's /api."""
        tokens = await self.bot.get_shared_api_tokens(TOKEN_SERVICE)
        base, key = (tokens.get("url") or "").strip().rstrip("/"), (tokens.get("api_key") or "").strip()
        if not base or not key:
            raise ArcaneError(
                "Arcane isn't set up yet. DM the bot: `!set api arcane url <arcane url> api_key <key>`."
            )
        if not base.endswith("/api"):
            base += "/api"
        try:
            async with self._session.get(
                base + path,
                headers={"X-API-Key": key, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    raise ArcaneError("Arcane rejected the API key.")
                if resp.status == 403:
                    raise ArcaneError(
                        "Arcane's key doesn't have the permissions this needs: `containers:list` and `containers:read`"
                        " (and `environments:list` to list environments)."
                    )
                if resp.status == 404:
                    raise ArcaneError("Arcane returned 404. Check the environment with `!containercard environments`.")
                if resp.status >= 400:
                    raise ArcaneError(f"Arcane returned HTTP {resp.status}.")
                try:
                    return await resp.json(content_type=None)
                except ValueError:
                    raise ArcaneError("Arcane's answer wasn't JSON. Is the URL Arcane's?") from None
        except asyncio.TimeoutError:
            raise ArcaneError(f"Arcane didn't answer within {REQUEST_TIMEOUT} seconds.") from None
        except aiohttp.ClientError as e:
            log.warning("Arcane unreachable: %s", e)
            raise ArcaneError("Couldn't reach Arcane. Check the URL and that Arcane is running.") from None

    async def _env_path(self, env: Optional[str], path: str) -> str:
        env = env or await self.config.environment()
        return f"/environments/{quote(env, safe='')}{path}"

    async def _environments(self) -> List[Dict[str, Any]]:
        """Every environment Arcane has, as {id, name, status}."""
        data = _dict(await self._get("/environments?" + urlencode({"start": 0, "limit": PAGE_SIZE})))
        items = data.get("data") if isinstance(data.get("data"), list) else []
        return [
            {"id": str(i.get("id")), "name": str(i.get("name") or i.get("id")), "status": str(i.get("status") or "")}
            for i in items
            if isinstance(i, dict) and i.get("id") is not None
        ]

    async def _find_environment(self, wanted: str) -> Dict[str, Any]:
        """An environment by ID or name (any case). Raises ArcaneError unless exactly one matches."""
        envs = await self._environments()
        for env in envs:
            if env["id"] == wanted:
                return env
        named = [e for e in envs if e["name"].lower() == wanted.lower()]
        if len(named) == 1:
            return named[0]
        if named:
            raise UnknownEnvironment(f"More than one environment is called `{wanted}`. Use its ID.")
        raise UnknownEnvironment(
            f"Arcane has no environment `{_truncate(wanted, 60)}`. `!containercard environments` lists them."
        )

    async def _list(self, env: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every container in an environment (the default if None), hidden ones included."""
        found: List[Dict[str, Any]] = []
        for page in range(MAX_PAGES):
            query = urlencode(
                {
                    "start": page * PAGE_SIZE,
                    "limit": PAGE_SIZE,
                    "includeHidden": "true",
                    "includeInternal": "true",
                }
            )
            data = _dict(await self._get(await self._env_path(env, "/containers?" + query)))
            items = data.get("data") if isinstance(data.get("data"), list) else []
            found.extend(i for i in items if isinstance(i, dict))
            total = _dict(data.get("pagination")).get("totalItems") or 0
            if len(items) < PAGE_SIZE or (total and len(found) >= total):
                break
        return found

    @staticmethod
    def _names(summary: Dict[str, Any]) -> List[str]:
        return [str(n).lstrip("/") for n in summary.get("names") or []]

    async def _statuses(self, env: str, names: List[str]) -> Dict[str, Dict[str, Any]]:
        """Details for each named container in one environment. Raises ArcaneError if it can't be read."""
        ids: Dict[str, str] = {}
        for summary in await self._list(env):
            for name in self._names(summary):
                ids.setdefault(name, str(summary.get("id") or ""))
        out: Dict[str, Dict[str, Any]] = {}
        for name in set(names):
            if not ids.get(name):
                out[name] = {"missing": True}
                continue
            data = await self._get(await self._env_path(env, "/containers/" + quote(ids[name], safe="")))
            state = _dict(_dict(_dict(data).get("data")).get("state"))
            out[name] = {
                "state": state.get("status"),
                "running": bool(state.get("running")),
                "health": _dict(state.get("health")).get("status"),
                "startedAt": state.get("startedAt"),
                "finishedAt": state.get("finishedAt"),
            }
        return out

    # ---------- keeping the cards up to date ----------

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Container cards: sync failed")
            await asyncio.sleep(POLL_EVERY)

    async def _sync(self, only: Optional[str] = None, force: bool = False) -> None:
        async with self._lock:
            cards = await self.config.cards()
            for card_id in list(self._shown):
                if card_id not in cards:
                    self._shown.pop(card_id, None)
            if only is not None:
                cards = {only: cards[only]} if only in cards else {}
            if not cards:
                return
            default = await self.config.environment()
            by_env: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for card_id, card in cards.items():
                by_env.setdefault(card.get("environment") or default, {})[card_id] = card
            # Each environment is checked on its own, so one that's offline
            # (an agent on another machine, say) doesn't grey out the rest.
            wanted: Dict[str, Optional[str]] = {}  # card ID -> dot, for cards checked this time
            for env, env_cards in by_env.items():
                try:
                    statuses: Optional[Dict[str, Dict[str, Any]]] = await self._statuses(
                        env, [c["container"] for c in env_cards.values()]
                    )
                    self._failures[env] = 0
                except ArcaneError as e:
                    log.debug("Container cards: can't read Arcane environment %s: %s", env, e)
                    self._failures[env] = self._failures.get(env, 0) + 1
                    if self._failures[env] < UNAVAILABLE_AFTER and only is None and not force:
                        continue  # one missed check isn't worth an edit
                    statuses = None
                for card_id, card in env_cards.items():
                    status = None if statuses is None else statuses.get(card["container"])
                    wanted[card_id] = wanted_dot(status)
                    await self._show(card_id, card, render(card, status))
            await self._sync_dots(wanted)

    async def _sync_dots(self, wanted: Dict[str, Optional[str]]) -> None:
        dots = {int(k): str(v) for k, v in (await self.config.dots()).items()}
        for channel_id in list(self._dots):
            if channel_id not in dots:
                del self._dots[channel_id]
        now = time.monotonic()
        for channel_id, card_id in dots.items():
            dot = wanted.get(card_id)
            if dot is None:
                continue  # not checked this time, or status unknown: keep the last dot
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            ds = self._dots.get(channel_id)
            if ds is None:
                match = DOT_PREFIX.match(channel.name)
                ds = self._dots[channel_id] = _DotState(match.group(0)[:1] if match else None)
            if now < ds.retry_at:
                continue
            if dot != ds.want:
                ds.want, ds.want_since = dot, now
            if ds.want == ds.shown:
                continue
            # The first dot goes up straight away, and so do a restart and the
            # recovery from one. Anything else has to last before it is worth
            # one of the two renames.
            deliberate = DOT_BUSY in (ds.want, ds.shown)
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
            channel.edit(name=name[:100], reason="Container status"), timeout=RENAME_TIMEOUT
        )

    async def _show(self, card_id: str, card: Dict[str, Any], embed: Dict[str, Any]) -> None:
        digest = _hash(embed)
        if self._shown.get(card_id) == digest or time.monotonic() < self._retry_at.get(card_id, 0):
            return
        channel = self.bot.get_channel(int(card["channel_id"]))
        if channel is None:
            return  # the channel is gone, or the bot can't see it
        e = discord.Embed.from_dict(embed)
        e.timestamp = datetime.now(timezone.utc)
        try:
            try:
                message = channel.get_partial_message(int(card["message_id"]))
                await message.edit(embed=e, allowed_mentions=NO_MENTIONS)
            except discord.NotFound:
                # Someone deleted the card: put it back.
                message = await channel.send(embed=e, allowed_mentions=NO_MENTIONS)
                async with self.config.cards() as stored:
                    if card_id in stored:
                        stored[card_id]["message_id"] = message.id
        except discord.Forbidden as err:
            self._retry_at[card_id] = time.monotonic() + RETRY_FORBIDDEN
            log.warning("Container cards: no permission in channel %s: %s", card["channel_id"], err)
            return
        except discord.HTTPException as err:
            log.warning("Container cards: couldn't update card %s: %s", card_id, err)
            return
        self._shown[card_id] = digest

    async def _refresh(self, card_id: str) -> None:
        self._shown.pop(card_id, None)
        self._retry_at.pop(card_id, None)
        await self._sync(only=card_id)

    # ---------- owner commands ----------

    @commands.group(name="containercard", aliases=["ccard"])
    @commands.is_owner()
    async def containercard(self, ctx: commands.Context) -> None:
        """Live status cards for Docker containers, read from Arcane."""

    async def _card(self, ctx: commands.Context, card_id: str) -> Optional[Dict[str, Any]]:
        card = (await self.config.cards()).get(card_id)
        if card is None:
            await ctx.send(f"There's no card {card_id}. `!containercard list` shows them.")
        return card

    async def _set(self, card_id: str, **fields: Any) -> None:
        async with self.config.cards() as cards:
            cards[card_id].update(fields)
        await self._refresh(card_id)

    async def _locate(self, spec: str) -> Tuple[str, Optional[str]]:
        """Find a container from `name` or `name@environment`.

        Returns its name and the environment ID to store: None for the
        default environment. A bare name that isn't in the default environment
        is looked for in the others. Raises ArcaneError with what went wrong.
        """
        name, at, wanted = spec.rpartition("@") if "@" in spec else (spec, "", "")
        name = name.strip()
        default = await self.config.environment()
        if at:
            env = (await self._find_environment(wanted.strip()))["id"]
            if name not in {n for c in await self._list(env) for n in self._names(c)}:
                raise ArcaneError(
                    f"Environment `{wanted}` has no container called `{_truncate(name, 100)}`. "
                    f"`!containercard containers {wanted}` lists the ones it has."
                )
            return name, None if env == default else env
        if name in {n for c in await self._list(default) for n in self._names(c)}:
            return name, None
        try:
            others = [e for e in await self._environments() if e["id"] != default]
        except ArcaneError:
            others = []  # a key without environments:list can still use the default
        found = []
        for env in others:
            try:
                if name in {n for c in await self._list(env["id"]) for n in self._names(c)}:
                    found.append(env)
            except ArcaneError:
                continue  # an offline environment can't hold the answer
        if len(found) == 1:
            return name, found[0]["id"]
        if found:
            options = ", ".join(f"`{name}@{e['name']}`" for e in found)
            raise ArcaneError(f"`{name}` is in more than one environment. Say which: {options}.")
        raise ArcaneError(
            f"Arcane has no container called `{_truncate(name, 100)}`. "
            "`!containercard containers` lists the ones it has."
        )

    @containercard.command(name="add")
    async def cc_add(
        self, ctx: commands.Context, channel: discord.TextChannel, container: str, *, title: Optional[str] = None
    ) -> None:
        """Post a live card for a container in a channel.

        For a container in another Arcane environment, add `@environment`
        (its name or ID). A name that's only in one environment is found
        without it.

        Examples:
        `!containercard add #status jellyfin Jellyfin`
        `!containercard add #status plex@nas Plex`
        """
        async with ctx.typing():
            try:
                container, env = await self._locate(container)
            except ArcaneError as e:
                await ctx.send(str(e))
                return
        me = channel.guild.me
        perms = channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links and perms.view_channel):
            await ctx.send(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
            return
        card = {
            "channel_id": channel.id,
            "container": container,
            "title": _truncate(title.strip(), MAX_TITLE) if title else None,
            "description": None,
            "link": None,
            "link_label": None,
            "environment": env,
        }
        placeholder = discord.Embed(title=card["title"] or container, description="Checking\N{HORIZONTAL ELLIPSIS}")
        try:
            message = await channel.send(embed=placeholder, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            await ctx.send(f"Couldn't post in {channel.mention}: {e}")
            return
        card["message_id"] = message.id
        async with self.config.all() as data:
            card_id = str(data.get("next_id", 1))
            data["next_id"] = int(card_id) + 1
            data.setdefault("cards", {})[card_id] = card
        await self._refresh(card_id)
        await ctx.send(
            f"Card {card_id} is up in {channel.mention}. Add a link with "
            f"`!containercard link {card_id} <url> [label]` and a description with "
            f"`!containercard description {card_id} <text>`."
        )

    @containercard.command(name="title")
    async def cc_title(self, ctx: commands.Context, card_id: str, *, title: str) -> None:
        """Change a card's title. Use `clear` to go back to the container's name."""
        if await self._card(ctx, card_id) is None:
            return
        title = title.strip()
        await self._set(card_id, title=None if title.lower() == "clear" else _truncate(title, MAX_TITLE))
        await ctx.tick()

    @containercard.command(name="description", aliases=["desc"])
    async def cc_description(self, ctx: commands.Context, card_id: str, *, text: Optional[str] = None) -> None:
        """Set the text at the top of a card. Leave it out to remove it."""
        if await self._card(ctx, card_id) is None:
            return
        text = (text or "").strip()
        if len(text) > MAX_DESCRIPTION:
            await ctx.send(f"Keep the description under {MAX_DESCRIPTION} characters.")
            return
        await self._set(card_id, description=text or None)
        await ctx.tick()

    @containercard.command(name="link")
    async def cc_link(
        self, ctx: commands.Context, card_id: str, url: Optional[str] = None, *, label: Optional[str] = None
    ) -> None:
        """Set the link people use to open the service. Leave the URL out to remove it.

        Example: `!containercard link 1 https://jellyfin.example.com Watch now`
        """
        if await self._card(ctx, card_id) is None:
            return
        if url is None:
            await self._set(card_id, link=None, link_label=None)
            await ctx.tick()
            return
        clean = _clean_url(url)
        if clean is None:
            await ctx.send("That link needs to start with `http://` or `https://`.")
            return
        label = _truncate(label.strip(), MAX_LABEL) if label and label.strip() else None
        await self._set(card_id, link=clean, link_label=label)
        await ctx.tick()

    @containercard.command(name="container")
    async def cc_container(self, ctx: commands.Context, card_id: str, container: str) -> None:
        """Point a card at a different container, as `name` or `name@environment`."""
        if await self._card(ctx, card_id) is None:
            return
        async with ctx.typing():
            try:
                container, env = await self._locate(container)
            except ArcaneError as e:
                await ctx.send(str(e))
                return
        await self._set(card_id, container=container, environment=env)
        await ctx.tick()

    @containercard.command(name="environment")
    async def cc_environment(self, ctx: commands.Context, card_id: str, environment: str) -> None:
        """Move a card to another Arcane environment (name or ID), or `default`."""
        card = await self._card(ctx, card_id)
        if card is None:
            return
        if environment.lower() == "default":
            environment = await self.config.environment()
        spec = f"{card['container']}@{environment}"
        async with ctx.typing():
            try:
                container, env = await self._locate(spec)
            except ArcaneError as e:
                await ctx.send(str(e))
                return
        await self._set(card_id, container=container, environment=env)
        await ctx.tick()

    @containercard.command(name="remove", aliases=["delete"])
    async def cc_remove(self, ctx: commands.Context, card_id: str) -> None:
        """Stop updating a card and delete its message."""
        async with self._lock:
            async with self.config.cards() as cards:
                card = cards.pop(card_id, None)
            async with self.config.dots() as dots:
                for channel_id in [c for c, cid in dots.items() if str(cid) == card_id]:
                    del dots[channel_id]
            self._shown.pop(card_id, None)
            self._retry_at.pop(card_id, None)
        if card is None:
            await ctx.send(f"There's no card {card_id}.")
            return
        channel = self.bot.get_channel(int(card["channel_id"]))
        if channel is not None:
            try:
                await channel.get_partial_message(int(card["message_id"])).delete()
            except discord.HTTPException:
                await ctx.send("The card's gone from my list, but I couldn't delete its message. Delete it by hand.")
                return
        await ctx.tick()

    @containercard.command(name="list")
    async def cc_list(self, ctx: commands.Context) -> None:
        """Show every card."""
        cards = await self.config.cards()
        if not cards:
            await ctx.send("No cards yet. Add one with `!containercard add #channel <container> [title]`.")
            return
        lines = []
        for card_id, card in sorted(cards.items(), key=lambda kv: int(kv[0])):
            channel = self.bot.get_channel(int(card["channel_id"]))
            where = f"#{channel.name}" if channel else f"missing channel {card['channel_id']}"
            title = card.get("title") or card["container"]
            link = card.get("link") or "no link"
            target = card["container"] + (f"@{card['environment']}" if card.get("environment") else "")
            lines.append(f"{card_id:>3}  {_truncate(title, 30)}  [{target}]  {where}  {link}")
        for page in pagify("\n".join(lines), page_length=1900):
            await ctx.send(box(page))

    @containercard.command(name="containers")
    async def cc_containers(self, ctx: commands.Context, *, environment: Optional[str] = None) -> None:
        """List the containers Arcane knows about, to find the name to use.

        With no environment it lists every environment the key can see.
        """
        default = await self.config.environment()
        async with ctx.typing():
            try:
                if environment:
                    envs = [await self._find_environment(environment.strip())]
                else:
                    try:
                        envs = await self._environments()
                    except ArcaneError:
                        envs = []
                    if not any(e["id"] == default for e in envs):
                        envs.insert(0, {"id": default, "name": default, "status": ""})
            except ArcaneError as e:
                await ctx.send(str(e))
                return
            sections = []
            for env in envs:
                head = f"== {env['name']} (ID {env['id']}{', default' if env['id'] == default else ''}) =="
                try:
                    summaries = await self._list(env["id"])
                except ArcaneError as e:
                    sections.append(f"{head}\n  can't read it: {e}")
                    continue
                rows = sorted((self._names(s)[0] if self._names(s) else "?", s.get("state") or "?") for s in summaries)
                if not rows:
                    sections.append(f"{head}\n  no containers")
                    continue
                width = min(max(len(n) for n, _ in rows), 40)
                sections.append(head + "\n" + "\n".join(f"{_truncate(n, 40):<{width}}  {st}" for n, st in rows))
        if len(envs) > 1:
            sections.append(
                "Containers outside the default environment: add them as name@environment, "
                "e.g. !containercard add #status plex@nas"
            )
        for page in pagify("\n\n".join(sections), page_length=1900):
            await ctx.send(box(page))

    @containercard.command(name="environments", aliases=["envs"])
    async def cc_environments(self, ctx: commands.Context) -> None:
        """List Arcane's environments."""
        default = await self.config.environment()
        async with ctx.typing():
            try:
                envs = await self._environments()
            except ArcaneError as e:
                await ctx.send(str(e))
                return
        if not envs:
            await ctx.send("Arcane returned no environments.")
            return
        width = min(max(len(e["id"]) for e in envs), 40)
        lines = [
            f"{_truncate(e['id'], 40):<{width}}  {_truncate(e['name'], 40)}  {e['status']}"
            + ("  (default)" if e["id"] == default else "")
            for e in envs
        ]
        for page in pagify("\n".join(lines), page_length=1900):
            await ctx.send(box(page))

    @containercard.command(name="env")
    async def cc_env(self, ctx: commands.Context, environment: Optional[str] = None) -> None:
        """Show or set the default Arcane environment (`0` is Arcane's own machine).

        Cards added without `@environment` use it.
        """
        if environment is None:
            await ctx.send(f"Default Arcane environment: `{await self.config.environment()}`")
            return
        environment = environment.strip()
        try:
            environment = (await self._find_environment(environment))["id"]
        except UnknownEnvironment as e:
            await ctx.send(str(e))
            return
        except ArcaneError:
            pass  # a key without environments:list can still name one by ID
        await self.config.environment.set(environment)
        self._shown.clear()
        await ctx.tick()

    @containercard.group(name="dots")
    async def cc_dots(self, ctx: commands.Context) -> None:
        """Show a card's status as \N{LARGE GREEN CIRCLE} / \N{LARGE ORANGE CIRCLE} / \N{LARGE RED CIRCLE} at the start of a channel's name.

        \N{LARGE ORANGE CIRCLE} means starting, restarting, paused or unhealthy.
        """

    @cc_dots.command(name="add")
    async def cc_dots_add(self, ctx: commands.Context, channel: DotChannel, card_id: Optional[str] = None) -> None:
        """Put a card's status dot in a channel's name.

        The card defaults to the one posted in that channel, if there's only one.
        Example: `!containercard dots add #jellyfin 1`
        """
        cards = await self.config.cards()
        if card_id is None:
            here = [cid for cid, c in cards.items() if int(c["channel_id"]) == channel.id]
            if len(here) != 1:
                await ctx.send(
                    f"Say which card: `!containercard dots add #{channel.name} <card>`. "
                    "`!containercard list` shows the numbers.",
                    allowed_mentions=NO_MENTIONS,
                )
                return
            card_id = here[0]
        card = cards.get(card_id)
        if card is None:
            await ctx.send(f"There's no card {card_id}. `!containercard list` shows them.")
            return
        async with self.config.dots() as dots:
            dots[str(channel.id)] = card_id
        self._dots.pop(channel.id, None)
        title = discord.utils.escape_markdown(card.get("title") or card["container"])
        lines = [
            f"{channel.mention} will show **{title}**'s status in its name. It follows within a couple of "
            "minutes; Discord only allows two renames every ten minutes, so a container that flaps may take longer."
        ]
        pz = self.bot.get_cog("PZAdmin")
        if pz is not None:
            try:
                pz_dots = await pz.config.guild(channel.guild).status_channels()
            except Exception:
                pz_dots = {}
            if str(channel.id) in pz_dots:
                lines.append(
                    "\N{WARNING SIGN} The pzadmin cog already puts a dot in this channel's name. Turn one off "
                    "(`!pzadminset dots remove` or `!containercard dots remove`): both renaming it will fight over "
                    "Discord's limit."
                )
        if not channel.permissions_for(channel.guild.me).manage_channels:
            lines.append("\N{WARNING SIGN} I don't have **Manage Channels** on that channel yet, so I can't rename it.")
        await ctx.send("\n\n".join(lines), allowed_mentions=NO_MENTIONS)
        await self._refresh(card_id)

    @cc_dots.command(name="remove")
    async def cc_dots_remove(self, ctx: commands.Context, channel: DotChannel) -> None:
        """Stop showing a status dot and put the channel's plain name back."""
        async with self.config.dots() as dots:
            removed = dots.pop(str(channel.id), None)
        self._dots.pop(channel.id, None)
        if removed is None:
            await ctx.send(f"{channel.mention} doesn't have a status dot from me.", allowed_mentions=NO_MENTIONS)
            return
        try:
            await self._rename(channel, _base_name(channel.name))
        except (discord.HTTPException, asyncio.TimeoutError):
            await ctx.send(
                f"{channel.mention} won't get status dots any more, but Discord wouldn't let me rename it just now. "
                "Remove the dot by hand or try again in ten minutes.",
                allowed_mentions=NO_MENTIONS,
            )
            return
        await ctx.send(f"{channel.mention} won't get status dots any more.", allowed_mentions=NO_MENTIONS)

    @cc_dots.command(name="list")
    async def cc_dots_list(self, ctx: commands.Context) -> None:
        """List which channels show which card's status."""
        dots = await self.config.dots()
        if not dots:
            await ctx.send("No channels show a status dot. Use `!containercard dots add #channel [card]`.")
            return
        cards = await self.config.cards()
        lines = []
        for channel_id, card_id in dots.items():
            channel = self.bot.get_channel(int(channel_id))
            where = channel.mention if channel else f"deleted channel {channel_id}"
            card = cards.get(str(card_id))
            what = (card.get("title") or card["container"]) if card else "a removed card"
            lines.append(f"{where} \N{RIGHTWARDS ARROW} card {card_id}, {discord.utils.escape_markdown(what)}")
        await ctx.send(_truncate("\n".join(lines), 1900), allowed_mentions=NO_MENTIONS)

    @containercard.command(name="refresh")
    async def cc_refresh(self, ctx: commands.Context) -> None:
        """Check Arcane and update every card now."""
        async with ctx.typing():
            self._shown.clear()
            self._retry_at.clear()
            await self._sync(force=True)  # an owner asking gets the honest answer at once
        await ctx.tick()

    @containercard.command(name="show")
    async def cc_show(self, ctx: commands.Context) -> None:
        """Show settings and check the connection to Arcane (never shows the key)."""
        tokens = await self.bot.get_shared_api_tokens(TOKEN_SERVICE)
        lines = [
            f"URL:          {tokens.get('url') or 'NOT SET'}",
            f"API key:      {'set' if tokens.get('api_key') else 'NOT SET'}",
            f"Default env:  {await self.config.environment()}",
            f"Cards:        {len(await self.config.cards())}",
        ]
        async with ctx.typing():
            try:
                count = len(await self._list())
            except ArcaneError as e:
                lines.append(f"Check:        FAILED - {e}")
            else:
                lines.append(f"Check:        OK, {count} container{'s' if count != 1 else ''}")
        await ctx.send(box(_truncate("\n".join(lines), 1900)))
