"""Needle cog — auto-thread creation for designated channels.

Inspired by discord-needle (github.com/MarcusOtter/discord-needle).
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import get_config_value, open_db
from bot_modules.dm.batch.logic import PendingDM, unique_actors
from bot_modules.dm.batch.service import register_handler, unregister_handler
from bot_modules.dm.batch.store import drop_group, enqueue
from bot_modules.needle.logic import (
    DEFAULT_AUTO_ARCHIVE_MINUTES,
    REPLY_BATCH_KIND,
    REPLY_DM_BUTTON_TEMPLATE,
    TEXT_ONLY_NOTICE_SECONDS,
    AutoArchiveMinutes,
    MuteAction,
    auto_archive_minutes as clamp_auto_archive_minutes,
    build_reply_digest_text,
    build_text_only_notice,
    can_manage_thread,
    dm_button_may_write,
    mute_button_label,
    reply_dm_custom_id,
    resolve_starter_id,
    should_dm_starter,
    should_post_text_only_notice,
    starter_deleted_fate,
    starter_post_verdict,
    thread_create_options,
    toggled,
)
from bot_modules.needle.store import (
    ensure_optins_table,
    is_reply_opted_in,
    set_reply_optin,
)
from bot_modules.shared.dm_branding import send_branded_dm
from bot_modules.shared.name_resolver import build_name_fn
from bot_modules.no_contact.service import is_no_contact_conn

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.needle")

TitleType = Literal["first_fifty", "first_line", "user_date", "custom"]

_DEFAULT_REPLY = "Thread created by $USER in $CHANNEL"


# ── DB helpers ────────────────────────────────────────────────────────────────


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS needle_channels (
            guild_id            INTEGER NOT NULL,
            channel_id          INTEGER NOT NULL,
            title_type          TEXT    NOT NULL DEFAULT 'first_fifty',
            custom_title        TEXT    NOT NULL DEFAULT '',
            include_bots        INTEGER NOT NULL DEFAULT 0,
            slowmode            INTEGER NOT NULL DEFAULT 0,
            delete_behavior     TEXT    NOT NULL DEFAULT 'archive_if_empty',
            reply_type          TEXT    NOT NULL DEFAULT 'default',
            custom_reply        TEXT    NOT NULL DEFAULT '',
            default_reactions   TEXT    NOT NULL DEFAULT '',
            notify_op_on_reply  INTEGER NOT NULL DEFAULT 0,
            require_attachment  INTEGER NOT NULL DEFAULT 0,
            auto_archive_minutes INTEGER NOT NULL DEFAULT 1440,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)
    ensure_optins_table(conn)


@dataclass
class NeedleChannelConfig:
    guild_id: int
    channel_id: int
    title_type: TitleType
    custom_title: str
    include_bots: bool
    slowmode: int
    delete_behavior: str
    reply_type: str
    custom_reply: str
    default_reactions: str
    #: DM the thread's starter on replies — if they opted in (migration 223;
    #: ships off; the member's opt-in is migration 244).
    notify_op_on_reply: bool = False
    #: A member's post must carry an upload; a text-only one is removed and
    #: gets no thread (migration 246; ships off).
    require_attachment: bool = False
    #: How long a new thread stays unarchived with no activity, in minutes:
    #: one of Discord's four (60 / 1440 / 4320 / 10080). Migration 252; ships
    #: at 1440, the hard-coded value it replaced.
    auto_archive_minutes: AutoArchiveMinutes = DEFAULT_AUTO_ARCHIVE_MINUTES


@dataclass
class NeedleGlobalConfig:
    default_reply: str


def _row_to_config(row: sqlite3.Row) -> NeedleChannelConfig:
    return NeedleChannelConfig(
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        title_type=row["title_type"],
        custom_title=row["custom_title"],
        include_bots=bool(row["include_bots"]),
        slowmode=row["slowmode"],
        delete_behavior=row["delete_behavior"],
        reply_type=row["reply_type"],
        custom_reply=row["custom_reply"],
        default_reactions=row["default_reactions"] or "",
        notify_op_on_reply=bool(row["notify_op_on_reply"])
        if "notify_op_on_reply" in row.keys()
        else False,
        require_attachment=bool(row["require_attachment"])
        if "require_attachment" in row.keys()
        else False,
        auto_archive_minutes=clamp_auto_archive_minutes(row["auto_archive_minutes"])
        if "auto_archive_minutes" in row.keys()
        else DEFAULT_AUTO_ARCHIVE_MINUTES,
    )


def _get_global_config(conn: sqlite3.Connection, guild_id: int) -> NeedleGlobalConfig:
    return NeedleGlobalConfig(
        default_reply=get_config_value(conn, "needle_default_reply", _DEFAULT_REPLY, guild_id),
    )


def _get_channel_config(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> NeedleChannelConfig | None:
    row = conn.execute(
        "SELECT * FROM needle_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    return _row_to_config(row) if row else None


def _upsert_channel(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    channel_id: int,
    title_type: TitleType,
    custom_title: str,
    include_bots: bool,
    slowmode: int,
    delete_behavior: str,
    reply_type: str,
    custom_reply: str,
    default_reactions: str,
    notify_op_on_reply: bool = False,
    require_attachment: bool = False,
    auto_archive_minutes: int = DEFAULT_AUTO_ARCHIVE_MINUTES,
) -> None:
    conn.execute(
        """
        INSERT INTO needle_channels
            (guild_id, channel_id, title_type, custom_title, include_bots, slowmode,
             delete_behavior, reply_type, custom_reply, default_reactions,
             notify_op_on_reply, require_attachment, auto_archive_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (guild_id, channel_id) DO UPDATE SET
            title_type          = excluded.title_type,
            custom_title        = excluded.custom_title,
            include_bots        = excluded.include_bots,
            slowmode            = excluded.slowmode,
            delete_behavior     = excluded.delete_behavior,
            reply_type          = excluded.reply_type,
            custom_reply        = excluded.custom_reply,
            default_reactions   = excluded.default_reactions,
            notify_op_on_reply  = excluded.notify_op_on_reply,
            require_attachment  = excluded.require_attachment,
            auto_archive_minutes = excluded.auto_archive_minutes
        """,
        (
            guild_id, channel_id, title_type, custom_title, int(include_bots), slowmode,
            delete_behavior, reply_type, custom_reply,
            ",".join(e.strip() for e in default_reactions.split(",") if e.strip()),
            int(notify_op_on_reply),
            int(require_attachment),
            clamp_auto_archive_minutes(auto_archive_minutes),
        ),
    )


def _delete_channel(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> int:
    """Stop auto-threading a channel. Members' reply-DM opt-ins stay: they are
    per member, not per channel, and still apply to the guild's other channels."""
    cur = conn.execute(
        "DELETE FROM needle_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    return cur.rowcount


def _list_channels(
    conn: sqlite3.Connection, guild_id: int
) -> list[NeedleChannelConfig]:
    rows = conn.execute(
        "SELECT * FROM needle_channels WHERE guild_id = ? ORDER BY channel_id",
        (guild_id,),
    ).fetchall()
    return [_row_to_config(r) for r in rows]


# ── Thread title logic ────────────────────────────────────────────────────────


def _build_thread_name(message: discord.Message, cfg: NeedleChannelConfig) -> str:
    content = message.clean_content.strip()
    if cfg.title_type == "first_line":
        title = content.split("\n", 1)[0].strip()
    elif cfg.title_type == "user_date":
        display = getattr(message.author, "display_name", None) or message.author.name
        date_str = message.created_at.strftime("%Y-%m-%d")
        title = f"{display} ({date_str})"
    elif cfg.title_type == "custom":
        display = getattr(message.author, "display_name", None) or message.author.name
        date_str = message.created_at.strftime("%Y-%m-%d")
        title = cfg.custom_title.replace("$USER", display).replace("$DATE", date_str)
    else:  # first_fifty
        title = content[:50].replace("\n", " ")
    return (title[:100].strip()) or "New Thread"


def _apply_variables(
    template: str,
    *,
    message: discord.Message,
    thread: discord.Thread,
) -> str:
    display = getattr(message.author, "display_name", None) or message.author.name
    # A member-controlled nickname (or username) is interpolated into the
    # welcome message the bot then sends and *pins*. Neutralize any mention
    # syntax so a nickname like "<@&roleId>" or "@everyone" can't ping on the
    # bot's behalf (the send also passes AllowedMentions.none() as defense),
    # and escape markdown so the nickname can't reformat the pinned message.
    safe_display = discord.utils.escape_markdown(discord.utils.escape_mentions(display))
    return (
        template
        .replace("$USER", safe_display)
        .replace("$CHANNEL", f"<#{message.channel.id}>")
        .replace("$THREAD", thread.mention)
    )


# ── Thread starter ───────────────────────────────────────────────────────────

#: thread id → the member whose message started it (0 = none could be found).
#: Filled at creation so the common case costs no REST call; a restart or an
#: evicted entry falls back to the starter message. Bounded, oldest out first.
_STARTER_CACHE: dict[int, int] = {}
_STARTER_CACHE_MAX = 2048


#: (channel id, member id) → when that member was last told their text-only
#: post was removed. Memory only, never stored: it throttles a note, and a
#: restart forgetting it costs one repeated note. Bounded, oldest out first.
_TEXT_ONLY_NOTICE_AT: dict[tuple[int, int], float] = {}
_TEXT_ONLY_NOTICE_MAX = 512


def _claim_text_only_notice(channel_id: int, user_id: int, now: float) -> bool:
    """True when this member should be sent the note, recording that they were."""
    key = (channel_id, user_id)
    if not should_post_text_only_notice(
        now=now, last_notice_at=_TEXT_ONLY_NOTICE_AT.get(key)
    ):
        return False
    _TEXT_ONLY_NOTICE_AT.pop(key, None)
    _TEXT_ONLY_NOTICE_AT[key] = now
    while len(_TEXT_ONLY_NOTICE_AT) > _TEXT_ONLY_NOTICE_MAX:
        _TEXT_ONLY_NOTICE_AT.pop(next(iter(_TEXT_ONLY_NOTICE_AT)))
    return True


def _release_text_only_notice(channel_id: int, user_id: int) -> None:
    """Undo a claim whose note never went out, so the member's next text-only
    post tells them. Claimed before the send and released on failure, rather
    than recorded after it, so two posts landing together can't both send."""
    _TEXT_ONLY_NOTICE_AT.pop((channel_id, user_id), None)


#: Channels already logged as "can't delete here", so a busy channel writes one
#: warning per boot rather than one per post. The dashboard card carries the
#: standing warning (``needle.logic.removal_blocked``).
_WARNED_CANNOT_REMOVE: set[int] = set()


def _bot_can_manage_messages(guild: Any, channel_id: int) -> bool | None:
    """Can the bot delete posts in this channel? ``None`` when it can't be read
    (no guild, channel not cached, the bot's own member missing)."""
    if guild is None:
        return None
    channel = guild.get_channel(channel_id)
    me = getattr(guild, "me", None)
    if channel is None or me is None:
        return None
    return bool(channel.permissions_for(me).manage_messages)


async def _author_can_manage_messages(message: discord.Message) -> bool:
    """The moderator exemption's measurement.

    A guild message's author is normally a ``Member``. When Discord hands over
    a bare ``User`` (the member isn't in the cache), look them up rather than
    treat a moderator as an ordinary poster. If they can't be found the answer
    is no: they are not a member, so they are not a moderator.
    """
    author: Any = message.author
    if not isinstance(author, discord.Member):
        guild = message.guild
        if guild is None:
            return False
        author = guild.get_member(message.author.id)
        if author is None:
            try:
                author = await guild.fetch_member(message.author.id)
            except discord.HTTPException:
                return False
    channel = cast(discord.TextChannel, message.channel)
    return bool(channel.permissions_for(author).manage_messages)


def _remember_starter(thread_id: int, author_id: int) -> None:
    _STARTER_CACHE.pop(thread_id, None)
    _STARTER_CACHE[thread_id] = author_id
    while len(_STARTER_CACHE) > _STARTER_CACHE_MAX:
        _STARTER_CACHE.pop(next(iter(_STARTER_CACHE)))


async def _starter_author_id(thread: discord.Thread) -> int:
    """Who started this thread, as a member id (0 if unknown).

    A thread opened from a message shares that message's id, so the starter is
    the author of message ``thread.id`` in the parent. The fallback rule lives
    in ``needle.logic.resolve_starter_id``; this only fetches and caches.
    """
    cached = _STARTER_CACHE.get(thread.id)
    if cached is not None:
        return cached
    starter = thread.starter_message
    transient_failure = False
    if starter is None and isinstance(thread.parent, discord.TextChannel):
        try:
            starter = await thread.parent.fetch_message(thread.id)
        except discord.NotFound:
            starter = None
        except discord.HTTPException:
            # A rate limit or a transient 5xx, not proof the message is gone.
            # Falling through to the owner-id guess and caching a wrong 0
            # forever would silently and permanently disable this thread's
            # reply DMs and close/rename permission, so don't remember it.
            starter = None
            transient_failure = True
    me = thread.guild.me
    author_id = resolve_starter_id(
        starter_author_id=starter.author.id if starter is not None else None,
        owner_id=thread.owner_id or 0,
        bot_id=me.id if me else 0,
    )
    if transient_failure and author_id == 0:
        return 0
    _remember_starter(thread.id, author_id)
    return author_id


async def _has_thread_perm(
    user: discord.User | discord.Member, thread: discord.Thread
) -> bool:
    has_manage = isinstance(user, discord.Member) and user.guild_permissions.manage_threads
    # Short-circuit before the starter lookup, which can cost a REST call.
    if can_manage_thread(
        user_id=user.id, owner_id=thread.owner_id or 0,
        starter_author_id=0, has_manage_threads=has_manage,
    ):
        return True
    return can_manage_thread(
        user_id=user.id,
        owner_id=thread.owner_id or 0,
        starter_author_id=await _starter_author_id(thread),
        has_manage_threads=has_manage,
    )


# ── Persistent welcome-message view ──────────────────────────────────────────


class NeedleTitleModal(discord.ui.Modal, title="Edit Thread Title"):
    new_title: discord.ui.TextInput = discord.ui.TextInput(
        label="New title",
        required=True,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Not in a thread.", ephemeral=True)
            return
        name = str(self.new_title.value).strip()[:100]
        if not name:
            await interaction.response.send_message("❌ Title can't be empty.", ephemeral=True)
            return
        await interaction.channel.edit(name=name)
        await interaction.response.send_message(
            f"Renamed to **{discord.utils.escape_markdown(name)}**.", ephemeral=True
        )


class NeedleThreadView(discord.ui.View):
    """Persistent view with Archive + Edit-Title buttons, attached to welcome messages."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Archive Thread",
        style=discord.ButtonStyle.success,
        custom_id="needle:close",
    )
    async def close_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Not in a thread.", ephemeral=True)
            return
        thread = interaction.channel
        if not await _has_thread_perm(interaction.user, thread):
            await interaction.response.send_message(
                "❌ Only the member who started this thread or a moderator can archive it.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Thread archived.")
        await thread.edit(archived=True, locked=False)

    @discord.ui.button(
        label="Edit Title",
        style=discord.ButtonStyle.primary,
        custom_id="needle:title",
    )
    async def title_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("❌ Not in a thread.", ephemeral=True)
            return
        if not await _has_thread_perm(interaction.user, interaction.channel):
            await interaction.response.send_message(
                "❌ Only the member who started this thread or a moderator can rename it.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(NeedleTitleModal())


# ── Reply DM toggle ──────────────────────────────────────────────────────────


class NeedleReplyMuteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=REPLY_DM_BUTTON_TEMPLATE,
):
    """The reply DM's "Turn off reply DMs" button, and its undo.

    Pressed in a DM, so the interaction has no guild: everything the press
    needs rides in the custom id. It only ever changes the presser's own
    opt-in, and for the whole server — the same switch as Help & Settings →
    Roles & Pings. A press on a DM sent before migration 244 (a per-thread
    "Stop" button) lands here too, and means the same thing.
    """

    def __init__(
        self, action: MuteAction, *, guild_id: int, channel_id: int, thread_id: int
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=mute_button_label(action),
                style=discord.ButtonStyle.secondary,
                custom_id=reply_dm_custom_id(
                    action, guild_id=guild_id, channel_id=channel_id, thread_id=thread_id
                ),
            )
        )
        self.action: MuteAction = action
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.thread_id = thread_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> NeedleReplyMuteButton:
        return cls(
            cast(MuteAction, match["action"]),
            guild_id=int(match["guild_id"]),
            channel_id=int(match["channel_id"]),
            thread_id=int(match["thread_id"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db_path = cast(Any, interaction.client).ctx.db_path
        user_id = interaction.user.id
        guild = interaction.client.get_guild(self.guild_id)
        is_member = guild is not None and guild.get_member(user_id) is not None
        if not dm_button_may_write(self.action, is_member=is_member):
            await interaction.response.send_message(
                "❌ You're no longer in that server.", ephemeral=True
            )
            return

        def _write() -> None:
            with open_db(db_path) as conn:
                set_reply_optin(
                    conn, guild_id=self.guild_id, user_id=user_id,
                    on=self.action == "unmute",
                )

        await asyncio.to_thread(_write)
        await interaction.response.edit_message(
            view=_reply_dm_view(
                toggled(self.action),
                guild_id=self.guild_id, channel_id=self.channel_id, thread_id=self.thread_id,
            )
        )


def _reply_dm_view(
    action: MuteAction, *, guild_id: int, channel_id: int, thread_id: int
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        NeedleReplyMuteButton(
            action, guild_id=guild_id, channel_id=channel_id, thread_id=thread_id
        )
    )
    return view


# ── Batched reply DM ─────────────────────────────────────────────────────────


async def _deliver_reply_batch(
    bot: Bot, guild: discord.Guild, starter: discord.Member, rows: list[PendingDM]
) -> None:
    """Send one DM for a thread's replies over the batch window.

    The switches are read again here, not trusted from queue time: the channel
    may have stopped auto-threading or dropped reply DMs, or the starter may
    have turned reply DMs off since. ``dm_batch`` has already re-checked no-contact
    and that the starter is still in the server.
    """
    head = rows[0]
    thread_id = head.group_key
    channel_id = int(head.payload.get("channel_id") or 0)

    def _still_wanted() -> bool:
        with open_db(bot.ctx.db_path) as conn:
            cfg = _get_channel_config(conn, guild.id, channel_id)
            if cfg is None or not cfg.notify_op_on_reply:
                return False
            return is_reply_opted_in(conn, guild_id=guild.id, user_id=starter.id)

    if not channel_id or not await asyncio.to_thread(_still_wanted):
        return
    thread = guild.get_thread(thread_id)
    if thread is None:
        try:
            fetched = await guild.fetch_channel(thread_id)
        except discord.HTTPException:
            return  # deleted, or out of reach: nothing to point at
        if not isinstance(fetched, discord.Thread):
            return
        thread = fetched

    repliers = unique_actors(rows)
    name_fn = await build_name_fn(
        guild=guild, db_path=bot.ctx.db_path, guild_id=guild.id, user_ids=repliers,
    )
    text = build_reply_digest_text(
        replier_names=[name_fn(uid) for uid in repliers],
        reply_count=len(rows),
        thread_name=thread.name,
        guild_name=guild.name,
        guild_id=guild.id,
        thread_id=thread.id,
        first_reply_id=head.ref_id,
    )
    await send_branded_dm(
        starter,
        db_path=bot.ctx.db_path,
        guild=guild,
        embed=discord.Embed(description=text),
        view=_reply_dm_view("mute", guild_id=guild.id, channel_id=channel_id, thread_id=thread.id),
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ── Cog ──────────────────────────────────────────────────────────────────────


class NeedleCog(commands.Cog):
    """Auto-threading; channel configuration lives in the web dashboard."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        await asyncio.to_thread(self._init_db)
        self.bot.add_view(NeedleThreadView())
        self.bot.add_dynamic_items(NeedleReplyMuteButton)
        register_handler(REPLY_BATCH_KIND, _deliver_reply_batch)

    async def cog_unload(self) -> None:
        unregister_handler(REPLY_BATCH_KIND)
        self.bot.remove_dynamic_items(NeedleReplyMuteButton)

    def _init_db(self) -> None:
        with open_db(self.bot.ctx.db_path) as conn:
            _ensure_tables(conn)

    # ── DB thread helpers ──────────────────────────────────────────────────

    def _load_channel_config(
        self, guild_id: int, channel_id: int
    ) -> NeedleChannelConfig | None:
        with open_db(self.bot.ctx.db_path) as conn:
            return _get_channel_config(conn, guild_id, channel_id)

    def _load_global_config(self, guild_id: int) -> NeedleGlobalConfig:
        with open_db(self.bot.ctx.db_path) as conn:
            return _get_global_config(conn, guild_id)

    # ── on_message ────────────────────────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        if message.is_system():
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return

        if isinstance(message.channel, discord.Thread):
            await self._notify_starter(message, message.channel)
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        cfg = await asyncio.to_thread(
            self._load_channel_config, message.guild.id, message.channel.id
        )
        if cfg is None:
            return
        if message.author.bot and not cfg.include_bots:
            return

        verdict = starter_post_verdict(
            require_attachment=cfg.require_attachment,
            has_attachment=bool(message.attachments),
            author_is_bot=message.author.bot,
            # Only worked out when it can matter: a channel with the dial off,
            # or a post with an upload, never needs the permission lookup.
            author_can_manage_messages=(
                cfg.require_attachment
                and not message.attachments
                and not message.author.bot
                and await _author_can_manage_messages(message)
            ),
        )
        if verdict == "remove":
            await self._remove_text_only_post(message)
            return

        name = _build_thread_name(message, cfg)
        try:
            thread, gcfg = await asyncio.gather(
                message.create_thread(
                    name=name,
                    **thread_create_options(
                        archive_minutes=cfg.auto_archive_minutes, slowmode=cfg.slowmode
                    ),
                ),
                asyncio.to_thread(self._load_global_config, message.guild.id),
            )
        except discord.Forbidden:
            log.warning(
                "Needle: missing permission in channel %s (guild %s)",
                message.channel.id, message.guild.id,
            )
            return
        except discord.HTTPException as exc:
            log.warning("Needle: failed to create thread: %s", exc)
            return
        _remember_starter(thread.id, message.author.id)

        # Welcome message with action buttons
        await self._post_welcome(message, thread, cfg, gcfg)

        # Default emoji reactions — a cue for readers (quick voting, "seen"),
        # never a status the bot later reads back or swaps out.
        if cfg.default_reactions:
            for emoji in cfg.default_reactions.split(","):
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    log.debug("Needle: couldn't add reaction %r: skipping", emoji)

    async def _remove_text_only_post(self, message: discord.Message) -> None:
        """Delete a text-only post and tell its author where text goes.

        The post is deleted **before** any thread exists, so ``delete_behavior``
        has nothing to act on. If the delete fails the post stays, still without
        a thread: a thread on chatter is what the dial was switched on to stop.
        The note is sent only after a delete that worked, and a member counts
        as told only if the note went out.
        """
        try:
            await message.delete()
        except discord.NotFound:
            return  # someone else removed it first
        except discord.Forbidden:
            if message.channel.id not in _WARNED_CANNOT_REMOVE:
                _WARNED_CANNOT_REMOVE.add(message.channel.id)
                log.warning(
                    "Needle: can't remove text-only posts in channel %s (guild %s): "
                    "missing Manage Messages. Logged once per boot; the "
                    "Auto-Thread panel shows the warning until it is granted.",
                    message.channel.id, message.guild.id if message.guild else 0,
                )
            return
        except discord.HTTPException as exc:
            log.warning("Needle: failed to remove a text-only post: %s", exc)
            return
        _WARNED_CANNOT_REMOVE.discard(message.channel.id)

        if not _claim_text_only_notice(
            message.channel.id, message.author.id, time.monotonic()
        ):
            return
        try:
            await message.channel.send(
                build_text_only_notice(message.author.mention),
                delete_after=TEXT_ONLY_NOTICE_SECONDS,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[message.author]
                ),
            )
        except discord.HTTPException as exc:
            # The member was not told, so don't count them as told.
            _release_text_only_notice(message.channel.id, message.author.id)
            log.debug("Needle: couldn't post the text-only note: %s", exc)

    async def _notify_starter(self, message: discord.Message, thread: discord.Thread) -> None:
        """Queue a reply DM for the thread's starter, when the channel asks for it
        and the starter opted in. Checked here, before ``enqueue``: a member who
        never opted in leaves no ``dm_batch_pending`` row behind.

        Bots are never announced, whatever ``include_bots`` says — that dial is
        about opening threads. A no-contact pair loses only the DM. The DM
        itself goes out batched, from ``_deliver_reply_batch``.
        """
        guild = message.guild
        if guild is None or message.author.bot or not thread.parent_id:
            return
        cfg = await asyncio.to_thread(self._load_channel_config, guild.id, thread.parent_id)
        if cfg is None or not cfg.notify_op_on_reply:
            return
        starter_id = await _starter_author_id(thread)
        if not starter_id or starter_id == message.author.id:
            return
        starter = guild.get_member(starter_id)
        if starter is None:
            return  # left the server: a DM about it would be a stranger's

        def _gates() -> tuple[bool, bool]:
            with open_db(self.bot.ctx.db_path) as conn:
                return (
                    is_reply_opted_in(conn, guild_id=guild.id, user_id=starter_id),
                    is_no_contact_conn(conn, guild.id, starter_id, message.author.id),
                )

        opted_in, no_contact = await asyncio.to_thread(_gates)
        if not should_dm_starter(
            notify_enabled=cfg.notify_op_on_reply,
            starter_author_id=starter_id,
            replier_id=message.author.id,
            replier_is_bot=message.author.bot,
            opted_in=opted_in,
            no_contact=no_contact,
        ):
            return
        # Held, not sent: replies in the next ten minutes join this one and the
        # starter gets a single DM for the lot (_deliver_reply_batch).
        def _queue() -> None:
            with open_db(self.bot.ctx.db_path) as conn:
                enqueue(
                    conn,
                    kind=REPLY_BATCH_KIND,
                    guild_id=guild.id,
                    user_id=starter_id,
                    group_key=thread.id,
                    actor_id=message.author.id,
                    ref_id=message.id,
                    payload={"channel_id": thread.parent_id},
                )

        await asyncio.to_thread(_queue)

    async def _post_welcome(
        self,
        message: discord.Message,
        thread: discord.Thread,
        cfg: NeedleChannelConfig,
        gcfg: NeedleGlobalConfig,
    ) -> None:
        if cfg.reply_type == "none":
            return

        if cfg.reply_type == "custom":
            template = cfg.custom_reply
        else:
            template = gcfg.default_reply

        if not template.strip():
            return

        content = _apply_variables(template, message=message, thread=thread)
        try:
            msg = await thread.send(
                content,
                view=NeedleThreadView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Pin if we have permission; delete the "pinned a message" system message
            if thread.guild.me and thread.permissions_for(thread.guild.me).manage_messages:
                await msg.pin()
                async for sys_msg in thread.history(limit=5):
                    if (
                        sys_msg.type == discord.MessageType.pins_add
                        and sys_msg.author.id == (self.bot.user.id if self.bot.user else 0)
                    ):
                        await sys_msg.delete()
                        break
        except discord.HTTPException as exc:
            log.warning("Needle: failed to post welcome message: %s", exc)

    # ── on_raw_thread_delete ──────────────────────────────────────────────

    @commands.Cog.listener("on_raw_thread_delete")
    async def _on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        """A deleted thread's waiting reply DMs go with it."""
        _STARTER_CACHE.pop(payload.thread_id, None)

        def _purge() -> None:
            with open_db(self.bot.ctx.db_path) as conn:
                drop_group(
                    conn, kind=REPLY_BATCH_KIND,
                    guild_id=payload.guild_id, group_key=payload.thread_id,
                )

        await asyncio.to_thread(_purge)

    # ── on_message_delete ─────────────────────────────────────────────────

    @commands.Cog.listener("on_message_delete")
    async def _on_message_delete(self, message: discord.Message) -> None:
        if not message.guild:
            return
        thread = getattr(message, "thread", None)
        if not isinstance(thread, discord.Thread):
            return

        cfg = await asyncio.to_thread(
            self._load_channel_config, message.guild.id, message.channel.id
        )
        if cfg is None or cfg.delete_behavior == "nothing":
            return

        bot_member = message.guild.get_member(self.bot.user.id) if self.bot.user else None
        can_delete = (
            bot_member is not None
            and isinstance(message.channel, discord.TextChannel)
            and message.channel.permissions_for(bot_member).manage_threads
        )

        history_read, others_posted = True, False
        if cfg.delete_behavior == "archive_if_empty" and can_delete:
            me_id = self.bot.user.id if self.bot.user else 0
            try:
                async for m in thread.history(limit=None):
                    if m.author.id not in {message.author.id, me_id}:
                        others_posted = True
                        break
            except discord.HTTPException:
                history_read = False

        fate = starter_deleted_fate(
            behavior=cfg.delete_behavior,
            can_delete=can_delete,
            history_read=history_read,
            others_posted=others_posted,
        )
        if fate == "delete":
            try:
                await thread.delete()
                return
            except discord.HTTPException:
                pass
        if fate != "nothing":
            try:
                await thread.edit(archived=True)
            except discord.HTTPException:
                pass

    # ── /close ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="close",
        description="Archive this thread (its starter or manage-threads).",
    )
    async def close(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "❌ This command can only be used inside a thread.", ephemeral=True
            )
            return
        thread = interaction.channel
        if not await _has_thread_perm(interaction.user, thread):
            await interaction.response.send_message(
                "❌ Only the member who started this thread or a moderator can close it.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Thread archived.")
        await thread.edit(archived=True, locked=False)

    # ── /title ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="title",
        description="Rename this thread (its starter or manage-threads).",
    )
    @app_commands.describe(name="New title for this thread.")
    async def title(self, interaction: discord.Interaction, name: str) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "❌ This command can only be used inside a thread.", ephemeral=True
            )
            return
        if not await _has_thread_perm(interaction.user, interaction.channel):
            await interaction.response.send_message(
                "❌ Only the member who started this thread or a moderator can rename it.",
                ephemeral=True,
            )
            return
        name = name.strip()[:100]
        if not name:
            await interaction.response.send_message("❌ Title can't be empty.", ephemeral=True)
            return
        await interaction.channel.edit(name=name)
        await interaction.response.send_message(
            f"Thread renamed to **{discord.utils.escape_markdown(name)}**.", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(NeedleCog(bot))
