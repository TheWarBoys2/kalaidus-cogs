import asyncio
import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

log = logging.getLogger("red.kalaidus.upcoming")

REQUEST_TIMEOUT = 15
POLL_EVERY = 30 * 60  # seconds; release dates and requests don't move by the minute
UNAVAILABLE_AFTER = 3  # failed checks in a row before a message says its data may be stale
RETRY_FORBIDDEN = 600
DEFAULT_DAYS = 30
MIN_DAYS, MAX_DAYS = 7, 90
WEEK = 7
MAX_PER_SECTION = 12  # lines under one heading before "…and N more"
DESCRIPTION_LIMIT = 4000  # Discord allows 4096 characters in an embed description
# History events that mean a file landed in the library: a finished download, or
# a folder imported by hand.
IMPORTED = {"downloadFolderImported", "movieFolderImported", "seriesFolderImported"}
SEERR_TAKE = 100  # most recent requests read from Seerr
NO_MENTIONS = discord.AllowedMentions.none()

# Seerr (and Overseerr / Jellyseerr before it) request and media status codes.
REQUEST_APPROVED = 2
MEDIA_AVAILABLE = 5

COLOUR_SOON = 0x4A8FD4
COLOUR_WEEK = 0x7FA650
COLOUR_UNKNOWN = 0x8A8272
DOT_STALE = "\N{MEDIUM WHITE CIRCLE}"

SERVICES = {
    # shared token name -> (display name, API root under the URL)
    "radarr": ("Radarr", "/api/v3"),
    "sonarr": ("Sonarr", "/api/v3"),
    "seerr": ("Seerr", "/api/v1"),
}


class ServiceError(Exception):
    """A service couldn't be read; the message is safe to show in Discord."""


class NotSetUp(ServiceError):
    """No URL or key for the service in Red's shared API tokens."""


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def _escape(text: str) -> str:
    return discord.utils.escape_markdown(str(text)).replace("[", "(").replace("]", ")")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _release_day(value: Any) -> Optional[date]:
    """A Radarr release date. Radarr stores these as midnight UTC on the day,
    so the date is taken as written rather than moved into a timezone."""
    dt = _parse_time(value)
    return dt.date() if dt else None


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _long_date(day: date) -> str:
    return f"{day:%A} {_ordinal(day.day)} {day:%B}"


def _short_date(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _day_heading(day: date, today: date) -> str:
    diff = (day - today).days
    if diff == 0:
        return f"Today \N{MIDDLE DOT} {_long_date(day)}"
    if diff == 1:
        return f"Tomorrow \N{MIDDLE DOT} {_long_date(day)}"
    if diff < WEEK:
        return _long_date(day)
    return f"{_long_date(day)} \N{MIDDLE DOT} in {diff} days"


def _movie_line(movie: Dict[str, Any]) -> str:
    title = _escape(movie.get("title") or "Untitled")
    year = movie.get("year")
    return f"\N{CLAPPER BOARD} **{title}**" + (f" *({year})*" if year else "")


def _episode_code(numbers: List[Tuple[int, int]]) -> str:
    """S01E03, S02E01–E04 for a run in one season, or S01E10 +1 across seasons."""
    (s1, e1), (s2, e2) = numbers[0], numbers[-1]
    code = f"S{s1:02d}E{e1:02d}"
    if len(numbers) > 1:
        code += f"\N{EN DASH}E{e2:02d}" if s1 == s2 else f" +{len(numbers) - 1}"
    return code


def _show_line(name: str, numbers: List[Tuple[int, int]], aired: Optional[datetime] = None) -> str:
    line = f"\N{TELEVISION} **{_escape(name)}** *{_episode_code(sorted(set(numbers)))}*"
    if aired:
        line += f" \N{MIDDLE DOT} <t:{int(aired.timestamp())}:t>"  # each reader's own time
    return line


def _poster(movie: Dict[str, Any]) -> Optional[str]:
    for image in movie.get("images") or []:
        if isinstance(image, dict) and image.get("coverType") == "poster":
            url = image.get("remoteUrl") or ""
            if url.startswith("https://"):
                return url
    return None


def _stale_note(name: str, failure: Optional[Dict[str, Any]]) -> Optional[str]:
    if not failure or failure["count"] < UNAVAILABLE_AFTER:
        return None
    since = failure.get("since")
    when = f" since <t:{int(since)}:R>" if since else ""
    return f"{DOT_STALE} {name} hasn't answered{when}, so this may be out of date."


Section = Tuple[str, List[str]]  # (heading, lines)


def _layout(top: List[str], sections: List[Section], empty: str) -> str:
    """The description: any notes, then each section under a ### heading with a gap
    between them. If it's too long for Discord, every section is cut shorter until it fits."""
    most = MAX_PER_SECTION
    while True:
        parts = ["\n".join(top)] if top else []
        for heading, lines in sections:
            shown = lines[:most]
            if len(lines) > most:
                shown.append(f"-# \N{HORIZONTAL ELLIPSIS}and {len(lines) - most} more")
            parts.append(f"### {heading}\n" + "\n".join(shown))
        if not sections:
            parts.append(empty)
        text = "\n\n".join(parts)
        if len(text) <= DESCRIPTION_LIMIT or most == 1:
            return _truncate(text, DESCRIPTION_LIMIT)
        most -= 1


def _movies_by_day(movies: Optional[List[Dict[str, Any]]], first: date, last: date) -> Dict[date, List[Dict[str, Any]]]:
    """Movies due out digitally from first to last, by day. Only what's still to
    come: physical releases never reach the server on their own, and anything
    Radarr already has is left off."""
    days: Dict[date, List[Dict[str, Any]]] = {}
    for movie in movies or []:
        if movie.get("hasFile"):
            continue
        day = _release_day(movie.get("digitalRelease"))
        if day and first <= day <= last:
            days.setdefault(day, []).append(movie)
    for day_movies in days.values():
        day_movies.sort(key=lambda m: str(m.get("sortTitle") or m.get("title") or ""))
    return days


def _episodes_by_day(
    episodes: Optional[List[Dict[str, Any]]], today: date, tz: ZoneInfo
) -> Dict[date, List[Tuple[datetime, str]]]:
    """This week's episodes not in the library yet, by day, as (air time, line).
    Episodes of one show on one day go on one line."""
    shows: Dict[Tuple[date, Any], List[Tuple[datetime, Dict[str, Any]]]] = {}
    for ep in episodes or []:
        aired = _parse_time(ep.get("airDateUtc"))
        if ep.get("hasFile") or not aired:
            continue
        day = aired.astimezone(tz).date()
        if 0 <= (day - today).days < WEEK:
            key = ep.get("seriesId") or _dict(ep.get("series")).get("title")
            shows.setdefault((day, key), []).append((aired, ep))
    days: Dict[date, List[Tuple[datetime, str]]] = {}
    for (day, _), eps in shows.items():
        eps.sort(key=lambda a: a[0])
        first = eps[0][1]
        name = _dict(first.get("series")).get("title") or first.get("title") or "Unknown show"
        numbers = [(e.get("seasonNumber") or 0, e.get("episodeNumber") or 0) for _, e in eps]
        days.setdefault(day, []).append((eps[0][0], _show_line(name, numbers, eps[0][0])))
    return days


def _added_lines(
    movie_history: Optional[List[Dict[str, Any]]],
    episode_history: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """One line per movie and per show imported, newest first, from Radarr's and Sonarr's history."""
    lines: List[Tuple[str, str]] = []
    seen = set()
    for rec in movie_history or []:
        movie = _dict(rec.get("movie"))
        key = rec.get("movieId") or movie.get("tmdbId") or movie.get("title")
        if rec.get("eventType") not in IMPORTED or not movie or key in seen:
            continue
        seen.add(key)
        lines.append((str(rec.get("date") or ""), _movie_line(movie)))
    shows: Dict[Any, List[Dict[str, Any]]] = {}
    for rec in episode_history or []:
        if rec.get("eventType") in IMPORTED and rec.get("series") and rec.get("episode"):
            shows.setdefault(rec.get("seriesId") or _dict(rec["series"]).get("title"), []).append(rec)
    for recs in shows.values():
        numbers = [
            (_dict(r["episode"]).get("seasonNumber") or 0, _dict(r["episode"]).get("episodeNumber") or 0)
            for r in recs
        ]
        name = _dict(recs[0].get("series")).get("title") or "Unknown show"
        latest = max(str(r.get("date") or "") for r in recs)
        lines.append((latest, _show_line(name, numbers)))
    lines.sort(key=lambda item: item[0], reverse=True)
    return [line for _, line in lines]


def render_week(
    movies: Optional[List[Dict[str, Any]]],
    episodes: Optional[List[Dict[str, Any]]],
    added: List[str],
    today: date,
    tz: ZoneInfo,
    sources: List[str],
    problems: List[str],
) -> Dict[str, Any]:
    """The "this week" message, as an embed dict without a timestamp: what was
    added today, then each day with something due (movies first, then episodes
    by air time). movies and episodes are None when that service isn't set up
    or has never answered."""
    sections: List[Section] = []
    if added:
        sections.append(("\N{WHITE HEAVY CHECK MARK} Added today", added))
    films = _movies_by_day(movies, today, today + timedelta(days=WEEK - 1))
    shows = _episodes_by_day(episodes, today, tz)
    for i in range(WEEK):
        day = today + timedelta(days=i)
        lines = [_movie_line(m) for m in films.get(day, [])]
        lines += [line for _, line in sorted(shows.get(day, []), key=lambda s: s[0])]
        if lines:  # quiet days are left off
            sections.append((_day_heading(day, today), lines))
    embed: Dict[str, Any] = {
        "title": "\N{SPIRAL CALENDAR PAD} This week",
        "color": COLOUR_WEEK if not problems else COLOUR_UNKNOWN,
        "description": _layout(problems, sections, "Nothing new is due this week."),
        "footer": {"text": ("From " + ", ".join(sources)) if sources else "No services set up"},
    }
    return embed


def _request_line(req: Dict[str, Any], note: str = "") -> str:
    kind = "\N{TELEVISION}" if req.get("type") == "tv" else "\N{CLAPPER BOARD}"
    title = _escape(_truncate(req.get("title") or "Unknown title", 80))
    who = _dict(req.get("requestedBy")).get("displayName")
    line = f"{kind} **{title}**" + (f" *({req['year']})*" if req.get("year") else "") + note
    if req.get("discord_id"):
        # A mention in an embed shows as the person's name but never pings them.
        line += f" \N{MIDDLE DOT} for <@{req['discord_id']}>"
    elif who:
        line += f" \N{MIDDLE DOT} for {_escape(_truncate(str(who), 40))}"
    return line


def _discord_id(value: Any) -> Optional[str]:
    """A Discord user ID as Seerr stores it, or None if it doesn't look like one."""
    text = str(value or "").strip()
    return text if text.isdigit() and 15 <= len(text) <= 21 else None


def _settings_discord_id(settings: Dict[str, Any]) -> Optional[str]:
    """The first Discord ID in a Seerr user's settings.

    Seerr 3.3 and later keep a list ("discordIds"); Overseerr, Jellyseerr and
    older Seerr keep one ("discordId").
    """
    ids = settings.get("discordIds")
    for value in (ids if isinstance(ids, list) else [ids]) + [settings.get("discordId")]:
        found = _discord_id(value)
        if found:
            return found
    return None


def render_soon(
    movies: Optional[List[Dict[str, Any]]],
    requests: Optional[List[Dict[str, Any]]],
    today: date,
    days: int,
    problems: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The "coming later" message, as an embed dict without a timestamp: movies out
    digitally after this week (up to days ahead), then approved Seerr requests
    that aren't in the library yet. movies is None if Radarr has never answered;
    requests is None when Seerr isn't set up."""
    embed: Dict[str, Any] = {
        "title": "\N{CLAPPER BOARD} Coming later",
        "color": COLOUR_SOON,
        "footer": {"text": f"From Radarr{', Seerr' if requests is not None else ''} \N{MIDDLE DOT} movies up to {days} days ahead"},
    }
    top = list(problems or [])
    if movies is None and requests is None:
        embed["color"] = COLOUR_UNKNOWN
        embed["description"] = "\n".join(top) or "Waiting for Radarr\N{HORIZONTAL ELLIPSIS}"
        return embed
    sections: List[Section] = []
    later = _movies_by_day(movies, today + timedelta(days=WEEK), today + timedelta(days=days))
    for day in sorted(later):
        sections.append((_day_heading(day, today), [_movie_line(m) for m in later[day]]))
    if requests is not None:
        # Approved requests not in the library yet: the ones Radarr has a digital
        # date for first, soonest first, then the rest newest first.
        due = {m.get("tmdbId"): _release_day(m.get("digitalRelease")) for m in movies or [] if m.get("tmdbId")}
        coming: List[Tuple[Tuple[int, str], str]] = []
        for n, req in enumerate(requests):
            media = _dict(req.get("media"))
            if req.get("status") != REQUEST_APPROVED or media.get("status") == MEDIA_AVAILABLE:
                continue
            note, key = "", (1, f"{n:04d}")
            day = due.get(media.get("tmdbId")) if req.get("type") == "movie" else None
            if day and day >= today:
                note = f" \N{MIDDLE DOT} out {_short_date(day)}"
                key = (0, day.isoformat())
            coming.append((key, _request_line(req, note)))
        coming.sort(key=lambda c: c[0])
        if coming:
            sections.append(("\N{INBOX TRAY} Requested and on the way", [line for _, line in coming]))
    empty = f"No more movies are due out digitally in the next {days} days."
    embed["description"] = _layout(top, sections, empty)
    if later:
        poster = _poster(later[min(later)][0])
        if poster:
            embed["thumbnail"] = {"url": poster}
    return embed


def _hash(embed: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(embed, sort_keys=True).encode()).hexdigest()[:16]


class Upcoming(commands.Cog):
    """Live messages for upcoming digital movie releases and the week ahead in Radarr, Sonarr and Seerr."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=3108452292, force_registration=True)
        # messages: "soon" / "week" -> message ID in the channel
        self.config.register_global(channel_id=None, messages={}, days=DEFAULT_DAYS, timezone="UTC")
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._shown: Dict[str, str] = {}  # "soon" / "week" -> hash of what the message says
        self._retry_at = 0.0
        self._last: Dict[str, Any] = {}  # service -> last good answer
        self._failures: Dict[str, Dict[str, Any]] = {}  # service -> {count, since}
        self._titles: Dict[Tuple[str, Any], Tuple[str, Optional[str]]] = {}  # (movie|tv, TMDB ID) -> (title, year)
        self._discord_ids: Dict[Any, Optional[str]] = {}  # Seerr user ID -> their Discord ID, if set

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

    # ---------- the services ----------

    async def _get(self, service: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        name, root = SERVICES[service]
        tokens = await self.bot.get_shared_api_tokens(service)
        base, key = (tokens.get("url") or "").strip().rstrip("/"), (tokens.get("api_key") or "").strip()
        if not base or not key:
            raise NotSetUp(f"{name} isn't set up. DM the bot: `!set api {service} url <{service} url> api_key <key>`.")
        if not base.endswith(root):
            base += root
        try:
            async with self._session.get(
                base + path,
                params=params,
                headers={"X-Api-Key": key, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status in (401, 403):
                    raise ServiceError(f"{name} rejected the API key.")
                if resp.status == 404:
                    raise ServiceError(f"{name} returned 404. Is the URL {name}'s?")
                if resp.status >= 400:
                    raise ServiceError(f"{name} returned HTTP {resp.status}.")
                try:
                    return await resp.json(content_type=None)
                except ValueError:
                    raise ServiceError(f"{name}'s answer wasn't JSON. Is the URL {name}'s?") from None
        except asyncio.TimeoutError:
            raise ServiceError(f"{name} didn't answer within {REQUEST_TIMEOUT} seconds.") from None
        except aiohttp.ClientError as e:
            log.warning("%s unreachable: %s", name, e)
            raise ServiceError(f"Couldn't reach {name}. Check the URL and that it's running.") from None

    async def _calendar(self, service: str, start: date, end: date) -> List[Dict[str, Any]]:
        params = {
            "start": f"{start.isoformat()}T00:00:00Z",
            "end": f"{end.isoformat()}T23:59:59Z",
            "unmonitored": "false",
        }
        if service == "sonarr":
            params["includeSeries"] = "true"
        data = await self._get(service, "/calendar", params)
        if not isinstance(data, list):
            raise ServiceError(f"{SERVICES[service][0]}'s calendar wasn't a list. Is the URL right?")
        return [i for i in data if isinstance(i, dict)]

    async def _requests(self) -> List[Dict[str, Any]]:
        """Seerr's most recent requests, each with a "title" added."""
        data = _dict(
            await self._get("seerr", "/request", {"take": SEERR_TAKE, "skip": 0, "filter": "all", "sort": "added"})
        )
        results = [r for r in data.get("results") or [] if isinstance(r, dict)]
        out = []
        for req in results:
            media = _dict(req.get("media"))
            kind = "tv" if req.get("type") == "tv" else "movie"
            if req.get("status") != REQUEST_APPROVED or media.get("status") == MEDIA_AVAILABLE:
                continue  # only the ones that show up on the message need a title
            req = dict(req)
            req["title"], req["year"] = await self._title(kind, media.get("tmdbId"))
            req["discord_id"] = await self._requester_discord(_dict(req.get("requestedBy")))
            out.append(req)
        return out

    async def _requester_discord(self, user: Dict[str, Any]) -> Optional[str]:
        """The Discord ID a Seerr user saved in their notification settings, if any.

        Some Seerr versions include it with the request; otherwise it's asked for
        once per user and remembered until `!upcoming refresh` or a reload.
        """
        found = _settings_discord_id(_dict(user.get("settings")))
        uid = user.get("id")
        if found or not isinstance(uid, int):
            return found
        if uid not in self._discord_ids:
            try:
                data = _dict(await self._get("seerr", f"/user/{uid}/settings/notifications"))
            except ServiceError:
                return None  # try again next check
            self._discord_ids[uid] = _settings_discord_id(data)
        return self._discord_ids[uid]

    async def _title(self, kind: str, tmdb: Any) -> Tuple[Optional[str], Optional[str]]:
        """(title, year) for a TMDB ID. Seerr's requests carry IDs, not titles; Seerr looks them up."""
        if not tmdb:
            return None, None
        if (kind, tmdb) not in self._titles:
            try:
                data = _dict(await self._get("seerr", f"/{kind}/{int(tmdb)}"))
            except (ServiceError, ValueError):
                return None, None
            title = data.get("title") or data.get("name")
            if not title:
                return None, None
            year = str(data.get("releaseDate") or data.get("firstAirDate") or "")[:4]
            self._titles[(kind, tmdb)] = (str(title), year if year.isdigit() else None)
        return self._titles[(kind, tmdb)]

    async def _history(self, service: str, since: datetime) -> List[Dict[str, Any]]:
        """Radarr's or Sonarr's history since a time (imports are picked out later)."""
        params = {"date": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if service == "radarr":
            params["includeMovie"] = "true"
        else:
            params.update(includeSeries="true", includeEpisode="true")
        data = await self._get(service, "/history/since", params)
        if not isinstance(data, list):
            raise ServiceError(f"{SERVICES[service][0]}'s history wasn't a list.")
        return [i for i in data if isinstance(i, dict)]

    async def _fetch(self, service: str, call, key: Optional[str] = None) -> Tuple[Any, Optional[str], bool]:
        """(data, problem line, set up). Keeps the last good answer when a check fails.

        key keeps a second call to the same service (its history) apart from the first.
        """
        name = SERVICES[service][0]
        key = key or service
        try:
            data = await call()
        except NotSetUp:
            self._last.pop(key, None)
            self._failures.pop(key, None)
            return None, None, False
        except ServiceError as e:
            f = self._failures.setdefault(key, {"count": 0, "since": time.time()})
            f["count"] += 1
            log.debug("Upcoming: can't read %s: %s", name, e)
            note = _stale_note(name, f)
            if key not in self._last:
                return None, f"{DOT_STALE} {e}", True
            return self._last[key], note, True
        self._failures.pop(key, None)
        self._last[key] = data
        return data, None, True

    async def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(await self.config.timezone())
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    # ---------- keeping the messages up to date ----------

    async def _loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Upcoming: sync failed")
            await asyncio.sleep(POLL_EVERY)

    async def _build(self) -> Dict[str, Dict[str, Any]]:
        tz = await self._tz()
        today = datetime.now(tz).date()
        days = await self.config.days()
        # A day either side, so no timezone drops a release off either end.
        start, end = today - timedelta(days=1), today + timedelta(days=max(days, WEEK) + 1)
        movies, radarr_problem, radarr_on = await self._fetch("radarr", lambda: self._calendar("radarr", start, end))
        episodes, sonarr_problem, sonarr_on = await self._fetch(
            "sonarr", lambda: self._calendar("sonarr", start, today + timedelta(days=WEEK + 1))
        )
        requests, seerr_problem, seerr_on = await self._fetch("seerr", self._requests)
        # What landed in the library since midnight. A failed history check just
        # leaves the section off (or as it last was); the calendar's note covers
        # a service that's down.
        midnight = datetime.combine(today, datetime.min.time(), tzinfo=tz)
        movie_history = episode_history = None
        if radarr_on:
            movie_history, _, _ = await self._fetch("radarr", lambda: self._history("radarr", midnight), "radarr-history")
        if sonarr_on:
            episode_history, _, _ = await self._fetch(
                "sonarr", lambda: self._history("sonarr", midnight), "sonarr-history"
            )
        # A kept answer from before midnight mustn't carry yesterday over.
        today_only = lambda recs: [r for r in recs or [] if (_parse_time(r.get("date")) or midnight) >= midnight]
        added = _added_lines(today_only(movie_history), today_only(episode_history))
        if not radarr_on:
            radarr_problem = "Radarr isn't set up, so there's nothing to list. See `!upcoming show`."
        soon = render_soon(movies, requests, today, days, [p for p in (radarr_problem, seerr_problem) if p])
        sources = [n for n, on in (("Radarr", radarr_on), ("Sonarr", sonarr_on)) if on]
        problems = [p for p in (radarr_problem if radarr_on else None, sonarr_problem) if p]
        week = render_week(movies, episodes, added, today, tz, sources, problems)
        week["footer"]["text"] += f" \N{MIDDLE DOT} days in {tz.key}"
        # In this order, so a fresh setup reads this week first, then later.
        return {"week": week, "soon": soon}

    async def _sync(self, force: bool = False) -> None:
        async with self._lock:
            channel_id = await self.config.channel_id()
            if not channel_id:
                return
            if force:
                self._shown.clear()
                self._retry_at = 0.0
            if time.monotonic() < self._retry_at:
                return
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                return  # the channel is gone, or the bot can't see it
            for key, embed in (await self._build()).items():
                await self._show(channel, key, embed)

    async def _show(self, channel: discord.abc.Messageable, key: str, embed: Dict[str, Any]) -> None:
        digest = _hash(embed)
        if self._shown.get(key) == digest:
            return
        e = discord.Embed.from_dict(embed)
        e.timestamp = datetime.now(timezone.utc)
        message_id = (await self.config.messages()).get(key)
        try:
            gone = not message_id
            if not gone:
                try:
                    await channel.get_partial_message(int(message_id)).edit(embed=e, allowed_mentions=NO_MENTIONS)
                except discord.NotFound:
                    gone = True
            if gone:
                # Deleted, or never posted: put it back.
                message = await channel.send(embed=e, allowed_mentions=NO_MENTIONS)
                async with self.config.messages() as messages:
                    messages[key] = message.id
        except discord.Forbidden as err:
            self._retry_at = time.monotonic() + RETRY_FORBIDDEN
            log.warning("Upcoming: no permission in channel %s: %s", channel.id, err)
            return
        except discord.HTTPException as err:
            log.warning("Upcoming: couldn't update the %s message: %s", key, err)
            return
        self._shown[key] = digest

    # ---------- owner commands ----------

    @commands.group(name="upcoming")
    @commands.is_owner()
    async def upcoming(self, ctx: commands.Context) -> None:
        """Live messages for upcoming releases, from Radarr, Sonarr and Seerr."""

    @upcoming.command(name="setup")
    async def up_setup(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Post the two live messages in a channel (moving them if they're somewhere else).

        Example: `!upcoming setup #upcoming`
        """
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await ctx.send(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
            return
        await self._delete_messages()
        await self.config.channel_id.set(channel.id)
        await self.config.messages.set({})
        async with ctx.typing():
            await self._sync(force=True)
        posted = await self.config.messages()
        if len(posted) < 2:
            await ctx.send(f"Couldn't post both messages in {channel.mention}. Check my permissions there.")
            return
        await ctx.send(
            f"The messages are up in {channel.mention} and update every {POLL_EVERY // 60} minutes. "
            "Day headings use UTC until you set your timezone: `!upcoming timezone Europe/London`."
        )

    @upcoming.command(name="remove")
    async def up_remove(self, ctx: commands.Context) -> None:
        """Delete the live messages and stop updating them."""
        if not await self.config.channel_id():
            await ctx.send("There are no messages to remove.")
            return
        async with self._lock:
            await self._delete_messages()
            await self.config.channel_id.set(None)
            await self.config.messages.set({})
            self._shown.clear()
        await ctx.send("Removed. `!upcoming setup #channel` puts them back.")

    async def _delete_messages(self) -> None:
        channel_id = await self.config.channel_id()
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return
        for message_id in (await self.config.messages()).values():
            try:
                await channel.get_partial_message(int(message_id)).delete()
            except discord.HTTPException:
                pass  # already gone, or not allowed: nothing more to do

    @upcoming.command(name="days")
    async def up_days(self, ctx: commands.Context, days: int) -> None:
        """How many days ahead "Coming later" lists movies (7 to 90, default 30)."""
        if not MIN_DAYS <= days <= MAX_DAYS:
            await ctx.send(f"Pick a number from {MIN_DAYS} to {MAX_DAYS}.")
            return
        await self.config.days.set(days)
        await self._sync(force=True)
        await ctx.send(f"\"Coming later\" now lists movies up to {days} days ahead.")

    @upcoming.command(name="timezone", aliases=["tz"])
    async def up_timezone(self, ctx: commands.Context, name: str) -> None:
        """Set the timezone that decides which day an episode falls on, like `Europe/London`.

        Movie release dates are whole days in Radarr, so they don't move.
        """
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            await ctx.send(
                f"`{_truncate(name, 60)}` isn't a timezone I know. Use a name like `Europe/London` or "
                "`America/New_York`."
            )
            return
        await self.config.timezone.set(name)
        await self._sync(force=True)
        await ctx.send(f"Days now follow {name}.")

    @upcoming.command(name="refresh")
    async def up_refresh(self, ctx: commands.Context) -> None:
        """Update the messages now instead of waiting for the next check."""
        if not await self.config.channel_id():
            await ctx.send("Nothing is set up yet. Start with `!upcoming setup #channel`.")
            return
        self._titles.clear()
        self._discord_ids.clear()
        async with ctx.typing():
            await self._sync(force=True)
        await ctx.tick()

    @upcoming.command(name="show")
    async def up_show(self, ctx: commands.Context) -> None:
        """Show settings and check each service (never shows the keys)."""
        channel_id = await self.config.channel_id()
        lines = [
            f"Channel:   {('#' + str(self.bot.get_channel(channel_id) or channel_id)) if channel_id else 'not set up'}",
            f"Days:      {await self.config.days()}",
            f"Timezone:  {await self.config.timezone()}",
            "",
        ]
        today = datetime.now(await self._tz()).date()
        checks = {
            "radarr": lambda: self._calendar("radarr", today, today + timedelta(days=WEEK)),
            "sonarr": lambda: self._calendar("sonarr", today, today + timedelta(days=WEEK)),
            "seerr": lambda: self._get("seerr", "/request/count"),
        }
        async with ctx.typing():
            for service, (name, _) in SERVICES.items():
                tokens = await self.bot.get_shared_api_tokens(service)
                if not tokens.get("url") or not tokens.get("api_key"):
                    optional = " (optional)" if service == "sonarr" else ""
                    lines.append(f"{name + ':':<10} not set up{optional}")
                    continue
                try:
                    await checks[service]()
                except ServiceError as e:
                    lines.append(f"{name + ':':<10} FAILED - {e}")
                else:
                    lines.append(f"{name + ':':<10} OK ({tokens['url']})")
        await ctx.send(box(_truncate("\n".join(lines), 1900)))
