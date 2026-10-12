import asyncio
import contextlib
import logging
import re
import secrets
from collections import defaultdict
from typing import Dict, Literal, Optional, Tuple, Union

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list, pagify

log = logging.getLogger("red.kalaidus.rolegate")

MAX_ROLES = 25
MAX_LABEL = 80
MAX_DESCRIPTION = 150
MAX_EMBED_TOTAL = 6000
LOCK_EMOJI = "\N{LOCK}"
NO_MENTIONS = discord.AllowedMentions.none()
DANGEROUS_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "ban_members",
    "kick_members",
    "manage_messages",
    "mention_everyone",
)

# Audiobookshelf sign-up: the key and address live in Red's shared API tokens under this name.
ABS_SERVICE = "audiobookshelf"
# Optional: also make a Shelfarr account (for book requests) with the same login.
SHELFARR_SERVICE = "shelfarr"
ABS_TIMEOUT = 15
# Shelfarr only allows lowercase letters, numbers and underscores, so both accounts follow that.
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,32}$")
# No 0/O, 1/l/I, so the password is easy to type from a DM.
PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_LENGTH = 14


class AbsError(Exception):
    """Audiobookshelf couldn't create the account. The message is safe to show the approver."""


class AbsUsernameTaken(AbsError):
    pass


def _pending_key(user_id: int, role_id: int) -> str:
    return f"{user_id}:{role_id}"


def _jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


# ---------- persistent buttons ----------


class RoleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rolegate:req:(?P<role_id>[0-9]+)",
):
    """Panel button. The role ID lives in the custom_id, so it survives restarts."""

    def __init__(
        self, role_id: int, label: Optional[str] = None, gated: bool = True, emoji: Optional[str] = None
    ) -> None:
        # A button holds one emoji. With a custom emoji, the lock moves into the label.
        if emoji and gated and label:
            label = f"{label} {LOCK_EMOJI}"
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji or (LOCK_EMOJI if gated else None),
                style=discord.ButtonStyle.secondary,
                custom_id=f"rolegate:req:{role_id}",
            )
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match, /
    ) -> "RoleButton":
        return cls(int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RoleGate")
        if cog is None:
            await _cog_unavailable(interaction)
            return
        await cog.handle_role_click(interaction, self.role_id)


class DecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rolegate:(?P<action>approve|deny):(?P<user_id>[0-9]+):(?P<role_id>[0-9]+)",
):
    """Approve/Deny button on an approval card."""

    def __init__(self, action: str, user_id: int, role_id: int) -> None:
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Approve" if approve else "Deny",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"rolegate:{action}:{user_id}:{role_id}",
            )
        )
        self.action = action
        self.user_id = user_id
        self.role_id = role_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match, /
    ) -> "DecisionButton":
        return cls(match["action"], int(match["user_id"]), int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RoleGate")
        if cog is None:
            await _cog_unavailable(interaction)
            return
        await cog.handle_decision(interaction, self.action, self.user_id, self.role_id)


class UsernameModal(discord.ui.Modal, title="Audiobookshelf sign-up"):
    """Asks for the username to create when a sign-up role is approved."""

    username = discord.ui.TextInput(
        label="Pick a username",
        placeholder="Lowercase letters, numbers or underscores",
        min_length=3,
        max_length=32,
    )

    def __init__(self, role_id: int) -> None:
        super().__init__(timeout=600)
        self.role_id = role_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("RoleGate")
        if cog is None:
            await _cog_unavailable(interaction)
            return
        await cog.handle_signup_submit(interaction, self.role_id, self.username.value)


async def _cog_unavailable(interaction: discord.Interaction) -> None:
    with contextlib.suppress(discord.HTTPException):
        await interaction.response.send_message(
            "Role requests are unavailable right now. Try again later.", ephemeral=True
        )


# ---------- cog ----------


class RoleGate(commands.Cog):
    """Self-service roles, with admin approval for gated roles."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1262570562, force_registration=True)
        self.config.register_guild(
            approval_channel=None,
            roles={},  # "<role_id>": {"mode", "label", "description", "emoji"}
            panel={},  # {"channel_id": int, "message_id": int, "title": str}
            pending={},  # "<user_id>:<role_id>": {"channel_id", "message_id", "username" (sign-up roles only)}
            signup_link=None,  # address members use to reach Audiobookshelf, sent in the welcome DM
            signup_request_link=None,  # address members use to reach Shelfarr, sent in the welcome DM
            signups={},  # "<user_id>": Audiobookshelf username made for them, so each member gets one account
        )
        self._locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton, DecisionButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RoleButton, DecisionButton)

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        prefix = f"{user_id}:"
        for guild_id, data in (await self.config.all_guilds()).items():
            if str(user_id) in data.get("signups", {}):
                async with self.config.guild_from_id(guild_id).signups() as signups:
                    signups.pop(str(user_id), None)
            if not any(k.startswith(prefix) for k in data.get("pending", {})):
                continue
            async with self.config.guild_from_id(guild_id).pending() as pending:
                for key in [k for k in pending if k.startswith(prefix)]:
                    del pending[key]

    # ---------- helpers ----------

    @staticmethod
    def _role_problem(guild: discord.Guild, role: discord.Role) -> Optional[str]:
        """Return why the bot can't hand out this role, or None if it can."""
        me = guild.me
        if role.is_default():
            return "that's @everyone."
        if role.managed:
            return "it's managed by an integration or bot."
        if not me.guild_permissions.manage_roles:
            return "I don't have the Manage Roles permission."
        if role >= me.top_role:
            return "it's at or above my highest role. Move my role above it."
        return None

    async def _can_decide(self, user: Union[discord.Member, discord.User]) -> bool:
        if await self.bot.is_owner(user):
            return True
        return isinstance(user, discord.Member) and user.guild_permissions.manage_roles

    async def _build_panel(self, channel: discord.abc.GuildChannel, title: str):
        guild = channel.guild
        roles = await self.config.guild(guild).roles()
        view = discord.ui.View(timeout=None)
        embed = discord.Embed(
            title=title,
            description="Click a button below to get a role. Click it again to remove it.",
            colour=await self.bot.get_embed_colour(channel),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        fields = []
        any_gated = False
        for role_id, entry in roles.items():
            role = guild.get_role(int(role_id))
            if role is None:
                continue
            gated = entry["mode"] == "approval"
            any_gated |= gated
            emoji = entry.get("emoji") or None
            view.add_item(RoleButton(role.id, entry["label"], gated, emoji))
            name = f"{emoji + ' ' if emoji else ''}{entry['label']}{' ' + LOCK_EMOJI if gated else ''}"
            fields.append((name, entry.get("description") or ""))

        if not fields:
            embed.description = "No roles are set up yet."
        if any_gated:
            embed.set_footer(text=f"{LOCK_EMOJI} Needs a moderator's approval. You'll get a DM with the result.")

        for name, value in fields:
            embed.add_field(name=name, value=value or "\u200b", inline=False)
        if len(embed) > MAX_EMBED_TOTAL:
            log.warning("Panel for guild %s too long with descriptions; leaving them out", guild.id)
            embed.clear_fields()
            for name, _ in fields:
                embed.add_field(name=name, value="\u200b", inline=False)
        return embed, view

    # ---------- panel clicks ----------

    async def handle_role_click(self, interaction: discord.Interaction, role_id: int) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        if await self.bot.cog_disabled_in_guild(self, guild) or not await self.bot.allowed_by_whitelist_blacklist(
            member
        ):
            await interaction.response.send_message("You can't use this here.", ephemeral=True)
            return

        conf = self.config.guild(guild)
        entry = (await conf.roles()).get(str(role_id))
        role = guild.get_role(role_id)

        # Sign-up roles ask for a username first, unless we already made this member an account.
        # A modal has to be the first response, so no defer yet.
        if (
            entry is not None
            and role is not None
            and entry.get("signup")
            and role not in member.roles
            and self._role_problem(guild, role) is None
            and str(member.id) not in await conf.signups()
        ):
            if entry["mode"] == "approval" and _pending_key(member.id, role.id) in await conf.pending():
                await interaction.response.send_message(
                    f"Your request for **{role.name}** is still waiting for a moderator.", ephemeral=True
                )
                return
            await interaction.response.send_modal(UsernameModal(role.id))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        send = interaction.followup.send

        if entry is None or role is None:
            await send("That role isn't offered any more.", ephemeral=True)
            return

        problem = self._role_problem(guild, role)
        if problem:
            log.warning("Can't manage role %s in guild %s: %s", role.id, guild.id, problem)
            await send("I can't manage that role right now. Please let an admin know.", ephemeral=True)
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason="RoleGate: self-removed")
            except discord.HTTPException:
                log.exception("Failed removing role %s from %s", role.id, member.id)
                await send("Something went wrong removing that role.", ephemeral=True)
                return
            await send(f"Removed **{role.name}**.", ephemeral=True)
            return

        if entry["mode"] == "open":
            try:
                await member.add_roles(role, reason="RoleGate: self-assigned (open role)")
            except discord.HTTPException:
                log.exception("Failed adding role %s to %s", role.id, member.id)
                await send("Something went wrong adding that role.", ephemeral=True)
                return
            await send(f"You now have **{role.name}**.", ephemeral=True)
            return

        await send(await self._create_request(guild, member, role), ephemeral=True)

    async def handle_signup_submit(self, interaction: discord.Interaction, role_id: int, username: str) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            await interaction.response.send_message(
                "Usernames need 3 to 32 letters, numbers or underscores, with no spaces. "
                "Click the button again to try another.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        send = interaction.followup.send
        entry = (await self.config.guild(guild).roles()).get(str(role_id))
        role = guild.get_role(role_id)
        if entry is None or role is None:
            await send("That role isn't offered any more.", ephemeral=True)
            return
        if role in member.roles:
            await send(f"You already have **{role.name}**.", ephemeral=True)
            return
        if self._role_problem(guild, role):
            await send("I can't manage that role right now. Please let an admin know.", ephemeral=True)
            return
        if entry["mode"] == "approval":
            await send(await self._create_request(guild, member, role, username), ephemeral=True)
            return

        # Open sign-up role: make the account and hand out the role straight away.
        async with self._locks[guild.id]:
            if str(member.id) in await self.config.guild(guild).signups():
                await send("You already have an Audiobookshelf account. Click the button again to get the role.", ephemeral=True)
                return
            try:
                password, shelfarr = await self._create_accounts(username, member.display_name)
            except AbsUsernameTaken:
                await send(
                    f"The username **{username}** is already taken. Click the button again to pick another.",
                    ephemeral=True,
                )
                return
            except AbsError as e:
                log.warning("Audiobookshelf sign-up for %s failed: %s", member.id, e)
                await send("I couldn't create your account right now. Please let an admin know.", ephemeral=True)
                return
            await self._record_signup(guild, member.id, username)

        try:
            await member.add_roles(role, reason="RoleGate: Audiobookshelf sign-up")
            note = f"Your account is ready and you now have **{role.name}**."
        except discord.HTTPException:
            log.exception("Failed adding role %s to %s after sign-up", role.id, member.id)
            note = "Your account is ready, but I couldn't give you the role. Please let an admin know."
        welcome = await self._welcome_text(guild, username, password, shelfarr)
        try:
            await member.send(f"{note}\n\n{welcome}")
        except discord.HTTPException:
            # DMs closed: show the login here instead (only they can see it), so it isn't lost.
            await send(f"{note} I couldn't DM you, so here's your login. Save it now.\n\n{welcome}", ephemeral=True)
            return
        await send(f"{note} I've sent your login by DM.", ephemeral=True)

    async def _record_signup(self, guild: discord.Guild, user_id: int, username: str) -> None:
        async with self.config.guild(guild).signups() as signups:
            signups[str(user_id)] = username

    async def _create_request(
        self, guild: discord.Guild, member: discord.Member, role: discord.Role, username: Optional[str] = None
    ) -> str:
        conf = self.config.guild(guild)
        key = _pending_key(member.id, role.id)
        async with self._locks[guild.id]:
            if key in await conf.pending():
                return f"Your request for **{role.name}** is still waiting for a moderator."

            channel_id = await conf.approval_channel()
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                return "Role approvals aren't set up on this server yet. Please ask an admin."

            embed = discord.Embed(
                title="Role request",
                description=f"{member.mention} wants {role.mention}",
                colour=discord.Colour.gold(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
            embed.add_field(name="Role", value=f"{role.mention} (`{role.id}`)", inline=False)
            embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
            embed.add_field(
                name="Joined server",
                value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown",
            )
            if username:
                embed.add_field(name="Audiobookshelf username", value=f"`{username}`", inline=False)
            embed.set_thumbnail(url=member.display_avatar.url)

            view = discord.ui.View(timeout=None)
            view.add_item(DecisionButton("approve", member.id, role.id))
            view.add_item(DecisionButton("deny", member.id, role.id))

            try:
                message = await channel.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.exception("Couldn't post approval card in channel %s (guild %s)", channel.id, guild.id)
                return "I couldn't post your request. Please let an admin know."

            async with conf.pending() as pending:
                pending[key] = {"channel_id": channel.id, "message_id": message.id}
                if username:
                    pending[key]["username"] = username

        log.info("%s (%s) requested role %s in guild %s", member, member.id, role.id, guild.id)
        return f"Request for **{role.name}** sent. You'll get a DM when a moderator decides."

    # ---------- approve / deny ----------

    async def handle_decision(
        self, interaction: discord.Interaction, action: str, user_id: int, role_id: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not await self._can_decide(interaction.user):
            await interaction.response.send_message(
                "Only moderators with Manage Roles can decide role requests.", ephemeral=True
            )
            return

        await interaction.response.defer()
        conf = self.config.guild(guild)
        key = _pending_key(user_id, role_id)
        approver = interaction.user

        async with self._locks[guild.id]:
            info = (await conf.pending()).get(key)
            if info is None:
                await interaction.followup.send("This request has already been handled.", ephemeral=True)
                with contextlib.suppress(discord.HTTPException):
                    await interaction.edit_original_response(view=None)
                return

            member = guild.get_member(user_id)
            if member is None:
                with contextlib.suppress(discord.HTTPException):
                    member = await guild.fetch_member(user_id)
            role = guild.get_role(role_id)
            role_name = role.name if role else f"role {role_id}"

            granted = False
            taken = False
            account = None  # (username, password, shelfarr result) once an Audiobookshelf account exists
            entry = (await conf.roles()).get(str(role_id)) or {}
            if member is None:
                outcome = "Member has left the server."
                colour = discord.Colour.dark_grey()
            elif action == "deny":
                outcome = f"Denied by {approver.mention}"
                colour = discord.Colour.red()
            elif role is None:
                outcome = "Not granted: the role no longer exists."
                colour = discord.Colour.dark_grey()
            elif not entry:
                outcome = "Not granted: that role is no longer offered by RoleGate."
                colour = discord.Colour.dark_grey()
            elif problem := self._role_problem(guild, role):
                outcome = f"Not granted: I can't manage that role, {problem}"
                colour = discord.Colour.dark_grey()
            else:
                username = info.get("username")
                if entry.get("signup") and username:
                    try:
                        account = (username, *await self._create_accounts(username, member.display_name))
                        await self._record_signup(guild, user_id, username)
                    except AbsUsernameTaken:
                        taken = True
                        outcome = f"Not granted: the Audiobookshelf username `{username}` is already taken."
                        colour = discord.Colour.dark_grey()
                    except AbsError as e:
                        # Leave the request open so the approver can fix the problem and click Approve again.
                        log.warning("Audiobookshelf sign-up for %s failed: %s", user_id, e)
                        await interaction.followup.send(
                            f"I couldn't create the Audiobookshelf account: {e}\n"
                            "The request is still open, so you can click Approve again once that's fixed.",
                            ephemeral=True,
                        )
                        return
                if not taken:
                    try:
                        await member.add_roles(role, reason=f"RoleGate: approved by {approver} ({approver.id})")
                    except discord.HTTPException as e:
                        log.exception("Failed granting role %s to %s", role_id, user_id)
                        outcome = f"Not granted: Discord refused the role change ({e.status})."
                        colour = discord.Colour.dark_grey()
                    else:
                        granted = True
                        outcome = f"Approved by {approver.mention}"
                        colour = discord.Colour.green()
                    if account:
                        outcome += f"\nAudiobookshelf account `{account[0]}` created."

            if action == "deny" and member is None:
                outcome = f"Denied by {approver.mention} (member has left the server)"
                colour = discord.Colour.red()

            async with conf.pending() as pending:
                pending.pop(key, None)

        log.info("Request %s in guild %s: %s by %s -> %s", key, guild.id, action, approver.id, outcome)

        message = interaction.message
        if message is not None and message.embeds:
            embed = message.embeds[0].copy()
        else:
            embed = discord.Embed(title="Role request", description=f"<@{user_id}> wants <@&{role_id}>")
        embed.colour = colour
        embed.title = "Role request: " + ("approved" if granted else "denied" if action == "deny" else "closed")
        embed.add_field(name="Outcome", value=outcome, inline=False)
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(embed=embed, view=None, allowed_mentions=NO_MENTIONS)

        if member is None:
            return
        if granted:
            dm = f"Your request for **{role_name}** in **{guild.name}** was approved."
        elif taken:
            dm = (
                f"The Audiobookshelf username **{info['username']}** is already taken, so your request for "
                f"**{role_name}** in **{guild.name}** was closed. Click the button again to pick another."
            )
        elif action == "deny":
            dm = f"Your request for **{role_name}** in **{guild.name}** was denied."
        else:
            dm = (
                f"Your request for **{role_name}** in **{guild.name}** couldn't be completed. "
                "Please ask a moderator."
            )
        if account:
            dm += "\n\n" + await self._welcome_text(guild, *account)
        try:
            await member.send(dm)
        except discord.HTTPException:
            if account:
                # The password only exists in this DM, so hand it to the approver instead of losing it.
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        f"I couldn't DM {member.mention} (their DMs are probably closed). "
                        f"Pass these on yourself:\n\n{await self._welcome_text(guild, *account)}",
                        ephemeral=True,
                        allowed_mentions=NO_MENTIONS,
                    )

    # ---------- Audiobookshelf ----------

    async def _abs_request(self, method: str, path: str, json: Optional[dict] = None) -> aiohttp.ClientResponse:
        tokens = await self.bot.get_shared_api_tokens(ABS_SERVICE)
        base, key = (tokens.get("url") or "").strip().rstrip("/"), (tokens.get("api_key") or "").strip()
        if not base or not key:
            raise AbsError(
                f"Audiobookshelf isn't set up. DM the bot: `!set api {ABS_SERVICE} url <url> api_key <key>`."
            )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    base + path,
                    json=json,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=aiohttp.ClientTimeout(total=ABS_TIMEOUT),
                ) as resp:
                    await resp.read()
                    return resp
        except asyncio.TimeoutError:
            raise AbsError(f"Audiobookshelf didn't answer within {ABS_TIMEOUT} seconds.") from None
        except aiohttp.ClientError as e:
            log.warning("Audiobookshelf unreachable: %s", e)
            raise AbsError("Couldn't reach Audiobookshelf. Check the URL and that it's running.") from None

    @staticmethod
    async def _abs_check_status(resp: aiohttp.ClientResponse) -> None:
        if resp.status in (401, 403):
            raise AbsError("Audiobookshelf rejected the API key. It has to belong to an admin account.")
        if resp.status == 404:
            raise AbsError("Audiobookshelf returned 404. Is the URL the Audiobookshelf address, with no path?")
        if resp.status >= 400:
            text = (await resp.text())[:200]
            raise AbsError(f"Audiobookshelf returned HTTP {resp.status}: {text}")

    async def _abs_create_user(self, username: str) -> str:
        """Create a normal Audiobookshelf user and return its password."""
        password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))
        body = {
            "username": username,
            "password": password,
            "type": "user",
            "isActive": True,  # Audiobookshelf creates the account disabled unless this is set
            "permissions": {
                "download": True,
                "update": False,
                "delete": False,
                "upload": False,
                "createEreader": False,
                "accessAllLibraries": True,
                "accessAllTags": True,
                "accessExplicitContent": True,
            },
        }
        resp = await self._abs_request("POST", "/api/users", body)
        if resp.status == 400 and "taken" in (await resp.text()).lower():
            raise AbsUsernameTaken(username)
        await self._abs_check_status(resp)
        log.info("Created Audiobookshelf user %s", username)
        return password

    async def _shelfarr_request(self, json: dict) -> Optional[aiohttp.ClientResponse]:
        """POST to Shelfarr's user API. Returns None if Shelfarr isn't set up."""
        tokens = await self.bot.get_shared_api_tokens(SHELFARR_SERVICE)
        base, key = (tokens.get("url") or "").strip().rstrip("/"), (tokens.get("api_key") or "").strip()
        if not base or not key:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    base + "/api/v1/users",
                    json=json,
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=ABS_TIMEOUT),
                ) as resp:
                    await resp.read()
                    return resp
        except asyncio.TimeoutError:
            raise AbsError(f"Shelfarr didn't answer within {ABS_TIMEOUT} seconds.") from None
        except aiohttp.ClientError as e:
            log.warning("Shelfarr unreachable: %s", e)
            raise AbsError("Couldn't reach Shelfarr. Check the URL and that it's running.") from None

    async def _create_accounts(self, username: str, name: str) -> Tuple[str, Optional[bool]]:
        """Make the Audiobookshelf account, then the Shelfarr one if Shelfarr is set up.

        Returns the password and whether the Shelfarr account was made (None if Shelfarr isn't set up).
        A Shelfarr failure doesn't undo the Audiobookshelf account; it's logged and mentioned in the DM.
        """
        password = await self._abs_create_user(username)
        try:
            resp = await self._shelfarr_request({"name": name[:100] or username, "username": username, "password": password})
        except AbsError as e:
            log.warning("Shelfarr account for %s not made: %s", username, e)
            return password, False
        if resp is None:
            return password, None
        if resp.status in (200, 201):
            log.info("Created Shelfarr user %s", username)
            return password, True
        log.warning("Shelfarr account for %s not made: HTTP %s %s", username, resp.status, (await resp.text())[:200])
        return password, False

    async def _welcome_text(
        self, guild: discord.Guild, username: str, password: str, shelfarr: Optional[bool] = None
    ) -> str:
        conf = self.config.guild(guild)
        link = await conf.signup_link()
        lines = ["**Your Audiobookshelf login**"]
        if link:
            lines.append(f"Address: <{link}>")
        lines += [f"Username: `{username}`", f"Password: `{password}`", ""]
        if shelfarr:
            request_link = await conf.signup_request_link()
            where = f" at <{request_link}>" if request_link else ""
            lines += ["The same username and password work for requesting books on Shelfarr" + where + ".", ""]
        elif shelfarr is False:
            lines += ["I couldn't make your Shelfarr login for requesting books. Please ask an admin.", ""]
        lines.append(
            "Please change your password after you first log in: open your account from the "
            "person icon at the top right and use Change Password."
        )
        if shelfarr:
            lines.append("Changing it in one doesn't change the other.")
        return "\n".join(lines)

    # ---------- housekeeping ----------

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if str(role.id) not in await self.config.guild(role.guild).roles():
            return
        async with self.config.guild(role.guild).roles() as roles:
            roles.pop(str(role.id), None)

    # ---------- admin commands ----------

    @commands.group(name="rolegate")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_roles=True)
    async def rolegate(self, ctx: commands.Context) -> None:
        """Self-service role panel with admin approval."""

    @rolegate.command(name="channel")
    async def rolegate_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the private channel where approval requests are posted."""
        await self.config.guild(ctx.guild).approval_channel.set(channel.id)
        perms = channel.permissions_for(ctx.guild.me)
        missing = [
            name
            for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            )
            if not ok
        ]
        msg = f"Approval requests will go to {channel.mention}."
        if missing:
            msg += f"\n\N{WARNING SIGN} I'm missing {humanize_list(missing)} there, so requests will fail until that's fixed."
        await ctx.send(msg)

    @rolegate.command(name="add")
    async def rolegate_add(
        self,
        ctx: commands.Context,
        role: discord.Role,
        mode: Optional[Literal["approval", "open"]] = None,
        *,
        label: Optional[str] = None,
    ) -> None:
        """Add or update a role on the panel.

        Mode is `approval` (default) or `open`. Label defaults to the role name.
        Updating a role without giving a label keeps its current label.
        """
        problem = self._role_problem(ctx.guild, role)
        if problem:
            await ctx.send(f"I can't hand out {role.name}: {problem}")
            return

        async with self.config.guild(ctx.guild).roles() as roles:
            existing = roles.get(str(role.id))
            if existing is None and len(roles) >= MAX_ROLES:
                await ctx.send(f"The panel can hold at most {MAX_ROLES} roles. Remove one first.")
                return
            if label is None:
                label = existing["label"] if existing else role.name
            mode = mode or "approval"
            roles[str(role.id)] = {**(existing or {}), "mode": mode, "label": label[:MAX_LABEL]}

        msg = f"{'Updated' if existing else 'Added'} **{role.name}** as `{mode}` with label **{label[:MAX_LABEL]}**."
        risky = [p for p in DANGEROUS_PERMS if getattr(role.permissions, p)]
        if risky:
            msg += (
                f"\n\N{WARNING SIGN} This role has elevated permissions ({humanize_list(risky)}). "
                "Anyone who gets it can use them."
            )
        msg += f"\nRun `{ctx.clean_prefix}rolegate refresh` to update the posted panel."
        await ctx.send(msg, allowed_mentions=NO_MENTIONS)

    @rolegate.command(name="describe")
    async def rolegate_describe(
        self, ctx: commands.Context, role: discord.Role, *, description: Optional[str] = None
    ) -> None:
        """Set a short description shown under a role on the panel.

        Leave the description out to clear it.
        """
        if description:
            description = " ".join(description.split())
        if description and len(description) > MAX_DESCRIPTION:
            await ctx.send(f"Keep it to {MAX_DESCRIPTION} characters (that one is {len(description)}).")
            return
        async with self.config.guild(ctx.guild).roles() as roles:
            entry = roles.get(str(role.id))
            if entry is None:
                await ctx.send(f"That role isn't on the panel. Add it first with `{ctx.clean_prefix}rolegate add`.")
                return
            entry["description"] = description or ""
        verb = "Set" if description else "Cleared"
        await ctx.send(
            f"{verb} the description for **{entry['label']}**. "
            f"Run `{ctx.clean_prefix}rolegate refresh` to update the posted panel."
        )

    @rolegate.command(name="emoji")
    async def rolegate_emoji(self, ctx: commands.Context, role: discord.Role, emoji: Optional[str] = None) -> None:
        """Set the emoji shown on a role's button and panel entry.

        Use a normal emoji or one from this server. Leave it out to clear it.
        """
        async with self.config.guild(ctx.guild).roles() as roles:
            entry = roles.get(str(role.id))
            if entry is None:
                await ctx.send(f"That role isn't on the panel. Add it first with `{ctx.clean_prefix}rolegate add`.")
                return
            if emoji:
                # Discord is the only reliable judge of what counts as an emoji: try reacting with it.
                try:
                    await ctx.message.add_reaction(emoji)
                except discord.HTTPException:
                    await ctx.send("That doesn't look like an emoji I can use. Try a normal emoji or one from this server.")
                    return
                with contextlib.suppress(discord.HTTPException):
                    await ctx.message.remove_reaction(emoji, ctx.guild.me)
            entry["emoji"] = emoji or ""
        verb = f"Set the emoji for **{entry['label']}** to {emoji}" if emoji else f"Cleared the emoji for **{entry['label']}**"
        await ctx.send(f"{verb}. Run `{ctx.clean_prefix}rolegate refresh` to update the posted panel.")

    @rolegate.command(name="remove")
    async def rolegate_remove(self, ctx: commands.Context, role: Union[discord.Role, int]) -> None:
        """Remove a role from the panel (accepts a role or a role ID)."""
        role_id = role.id if isinstance(role, discord.Role) else role
        async with self.config.guild(ctx.guild).roles() as roles:
            removed = roles.pop(str(role_id), None)
        if removed is None:
            await ctx.send("That role isn't on the panel.")
            return
        await ctx.send(
            f"Removed **{removed['label']}**. Run `{ctx.clean_prefix}rolegate refresh` to update the posted panel."
        )

    @rolegate.command(name="list")
    async def rolegate_list(self, ctx: commands.Context) -> None:
        """Show configured roles and the approvals channel."""
        data = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(data["approval_channel"]) if data["approval_channel"] else None
        lines = [f"Approvals channel: {channel.mention if channel else 'not set'}"]
        if any(e.get("signup") for e in data["roles"].values()):
            lines.append(f"Sign-up address in the welcome DM: {data['signup_link'] or 'not set'}")
            lines.append(f"Shelfarr address in the welcome DM: {data['signup_request_link'] or 'not set'}")
        lines.append("")
        for role_id, entry in data["roles"].items():
            role = ctx.guild.get_role(int(role_id))
            where = role.mention if role else f"deleted role `{role_id}`"
            lock = LOCK_EMOJI + " " if entry["mode"] == "approval" else ""
            emoji = entry["emoji"] + " " if entry.get("emoji") else ""
            signup = " \N{BOOKS} Audiobookshelf sign-up" if entry.get("signup") else ""
            lines.append(f"{lock}{emoji}**{entry['label']}**: {where} (`{entry['mode']}`){signup}")
            if entry.get("description"):
                lines.append(f"> {entry['description']}")
        if not data["roles"]:
            lines.append("No roles configured.")
        for page in pagify("\n".join(lines)):
            await ctx.send(page, allowed_mentions=NO_MENTIONS)

    @rolegate.command(name="post")
    async def rolegate_post(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        *,
        title: str = "Pick your roles",
    ) -> None:
        """Post the role panel (defaults to this channel)."""
        channel = channel or ctx.channel
        if not await self.config.guild(ctx.guild).roles():
            await ctx.send(f"Add some roles first with `{ctx.clean_prefix}rolegate add`.")
            return
        embed, view = await self._build_panel(channel, title)
        try:
            message = await channel.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            await ctx.send(f"I couldn't post in {channel.mention}: {e.text or e.status}")
            return
        await self.config.guild(ctx.guild).panel.set(
            {"channel_id": channel.id, "message_id": message.id, "title": title}
        )
        if channel != ctx.channel:
            await ctx.send(f"Panel posted: {message.jump_url}")

    @rolegate.command(name="refresh")
    async def rolegate_refresh(self, ctx: commands.Context) -> None:
        """Update the posted panel to match the current role list."""
        panel = await self.config.guild(ctx.guild).panel()
        channel = ctx.guild.get_channel(panel.get("channel_id", 0))
        if channel is None:
            await ctx.send(f"No panel found. Post one with `{ctx.clean_prefix}rolegate post`.")
            return
        embed, view = await self._build_panel(channel, panel.get("title", "Pick your roles"))
        try:
            message = await channel.fetch_message(panel["message_id"])
            await message.edit(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
        except discord.NotFound:
            await self.config.guild(ctx.guild).panel.clear()
            await ctx.send(f"The panel message is gone. Post a new one with `{ctx.clean_prefix}rolegate post`.")
            return
        except discord.HTTPException as e:
            await ctx.send(f"I couldn't update the panel: {e.text or e.status}")
            return
        await ctx.send(f"Panel updated: {message.jump_url}")

    @rolegate.group(name="signup")
    async def rolegate_signup(self, ctx: commands.Context) -> None:
        """Create an Audiobookshelf account when a role is approved.

        The member picks a username when they click the role, and gets their login by DM once it's approved.
        """

    @rolegate_signup.command(name="enable")
    async def rolegate_signup_enable(self, ctx: commands.Context, role: discord.Role) -> None:
        """Ask for a username on this role and create an Audiobookshelf account.

        On an open role the account is made straight away; on an approval role, when it's approved.
        """
        async with self.config.guild(ctx.guild).roles() as roles:
            entry = roles.get(str(role.id))
            if entry is None:
                await ctx.send(f"That role isn't on the panel. Add it first with `{ctx.clean_prefix}rolegate add`.")
                return
            entry["signup"] = True
        when = "creates the account straight away" if entry["mode"] == "open" else "creates the account when you approve it"
        msg = f"Clicking **{entry['label']}** now asks for an Audiobookshelf username and {when}."
        tokens = await self.bot.get_shared_api_tokens(ABS_SERVICE)
        if not tokens.get("url") or not tokens.get("api_key"):
            msg += f"\n\N{WARNING SIGN} Audiobookshelf isn't set up yet. DM me: `{ctx.clean_prefix}set api {ABS_SERVICE} url <url> api_key <key>`."
        if not await self.config.guild(ctx.guild).signup_link():
            msg += f"\nSet the address members use with `{ctx.clean_prefix}rolegate signup link <url>` so it goes in their DM."
        await ctx.send(msg)

    @rolegate_signup.command(name="disable")
    async def rolegate_signup_disable(self, ctx: commands.Context, role: discord.Role) -> None:
        """Stop asking for a username on this role. Existing accounts are left alone."""
        async with self.config.guild(ctx.guild).roles() as roles:
            entry = roles.get(str(role.id))
            if entry is None or not entry.get("signup"):
                await ctx.send("That role doesn't have sign-up turned on.")
                return
            entry.pop("signup", None)
        await ctx.send(f"**{entry['label']}** is a normal approval role again.")

    @rolegate_signup.command(name="forget")
    async def rolegate_signup_forget(self, ctx: commands.Context, member: Union[discord.Member, int]) -> None:
        """Let a member sign up again, for example after you delete their Audiobookshelf account."""
        user_id = member.id if isinstance(member, discord.Member) else member
        async with self.config.guild(ctx.guild).signups() as signups:
            username = signups.pop(str(user_id), None)
        if username is None:
            await ctx.send("I haven't made an Audiobookshelf account for them.")
            return
        await ctx.send(
            f"Forgot that <@{user_id}> has the account `{username}`. They'll be asked for a username next time. "
            "This doesn't touch the account itself.",
            allowed_mentions=NO_MENTIONS,
        )

    @rolegate_signup.command(name="link")
    async def rolegate_signup_link(self, ctx: commands.Context, url: Optional[str] = None) -> None:
        """Set the Audiobookshelf address sent in the welcome DM. Leave it out to clear it."""
        if url and not url.lower().startswith(("http://", "https://")):
            await ctx.send("Give the full address, starting with http:// or https://.")
            return
        await self.config.guild(ctx.guild).signup_link.set(url or None)
        await ctx.send(f"Welcome DMs will link to <{url}>." if url else "Welcome DMs won't include an address.")

    @rolegate_signup.command(name="requestlink")
    async def rolegate_signup_requestlink(self, ctx: commands.Context, url: Optional[str] = None) -> None:
        """Set the Shelfarr address sent in the welcome DM. Leave it out to clear it."""
        if url and not url.lower().startswith(("http://", "https://")):
            await ctx.send("Give the full address, starting with http:// or https://.")
            return
        await self.config.guild(ctx.guild).signup_request_link.set(url or None)
        await ctx.send(f"Welcome DMs will link to <{url}> for requests." if url else "Welcome DMs won't include a Shelfarr address.")

    @rolegate_signup.command(name="check")
    async def rolegate_signup_check(self, ctx: commands.Context) -> None:
        """Check the bot can reach Audiobookshelf (and Shelfarr, if set up) with admin keys."""
        lines = []
        async with ctx.typing():
            try:
                await self._abs_check_status(await self._abs_request("GET", "/api/users"))
                lines.append("\N{WHITE HEAVY CHECK MARK} Audiobookshelf is reachable and the key can create accounts.")
            except AbsError as e:
                lines.append(f"\N{CROSS MARK} {e}")
            try:
                # An empty user is rejected with 422 only after the key and its scope pass, so nothing is created.
                resp = await self._shelfarr_request({})
            except AbsError as e:
                lines.append(f"\N{CROSS MARK} {e}")
            else:
                if resp is None:
                    lines.append(
                        "Shelfarr isn't set up, so only Audiobookshelf accounts are made. To add it, DM me: "
                        f"`{ctx.clean_prefix}set api {SHELFARR_SERVICE} url <url> api_key <key>`."
                    )
                elif resp.status == 422:
                    lines.append("\N{WHITE HEAVY CHECK MARK} Shelfarr is reachable and the key can create accounts.")
                elif resp.status == 401:
                    lines.append("\N{CROSS MARK} Shelfarr rejected the API key.")
                elif resp.status == 403:
                    lines.append(
                        "\N{CROSS MARK} Shelfarr's key can't create users. Use the API token from Shelfarr's "
                        "Admin, Settings, Security instead."
                    )
                else:
                    lines.append(f"\N{CROSS MARK} Shelfarr returned HTTP {resp.status}. Is the URL Shelfarr's, with no path?")
        await ctx.send("\n".join(lines))

    @rolegate.command(name="pending")
    async def rolegate_pending(self, ctx: commands.Context) -> None:
        """List pending role requests."""
        pending = await self.config.guild(ctx.guild).pending()
        if not pending:
            await ctx.send("No pending requests.")
            return
        lines = []
        for key, info in pending.items():
            user_id, role_id = key.split(":")
            role = ctx.guild.get_role(int(role_id))
            role_text = role.mention if role else f"deleted role `{role_id}`"
            url = _jump_url(ctx.guild.id, info["channel_id"], info["message_id"])
            lines.append(f"<@{user_id}> \N{RIGHTWARDS ARROW} {role_text}: [card]({url})")
        for page in pagify("\n".join(lines)):
            await ctx.send(page, allowed_mentions=NO_MENTIONS)
