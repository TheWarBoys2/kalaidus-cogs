# pzadmin

Project Zomboid server status, player lists, restarts and mod requests in Discord, plus an optional 🟢 / 🟠 / 🔴 at the start of a channel's name.

The cog only talks to [PZAdmin](https://github.com/TheWarBoys2/pzadmin)'s HTTP API (`/api/v1`) with an API key. PZAdmin does the RCON, container and mod work itself. The bot never talks to the game server or Docker directly.

## Setup

1. **Make a key in PZAdmin** under Settings → API keys. What to tick depends on what you want the bot to do:

   | Access | Needed for |
   | --- | --- |
   | read (always on) | `!pz servers`, `!pz status`, `!pz players`, status dots, `!pzadminset show` |
   | request | `!pz request` |
   | control | `!pzctl restart` |

   The cog never uses the console scope, so leave that off. Limit the key to the servers the bot should see; servers outside the key's list look like they don't exist to the cog.

2. **Install and point it at PZAdmin**:

   ```
   !cog install kalaidus-cogs pzadmin
   !load pzadmin
   !pzadminset url http://192.168.0.50:27815
   ```

   Then DM the bot: `!set api pzadmin api_key <key>`

3. **Check it** with `!pzadminset show`. It reads `/api/v1/key` and lists the key's name, scopes, servers and expiry, or says why it failed.

4. **Slash commands** (optional): `!slash enable pz`, `!slash enable pzctl`, then `!slash sync`.

5. **Lock down restarts.** See [Permissions](#permissions) below. Out of the box, anyone who can see the bot can type `!pzctl restart`.

6. **Mod requests** (optional): give the key the request scope, then link each server's channel with `!pzadminset link #channel <server>`. Run `!slash sync` again so `/pz request` shows up.

7. **Status dots** (optional): give the bot Manage Channels on the channel, then `!pzadminset dots add #channel [server]`.

## Commands

`<server>` is a server's name or ID as PZAdmin knows it (not case sensitive). Slash versions offer the names as you type.

### Anyone: `!pz`

| Command | What it does |
| --- | --- |
| `!pz servers` | Every server the key can see: 🟢 online with players/max, or its state, plus its port slot and port if it has one |
| `!pz status <server>` | One server in detail: state, players, uptime or when it was last online, address, port slot, latency, mods enabled and missing, backups, any scheduled restart |
| `!pz players <server>` | Who is online and for how long |
| `!pz request <workshop link or ID> [note]` | Asks PZAdmin to add a mod to the server this channel is linked to. The note (up to 300 characters) is shown to admins |

`!pz` commands only work in a Discord server, not in DMs.

**How `!pz request` works:** it only works in a channel linked with `!pzadminset link` (threads and forum posts count as their parent channel). The bot sends the Workshop ID, the note and the requester as `username (user ID)` to PZAdmin. PZAdmin checks the item with Steam and adds it to the Requests section of that server's Mods tab, where an admin approves or rejects it. The bot replies with the mod's title and mod IDs, or says why it was refused: already installed, already requested, too many requests waiting, not a Project Zomboid Workshop item, or the key lacks the request scope. Each person can make one request every 30 seconds (a refused request doesn't count).

The bot does **not** tell the requester when a request is approved or rejected. They'll see it in-game or in PZAdmin's own announcements.

### Restarts: `!pzctl`

| Command | What it does |
| --- | --- |
| `!pzctl restart <server> [reason]` | Asks you to confirm (Confirm / Cancel buttons, only you can press them, 30 seconds), then asks PZAdmin for a graceful restart |

PZAdmin warns players, saves and restarts, so the reply can take a few minutes; the bot waits up to 6 minutes. The reason (up to 200 characters) is shown to players in PZAdmin's Discord announcements. One restart per Discord server per minute (cancelling or timing out doesn't count), and the bot won't send a second restart for a server while one it sent is still going. PZAdmin logs the restart against the key's name.

### Bot owner: `!pzadminset`

| Command | What it does |
| --- | --- |
| `!pzadminset url <url>` | Sets PZAdmin's address, e.g. `http://192.168.0.50:27815`. A trailing `/api/v1` is stripped |
| `!pzadminset show` | Shows the URL, whether a key is set, and checks the key against PZAdmin. Never shows the key |
| `!pzadminset link #channel <server>` | Makes `!pz request` in that channel (and its threads) ask for mods on that server. Linking again replaces the server |
| `!pzadminset unlink #channel` | Stops a channel taking mod requests |
| `!pzadminset links` | Lists linked channels |
| `!pzadminset dots add #channel [server]` | Puts the server's status at the start of the channel's name. Without a server, uses the one the channel is linked to |
| `!pzadminset dots remove #channel` | Stops the dots and puts the plain name back |
| `!pzadminset dots list` | Lists channels with dots |

Text, voice and forum channels can be linked. Dots also work on stage channels.

## Permissions

`!pz` is open to everyone. `!pzadminset` is owner only.

`!pzctl` has no built-in check on the prefix command. The slash command is hidden from non-administrators by default (server admins can change that in Server Settings → Integrations), but `!pzctl restart` typed as a message works for anyone. Use Red's permissions to lock it down:

```
!permissions setdefaultglobalrule deny pzctl
!permissions addglobalrule allow pzctl <user or role>
```

You can do the same to `pz request` if you don't want everyone requesting mods.

## Status dots

Every 30 seconds the cog reads `/api/v1/servers` and works out a dot for each channel:

| PZAdmin state | Dot |
| --- | --- |
| `online` | 🟢 |
| `restarting` or `deploying` | 🟠 |
| `offline` or `stopped` | 🔴 |
| `unknown` (not checked yet) | no change |

If PZAdmin can't be reached, nothing changes: an unreachable PZAdmin isn't the same as a server being down.

- A channel's first dot goes up straight away, and so does 🟠 and the change back from 🟠. Anything else has to hold for 90 seconds, so a blip doesn't cost a rename.
- Discord only allows two renames per channel every ten minutes. The cog counts its own renames and waits when it's used them up. If Discord refuses a rename, it tries that channel again after ten minutes.
- Text and forum channels get `🟢-name`, voice and stage channels `🟢 name`. Any dot already at the start of the name (including PZAdmin's own) is replaced, not stacked.
- What's been shown is kept in memory, so after a bot restart the cog reads the current name to pick up where it left off.

Don't use this on a channel where PZAdmin's own "Show 🟢 / 🔴 in the channel's name" option is on. Both bots would rename it and run out of Discord's limit.

## What it calls in PZAdmin

| Call | Used by | Scope |
| --- | --- | --- |
| `GET /api/v1/servers` | `!pz servers`, name lookups, autocomplete, dots | read |
| `GET /api/v1/servers/{id}` | `!pz status` | read |
| `GET /api/v1/servers/{id}/players` | `!pz players` | read |
| `POST /api/v1/servers/{id}/mod-requests` | `!pz request` | request |
| `POST /api/v1/servers/{id}/lifecycle` with `{"action": "restart"}` | `!pzctl restart` | control |
| `GET /api/v1/key` | `!pzadminset show` | read |

The key is sent as `Authorization: Bearer <key>`. The server list is cached for 60 seconds for name lookups and autocomplete; `!pz servers` and the dot check always read it fresh. PZAdmin's full API is documented in [docs/api.md](https://github.com/TheWarBoys2/pzadmin/blob/main/docs/api.md). Use HTTPS for PZAdmin's URL if the bot reaches it over anything but your own network, since the key travels in a header.

## What it stores

PZAdmin's URL (global), and per Discord server which channels are linked to which PZAdmin server and which get dots. Mod requests send the requester's Discord username and ID to PZAdmin, which keeps them with the request.
