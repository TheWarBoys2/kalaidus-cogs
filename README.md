# kalaidus-cogs

Custom [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) cogs for the Kalaidus homelab. Each cog has its own README with setup, every command and what it talks to.

Commands below use `!` as the prefix. Swap in your own if it's different.

| Cog | What it does | Talks to | Who can use it |
| --- | --- | --- | --- |
| [pzadmin](pzadmin/README.md) | Project Zomboid server status, player lists, restarts, mod requests and status dots in channel names | [PZAdmin](https://github.com/TheWarBoys2/pzadmin)'s `/api/v1` | Anyone for status and requests; restarts need you to lock them down (see its README); settings are owner only |
| [containerstatus](containerstatus/README.md) | Live status cards and channel-name dots for any Docker container | Arcane's API (read only) | Bot owner only |
| [upcoming](upcoming/README.md) | Two live messages: this week (added today, then each day), and movies and requests coming later | Radarr, and optionally Sonarr and Seerr (read only) | Bot owner sets it up; everyone in the channel reads it |
| [rolegate](rolegate/README.md) | A button panel for self-service roles, with moderator approval for some | Discord only | Admins or anyone with Manage Roles set it up; members click buttons |
| [shelfarrsignup](shelfarrsignup/README.md) | A button that lets people request a Shelfarr account; an admin approves it and the bot creates it and DMs their login | Shelfarr's `/api/v1/users` | Admins or anyone with Manage Server set it up and approve; members click the button |
| [foundry](foundry/README.md) | Runs a Foundry VTT maintenance action (fix file permissions) | [OliveTin](https://www.olivetin.app/) | Anyone who can run the command (see its README) |

## Installing

```
!repo add kalaidus-cogs https://github.com/TheWarBoys2/kalaidus-cogs
!cog install kalaidus-cogs pzadmin
!load pzadmin
```

Swap `pzadmin` for any cog in the table. Each cog's README carries on from there.

## Updating

```
!cog update pzadmin
!reload pzadmin
```

`!cog update` with no name updates every installed cog from this repo. If a slash command looks out of date in Discord afterwards, run `!slash sync`.

## Keys and addresses

Where a cog needs a key, it lives in Red's shared API tokens, not in the cog's own settings. Set them in a DM to the bot so the key isn't posted in a server channel:

| Cog | Command |
| --- | --- |
| pzadmin | `!set api pzadmin api_key <key>` (the URL is set separately with `!pzadminset url`) |
| containerstatus | `!set api arcane url <arcane url> api_key <key>` |
| upcoming | `!set api radarr url <url> api_key <key>`, and the same for `sonarr` and `seerr` |
| foundry | `!set api olivetin api_key <key>` (the URL is set separately with `!foundryset url`) |
| shelfarrsignup | `!set api shelfarr url <shelfarr url> api_key <token>` (a Shelfarr token with only the `users:write` scope) |

None of the cogs ever print a key back. Their `show` commands only say whether one is set.

## How the cogs fit together

- **pzadmin and containerstatus both put status dots in channel names.** They use the same rules (a change has to hold for 90 seconds, except restarts, and no more than two renames per channel every ten minutes, which is Discord's limit). containerstatus checks which channels the pzadmin cog already dots and never renames those. Its `setup` and `autosetup` commands also skip channels linked to a PZAdmin server or holding PZAdmin's own status card.
- **PZAdmin has its own Discord bot**, separate from these cogs. It posts announcements and status cards and can also put a dot in a channel's name (an option on PZAdmin's Discord page). Don't turn that on for a channel where the pzadmin cog also puts a dot: both would rename it and use up Discord's rename limit.
- **Everything is polled.** None of the cogs get pushed updates. Cards, dots and messages only change while the bot is running, on each cog's own timer (30 seconds for pzadmin dots and container cards, 30 minutes for upcoming).

## What's stored

| Cog | Stored in Red's config |
| --- | --- |
| pzadmin | PZAdmin's URL; per Discord server, which channels are linked to which PZAdmin server and which get dots |
| containerstatus | Default Arcane environment, the cards (channel, message, container, title, description, link), dots and ignored channels |
| upcoming | The channel, the two message IDs, days ahead and timezone |
| rolegate | Per Discord server: the approvals channel, the roles on the panel, where the panel is, and pending requests (user ID and role ID, removed once decided) |
| shelfarrsignup | Per Discord server: the approvals channel, where the panel is, waiting requests (user ID, chosen username and display name) and the Shelfarr username created for each user ID. Globally: the login address. Never passwords |
| foundry | OliveTin's URL, the action ID and the timeout |

pzadmin sends a requester's Discord username and ID to PZAdmin with each mod request, and PZAdmin keeps them with the request. shelfarrsignup sends the username and display name someone picked to Shelfarr when an admin approves them. No cog stores anything else about users.

## Testing

There is no CI and no automated test suite in this repo. Changes are checked by hand, and some of the cogs were only tried against stand-in versions of the services they talk to, so a new version of Arcane, Radarr, Sonarr, Seerr, OliveTin or Shelfarr could still surprise them.
