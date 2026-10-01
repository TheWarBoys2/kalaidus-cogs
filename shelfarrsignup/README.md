# shelfarrsignup

Lets people ask for a [Shelfarr](https://github.com/Pedro-Revez-Silva/shelfarr) account from Discord. They click a button on a panel and pick a username. A card with **Approve and create** and **Deny** buttons goes to a private channel. When an admin approves it, the bot creates the account through Shelfarr's API with a random password and DMs the member their login, telling them to change the password in Shelfarr.

Shelfarr has no open sign-up (after the first account, only an admin can create users), so this saves you creating each account by hand.

Buttons keep working after the bot restarts.

## Setup

1. **Make a Shelfarr API token.** Log in to Shelfarr as an admin, go to your **Profile → API tokens**, and create a token with only the `users:write` scope. Copy it, because Shelfarr only shows it once.
2. **Install**:

   ```
   !cog install kalaidus-cogs shelfarrsignup
   !load shelfarrsignup
   ```
3. **Add the token.** DM the bot, so the token isn't posted in a server channel:

   ```
   !set api shelfarr url <shelfarr url> api_key <token>
   ```

   The URL is where the **bot** reaches Shelfarr. A LAN address like `http://192.168.0.215:5056` is the safest choice, because containers on the homelab can't always resolve `kalaidus.co.uk` names.
4. **Set the login address** people should use, which is shown on the panel and in the DM: `!shelfarrsignup loginurl https://shelfarr.kalaidus.co.uk`
5. **Pick the approvals channel**, somewhere only admins can see: `!shelfarrsignup channel #shelfarr-approvals`. The bot needs View Channel, Send Messages and Embed Links there, and says if any are missing.
6. **Check it works**: `!shelfarrsignup show` should say `Shelfarr: OK`. The check doesn't create anything.
7. **Post the panel**: `!shelfarrsignup post [#channel] [title]`. It defaults to the current channel and the title "Get a Shelfarr account".

## Commands

Prefix only. The bot owner, Red admins (the roles set with `!set roles addadminrole`) and anyone with Manage Server can use them. `loginurl` is owner only.

| Command | What it does |
| --- | --- |
| `!shelfarrsignup channel #channel` | Sets where requests go |
| `!shelfarrsignup loginurl <address>` | The address people log in at, shown on the panel and in the DM |
| `!shelfarrsignup post [#channel] [title]` | Posts the panel. Posting again makes a new panel; the cog only remembers the latest one for `refresh` |
| `!shelfarrsignup refresh` | Updates the posted panel, for example after changing the login address |
| `!shelfarrsignup pending` | Lists requests still waiting, with links to their cards |
| `!shelfarrsignup forget <@member or user ID>` | Lets someone request again by clearing their waiting request and the account the cog remembers for them. It doesn't delete anything in Shelfarr |
| `!shelfarrsignup show` | The settings, whether the token is set (never the token itself), and a live check that Shelfarr accepts it |

## What happens when someone clicks

- **They pick a username** (and optionally a display name, which defaults to their Discord name). Usernames follow Shelfarr's rules: 3 to 32 characters, lowercase letters, numbers and underscores only. Capitals are turned into lowercase.
- **A card goes to the approvals channel** showing who asked, the username, when their Discord account was made and when they joined.
- **They can't ask twice.** Clicking again while a request is waiting, or after an account was made for them, just tells them so.

All of these replies are only visible to the person who clicked.

**Who can approve or deny:** anyone with Manage Server, Red admins, or the bot owner.

When an admin approves:

- **The account is created** as a normal user, never an admin, with a random 16-character password. The card turns green and loses its buttons, and the member gets a DM with the address, username and password.
- **If their DMs are closed**, the login is shown only to the admin who clicked, so they can pass it on. The card says so.
- **If Shelfarr refuses the username** (usually because it's taken), the card turns grey with Shelfarr's reason, and the member is DMed to click the button again and pick another.
- **If the bot can't reach Shelfarr** or the token is wrong, nothing changes: the admin is told why and the request stays open to approve again once it's fixed.

Denying turns the card red and DMs the member that it was denied.

## What it stores

Per Discord server: the approvals channel, where the latest panel is, waiting requests (Discord user ID, chosen username and display name, and where the card is), and the Shelfarr username created for each Discord user ID, so the same person can't request twice. Globally: the login address. **Passwords are never stored or logged**; the bot only holds one long enough to create the account and send the DM. Red's "delete my data" removes a user's request and remembered account.
