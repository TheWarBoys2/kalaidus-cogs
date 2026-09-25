import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import Dict, Literal, Optional, Union

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list, pagify

log = logging.getLogger("red.kalaidus.rolegate")

MAX_ROLES = 25
MAX_LABEL = 80
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

    def __init__(self, role_id: int, label: Optional[str] = None, gated: bool = True) -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=LOCK_EMOJI if gated else None,
                style=discord.ButtonStyle.primary if gated else discord.ButtonStyle.secondary,
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
            roles={},  # "<role_id>": {"mode": "approval"|"open", "label": str}
            panel={},  # {"channel_id": int, "message_id": int, "title": str}
            pending={},  # "<user_id>:<role_id>": {"channel_id": int, "message_id": int}
        )
        self._locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton, DecisionButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RoleButton, DecisionButton)

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        prefix = f"{user_id}:"
        for guild_id, data in (await self.config.all_guilds()).items():
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

    async def _build_panel(self, guild: discord.Guild, title: str):
        roles = await self.config.guild(guild).roles()
        view = discord.ui.View(timeout=None)
        lines = []
        for role_id, entry in roles.items():
            role = guild.get_role(int(role_id))
            if role is None:
                continue
            gated = entry["mode"] == "approval"
            view.add_item(RoleButton(role.id, entry["label"], gated))
            lines.append(f"{LOCK_EMOJI + ' ' if gated else ''}**{entry['label']}**: {role.mention}")

        description = "\n".join(lines) or "No roles are set up yet."
        if lines:
            description += (
                f"\n\nClick a button to get that role. Roles marked {LOCK_EMOJI} need a "
                "moderator to approve your request first; you'll get a DM with the result. "
                "Click a role you already have to remove it."
            )
        embed = discord.Embed(title=title, description=description, colour=discord.Colour.blurple())
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        send = interaction.followup.send

        entry = (await self.config.guild(guild).roles()).get(str(role_id))
        role = guild.get_role(role_id)
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

    async def _create_request(self, guild: discord.Guild, member: discord.Member, role: discord.Role) -> str:
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
            if key not in await conf.pending():
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
            if member is None:
                outcome = "Member has left the server."
                colour = discord.Colour.dark_grey()
            elif action == "deny":
                outcome = f"Denied by {approver.mention}"
                colour = discord.Colour.red()
            elif role is None:
                outcome = "Not granted: the role no longer exists."
                colour = discord.Colour.dark_grey()
            elif str(role_id) not in await conf.roles():
                outcome = "Not granted: that role is no longer offered by RoleGate."
                colour = discord.Colour.dark_grey()
            elif problem := self._role_problem(guild, role):
                outcome = f"Not granted: I can't manage that role, {problem}"
                colour = discord.Colour.dark_grey()
            else:
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
        elif action == "deny":
            dm = f"Your request for **{role_name}** in **{guild.name}** was denied."
        else:
            dm = (
                f"Your request for **{role_name}** in **{guild.name}** couldn't be completed. "
                "Please ask a moderator."
            )
        with contextlib.suppress(discord.HTTPException):
            await member.send(dm)

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
            roles[str(role.id)] = {"mode": mode, "label": label[:MAX_LABEL]}

        msg = f"{'Updated' if existing else 'Added'} **{role.name}** as `{mode}` with label **{label[:MAX_LABEL]}**."
        risky = [p for p in DANGEROUS_PERMS if getattr(role.permissions, p)]
        if risky:
            msg += (
                f"\n\N{WARNING SIGN} This role has elevated permissions ({humanize_list(risky)}). "
                "Anyone who gets it can use them."
            )
        msg += f"\nRun `{ctx.clean_prefix}rolegate refresh` to update the posted panel."
        await ctx.send(msg, allowed_mentions=NO_MENTIONS)

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
        lines = [f"Approvals channel: {channel.mention if channel else 'not set'}", ""]
        for role_id, entry in data["roles"].items():
            role = ctx.guild.get_role(int(role_id))
            where = role.mention if role else f"deleted role `{role_id}`"
            lock = LOCK_EMOJI + " " if entry["mode"] == "approval" else ""
            lines.append(f"{lock}**{entry['label']}**: {where} (`{entry['mode']}`)")
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
        embed, view = await self._build_panel(ctx.guild, title)
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
        embed, view = await self._build_panel(ctx.guild, panel.get("title", "Pick your roles"))
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
