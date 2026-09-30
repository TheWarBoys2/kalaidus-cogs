# rolegate

A button panel where members pick their own roles. **Open** roles are handed out straight away. **Approval** roles (marked 🔒) post a request card to a private channel, where a moderator approves or denies it, and the member gets a DM with the result. Members can always remove a role they have by clicking its button again.

It only uses Discord. Buttons keep working after the bot restarts.

## Setup

1. **Give the bot Manage Roles**, and drag the bot's own role above every role it should hand out. Discord won't let a bot give a role at or above its own.
2. **Install**:

   ```
   !cog install kalaidus-cogs rolegate
   !load rolegate
   ```
3. **Pick the approvals channel**, somewhere only moderators can see: `!rolegate channel #role-approvals`. The bot needs View Channel, Send Messages and Embed Links there, and says if any are missing.
4. **Add roles**: `!rolegate add @Role [approval|open] [label]`. Without a mode it's `approval`. The label (up to 80 characters) is the button text and defaults to the role name.
5. **Dress them up** (optional): `!rolegate describe @Role <text>` and `!rolegate emoji @Role <emoji>`.
6. **Post the panel**: `!rolegate post [#channel] [title]`. It defaults to the current channel and the title "Pick your roles".

After changing roles, run `!rolegate refresh` to update the posted panel. Edits don't show on the panel until you do.

## Commands

Prefix only. The bot owner, Red admins (the roles set with `!set roles addadminrole`) and anyone with Manage Roles can use them.

| Command | What it does |
| --- | --- |
| `!rolegate channel #channel` | Sets where approval requests go |
| `!rolegate add @Role [approval\|open] [label]` | Adds a role, or updates its mode and label. Updating without a label keeps the old one. Warns if the role has powerful permissions (Administrator, Manage Server, Manage Roles, Manage Channels, Ban, Kick, Manage Messages, Mention Everyone) |
| `!rolegate describe @Role [text]` | A line shown under the role on the panel (up to 150 characters). Leave the text out to clear it |
| `!rolegate emoji @Role [emoji]` | The emoji on the role's button and panel entry. A normal emoji or one from this server; the bot checks it by reacting with it. Leave it out to clear it |
| `!rolegate remove <@Role or role ID>` | Takes a role off the panel (a role ID works for a deleted role) |
| `!rolegate list` | The approvals channel and every role with its mode, label, emoji and description |
| `!rolegate post [#channel] [title]` | Posts the panel. Posting again makes a new panel; the cog only remembers the latest one for `refresh` |
| `!rolegate refresh` | Updates the posted panel to match the current roles |
| `!rolegate pending` | Lists requests still waiting, with links to their cards |

The panel holds at most 25 roles.

## What happens when someone clicks

- **They already have the role**: it's removed.
- **Open role**: it's added.
- **Approval role**: a card goes to the approvals channel showing who, which role, when their account was made and when they joined, with Approve and Deny buttons. The member is told it's been sent. Clicking again while it's waiting just says it's still waiting.

All of these replies are only visible to the person who clicked.

**Who can approve or deny:** anyone with Manage Roles, or the bot owner. Anyone else pressing the buttons is told they can't.

When a request is decided, the card turns green (approved), red (denied) or grey (couldn't be done), gets an Outcome line naming who decided, and loses its buttons. The member gets a DM: approved, denied, or "couldn't be completed, please ask a moderator". A request can't be granted if the member has left, the role was deleted or taken off the panel, or the bot can no longer manage it; the card says which. If the member has DMs closed, the DM is skipped silently.

If a role is deleted in Discord, it's taken off the panel list automatically (run `!rolegate refresh` to update the panel itself).

## What it stores

Per Discord server: the approvals channel, the roles on the panel (mode, label, description, emoji), where the latest panel is, and pending requests as user ID and role ID. A pending request is removed once it's approved or denied. Red's "delete my data" removes a user's pending requests.
