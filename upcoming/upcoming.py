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
MAX_SOON = 20  # movies listed on the "out digitally soon" message
MAX_PER_DAY = 8  # lines per day on the week message
MAX_REQUESTS = 10  # lines in the Seerr section
MAX_ADDED = 12  # lines in "Added today"
ADDED_LIMIT = 900
# History events that mean a file landed in the library: a finished download, or
# a folder imported by hand.
IMPORTED = {"downloadFolderImported", "movieFolderImported", "seriesFolderImported"}
SEERR_TAKE = 100  # most recent requests read from Seerr
# Discord allows 6000 characters in an embed, so the week's fields share that out.
DAY_LIMIT = 520
REQUEST_LIMIT = 700
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


def _day_label(day: date, today: date) -> str:
    diff = (day - today).days
    if diff == 0:
        return "today"
    if diff == 1:
        return "tomorrow"
    return f"in {diff} days"


def _short_date(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _limit_lines(lines: List[str], most: int, limit: int, more: str = "") -> str:
    """Join lines, keeping under a character limit and adding "…and N more"."""
    shown = lines[:most]
    while True:
        extra = len(lines) - len(shown)
        tail = [f"\N{HORIZONTAL ELLIPSIS}and {extra} more{more}"] if extra else []
        text = "\n".join(shown + tail)
        if len(text) <= limit or not shown:
            return _truncate(text, limit)
        shown = shown[:-1]


def _movie_line(movie: Dict[str, Any], with_link: bool = True) -> str:
    title = _escape(movie.get("title") or "Untitled")
    year = movie.get("year")
    name = f"**{title}**" + (f" ({year})" if year else "")
    tmdb = movie.get("tmdbId")
    if with_link and tmdb:
        name = f"[{name}](https://www.themoviedb.org/movie/{tmdb})"
    return name


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


def render_soon(
    movies: Optional[List[Dict[str, Any]]],
    today: date,
    days: int,
    problem: Optional[str] = None,
) -> Dict[str, Any]:
    """The "out digitally soon" message, as an embed dict without a timestamp.

    movies is Radarr's calendar (None if Radarr has never answered);
    problem is a line to show above the list, if any.
    """
    embed: Dict[str, Any] = {
        "title": "\N{CLAPPER BOARD} Out digitally soon",
        "color": COLOUR_SOON,
        "footer": {"text": f"From Radarr \N{MIDDLE DOT} next {days} days"},
    }
    lines: List[str] = [problem, ""] if problem else []
    if movies is None:
        embed["color"] = COLOUR_UNKNOWN
        embed["description"] = "\n".join(lines).strip() or "Waiting for Radarr\N{HORIZONTAL ELLIPSIS}"
        return embed
    end = today + timedelta(days=days)
    due: List[Tuple[date, str, Dict[str, Any]]] = []
    for movie in movies:
        if movie.get("hasFile"):
            continue  # already in the library, so nothing to wait for
        day = _release_day(movie.get("digitalRelease"))
        if day and today <= day <= end:
            due.append((day, str(movie.get("sortTitle") or movie.get("title") or ""), movie))
    due.sort(key=lambda d: (d[0], d[1]))
    if not due:
        lines.append(f"Nothing new is due out digitally in the next {days} days.")
    # One heading per day, with that day's movies under it.
    items = []
    previous = None
    for day, _, movie in due:
        line = _movie_line(movie)
        if day != previous:
            heading = f"**{_short_date(day)}** \N{MIDDLE DOT} {_day_label(day, today)}"
            line = ("\n" if previous else "") + heading + "\n" + line
            previous = day
        items.append(line)
    budget = 4000 - len("\n".join(lines))
    if items:
        lines.append(_limit_lines(items, MAX_SOON, budget))
    embed["description"] = "\n".join(lines).strip()
    if due:
        poster = _poster(due[0][2])
        if poster:
            embed["thumbnail"] = {"url": poster}
    return embed


def _week_items(
    movies: Optional[List[Dict[str, Any]]],
    episodes: Optional[List[Dict[str, Any]]],
    today: date,
    tz: ZoneInfo,
) -> Dict[date, List[Tuple[str, str]]]:
    """Each day's lines for the week, as (sort key, line)."""
    days: Dict[date, List[Tuple[str, str]]] = {today + timedelta(days=i): [] for i in range(WEEK)}
    # Only what's still to come: digital releases (physical ones never reach the
    # server on their own) and nothing Radarr or Sonarr already has.
    for movie in movies or []:
        if movie.get("hasFile"):
            continue
        day = _release_day(movie.get("digitalRelease"))
        if day in days:
            line = f"\N{CLAPPER BOARD} {_movie_line(movie, with_link=False)}"
            days[day].append(("0" + str(movie.get("sortTitle") or movie.get("title") or ""), line))
    # Episodes of one show on one day go on one line.
    shows: Dict[Tuple[date, Any], List[Dict[str, Any]]] = {}
    for ep in episodes or []:
        if ep.get("hasFile"):
            continue
        aired = _parse_time(ep.get("airDateUtc"))
        if not aired:
            continue
        day = aired.astimezone(tz).date()
        if day in days:
            series = _dict(ep.get("series"))
            shows.setdefault((day, ep.get("seriesId") or series.get("title")), []).append(ep)
    for (day, _), eps in shows.items():
        eps.sort(key=lambda e: (e.get("seasonNumber") or 0, e.get("episodeNumber") or 0))
        first, last = eps[0], eps[-1]
        series = _dict(first.get("series"))
        name = _escape(series.get("title") or first.get("title") or "Unknown show")
        code = f"S{first.get('seasonNumber') or 0:02d}E{first.get('episodeNumber') or 0:02d}"
        if len(eps) > 1 and last.get("seasonNumber") == first.get("seasonNumber"):
            code += f"\N{EN DASH}E{last.get('episodeNumber') or 0:02d}"
        elif len(eps) > 1:
            code += f" +{len(eps) - 1}"
        aired = _parse_time(first.get("airDateUtc"))
        # The air time leads, so a day reads like a TV guide.
        line = f"<t:{int(aired.timestamp())}:t> **{name}** {code}"
        days[day].append(("1" + aired.isoformat() + name, line))
    return days


def _request_line(req: Dict[str, Any], note: str = "") -> str:
    kind = "\N{TELEVISION}" if req.get("type") == "tv" else "\N{CLAPPER BOARD}"
    title = _escape(_truncate(req.get("title") or "Unknown title", 80))
    who = _dict(req.get("requestedBy")).get("displayName")
    line = f"{kind} **{title}**" + (f" ({req['year']})" if req.get("year") else "") + note
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


def _added_lines(
    movie_history: Optional[List[Dict[str, Any]]],
    episode_history: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """One line per movie and per show imported, from Radarr's and Sonarr's history."""
    lines: List[Tuple[str, str]] = []
    seen = set()
    for rec in movie_history or []:
        movie = _dict(rec.get("movie"))
        key = rec.get("movieId") or movie.get("tmdbId") or movie.get("title")
        if rec.get("eventType") not in IMPORTED or not movie or key in seen:
            continue
        seen.add(key)
        lines.append((str(rec.get("date") or ""), f"\N{CLAPPER BOARD} {_movie_line(movie, with_link=False)}"))
    shows: Dict[Any, List[Dict[str, Any]]] = {}
    for rec in episode_history or []:
        if rec.get("eventType") in IMPORTED and rec.get("series") and rec.get("episode"):
            shows.setdefault(rec.get("seriesId") or _dict(rec["series"]).get("title"), []).append(rec)
    for recs in shows.values():
        eps = {}
        for rec in recs:
            ep = _dict(rec.get("episode"))
            eps[(ep.get("seasonNumber") or 0, ep.get("episodeNumber") or 0)] = rec
        numbers = sorted(eps)
        (s1, e1), (s2, e2) = numbers[0], numbers[-1]
        code = f"S{s1:02d}E{e1:02d}"
        if len(numbers) > 1:
            code += f"\N{EN DASH}E{e2:02d}" if s1 == s2 else f" +{len(numbers) - 1}"
        name = _escape(_dict(recs[0].get("series")).get("title") or "Unknown show")
        latest = max(str(r.get("date") or "") for r in recs)
        lines.append((latest, f"\N{TELEVISION} **{name}** {code}"))
    lines.sort(key=lambda item: item[0], reverse=True)  # newest first
    return [line for _, line in lines]


def render_week(
    movies: Optional[List[Dict[str, Any]]],
    episodes: Optional[List[Dict[str, Any]]],
    requests: Optional[List[Dict[str, Any]]],
    today: date,
    tz: ZoneInfo,
    sources: List[str],
    problems: List[str],
    tv: bool = False,
    added: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The week-ahead message, as an embed dict without a timestamp.

    Each input is None when that service isn't set up or has never answered.
    With tv (Sonarr is set up) the days list only episodes, since the movies
    are on the "out digitally soon" message already; without it they list movies.
    """
    fields: List[Dict[str, Any]] = []
    if added:
        fields.append(
            {
                "name": f"\N{WHITE HEAVY CHECK MARK} Added today ({len(added)})",
                "value": _limit_lines(added, MAX_ADDED, ADDED_LIMIT),
                "inline": False,
            }
        )
    listed = None if tv else movies
    for day, items in _week_items(listed, episodes, today, tz).items():
        if not items:
            continue  # quiet days are left off rather than saying so
        label = {0: "Today \N{MIDDLE DOT} ", 1: "Tomorrow \N{MIDDLE DOT} "}.get((day - today).days, "")
        items.sort()
        value = _limit_lines([line for _, line in items], MAX_PER_DAY, DAY_LIMIT)
        fields.append({"name": label + _short_date(day), "value": value, "inline": False})
    lines = list(problems)
    if not fields and (listed is not None or episodes is not None):
        lines.append("No new episodes this week." if tv else "Nothing new is due this week.")
    if requests is not None:
        # A movie Radarr expects out digitally this week says so.
        due = {
            m.get("tmdbId"): _release_day(m.get("digitalRelease"))
            for m in movies or []
            if m.get("tmdbId")
        }
        # Approved requests that aren't in the library yet: the ones with a
        # digital date first, soonest first, then the rest newest first.
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
            fields.append(
                {
                    "name": f"\N{INBOX TRAY} Requested and on the way ({len(coming)})",
                    "value": _limit_lines([line for _, line in coming], MAX_REQUESTS, REQUEST_LIMIT),
                    "inline": False,
                }
            )
    description = "\n".join(lines) if lines else None
    embed: Dict[str, Any] = {
        "title": "\N{TELEVISION} New episodes this week" if tv else "\N{SPIRAL CALENDAR PAD} Coming up this week",
        "color": COLOUR_WEEK if not problems else COLOUR_UNKNOWN,
        "fields": fields,
        "footer": {"text": ("From " + ", ".join(sources)) if sources else "No services set up"},
    }
    if description:
        embed["description"] = description
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
        found = _discord_id(_dict(user.get("settings")).get("discordId"))
        uid = user.get("id")
        if found or not isinstance(uid, int):
            return found
        if uid not in self._discord_ids:
            try:
                data = _dict(await self._get("seerr", f"/user/{uid}/settings/notifications"))
            except ServiceError:
                return None  # try again next check
            self._discord_ids[uid] = _discord_id(data.get("discordId"))
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
        soon = render_soon(movies, today, days, radarr_problem)
        sources = [n for n, on in (("Radarr", radarr_on), ("Sonarr", sonarr_on), ("Seerr", seerr_on)) if on]
        problems = [p for p in (radarr_problem if radarr_on else None, sonarr_problem, seerr_problem) if p]
        week = render_week(movies, episodes, requests, today, tz, sources, problems, tv=sonarr_on, added=added)
        week["footer"]["text"] += f" \N{MIDDLE DOT} days in {tz.key}"
        return {"soon": soon, "week": week}

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
        """How many days ahead "Out digitally soon" looks (7 to 90, default 30)."""
        if not MIN_DAYS <= days <= MAX_DAYS:
            await ctx.send(f"Pick a number from {MIN_DAYS} to {MAX_DAYS}.")
            return
        await self.config.days.set(days)
        await self._sync(force=True)
        await ctx.send(f"\"Out digitally soon\" now looks {days} days ahead.")

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
