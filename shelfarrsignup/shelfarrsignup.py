import asyncio
import contextlib
import logging
import re
import secrets
import string
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Union

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify

log = logging.getLogger("red.kalaidus.shelfarrsignup")

SERVICE = "shelfarr"
USERS_PATH = "/api/v1/users"
# Shelfarr's own rules (app/models/user.rb): lowercase letters, numbers and underscores,
# and a password of 12+ characters with a lowercase letter, an uppercase letter and a number.
USERNAME_RE = re.compile(r"[a-z0-9_]+")
USERNAME_MIN = 3
USERNAME_MAX = 32
NAME_MAX = 50
PASSWORD_LENGTH = 16
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
NO_MENTIONS = discord.AllowedMentions.none()
DEFAULT_TITLE = "Get a Shelfarr account"


class ShelfarrError(Exception):
    """Shelfarr refused the account. The message is safe to show to admins."""


class ShelfarrSetupError(ShelfarrError):
    """The bot can't talk to Shelfarr (not set up, bad token, unreachable). Not the member's fault."""


def normalise_username(raw: str) -> str:
    return raw.strip().lower()


def username_problem(username: str) -> Optional[str]:
    if not USERNAME_MIN <= len(username) <= USERNAME_MAX:
        return f"Usernames must be {USERNAME_MIN} to {USERNAME_MAX} characters."
    if not USERNAME_RE.fullmatch(username):
        return "Usernames can only use lowercase letters, numbers and underscores."
    return None


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A random password that always meets Shelfarr's rules."""
    # Leave out characters that are easy to misread when copying from a DM.
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "Il1O0o")
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password


# ---------- persistent buttons ----------


class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shelfarrsignup:open"):
    """The panel button. Opens the username form."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Request an account",
                emoji="\N{BOOKS}",
                style=discord.ButtonStyle.primary,
                custom_id="shelfarrsignup:open",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match, /
    ) -> "SignupButton":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("ShelfarrSignup")
        if cog is None:
            await _cog_unavailable(interaction)
            return
        await cog.handle_open(interaction)


class DecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"shelfarrsignup:(?P<action>approve|deny):(?P<user_id>[0-9]+)",
):
    """Approve/Deny button on a request card."""

    def __init__(self, action: str, user_id: int) -> None:
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Approve and create" if approve else "Deny",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"shelfarrsignup:{action}:{user_id}",
            )
        )
        self.action = action
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match, /
    ) -> "DecisionButton":
        return cls(match["action"], int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("ShelfarrSignup")
        if cog is None:
            await _cog_unavailable(interaction)
            return
        await cog.handle_decision(interaction, self.action, self.user_id)


class SignupModal(discord.ui.Modal, title="Request a Shelfarr account"):
    username = discord.ui.TextInput(
        label="Username",
        placeholder="lowercase letters, numbers and _ only",
        min_length=USERNAME_MIN,
        max_length=USERNAME_MAX,
    )
    name = discord.ui.TextInput(
        label="Display name (optional)",
        placeholder="Leave blank to use your Discord name",
        required=False,
        max_length=NAME_MAX,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("ShelfarrSignup")
        if cog is None:
            await _cog_unavailable(interaction)
            return
        await cog.handle_submit(interaction, self.username.value, self.name.value)


async def _cog_unavailable(interaction: discord.Interaction) -> None:
    with contextlib.suppress(discord.HTTPException):
        await interaction.response.send_message(
            "Account requests are unavailable right now. Try again later.", ephemeral=True
        )


# ---------- cog ----------


class ShelfarrSignup(commands.Cog):
    """Request a Shelfarr account from Discord, with admin approval."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1262570564, force_registration=True)
        self.config.register_global(login_url=None)
        self.config.register_guild(
            approval_channel=None,
            panel={},  # {"channel_id": int, "message_id": int, "title": str}
            pending={},  # "<user_id>": {"username", "name", "channel_id", "message_id"}
            accounts={},  # "<user_id>": "<shelfarr username>", once created
        )
        self._locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        self.bot.add_dynamic_items(SignupButton, DecisionButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(SignupButton, DecisionButton)
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        key = str(user_id)
        for guild_id, data in (await self.config.all_guilds()).items():
            if key not in data.get("pending", {}) and key not in data.get("accounts", {}):
                continue
            conf = self.config.guild_from_id(guild_id)
            async with conf.pending() as pending:
                pending.pop(key, None)
            async with conf.accounts() as accounts:
                accounts.pop(key, None)

    # ---------- Shelfarr API ----------

    async def _api(self) -> Tuple[str, str]:
        tokens = await self.bot.get_shared_api_tokens(SERVICE)
        base = (tokens.get("url") or "").strip().rstrip("/")
        key = (tokens.get("api_key") or "").strip()
        if not base or not key:
            raise ShelfarrSetupError(
                f"Shelfarr isn't set up. DM the bot: `!set api {SERVICE} url <shelfarr url> api_key <token>`."
            )
        return base, key

    async def _create_user(self, username: str, name: str, password: str) -> None:
        base, key = await self._api()
        try:
            async with self._session.post(
                base + USERS_PATH,
                json={"username": username, "name": name, "password": password},
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            ) as resp:
                if resp.status == 201:
                    return
                if resp.status == 401:
                    raise ShelfarrSetupError("Shelfarr rejected the API token. Check it hasn't been revoked.")
                if resp.status == 403:
                    raise ShelfarrSetupError("The API token is missing the `users:write` scope.")
                if resp.status >= 500:
                    raise ShelfarrSetupError(f"Shelfarr had an error (HTTP {resp.status}). Try again shortly.")
                errors: List[str] = []
                with contextlib.suppress(aiohttp.ContentTypeError, ValueError):
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        errors = [str(e) for e in data.get("errors") or []]
                raise ShelfarrError("; ".join(errors) or f"Shelfarr returned HTTP {resp.status}.")
        except asyncio.TimeoutError:
            raise ShelfarrSetupError("Timed out waiting for Shelfarr.") from None
        except aiohttp.ClientError as e:
            raise ShelfarrSetupError(f"Couldn't reach Shelfarr: {e}") from None

    async def _check_api(self) -> str:
        """Confirm the URL and token work without creating anything."""
        base, key = await self._api()
        try:
            # An empty body fails validation (422) only after auth and scope checks pass.
            async with self._session.post(
                base + USERS_PATH,
                json={},
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            ) as resp:
                if resp.status in (400, 422):
                    return "OK"
                if resp.status == 401:
                    return "FAILED - token rejected"
                if resp.status == 403:
                    return "FAILED - token is missing the users:write scope"
                return f"FAILED - HTTP {resp.status}"
        except asyncio.TimeoutError:
            return "FAILED - timed out"
        except aiohttp.ClientError as e:
            return f"FAILED - {e}"

    # ---------- helpers ----------

    async def _can_decide(self, user: Union[discord.Member, discord.User]) -> bool:
        if await self.bot.is_owner(user):
            return True
        if not isinstance(user, discord.Member):
            return False
        return user.guild_permissions.manage_guild or await self.bot.is_admin(user)

    async def _blocked(self, interaction: discord.Interaction) -> Optional[str]:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return "This only works in a server."
        if await self.bot.cog_disabled_in_guild(self, guild) or not await self.bot.allowed_by_whitelist_blacklist(
            interaction.user
        ):
            return "You can't use this here."
        data = await self.config.guild(guild).all()
        key = str(interaction.user.id)
        if key in data["accounts"]:
            return f"You already have a Shelfarr account (**{data['accounts'][key]}**). Ask an admin if you've lost access."
        if key in data["pending"]:
            return "Your request is still waiting for an admin."
        if not data["approval_channel"] or guild.get_channel(data["approval_channel"]) is None:
            return "Account requests aren't set up on this server yet. Please ask an admin."
        return None

    async def _build_panel(self, channel: discord.abc.GuildChannel, title: str):
        login_url = await self.config.login_url()
        embed = discord.Embed(
            title=title,
            description=(
                "Shelfarr is where you request ebooks and audiobooks.\n\n"
                "Click the button and pick a username. Once an admin approves it, "
                "I'll DM you your login details."
            ),
            colour=await self.bot.get_embed_colour(channel),
        )
        if login_url:
            embed.add_field(name="Where", value=login_url, inline=False)
        embed.set_footer(text="Make sure you allow DMs from this server, or I can't send your password.")
        view = discord.ui.View(timeout=None)
        view.add_item(SignupButton())
        return embed, view

    # ---------- member side ----------

    async def handle_open(self, interaction: discord.Interaction) -> None:
        problem = await self._blocked(interaction)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.send_modal(SignupModal())

    async def handle_submit(self, interaction: discord.Interaction, raw_username: str, raw_name: str) -> None:
        problem = await self._blocked(interaction)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        username = normalise_username(raw_username)
        problem = username_problem(username)
        if problem:
            await interaction.response.send_message(f"{problem} Click the button to try again.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        member = interaction.user
        name = " ".join(raw_name.split())[:NAME_MAX] or member.display_name[:NAME_MAX]
        conf = self.config.guild(guild)

        async with self._locks[guild.id]:
            data = await conf.all()
            if str(member.id) in data["pending"] or str(member.id) in data["accounts"]:
                await interaction.followup.send("You already have a request or an account.", ephemeral=True)
                return
            if username in data["accounts"].values() or any(
                p["username"] == username for p in data["pending"].values()
            ):
                await interaction.followup.send(
                    f"**{username}** is already taken. Click the button to pick another.", ephemeral=True
                )
                return
            channel = guild.get_channel(data["approval_channel"] or 0)
            if channel is None:
                await interaction.followup.send(
                    "Account requests aren't set up on this server yet. Please ask an admin.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title="Shelfarr account request",
                description=f"{member.mention} wants an account",
                colour=discord.Colour.gold(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Username", value=f"`{username}`")
            embed.add_field(name="Display name", value=discord.utils.escape_markdown(name))
            embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
            embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
            embed.add_field(
                name="Joined server",
                value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown",
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            view = discord.ui.View(timeout=None)
            view.add_item(DecisionButton("approve", member.id))
            view.add_item(DecisionButton("deny", member.id))

            try:
                message = await channel.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                log.exception("Couldn't post request card in channel %s (guild %s)", channel.id, guild.id)
                await interaction.followup.send("I couldn't send your request. Please let an admin know.", ephemeral=True)
                return

            async with conf.pending() as pending:
                pending[str(member.id)] = {
                    "username": username,
                    "name": name,
                    "channel_id": channel.id,
                    "message_id": message.id,
                }

        log.info("%s (%s) requested Shelfarr username %s in guild %s", member, member.id, username, guild.id)
        await interaction.followup.send(
            f"Request for **{username}** sent. I'll DM you your login once an admin approves it.", ephemeral=True
        )

    # ---------- approve / deny ----------

    async def handle_decision(self, interaction: discord.Interaction, action: str, user_id: int) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if not await self._can_decide(interaction.user):
            await interaction.response.send_message(
                "Only admins with Manage Server can decide account requests.", ephemeral=True
            )
            return

        await interaction.response.defer()
        conf = self.config.guild(guild)
        key = str(user_id)
        approver = interaction.user
        password: Optional[str] = None
        created = False

        async with self._locks[guild.id]:
            request = (await conf.pending()).get(key)
            if request is None:
                await interaction.followup.send("This request has already been handled.", ephemeral=True)
                with contextlib.suppress(discord.HTTPException):
                    await interaction.edit_original_response(view=None)
                return

            username, name = request["username"], request["name"]
            if action == "deny":
                outcome = f"Denied by {approver.mention}"
                colour = discord.Colour.red()
            else:
                password = generate_password()
                try:
                    await self._create_user(username, name, password)
                except ShelfarrSetupError as e:
                    # Not the member's fault: keep the request open so it can be approved again once fixed.
                    await interaction.followup.send(f"{str(e).rstrip('.')}. The request is still open.", ephemeral=True)
                    return
                except ShelfarrError as e:
                    outcome = f"Not created: {e}"
                    colour = discord.Colour.dark_grey()
                    password = None
                else:
                    created = True
                    outcome = f"Created by {approver.mention}"
                    colour = discord.Colour.green()
                    async with conf.accounts() as accounts:
                        accounts[key] = username

            async with conf.pending() as pending:
                pending.pop(key, None)

        log.info("Shelfarr request %s (%s) in guild %s: %s by %s", key, username, guild.id, action, approver.id)

        member = guild.get_member(user_id)
        if member is None:
            with contextlib.suppress(discord.HTTPException):
                member = await guild.fetch_member(user_id)

        dm_sent = False
        if member is not None:
            dm_sent = await self._notify(member, guild, action, created, username, password)
        if created and not dm_sent:
            outcome += "\nCouldn't DM them, so the login was shown to the approver only."
            await interaction.followup.send(
                f"I couldn't DM <@{user_id}>. Send them these yourself:\n"
                f"{await self._login_text(username, password)}",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )

        message = interaction.message
        if message is not None and message.embeds:
            embed = message.embeds[0].copy()
        else:
            embed = discord.Embed(description=f"<@{user_id}> wants an account")
        embed.colour = colour
        embed.title = "Shelfarr account request: " + (
            "created" if created else "denied" if action == "deny" else "not created"
        )
        embed.add_field(name="Outcome", value=outcome, inline=False)
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(embed=embed, view=None, allowed_mentions=NO_MENTIONS)

    async def _login_text(self, username: str, password: str) -> str:
        login_url = await self.config.login_url()
        lines = []
        if login_url:
            lines.append(f"**Where:** {login_url}")
        lines.append(f"**Username:** `{username}`")
        lines.append(f"**Password:** `{password}`")
        return "\n".join(lines)

    async def _notify(
        self,
        member: discord.Member,
        guild: discord.Guild,
        action: str,
        created: bool,
        username: str,
        password: Optional[str],
    ) -> bool:
        if created:
            text = (
                f"Your Shelfarr account from **{guild.name}** is ready.\n\n"
                f"{await self._login_text(username, password)}\n\n"
                "This password was made up for you, so change it after you log in: click your name in "
                "the top right to open your profile, then change your password."
            )
        elif action == "deny":
            text = f"Your Shelfarr account request in **{guild.name}** was denied."
        else:
            text = (
                f"Your Shelfarr account request in **{guild.name}** couldn't be completed "
                f"(the username **{username}** may already be taken). Click the button again to pick another."
            )
        try:
            await member.send(text)
        except discord.HTTPException:
            return False
        return True

    # ---------- admin commands ----------

    @commands.group(name="shelfarrsignup")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def shelfarrsignup(self, ctx: commands.Context) -> None:
        """Shelfarr account requests from Discord."""

    @shelfarrsignup.command(name="channel")
    async def shelfarrsignup_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the private channel where account requests are posted."""
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
        msg = f"Account requests will go to {channel.mention}. Keep it private: the cards show who asked."
        if missing:
            msg += f"\n\N{WARNING SIGN} I'm missing {', '.join(missing)} there, so requests will fail until that's fixed."
        await ctx.send(msg)

    @shelfarrsignup.command(name="loginurl")
    @commands.is_owner()
    async def shelfarrsignup_loginurl(self, ctx: commands.Context, url: str) -> None:
        """Set the address people log in at, shown on the panel and in the DM.

        This can differ from the API URL the bot uses, e.g. https://shelfarr.example.com
        """
        url = url.strip().rstrip("/")
        await self.config.login_url.set(url)
        await ctx.send(f"Login address set to <{url}>. Run `{ctx.clean_prefix}shelfarrsignup refresh` to update the panel.")

    @shelfarrsignup.command(name="post")
    async def shelfarrsignup_post(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *, title: str = DEFAULT_TITLE
    ) -> None:
        """Post the request panel (defaults to this channel)."""
        channel = channel or ctx.channel
        embed, view = await self._build_panel(channel, title)
        try:
            message = await channel.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            await ctx.send(f"I couldn't post in {channel.mention}: {e.text or e.status}")
            return
        await self.config.guild(ctx.guild).panel.set({"channel_id": channel.id, "message_id": message.id, "title": title})
        if channel != ctx.channel:
            await ctx.send(f"Panel posted: {message.jump_url}")

    @shelfarrsignup.command(name="refresh")
    async def shelfarrsignup_refresh(self, ctx: commands.Context) -> None:
        """Update the posted panel (e.g. after changing the login address)."""
        panel = await self.config.guild(ctx.guild).panel()
        channel = ctx.guild.get_channel(panel.get("channel_id", 0))
        if channel is None:
            await ctx.send(f"No panel found. Post one with `{ctx.clean_prefix}shelfarrsignup post`.")
            return
        embed, view = await self._build_panel(channel, panel.get("title", DEFAULT_TITLE))
        try:
            message = await channel.fetch_message(panel["message_id"])
            await message.edit(embed=embed, view=view, allowed_mentions=NO_MENTIONS)
        except discord.NotFound:
            await self.config.guild(ctx.guild).panel.clear()
            await ctx.send(f"The panel message is gone. Post a new one with `{ctx.clean_prefix}shelfarrsignup post`.")
            return
        except discord.HTTPException as e:
            await ctx.send(f"I couldn't update the panel: {e.text or e.status}")
            return
        await ctx.send(f"Panel updated: {message.jump_url}")

    @shelfarrsignup.command(name="pending")
    async def shelfarrsignup_pending(self, ctx: commands.Context) -> None:
        """List requests still waiting, with links to their cards."""
        pending = await self.config.guild(ctx.guild).pending()
        if not pending:
            await ctx.send("No pending requests.")
            return
        lines = [
            f"<@{user_id}> \N{RIGHTWARDS ARROW} `{info['username']}`: "
            f"[card](https://discord.com/channels/{ctx.guild.id}/{info['channel_id']}/{info['message_id']})"
            for user_id, info in pending.items()
        ]
        for page in pagify("\n".join(lines)):
            await ctx.send(page, allowed_mentions=NO_MENTIONS)

    @shelfarrsignup.command(name="forget")
    async def shelfarrsignup_forget(self, ctx: commands.Context, user: Union[discord.Member, int]) -> None:
        """Let someone request again (clears their request and remembered account here).

        It doesn't delete anything in Shelfarr.
        """
        key = str(user.id if isinstance(user, discord.Member) else user)
        conf = self.config.guild(ctx.guild)
        async with self._locks[ctx.guild.id]:
            async with conf.pending() as pending:
                had_pending = pending.pop(key, None) is not None
            async with conf.accounts() as accounts:
                had_account = accounts.pop(key, None)
        if not had_pending and not had_account:
            await ctx.send("Nothing stored for that user.")
            return
        await ctx.send(f"Cleared <@{key}>. They can click the button again.", allowed_mentions=NO_MENTIONS)

    @shelfarrsignup.command(name="show")
    async def shelfarrsignup_show(self, ctx: commands.Context) -> None:
        """Show settings and test the Shelfarr connection (never shows the token)."""
        data = await self.config.guild(ctx.guild).all()
        tokens = await self.bot.get_shared_api_tokens(SERVICE)
        channel = ctx.guild.get_channel(data["approval_channel"] or 0)
        try:
            status = await self._check_api()
        except ShelfarrError as e:
            status = f"FAILED - {e}"
        await ctx.send(
            box(
                f"Approvals:  {('#' + channel.name) if channel else 'not set'}\n"
                f"Login URL:  {await self.config.login_url() or 'not set'}\n"
                f"API URL:    {tokens.get('url') or 'not set'}\n"
                f"API token:  {'set' if tokens.get('api_key') else 'NOT SET'}\n"
                f"Shelfarr:   {status}\n"
                f"Pending:    {len(data['pending'])}\n"
                f"Created:    {len(data['accounts'])}"
            )
        )
