# containerstatus

Live status cards for any Docker container, and optionally a status dot at the start of a channel's name. Container status comes from **Arcane**'s API.

The bot only reads from Arcane. It can't start, stop, update or change anything, and it never talks to Docker directly.

## What a card shows

Each card is an embed the bot keeps editing:

| Dot | Card says | When |
| --- | --- | --- |
| 🟢 | Online, up since … | Running, and healthy or with no healthcheck |
| 🔵 | Online, plus "Update available" with the versions if Arcane knows them | Running and healthy, and Arcane's own image update check found a newer image |
| 🟠 | Starting / Unhealthy / Restarting / Paused | Healthcheck still starting or failing, or Docker says restarting or paused |
| 🔴 | Offline, last seen … | Anything else, e.g. exited or created |
| 🔴 | Not found | Arcane has no container by that name |
| ⚪ | Status unavailable | Arcane failed three checks in a row for that environment |

You can add a title (defaults to the container name), a description at the top, and an "Open" link with an optional label. The footer says "Updated via Arcane" with the time of the last edit.

The 🔵 comes from the update result Arcane already stores from its own scheduled checks. The cog doesn't ask Arcane to check, so it's only as fresh as Arcane's schedule.

## Setup

1. **Make an Arcane API key** whose role only has `containers:list` and `containers:read`. Add `environments:list` if you want cards from more than one Arcane environment (or to list them).
2. **Install and add the key**:

   ```
   !cog install kalaidus-cogs containerstatus
   !load containerstatus
   ```

   Then DM the bot: `!set api arcane url <arcane url> api_key <key>`

   The URL is the address you open Arcane at in a browser. `/api` is added if you leave it off.
3. **Check it** with `!containercard show`. It lists the settings and how many containers it can see.
4. **Post cards**, one of three ways:
   - `!containercard autosetup [category]` finds text channels named after containers, shows you the plan, and after you type `yes` posts a card and adds a dot in each.
   - `!containercard setup #channel [container]` does one channel: a card plus a dot.
   - `!containercard add #channel <container> [title]` posts just a card.

The bot needs View Channel, Send Messages and Embed Links in a card's channel, and Manage Channels there for a dot.

## Commands

Every command is bot owner only and prefix only (no slash commands). `!ccard` is a short alias for `!containercard`. `<card>` is the card number from `!containercard list`.

### Cards

| Command | What it does |
| --- | --- |
| `!containercard add #channel <container> [title]` | Posts a card. Use `name@environment` for a container outside the default environment; a name that's only in one environment is found without it |
| `!containercard title <card> <title>` | Changes the title. `clear` goes back to the container's name |
| `!containercard description <card> [text]` | Sets the text at the top (up to 1000 characters). Leave it out to remove it. Alias `desc` |
| `!containercard link <card> [url] [label]` | Sets the "Open" link (http or https). Leave the URL out to remove it |
| `!containercard container <card> <container>` | Points the card at another container (`name` or `name@environment`) |
| `!containercard environment <card> <environment>` | Moves the card to the same-named container in another environment, or `default` |
| `!containercard remove <card>` | Stops updating the card, deletes its message, and removes any dot that used it. Alias `delete` |
| `!containercard list` | Every card: number, title, container, channel, link |
| `!containercard refresh` | Checks Arcane and updates every card now |
| `!containercard show` | Settings and a connection check. Never shows the key |

### Setting up from channel names

| Command | What it does |
| --- | --- |
| `!containercard setup #channel [container]` | Card and dot for one channel (card only if the bot lacks Manage Channels there). Without a container, it uses the one the channel is named after |
| `!containercard autosetup [category]` | Looks at every text channel (or one category's) without a card, matches names to containers across all environments, shows the plan, and waits 60 seconds for you to type `yes` |
| `!containercard ignore [#channel]` | Keeps setup and autosetup out of a channel. With no channel, lists ignored ones |
| `!containercard unignore #channel` | Lets setup and autosetup use it again |
| `!containercard cleanup` | Lists this cog's cards and dots in channels PZAdmin covers or you've ignored, then removes them after you type `yes`. Doesn't rename channels, since PZAdmin may be the one showing the dot |

**How names are matched:** dots, emoji, case, dashes and underscores are ignored, so `#🟢-home-assistant` matches `home_assistant`. An exact match wins. Otherwise a channel matches containers whose names start with it; if there are several, one ending in `server`, `app`, `web`, `main`, `core` or `frontend` is picked (so `#immich` → `immich_server`). If it's still ambiguous, the channel is skipped and listed so you can use `setup` with a name.

**Channels left alone** by setup and autosetup (and removed by cleanup): ones you've ignored, ones the pzadmin cog links or puts a dot in, and ones whose last 50 messages include PZAdmin's own status card (footer "Updated by PZAdmin"). `!containercard add` still works there if you really want a card.

### Environments

| Command | What it does |
| --- | --- |
| `!containercard containers [environment]` | Lists container names and states, for every environment or one |
| `!containercard environments` | Lists Arcane's environments (needs `environments:list`). Alias `envs` |
| `!containercard env [environment]` | Shows or sets the default environment. `0` (the default) is Arcane's own machine |

A card added without `@environment` follows the default, so changing the default moves those cards too.

### Channel-name dots

| Command | What it does |
| --- | --- |
| `!containercard dots add #channel [card]` | Puts the card's dot at the start of the channel's name. Without a card, uses the one card in that channel |
| `!containercard dots remove #channel` | Stops the dot and puts the plain name back |
| `!containercard dots list` | Lists channels with dots, and says when a waiting change will go up and why |

## How it works

- Every 30 seconds the cog reads the container list from each environment its cards use, then each card's container in detail. It only edits a card when what it would say has changed.
- Each environment is checked separately, so one that's offline (an agent on another machine) doesn't grey out the rest. A single failed check changes nothing; after three in a row that environment's cards turn ⚪ and their dots stay as they were.
- If someone deletes a card's message, the next check posts it again. If the bot isn't allowed to post in a channel, it tries again after ten minutes.
- Dots follow the same rules as the pzadmin cog: the first dot and any change to or from 🟠 go up straight away; anything else has to hold for 90 seconds; at most two renames per channel every ten minutes (Discord's limit). The cog never renames a channel the pzadmin cog puts a dot in, and warns you if you try.
- Cards, dots and the default environment are global to the bot, not per Discord server.

## What it calls in Arcane

All `GET`, with the key in an `X-API-Key` header:

| Call | Used for |
| --- | --- |
| `/api/environments` | Listing environments and finding one by name |
| `/api/environments/{env}/containers` | Container names, IDs and stored update results (hidden and internal containers included) |
| `/api/environments/{env}/containers/{id}` | A container's state, health, start and finish times |

## What it stores

The default environment, the cards (channel, message, container, environment, title, description, link), which channels have dots, and ignored channels. Nothing about users.
