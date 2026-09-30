# foundry

Lets people trigger a Foundry VTT maintenance job from Discord: `!foundry fixperms` fixes file permissions on the gmjj Foundry instance.

The bot never runs anything itself. It asks an **[OliveTin](https://www.olivetin.app/)** instance to run a predefined action by its ID, waits for it to finish, and posts the result. What the action actually does is whatever you've set up in OliveTin.

## Setup

1. **In OliveTin**, have an action that fixes the permissions (the cog uses the ID `fix-gmjj-perms` unless you change it) and an API key the bot can use.
2. **Install and configure**:

   ```
   !cog install kalaidus-cogs foundry
   !load foundry
   !foundryset url http://192.168.0.103:1337
   ```

   Then DM the bot: `!set api olivetin api_key <key>`
3. **Check it** with `!foundryset show`.
4. **Slash command** (optional): `!slash enable foundry`, then `!slash sync`.
5. **Decide who can use it.** See [Permissions](#permissions).

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `!foundry fixperms` | See below | Runs the OliveTin action and replies ✅ or ❌ with its output (the last 1800 characters if it's long) |
| `!foundryset url <url>` | Bot owner | Sets OliveTin's address |
| `!foundryset action <action id>` | Bot owner | Sets which OliveTin action `fixperms` runs |
| `!foundryset show` | Bot owner | Shows the URL, action, timeout and whether a key is set. Never shows the key |

`!foundry` only works in a Discord server, not in DMs.

## How it works

- `fixperms` sends `POST /api/olivetin.api.v1.OliveTinApiService/StartActionAndWait` with the action ID and the key as `Authorization: Bearer <key>`, then waits up to 330 seconds for OliveTin to finish. There's no command to change that timeout.
- ✅ means the action exited with code 0. ❌ covers a non-zero exit, OliveTin blocking the action (rate limit or concurrency), the action timing out on the server, an HTTP error, or OliveTin not answering.
- Only one run at a time across the whole bot; a second request while one is running is told to wait. There's also a 60-second cooldown per Discord server.
- Who asked and whether it worked are written to Red's log.

## Permissions

The slash command is hidden from non-administrators by default (server admins can change that in Server Settings → Integrations). The prefix command `!foundry fixperms` has no built-in check, so anyone who can see the bot can run it. If that's not what you want, lock it down with Red's permissions:

```
!permissions setdefaultglobalrule deny foundry
!permissions addglobalrule allow foundry <user or role>
```

## What it stores

OliveTin's URL, the action ID and the timeout. Nothing about users.
