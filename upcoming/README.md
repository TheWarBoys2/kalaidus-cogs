# upcoming

Keeps two messages in one channel up to date with what's coming to your media server. Apart from the "Added today" section, they only list things that are still on the way: anything Radarr or Sonarr already has the file for is left off, and physical (disc) release dates are ignored because a disc release doesn't put anything on the server.

Each message is split into sections with a heading and a gap between them, and every line starts with 🎬 for a movie or 📺 for a show.

- **🗓️ This week**:
  - **✅ Added today**: movies and episodes Radarr and Sonarr imported since midnight in your timezone, newest first, with a show's episodes on one line (like *S11E26–E37*). It comes from their history, so it includes anything imported today, even an old film someone just requested. A quality upgrade of something already in the library counts as an import too, so it can show up here.
  - Then a heading for each day from today to six days ahead that has something due (like "Tomorrow · Thursday 1st October"): digital movie releases first, then new episodes with their air time in each reader's own timezone. Days with nothing due are left off.
- **🎬 Coming later**:
  - Movies out digitally after this week, up to 30 days ahead (you can pick 7 to 90), under a heading per day with how many days away it is, and the first one's poster.
  - **📥 Requested and on the way**, if Seerr is set up: approved requests that aren't in the library yet and who asked for them. Movies Radarr has a digital date for come first, soonest first, with "out …" and the date. If the requester has saved their Discord ID in Seerr (their profile → Notifications → Discord), they're shown as a Discord mention, otherwise by their Seerr name. The mention never pings anyone: the bot posts with pings turned off, and the message is edited rather than reposted. Requests still waiting for approval aren't shown, since they may never be added.

If a section gets too long for one Discord message, each section is cut shorter and ends with "…and N more".

The bot only reads from Radarr, Sonarr and Seerr. It can't approve requests or change anything.

## What it can and can't know

- It only knows about movies Radarr is **monitoring**, not every film coming out. The same goes for Sonarr and monitored series.
- The Seerr section looks at Seerr's 100 most recent requests. Seerr gives TMDB IDs rather than titles, so the cog asks Seerr for each title once and remembers it until `!upcoming refresh` or a reload.
- Release dates are whole days in Radarr and are shown as written. Your timezone only decides which day an episode falls on and what counts as "today", including for "Added today".

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

   Radarr is the one that matters. Sonarr and Seerr are optional: leave either out and its part of the messages is simply left off. The URL is the address you open each one at; `/api/v3` (Radarr, Sonarr) or `/api/v1` (Seerr) is added if you leave it off.
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
| `!upcoming days <7-90>` | How far ahead "Coming later" lists movies. Default 30 |
| `!upcoming timezone <name>` | Sets the timezone, e.g. `Europe/London` or `America/New_York`. Alias `tz` |
| `!upcoming refresh` | Checks everything and updates the messages now, and forgets remembered Seerr titles and Discord IDs |
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
| Radarr | `/api/v3/calendar` (monitored only) | Digital release dates, whether the file is in the library |
| Radarr | `/api/v3/history/since` (with movie) | Movies imported since midnight |
| Sonarr | `/api/v3/calendar` (monitored only, with series) | Episodes airing this week, whether the file is in the library |
| Sonarr | `/api/v3/history/since` (with series and episode) | Episodes imported since midnight |
| Seerr | `/api/v1/request` | The 100 most recent requests |
| Seerr | `/api/v1/movie/{tmdb id}`, `/api/v1/tv/{tmdb id}` | Titles and years for the approved requests |
| Seerr | `/api/v1/user/{id}/settings/notifications` | A requester's Discord ID, when Seerr didn't include it with the request. Asked once per requester |
| Seerr | `/api/v1/request/count` | Only by `!upcoming show`, to check the key |

## What it stores

The channel, the two message IDs, days ahead and timezone. Seerr requester names and Discord IDs are shown on the "Coming later" message but not stored; the IDs are remembered in memory until `!upcoming refresh` or a reload.
