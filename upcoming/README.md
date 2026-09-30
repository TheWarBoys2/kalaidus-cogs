# upcoming

Keeps two messages in one channel up to date with what's coming to your media server:

- **🎬 Out digitally soon**: movies in Radarr whose digital release is in the next 30 days (you can pick 7 to 90). Each shows the date, how many days away it is, a link to TMDB, and "✅ in library" if Radarr already has the file. Up to 20 are listed, soonest first, with the first one's poster.
- **🗓️ Coming up this week**: today and the next six days. Each day lists Radarr's digital and physical releases and, if Sonarr is set up, new episodes (several episodes of one show on one day go on one line, with the air time in each reader's own timezone). Below that, if Seerr is set up: requests waiting for approval, and approved requests not in the library yet (with "out digitally …" when Radarr expects the movie this week or later).

The bot only reads from Radarr, Sonarr and Seerr. It can't approve requests or change anything.

## What it can and can't know

- It only knows about movies Radarr is **monitoring**, not every film coming out. The same goes for Sonarr and monitored series.
- The Seerr sections look at Seerr's 100 most recent requests. Seerr gives TMDB IDs rather than titles, so the cog asks Seerr for each title once and remembers it until `!upcoming refresh` or a reload.
- Release dates are whole days in Radarr and are shown as written. Your timezone only decides which day an episode falls on and what counts as "today".

## Setup

1. **Find each API key**: Settings → General in Radarr and Sonarr, Settings → General → API Key in Seerr (Overseerr and Jellyseerr work the same way).
2. **Install and add the keys**:

   ```
   !cog install kalaidus-cogs upcoming
   !load upcoming
   ```

   Then DM the bot:

   ```
   !set api radarr url <radarr url> api_key <key>
   !set api sonarr url <sonarr url> api_key <key>
   !set api seerr url <seerr url> api_key <key>
   ```

   Radarr is the one that matters. Sonarr and Seerr are optional: leave either out and its part of the week message is simply left off. The URL is the address you open each one at; `/api/v3` (Radarr, Sonarr) or `/api/v1` (Seerr) is added if you leave it off.
3. **Check them** with `!upcoming show`.
4. **Set your timezone**: `!upcoming timezone Europe/London`. It's UTC until you do.
5. **Post the messages**: `!upcoming setup #channel`. The bot needs View Channel, Send Messages and Embed Links there.

The cog needs the `tzdata` Python package, which Red installs with it.

## Commands

All bot owner only and prefix only.

| Command | What it does |
| --- | --- |
| `!upcoming setup #channel` | Posts both messages there. If they were somewhere else, the old ones are deleted first |
| `!upcoming remove` | Deletes both messages and stops updating them |
| `!upcoming days <7-90>` | How far ahead "Out digitally soon" looks. Default 30 |
| `!upcoming timezone <name>` | Sets the timezone, e.g. `Europe/London` or `America/New_York`. Alias `tz` |
| `!upcoming refresh` | Checks everything and updates the messages now, and forgets remembered Seerr titles |
| `!upcoming show` | Channel, days, timezone, and a live check of each service. Never shows the keys |

There's one pair of messages for the whole bot, not one per Discord server.

## How it works

- Every 30 minutes the cog reads Radarr's and Sonarr's calendars and Seerr's requests, rebuilds both messages, and edits them only if something changed.
- If a service doesn't answer, the messages keep its last good answer. After three failed checks in a row a ⚪ line says that service hasn't answered since a given time, so the list may be out of date. If a service has never answered since the bot started, its error is shown instead.
- If someone deletes one of the messages, the next check posts it again. If the bot isn't allowed to post, it tries again after ten minutes.
- Messages only update while the bot is running.

## What it calls

All `GET`, with the key in an `X-Api-Key` header:

| Service | Call | Used for |
| --- | --- | --- |
| Radarr | `/api/v3/calendar` (monitored only) | Digital and physical release dates, whether the file is in the library |
| Sonarr | `/api/v3/calendar` (monitored only, with series) | Episodes airing this week |
| Seerr | `/api/v1/request` | The 100 most recent requests |
| Seerr | `/api/v1/movie/{tmdb id}`, `/api/v1/tv/{tmdb id}` | Titles and years for those requests |
| Seerr | `/api/v1/request/count` | Only by `!upcoming show`, to check the key |

## What it stores

The channel, the two message IDs, days ahead and timezone. Seerr requester names are shown on the week message but not stored.
