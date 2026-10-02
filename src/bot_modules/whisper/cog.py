"""Whisper cog — anonymous-message guessing game (Whisper clone).

Every card the cog sends is a Components V2 layout built in
``whisper/layouts.py`` (2026-09-30 conversion); the classes here add the
behaviour — binding the builders' custom ids, answering presses, and the
DB reads and writes around them. A whisper's DM redraws from the same
builder after every press, which is also how a DM sent as an embed before
the conversion becomes a V2 card the first time one of its buttons is used.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.analytics.usage_telemetry_service import record_event
from bot_modules.breaks.store import is_on_break
from bot_modules.core.background import spawn
from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.core.db_utils import open_db
from bot_modules.core.layout import ExpiringLayoutView, bind, card
from bot_modules.core.role_safety import grant_block_reason
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.core.utils import MemberScoped
from bot_modules.dm.batch.logic import PendingDM, unique_actors
from bot_modules.dm.batch.service import register_handler, unregister_handler
from bot_modules.dm.batch.store import enqueue
from bot_modules.docs.help_card import load_doc_body
from bot_modules.no_contact import service as no_contact_service
from bot_modules.no_contact.logic import SURFACE_WHISPER
from bot_modules.panels.title_service import with_lobby_title
from bot_modules.shared.dm_branding import (
    brand_dm_layout,
    guild_display_name,
    guild_icon_url,
    send_branded_dm,
)
from bot_modules.shared.name_resolver import NameFn, build_name_fn, mention
from bot_modules.whisper.layouts import (
    DELETE_CANCEL_ID,
    DELETE_CONFIRM_ID,
    GUESS_CLEAR_ID,
    GUESS_FILTER_ID,
    GUESS_NEXT_ID,
    GUESS_PICK_ID,
    GUESS_PREV_ID,
    HELP_DOC_KEY,
    HELP_ID,
    INBOX_ACTION_IDS,
    INBOX_BACK_ID,
    INBOX_CLEAR_ID,
    INBOX_FILTER_ID,
    INBOX_ID,
    INBOX_NEXT_ID,
    INBOX_OPEN_ID_TEMPLATE,
    INBOX_PAGE_SIZE,
    INBOX_PREV_ID,
    OPTIN_CANCEL_ID,
    OPTIN_CONFIRM_ID,
    SEND_ID,
    SENT_ID,
    TITLE,
    build_delete_confirm_layout,
    build_deleted_layout,
    build_guess_digest_layout,
    build_guess_list_picker_layout,
    build_guess_native_picker_layout,
    build_guess_result_layout,
    build_help_layout,
    build_inbox_detail_layout,
    build_inbox_list_layout,
    build_launcher_layout,
    build_line_layout,
    build_optin_done_layout,
    build_optin_layout,
    build_optout_done_layout,
    build_pool_refusal_layout,
    build_send_refusal_layout,
    build_reply_audit_layout,
    build_reply_dm_layout,
    build_reply_report_audit_layout,
    build_report_audit_layout,
    build_send_feed_layout,
    build_share_feed_layout,
    build_solved_feed_layout,
    build_whisper_dm_layout,
)
from bot_modules.whisper.logic import (
    GUESS_BATCH_KIND,
    check_send_cooldown,
    filter_whispers_by_message,
    format_cooldown_message,
    format_hourly_cap_message,
    fuzzy_score_members,
    guess_payload,
    inbox_action_buttons,
    member_picker_placeholder,
    outcome_from_payload,
    prune_recent_target_sends,
    recompute_inbox_after_delete,
    sender_feedback_wanted,
)
from bot_modules.whisper.models import (
    STATE_PENDING,
    STATE_SHARED,
    Whisper,
    WhisperConfig,
    WhisperState,
)
from bot_modules.whisper.repo import (
    backfill_whisper_launcher_channel,
    count_replies,
    delete_reply,
    delete_whisper,
    get_reply,
    get_whisper,
    get_whisper_config,
    insert_guess,
    insert_reply,
    insert_reply_report,
    insert_report,
    insert_whisper,
    list_received_in_states,
    list_sent,
    mark_solved,
    set_whisper_launcher_ids,
    set_whisper_message_ids,
    soft_delete_whisper,
    try_consume_guess,
    update_whisper_state,
    whisper_launcher_guilds,
)
from bot_modules.whisper.service import (
    ERROR_BOT_DM_FAILED,
    ERROR_GUESS_ALREADY_SOLVED,
    ERROR_GUESS_NO_ATTEMPTS,
    ERROR_GUESS_NOT_TARGET,
    ERROR_NOT_CONFIGURED,
    ERROR_TARGET_NEEDS_ROLE,
    MAX_MESSAGE_LENGTH,
    REPLY_CONFIRMATION,
    SENT_CONFIRMATION,
    GuessOutcome,
    GuessValidationError,
    SendValidationError,
    TransitionValidationError,
    evaluate_guess,
    guess_candidate_rejection,
    is_configured,
    is_terminal_for_sender,
    validate_delete,
    validate_reply,
    validate_send,
    validate_share,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.whisper")


#: The usage row written when the send form's pick hasn't opted in — the
#: form's member picker is Discord's own and can't mark who accepts
#: whispers, so this is how often that bites (Ben, 2026-09-30). Counted, no
#: text kept.
POOL_REFUSAL_EVENT_KIND = "whisper"
POOL_REFUSAL_EVENT_NAME = "send_refused_pool"

#: Below fifteen minutes on purpose: Discord stops taking edits to an
#: ephemeral message through its interaction after that, so a longer
#: timeout's grey-out would fail silently.
_INBOX_TIMEOUT = 300
_PICKER_TIMEOUT = 120

#: What an edit passes to turn a pre-conversion embed DM into a V2 card —
#: the guide's "every field explicit" rule for a converting edit.
_CLEAR_EMBED: dict[str, Any] = {"content": None, "embed": None, "attachments": []}


# ── DB shims (sync, called via asyncio.to_thread) ────────────────────────────

def _load_config(db_path: Path, guild_id: int) -> WhisperConfig:
    with open_db(db_path) as conn:
        return get_whisper_config(conn, guild_id)


def _do_insert_whisper(
    db_path: Path,
    *,
    guild_id: int,
    sender_id: int,
    target_id: int,
    message: str,
    guesses_left: int = 3,
) -> int:
    with open_db(db_path) as conn:
        return insert_whisper(
            conn, guild_id=guild_id, sender_id=sender_id,
            target_id=target_id, message=message, guesses_left=guesses_left,
        )


def _do_set_message_ids(
    db_path: Path, whisper_id: int, *, channel_msg_id: int, dm_msg_id: int
) -> None:
    with open_db(db_path) as conn:
        set_whisper_message_ids(
            conn, whisper_id, channel_msg_id=channel_msg_id, dm_msg_id=dm_msg_id
        )


def _do_load_whisper(db_path: Path, whisper_id: int) -> Whisper | None:
    with open_db(db_path) as conn:
        return get_whisper(conn, whisper_id)


def _do_delete_whisper(db_path: Path, whisper_id: int) -> None:
    with open_db(db_path) as conn:
        delete_whisper(conn, whisper_id)


def _do_record_guess(
    db_path: Path,
    *,
    whisper_id: int,
    guessed_id: int,
    correct: bool,
) -> bool:
    """Returns True if the guess was consumed. False means race-lost (someone else solved it first)."""
    with open_db(db_path) as conn:
        if not try_consume_guess(conn, whisper_id):
            return False
        insert_guess(conn, whisper_id=whisper_id, guessed_id=guessed_id, correct=correct)
        if correct:
            mark_solved(conn, whisper_id)
        return True


def _do_update_state(db_path: Path, whisper_id: int, new_state: WhisperState) -> None:
    with open_db(db_path) as conn:
        update_whisper_state(conn, whisper_id, new_state)


def _do_list_received_in_states(
    db_path: Path,
    *,
    guild_id: int,
    target_id: int,
    states: list[WhisperState],
) -> list[Whisper]:
    with open_db(db_path) as conn:
        return list_received_in_states(
            conn, guild_id=guild_id, target_id=target_id, states=states
        )


def _do_list_sent(
    db_path: Path, *, guild_id: int, sender_id: int
) -> list[Whisper]:
    with open_db(db_path) as conn:
        return list_sent(conn, guild_id=guild_id, sender_id=sender_id)


def _do_soft_delete(db_path: Path, whisper_id: int) -> None:
    with open_db(db_path) as conn:
        soft_delete_whisper(conn, whisper_id)


def _do_count_replies(db_path: Path, whisper_id: int) -> int:
    with open_db(db_path) as conn:
        return count_replies(conn, whisper_id)


def _do_set_launcher_ids(
    db_path: Path, guild_id: int, channel_id: int, message_id: int
) -> None:
    with open_db(db_path) as conn:
        set_whisper_launcher_ids(conn, guild_id, channel_id, message_id)


def _do_launcher_guilds(db_path: Path) -> set[int]:
    with open_db(db_path) as conn:
        return whisper_launcher_guilds(conn)


def _do_backfill_launcher_channels(db_path: Path, guild_ids: set[int]) -> None:
    with open_db(db_path) as conn:
        for guild_id in guild_ids:
            backfill_whisper_launcher_channel(conn, guild_id)


def _do_insert_reply(
    db_path: Path,
    *,
    whisper_id: int,
    from_user_id: int,
    to_user_id: int,
    content: str,
) -> int:
    with open_db(db_path) as conn:
        return insert_reply(
            conn,
            whisper_id=whisper_id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            content=content,
        )


def _do_insert_report(
    db_path: Path,
    *,
    whisper_id: int,
    reporter_id: int,
    reason: str,
) -> bool:
    """Returns True if inserted, False if this reporter already reported this whisper."""
    with open_db(db_path) as conn:
        return insert_report(
            conn,
            whisper_id=whisper_id,
            reporter_id=reporter_id,
            reason=reason,
        )


def _do_get_reply(db_path: Path, reply_id: int):
    with open_db(db_path) as conn:
        return get_reply(conn, reply_id)


def _do_delete_reply(db_path: Path, reply_id: int) -> None:
    with open_db(db_path) as conn:
        delete_reply(conn, reply_id)


def _do_insert_reply_report(
    db_path: Path,
    *,
    reply_id: int,
    reporter_id: int,
    reason: str,
) -> bool:
    """Returns True if inserted, False if duplicate."""
    with open_db(db_path) as conn:
        return insert_reply_report(
            conn,
            reply_id=reply_id,
            reporter_id=reporter_id,
            reason=reason,
        )


#: The member id on the pool-refusal usage row. Zero on purpose: the count
#: answers "how often are people affected", and a member id would make it
#: per-member data the disclosure texts say the usage table doesn't hold.
POOL_REFUSAL_USER_ID = 0


def _do_count_pool_refusal(db_path: Path, guild_id: int) -> None:
    with open_db(db_path) as conn:
        record_event(
            conn, guild_id, POOL_REFUSAL_EVENT_KIND, POOL_REFUSAL_EVENT_NAME,
            POOL_REFUSAL_USER_ID,
        )


# ── The whisper's DM card ────────────────────────────────────────────────────


def _dm_view(bot: Bot, whisper: Whisper) -> discord.ui.LayoutView:
    """The whisper's DM as it should read *now*, branded for its guild.

    ``send_branded_dm`` stamps the attribution on the first send; a redraw
    is a plain ``message.edit``, so the stamp is applied here the same way
    (``brand_dm_layout`` is pure). A guild the bot has left leaves the card
    unstamped rather than unsent.
    """
    view = card(build_whisper_dm_layout(whisper))
    guild = bot.get_guild(whisper.guild_id)
    return brand_dm_layout(
        view, guild_name=guild_display_name(guild), guild_icon_url=guild_icon_url(guild),
    )


async def _redraw_dm(
    bot: Bot, whisper: Whisper, *, message: discord.Message | None = None,
) -> None:
    """Redraw the whisper's DM from its current row, best-effort.

    ``message`` is the DM when the press came from it; otherwise the DM is
    fetched by its stored id — opening the DM, fetching and editing is up
    to three REST calls, so callers on the interaction path defer first.
    The edit clears ``content``/``embed``/``attachments`` explicitly, which
    is what turns a pre-conversion embed DM into the V2 card.
    """
    if not whisper.dm_msg_id:
        return
    try:
        if message is None or message.id != whisper.dm_msg_id:
            user = bot.get_user(whisper.target_id) or await bot.fetch_user(whisper.target_id)
            dm_channel = await user.create_dm()
            message = await dm_channel.fetch_message(whisper.dm_msg_id)
        await message.edit(view=_dm_view(bot, whisper), **_CLEAR_EMBED)
    except discord.HTTPException:
        log.warning("Failed to redraw the DM for whisper #%s", whisper.id)


# ── Per-whisper Dynamic buttons (custom_id contains whisper_id) ──────────────
#
# These use discord.ui.DynamicItem so that after a bot restart the button
# clicks on existing DMs still route correctly via regex matching of the
# persisted custom_id. The DM card itself is built by ``layouts`` with plain
# buttons carrying these ids; the dispatcher matches the id, not the view.


class WhisperGuessButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:guess:(?P<id>\d+)"),
):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Guess",
                style=discord.ButtonStyle.secondary,
                custom_id=f"whisper:guess:{whisper_id}",
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperGuessButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("❌ Whisper not found.", ephemeral=True)
            return
        if interaction.user.id != whisper.target_id:
            await interaction.response.send_message(ERROR_GUESS_NOT_TARGET, ephemeral=True)
            return
        if whisper.solved:
            await interaction.response.send_message(ERROR_GUESS_ALREADY_SOLVED, ephemeral=True)
            return
        if whisper.guesses_left <= 0:
            await interaction.response.send_message(ERROR_GUESS_NO_ATTEMPTS, ephemeral=True)
            return

        # In a guild interaction, use the native avatar/presence picker.
        # Auto-populated user selects can't resolve guild members from a DM,
        # so DM clicks fall through to the string-based picker below.
        if interaction.guild is not None:
            view: ExpiringLayoutView = WhisperGuessNativeView(
                self.bot, whisper, invoker_id=interaction.user.id,
            )
            await interaction.response.send_message(view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return

        guild = self.bot.get_guild(whisper.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "❌ Couldn't find the server — try again.", ephemeral=True
            )
            return
        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, whisper.guild_id)
        if cfg.role_id == 0:
            await interaction.response.send_message(
                "❌ Whisper role isn't configured.", ephemeral=True
            )
            return
        role = guild.get_role(cfg.role_id)
        if role is None:
            await interaction.response.send_message(
                "❌ Whisper role no longer exists.", ephemeral=True
            )
            return

        members = sorted(
            [m for m in role.members if m.id != whisper.target_id],
            key=lambda m: m.display_name.lower(),
        )
        if not members:
            await interaction.response.send_message(
                "❌ No other opted-in members to guess from.", ephemeral=True
            )
            return

        view = WhisperGuessListView(
            self.bot, whisper, members, invoker_id=interaction.user.id,
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


class WhisperShareButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:share:(?P<id>\d+)"),
):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Share",
                style=discord.ButtonStyle.secondary,
                custom_id=f"whisper:share:{whisper_id}",
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperShareButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("❌ Whisper not found.", ephemeral=True)
            return
        try:
            validate_share(whisper, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        # Replacing the feed announcement is a fetch, a delete and a send, and
        # the DM's redraw is an edit after that — well past what fits in
        # Discord's 3 s window before the confirmation.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await asyncio.to_thread(
            _do_update_state, self.bot.ctx.db_path, self.whisper_id, STATE_SHARED
        )
        feed_jump_url = await _share_side_effects(self.bot, whisper)

        shared = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if shared is not None:
            await _redraw_dm(self.bot, shared, message=interaction.message)

        confirm = "Shared to the whisper feed."
        if feed_jump_url:
            confirm = f"[Shared to the whisper feed.]({feed_jump_url})"
        await interaction.followup.send(confirm, ephemeral=True)


class WhisperDeleteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:delete:(?P<id>\d+)"),
):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Delete",
                style=discord.ButtonStyle.danger,
                custom_id=f"whisper:delete:{whisper_id}",
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperDeleteButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("❌ Whisper not found.", ephemeral=True)
            return
        try:
            validate_delete(whisper, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        # Nothing destructive is one tap (guide § Controls): the confirm card
        # does the delete, and redraws this DM with its buttons gone.
        view = WhisperDeleteConfirmView(
            self.bot, self.whisper_id, invoker_id=interaction.user.id,
            dm_message=interaction.message,
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


class WhisperReplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:reply:(?P<id>\d+)"),
):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Reply",
                style=discord.ButtonStyle.secondary,
                custom_id=f"whisper:reply:{whisper_id}",
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperReplyButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("❌ Whisper not found.", ephemeral=True)
            return
        reply_count = await asyncio.to_thread(
            _do_count_replies, self.bot.ctx.db_path, self.whisper_id
        )
        try:
            validate_reply(
                whisper,
                invoker_id=interaction.user.id,
                reply_count=reply_count,
            )
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        await interaction.response.send_modal(
            WhisperReplyModal(self.bot, self.whisper_id)
        )


class WhisperReportButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:report:(?P<id>\d+)"),
):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Report",
                style=discord.ButtonStyle.secondary,
                custom_id=f"whisper:report:{whisper_id}",
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperReportButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("❌ Whisper not found.", ephemeral=True)
            return
        if interaction.user.id != whisper.target_id:
            await interaction.response.send_message(
                "❌ Only the recipient can report a whisper.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            WhisperReportModal(self.bot, self.whisper_id)
        )


# ── Reply / Report modals ────────────────────────────────────────────────────


async def _post_mod_log(
    bot: Bot, guild: discord.Guild | None, cfg: WhisperConfig,
    items: list[discord.ui.Item[Any]],
) -> None:
    """Mirror a notice to the guild's mod-log channel, if one is set.
    Best-effort: the dashboard's audit log is canonical."""
    if not cfg.log_channel_id or guild is None:
        return
    log_channel = guild.get_channel(cfg.log_channel_id)
    if not isinstance(log_channel, discord.TextChannel):
        return
    try:
        await log_channel.send(
            view=card(items), allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.warning("Failed to post a whisper notice to the mod log")


class WhisperReplyModal(discord.ui.Modal, title="Reply Anonymously"):
    reply_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.long,
        required=True,
        max_length=1000,
        placeholder="Your identity is logged by admins for moderation.",
    )

    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.whisper_id = whisper_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message(
                "❌ Whisper not found.", ephemeral=True
            )
            return
        reply_count = await asyncio.to_thread(
            _do_count_replies, self.bot.ctx.db_path, self.whisper_id
        )
        try:
            validate_reply(
                whisper,
                invoker_id=interaction.user.id,
                reply_count=reply_count,
            )
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        to_user_id = (
            whisper.sender_id
            if interaction.user.id == whisper.target_id
            else whisper.target_id
        )

        content = str(self.reply_input.value).strip()
        if not content:
            await interaction.response.send_message(
                "❌ Reply can't be empty.", ephemeral=True
            )
            return

        # No-contact gate on replies as well as sends. A whisper delivered
        # *before* the pair was added is still an open two-way channel — the
        # reply button sits in a DM that never expires — so gating only the
        # send would leave every pre-existing thread live. Symmetric, like the
        # rule itself: it does not matter which of the two is replying.
        # A recipient on a Take a Break gets no bot DMs: the same quiet
        # success, so a break can't be told from an ordinary delivery.
        if await asyncio.to_thread(
            no_contact_service.check_and_record,
            self.bot.ctx.db_path,
            whisper.guild_id,
            actor_id=interaction.user.id,
            target_id=to_user_id,
            surface=SURFACE_WHISPER,
        ) or await asyncio.to_thread(
            is_on_break, self.bot.ctx.db_path, whisper.guild_id, to_user_id
        ):
            # Still write the reply row, then stop. It is never delivered and
            # never reaches the mod log, so she sees nothing — but the row is
            # what ``_do_count_replies`` reads, and the one-reply-per-whisper
            # cap has to trip for him exactly as it would on a real thread.
            # Skipping the insert made a second press succeed where a genuine
            # one returns "already replied", so pressing Reply twice told him
            # the thread was gated.
            await asyncio.to_thread(
                _do_insert_reply,
                self.bot.ctx.db_path,
                whisper_id=self.whisper_id,
                from_user_id=interaction.user.id,
                to_user_id=to_user_id,
                content=content,
            )
            await interaction.response.send_message(
                REPLY_CONFIRMATION, ephemeral=True
            )
            return

        # Persist first so we have the reply_id for the report button.
        reply_id = await asyncio.to_thread(
            _do_insert_reply,
            self.bot.ctx.db_path,
            whisper_id=self.whisper_id,
            from_user_id=interaction.user.id,
            to_user_id=to_user_id,
            content=content,
        )

        recipient = interaction.client.get_user(to_user_id) or await interaction.client.fetch_user(to_user_id)  # type: ignore[attr-defined]
        # The reply is anonymous as to its *author*, not its origin: the
        # recipient sent the original whisper, so naming the server tells
        # them nothing they don't know and keeps DM branding consistent.
        origin = interaction.client.get_guild(whisper.guild_id)  # type: ignore[attr-defined]
        sent = await send_branded_dm(
            recipient,
            db_path=self.bot.ctx.db_path,
            guild=origin,
            layout=card(build_reply_dm_layout(
                whisper_id=self.whisper_id,
                whisper_message=whisper.message,
                reply_content=content,
                reply_id=reply_id,
            )),
        )
        if sent is None:
            await asyncio.to_thread(_do_delete_reply, self.bot.ctx.db_path, reply_id)
            await interaction.response.send_message(
                "❌ Couldn't deliver — they have DMs disabled.", ephemeral=True
            )
            return

        # Post reply to mod log (best-effort — don't fail the reply if this fails)
        try:
            cfg = await asyncio.to_thread(
                _load_config, self.bot.ctx.db_path, whisper.guild_id
            )
            guild = self.bot.get_guild(whisper.guild_id)
            if cfg.log_channel_id and guild is not None:
                items = build_reply_audit_layout(
                    whisper_id=self.whisper_id,
                    from_user_id=interaction.user.id,
                    to_user_id=to_user_id,
                    content=content,
                    name_fn=await build_name_fn(
                        guild=guild,
                        db_path=self.bot.ctx.db_path,
                        guild_id=whisper.guild_id,
                        user_ids=[interaction.user.id, to_user_id],
                    ),
                )
                await _post_mod_log(self.bot, guild, cfg, items)
        except Exception:  # noqa: BLE001 — best-effort mod-log post spans a name lookup, card build and Discord send; must not fail the reply itself
            log.warning("Failed to post whisper reply to mod log")

        await interaction.response.send_message(
            REPLY_CONFIRMATION, ephemeral=True
        )


class WhisperReportModal(discord.ui.Modal, title="Report Whisper"):
    reason_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.long,
        required=False,
        max_length=500,
    )

    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.whisper_id = whisper_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message(
                "❌ Whisper not found.", ephemeral=True
            )
            return
        if interaction.user.id != whisper.target_id:
            await interaction.response.send_message(
                "❌ Only the recipient can report a whisper.", ephemeral=True
            )
            return

        reason = str(self.reason_input.value).strip() or "(no reason provided)"

        # Always persist the report — it surfaces on the web mod dashboard
        # regardless of whether a Discord mod-log channel is configured.
        inserted = await asyncio.to_thread(
            _do_insert_report,
            self.bot.ctx.db_path,
            whisper_id=whisper.id,
            reporter_id=interaction.user.id,
            reason=reason,
        )
        if not inserted:
            await interaction.response.send_message(
                "❌ You've already reported this whisper.", ephemeral=True
            )
            return

        # Best-effort Discord mod-log post (optional; web dashboard is canonical).
        cfg = await asyncio.to_thread(
            _load_config, self.bot.ctx.db_path, whisper.guild_id
        )
        if cfg.log_channel_id and interaction.guild is not None:
            items = build_report_audit_layout(
                whisper=whisper,
                reason=reason,
                name_fn=await build_name_fn(
                    guild=interaction.guild,
                    db_path=self.bot.ctx.db_path,
                    guild_id=whisper.guild_id,
                    user_ids=[whisper.sender_id, whisper.target_id],
                ),
            )
            await _post_mod_log(self.bot, interaction.guild, cfg, items)
        await interaction.response.send_message(
            "Report submitted to moderators.", ephemeral=True
        )


class WhisperReportReplyModal(discord.ui.Modal, title="Report Reply"):
    reason_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.long,
        required=False,
        max_length=500,
    )

    def __init__(self, bot: Bot, reply_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.reply_id = reply_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reply = await asyncio.to_thread(
            _do_get_reply, self.bot.ctx.db_path, self.reply_id
        )
        if reply is None:
            await interaction.response.send_message("❌ Reply not found.", ephemeral=True)
            return
        if interaction.user.id != reply.to_user_id:
            await interaction.response.send_message(
                "❌ Only the recipient can report a reply.", ephemeral=True
            )
            return

        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, reply.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("❌ Whisper not found.", ephemeral=True)
            return

        reason = str(self.reason_input.value).strip() or "(no reason provided)"

        # Persist first — the web dashboard surfaces this regardless of any
        # Discord mod-log channel configuration.
        inserted = await asyncio.to_thread(
            _do_insert_reply_report,
            self.bot.ctx.db_path,
            reply_id=self.reply_id,
            reporter_id=interaction.user.id,
            reason=reason,
        )
        if not inserted:
            await interaction.response.send_message(
                "❌ You've already reported this reply.", ephemeral=True
            )
            return

        cfg = await asyncio.to_thread(
            _load_config, self.bot.ctx.db_path, whisper.guild_id
        )
        if cfg.log_channel_id and interaction.guild is not None:
            items = build_reply_report_audit_layout(
                reply=reply,
                reporter_id=interaction.user.id,
                reason=reason,
                name_fn=await build_name_fn(
                    guild=interaction.guild,
                    db_path=self.bot.ctx.db_path,
                    guild_id=whisper.guild_id,
                    user_ids=[reply.from_user_id, interaction.user.id],
                ),
            )
            await _post_mod_log(self.bot, interaction.guild, cfg, items)
        await interaction.response.send_message(
            "Reply reported to moderators.", ephemeral=True
        )


class WhisperReportReplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:report_reply:(?P<id>\d+)"),
):
    def __init__(self, bot: Bot, reply_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Report",
                style=discord.ButtonStyle.secondary,
                custom_id=f"whisper:report_reply:{reply_id}",
            )
        )
        self.bot = bot
        self.reply_id = reply_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperReportReplyButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        reply = await asyncio.to_thread(
            _do_get_reply, self.bot.ctx.db_path, self.reply_id
        )
        if reply is None:
            await interaction.response.send_message("❌ Reply not found.", ephemeral=True)
            return
        if interaction.user.id != reply.to_user_id:
            await interaction.response.send_message(
                "❌ Only the recipient can report a reply.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            WhisperReportReplyModal(self.bot, self.reply_id)
        )


# ── Delete confirm (from the DM or the inbox) ────────────────────────────────


class WhisperDeleteConfirmView(MemberScoped, ExpiringLayoutView):
    """The gate in front of Delete. **Paradigm 10.**

    Yes soft-deletes and redraws where the press came from — the DM with
    its buttons gone, or the inbox back on its list — then turns this card
    into a receipt. Cancel deletes the confirm and leaves everything as it
    was (guide § Words: Cancel is silent).
    """

    SCOPE_DENIAL = None

    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        invoker_id: int,
        dm_message: discord.Message | None = None,
        inbox: WhisperInboxView | None = None,
    ) -> None:
        super().__init__(timeout=_PICKER_TIMEOUT)
        self.bot = bot
        self.whisper_id = whisper_id
        self.scope_user_id = invoker_id
        self._dm_message = dm_message
        self._inbox = inbox
        for item in build_delete_confirm_layout(whisper_id):
            self.add_item(item)
        bind(self, DELETE_CONFIRM_ID, self._on_yes)
        bind(self, DELETE_CANCEL_ID, self._on_cancel)

    async def _on_yes(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        self.stop()
        if whisper is None:
            await interaction.response.edit_message(
                view=card(build_line_layout("❌ Whisper not found."))
            )
            return
        try:
            validate_delete(whisper, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.edit_message(view=card(build_line_layout(e.message)))
            return
        await asyncio.to_thread(_do_soft_delete, self.bot.ctx.db_path, self.whisper_id)
        await interaction.response.edit_message(view=card(build_deleted_layout(self.whisper_id)))

        if self._inbox is not None:
            await self._inbox.after_delete(self.whisper_id)
        deleted = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if deleted is not None:
            await _redraw_dm(self.bot, deleted, message=self._dm_message)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


# ── Guess outcome helper + pickers ───────────────────────────────────────────


async def _notify_sender_of_guess(
    bot: Bot,
    guild: discord.Guild,
    cfg: WhisperConfig,
    whisper: Whisper,
    *,
    guessed_id: int,
    outcome: GuessOutcome,
) -> None:
    """Queue the sender's one line about the guess just made on their whisper.

    Best-effort and last in the flow, after the target's own response. The
    gates live in ``sender_feedback_wanted`` (guild dial, sender still in the
    pool) and are read again when the batch goes out. Held, not sent: every
    guess on this whisper over the next ten minutes arrives as one DM
    (``_deliver_guess_batch``). The guessed member rides as ``actor_id`` so an
    erasure takes the row with them.
    """
    sender = guild.get_member(whisper.sender_id)
    if sender is None:
        return
    if not sender_feedback_wanted(cfg, sender_role_ids={r.id for r in sender.roles}):
        return

    def _queue() -> None:
        with open_db(bot.ctx.db_path) as conn:
            enqueue(
                conn,
                kind=GUESS_BATCH_KIND,
                guild_id=guild.id,
                user_id=whisper.sender_id,
                group_key=whisper.id,
                actor_id=guessed_id,
                payload=guess_payload(outcome),
            )

    try:
        await asyncio.to_thread(_queue)
    except Exception:  # noqa: BLE001 — the guess already landed; the sender's note is a courtesy
        log.warning("Failed to queue the sender note for a guess on #%s", whisper.id, exc_info=True)


async def _deliver_guess_batch(
    bot: Bot, guild: discord.Guild, sender: discord.Member, rows: list[PendingDM]
) -> None:
    """One DM for every guess on a whisper over the batch window.

    Each guessed member is named by resolved display name — except when the
    sender holds a no-contact pair with them, checked now, where the name
    degrades to "someone": naming a blocked party to the other side is
    contact, and the degraded line reads the same as any other wrong guess.
    """
    cfg = await asyncio.to_thread(_load_config, bot.ctx.db_path, guild.id)
    if not sender_feedback_wanted(cfg, sender_role_ids={r.id for r in sender.roles}):
        return
    guessed = unique_actors(rows)

    def _blocked() -> set[int]:
        with open_db(bot.ctx.db_path) as conn:
            return {
                uid for uid in guessed
                if no_contact_service.is_no_contact_conn(conn, guild.id, sender.id, uid)
            }

    blocked = await asyncio.to_thread(_blocked)
    name_fn = await build_name_fn(
        guild=guild, db_path=bot.ctx.db_path, guild_id=guild.id,
        user_ids=[uid for uid in guessed if uid not in blocked],
    )
    items = build_guess_digest_layout(
        whisper_id=rows[0].group_key,
        guesses=[
            (
                "someone" if r.actor_id in blocked else name_fn(r.actor_id),
                outcome_from_payload(r.payload),
            )
            for r in rows
        ],
    )
    try:
        await send_branded_dm(
            sender,
            db_path=bot.ctx.db_path,
            guild=guild,
            layout=card(items),
        )
    except discord.HTTPException:
        log.warning("Failed to DM whisper sender about guesses on #%s", rows[0].group_key)


async def _handle_guess_outcome(
    interaction: discord.Interaction,
    bot: Bot,
    whisper: Whisper,
    guessed_id: int,
) -> None:
    """Consume the guess and turn the picker into its result card.

    The picker is a V2 message with no ``content`` to edit, so every
    answer — a refusal, a race lost, the outcome — is a card of its own.
    """
    try:
        outcome = evaluate_guess(
            whisper, guesser_id=interaction.user.id, guessed_id=guessed_id
        )
    except GuessValidationError as e:
        await interaction.response.edit_message(view=card(build_line_layout(e.message)))
        return

    consumed = await asyncio.to_thread(
        _do_record_guess,
        bot.ctx.db_path,
        whisper_id=whisper.id,
        guessed_id=guessed_id,
        correct=outcome.correct,
    )
    if not consumed:
        await interaction.response.edit_message(
            view=card(build_line_layout("❌ This whisper was solved by another tab."))
        )
        return

    guild = interaction.guild or bot.get_guild(whisper.guild_id)
    cfg = await asyncio.to_thread(_load_config, bot.ctx.db_path, whisper.guild_id)
    await interaction.response.edit_message(view=card(build_guess_result_layout(outcome)))

    if outcome.correct:
        # Quest hook: solving the guessing game. Fires only after the
        # race-consumed check, so a two-tab solve pays once. The solved feed
        # post already names the target, so this leaks nothing new.
        from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

        await fire_member_trigger(
            bot, whisper.guild_id, interaction.user.id, "whisper_guess",
            occurrence=str(whisper.id),
        )
        if guild:
            feed_channel = guild.get_channel(cfg.channel_id)
            if isinstance(feed_channel, discord.TextChannel):
                name_fn = await build_name_fn(
                    guild=guild, db_path=bot.ctx.db_path, guild_id=whisper.guild_id,
                    user_ids=[whisper.target_id],
                )
                try:
                    await feed_channel.send(
                        view=card(build_solved_feed_layout(whisper.target_id, name_fn)),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.warning("Failed to post solved message to feed")
    # Redraw the DM from the row as it stands now: the card carries the
    # guesses-left count, so a wrong guess stales it, and a solve or the last
    # guess also takes the Guess button away. After the answer above —
    # opening the DM, fetching it and editing it is up to three REST calls,
    # too many to spend inside Discord's 3 s window.
    fresh = await asyncio.to_thread(_do_load_whisper, bot.ctx.db_path, whisper.id)
    if fresh is not None:
        await _redraw_dm(bot, fresh)
    if guild is not None:
        await _notify_sender_of_guess(
            bot, guild, cfg, whisper, guessed_id=guessed_id, outcome=outcome,
        )


def _selected_values(view: discord.ui.LayoutView, custom_id: str) -> list[Any]:
    """The picked values of the select carrying ``custom_id``."""
    for item in view.walk_children():
        if getattr(item, "custom_id", None) == custom_id:
            return list(getattr(item, "values", []))
    return []


class WhisperGuessNativeView(MemberScoped, ExpiringLayoutView):
    """Native member picker (avatars + presence dots) for the guess flow.
    **Paradigm 9 — game turn card.**

    Auto-populated user selects only resolve guild members inside a *guild*
    interaction, so this is used from in-guild surfaces (the inbox). The DM
    Guess button falls back to the paged string picker below. Because the
    native picker isn't limited to the opt-in pool, the selection is
    validated before any guess attempt is consumed.
    """

    SCOPE_DENIAL = None

    def __init__(self, bot: Bot, whisper: Whisper, *, invoker_id: int) -> None:
        super().__init__(timeout=_PICKER_TIMEOUT)
        self.bot = bot
        self.whisper_id = whisper.id
        self.scope_user_id = invoker_id
        for item in build_guess_native_picker_layout(whisper):
            self.add_item(item)
        bind(self, GUESS_PICK_ID, self._on_pick)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = _selected_values(self, GUESS_PICK_ID)
        if not values:
            await interaction.response.defer()
            return
        guessed = values[0]
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        self.stop()
        if whisper is None:
            await interaction.response.edit_message(
                view=card(build_line_layout("❌ Whisper not found."))
            )
            return
        if whisper.solved:
            await interaction.response.edit_message(
                view=card(build_line_layout(ERROR_GUESS_ALREADY_SOLVED))
            )
            return
        if whisper.guesses_left <= 0:
            await interaction.response.edit_message(
                view=card(build_line_layout(ERROR_GUESS_NO_ATTEMPTS))
            )
            return
        # A stray pick — a bot, or a member outside the opt-in pool — must not
        # burn an attempt: the native picker offers the whole server, and the
        # DM-side string picker only ever offered the pool. Refuse before
        # anything is consumed so the two entry points play by one rule.
        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, whisper.guild_id)
        rejection = guess_candidate_rejection(
            cfg,
            candidate_role_ids={r.id for r in getattr(guessed, "roles", [])},
            candidate_is_bot=bool(getattr(guessed, "bot", False)),
        )
        if rejection is not None:
            await interaction.response.edit_message(view=card(build_line_layout(rejection)))
            return
        await _handle_guess_outcome(interaction, self.bot, whisper, guessed.id)


_GUESS_PAGE_SIZE = 25


class _WhisperFilterModal(discord.ui.Modal, title="Filter Names"):
    query: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Search",
        placeholder="Type a name…",
        required=True,
        max_length=50,
    )

    def __init__(self, parent: WhisperGuessListView) -> None:
        super().__init__()
        self._parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._parent.apply_filter(self.query.value.strip())
        await interaction.response.edit_message(view=self._parent)


class WhisperGuessListView(MemberScoped, ExpiringLayoutView):
    """The DM-side guess picker: the opt-in pool, paged and filterable.
    **Paradigm 9 — game turn card.** Re-rendered in place on every page
    turn and filter."""

    SCOPE_DENIAL = None

    def __init__(
        self,
        bot: Bot,
        whisper: Whisper,
        members: Sequence[discord.Member],
        *,
        invoker_id: int,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=_PICKER_TIMEOUT)
        self.bot = bot
        self.whisper = whisper
        self.whisper_id = whisper.id
        self.scope_user_id = invoker_id
        self._all_members = list(members)
        self._display_members = self._all_members
        self._filter_query = ""
        self._page = page
        self.render()

    def _page_count(self) -> int:
        return max(1, (len(self._display_members) + _GUESS_PAGE_SIZE - 1) // _GUESS_PAGE_SIZE)

    def apply_filter(self, query: str) -> None:
        self._display_members = (
            fuzzy_score_members(self._all_members, query) if query else self._all_members
        )
        self._filter_query = query
        self._page = 0
        self.render()

    def render(self) -> None:
        self.clear_items()
        page_count = self._page_count()
        start = self._page * _GUESS_PAGE_SIZE
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in self._display_members[start:start + _GUESS_PAGE_SIZE]
        ]
        items = build_guess_list_picker_layout(
            self.whisper, options,
            placeholder=member_picker_placeholder(
                filter_query=self._filter_query,
                display_count=len(self._display_members),
                page=self._page,
                page_count=page_count,
                base="Pick the sender…",
            ),
            page=self._page, page_count=page_count, filter_query=self._filter_query,
        )
        for item in items:
            self.add_item(item)
        bind(self, GUESS_PICK_ID, self._on_pick)
        bind(self, GUESS_FILTER_ID, self._on_filter)
        if page_count > 1:
            bind(self, GUESS_PREV_ID, self._on_prev)
            bind(self, GUESS_NEXT_ID, self._on_next)
        if self._filter_query:
            bind(self, GUESS_CLEAR_ID, self._on_clear_filter)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = _selected_values(self, GUESS_PICK_ID)
        if not values or values[0] == "__none__":
            await interaction.response.defer()
            return
        guessed_id = int(values[0])
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        self.stop()
        if whisper is None:
            await interaction.response.edit_message(
                view=card(build_line_layout("❌ Whisper not found."))
            )
            return
        if whisper.solved:
            await interaction.response.edit_message(
                view=card(build_line_layout(ERROR_GUESS_ALREADY_SOLVED))
            )
            return
        if whisper.guesses_left <= 0:
            await interaction.response.edit_message(
                view=card(build_line_layout(ERROR_GUESS_NO_ATTEMPTS))
            )
            return
        await _handle_guess_outcome(interaction, self.bot, whisper, guessed_id)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        self.render()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self._page = min(self._page_count() - 1, self._page + 1)
        self.render()
        await interaction.response.edit_message(view=self)

    async def _on_filter(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_WhisperFilterModal(self))

    async def _on_clear_filter(self, interaction: discord.Interaction) -> None:
        self.apply_filter("")
        await interaction.response.edit_message(view=self)


# ── Shared share side-effects (feed channel update) ──────────────────────────


async def _share_side_effects(bot: Bot, whisper: Whisper) -> str | None:
    """Delete the original 'someone sent X a whisper' announcement and post the
    full message to the feed channel. Best-effort — logs but doesn't raise.
    Returns the new feed post's jump link when the post landed."""
    guild = bot.get_guild(whisper.guild_id)
    if guild is None:
        return None
    cfg = await asyncio.to_thread(_load_config, bot.ctx.db_path, whisper.guild_id)
    feed_channel = guild.get_channel(cfg.channel_id)
    if not isinstance(feed_channel, discord.TextChannel):
        return None
    name_fn = await build_name_fn(
        guild=guild,
        db_path=bot.ctx.db_path,
        guild_id=whisper.guild_id,
        user_ids=[whisper.target_id],
    )
    if whisper.channel_msg_id:
        try:
            old = await feed_channel.fetch_message(whisper.channel_msg_id)
            await old.delete()
        except discord.HTTPException:
            log.warning("Failed to delete original announcement on share")
    try:
        new_msg = await feed_channel.send(
            view=card(build_share_feed_layout(whisper, name_fn)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await asyncio.to_thread(
            _do_set_message_ids,
            bot.ctx.db_path,
            whisper.id,
            channel_msg_id=new_msg.id,
            dm_msg_id=whisper.dm_msg_id or 0,
        )
        cog = bot.get_cog("WhisperCog")
        if isinstance(cog, WhisperCog):
            spawn(cog.refresh_whisper_launcher(whisper.guild_id), label="whisper launcher refresh", logger=log)
        return new_msg.jump_url
    except discord.HTTPException:
        log.warning("Failed to post share announcement to feed")
        return None


# ── Inbox (received + sent): a paged list, and the opened whisper ────────────


class _WhisperInboxFilterModal(discord.ui.Modal, title="Filter Whispers"):
    query: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Search by content",
        placeholder="Type a few words from the message…",
        required=True,
        max_length=100,
    )

    def __init__(self, parent: WhisperInboxView) -> None:
        super().__init__()
        self._parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._parent.apply_filter(str(self.query.value).strip())
        await interaction.response.edit_message(view=self._parent)


class WhisperInboxView(MemberScoped, ExpiringLayoutView):
    """The whisper inbox. **Paradigm 6 — list with per-row actions.**

    ``mode='received'`` is the recipient's inbox, ``'sent'`` the sender's
    own list. Two panes on one ephemeral message: the list (ten rows a
    page, an Open beside each, Filter and the arrows below) and the opened
    whisper (its text, status, actions, and Back to the page and filter
    you left). Ben, 2026-09-30: "open the whisper in a new pane, have a
    back button". State lives on the view; every press re-renders it in
    place.
    """

    SCOPE_DENIAL = "❌ This inbox isn't yours."

    def __init__(
        self,
        bot: Bot,
        whispers: list[Whisper],
        *,
        invoker_id: int,
        mode: str = "received",
        name_fn: NameFn | None = None,
    ) -> None:
        super().__init__(timeout=_INBOX_TIMEOUT)
        self.bot = bot
        self.scope_user_id = invoker_id
        self._mode = mode
        # render() is sync (it runs on every press), so the resolver is
        # prefetched by the caller rather than looked up here.
        self._name_fn: NameFn = name_fn or mention
        self._all: list[Whisper] = list(whispers)
        self._display: list[Whisper] = list(whispers)
        self._filter_query = ""
        self._page = 0
        self._open_id: int | None = None
        self.render()

    # ── helpers ────────────────────────────────────────────────────────────

    def _opened(self) -> Whisper | None:
        if self._open_id is None:
            return None
        return next((w for w in self._all if w.id == self._open_id), None)

    def _page_count(self) -> int:
        return max(1, (len(self._display) + INBOX_PAGE_SIZE - 1) // INBOX_PAGE_SIZE)

    def _page_slice(self) -> list[Whisper]:
        start = self._page * INBOX_PAGE_SIZE
        return self._display[start : start + INBOX_PAGE_SIZE]

    def apply_filter(self, query: str) -> None:
        self._display = filter_whispers_by_message(self._all, query) if query else list(self._all)
        self._filter_query = query
        self._page = 0
        self._open_id = None
        self.render()

    async def after_delete(self, whisper_id: int) -> None:
        """Drop a whisper the confirm card just deleted and redraw the list.
        The confirm is its own message, so this edits the inbox by the
        handle ``ExpiringLayoutView`` already keeps."""
        self._all, self._display, self._page, _ = recompute_inbox_after_delete(
            all_whispers=self._all,
            display_whispers=self._display,
            deleted_id=whisper_id,
            page=self._page,
            page_size=INBOX_PAGE_SIZE,
        )
        self._open_id = None
        self.render()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.warning("Failed to redraw the whisper inbox after a delete")

    # ── rendering ──────────────────────────────────────────────────────────

    def render(self) -> None:
        self.clear_items()
        opened = self._opened()
        if opened is not None:
            self._render_detail(opened)
        else:
            self._render_list()

    def _render_list(self) -> None:
        page = self._page_slice()
        items = build_inbox_list_layout(
            page,
            mode=self._mode,
            total=len(self._all),
            shown=len(self._display),
            page=self._page,
            page_count=self._page_count(),
            filter_query=self._filter_query,
            name_fn=self._name_fn,
        )
        for item in items:
            self.add_item(item)
        if not self._all:
            return
        for w in page:
            bind(self, INBOX_OPEN_ID_TEMPLATE.format(whisper_id=w.id), self._opener(w.id))
        bind(self, INBOX_FILTER_ID, self._on_filter)
        if self._page_count() > 1:
            bind(self, INBOX_PREV_ID, self._on_prev)
            bind(self, INBOX_NEXT_ID, self._on_next)
        if self._filter_query:
            bind(self, INBOX_CLEAR_ID, self._on_clear_filter)

    def _render_detail(self, whisper: Whisper) -> None:
        for item in build_inbox_detail_layout(
            whisper, mode=self._mode, name_fn=self._name_fn,
        ):
            self.add_item(item)
        handlers: dict[str, Callable[[discord.Interaction], Any]] = {
            "guess": self._on_guess,
            "share": self._on_share,
            "reply": self._on_reply,
            "report": self._on_report,
            "delete": self._on_delete,
        }
        for key in inbox_action_buttons(whisper, mode=self._mode):
            bind(self, INBOX_ACTION_IDS[key], handlers[key])
        bind(self, INBOX_BACK_ID, self._on_back)

    # ── list callbacks ─────────────────────────────────────────────────────

    def _opener(self, whisper_id: int) -> Callable[[discord.Interaction], Any]:
        async def _open(interaction: discord.Interaction) -> None:
            self._open_id = whisper_id
            self.render()
            await interaction.response.edit_message(view=self)
        return _open

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self._open_id = None
        self.render()
        await interaction.response.edit_message(view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        self.render()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self._page = min(self._page_count() - 1, self._page + 1)
        self.render()
        await interaction.response.edit_message(view=self)

    async def _on_filter(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_WhisperInboxFilterModal(self))

    async def _on_clear_filter(self, interaction: discord.Interaction) -> None:
        self.apply_filter("")
        await interaction.response.edit_message(view=self)

    # ── action callbacks ───────────────────────────────────────────────────

    async def _on_share(self, interaction: discord.Interaction) -> None:
        opened = self._opened()
        if opened is None:
            return
        try:
            validate_share(opened, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        # The feed repost is a fetch, a delete and a send — defer, then
        # redraw this pane through the interaction's stored handle.
        await interaction.response.defer()
        await asyncio.to_thread(
            _do_update_state, self.bot.ctx.db_path, opened.id, STATE_SHARED
        )
        await _share_side_effects(self.bot, opened)
        opened.state = STATE_SHARED
        self.render()
        await interaction.edit_original_response(view=self)
        # The DM loses its Share and Delete too.
        fresh = await asyncio.to_thread(_do_load_whisper, self.bot.ctx.db_path, opened.id)
        if fresh is not None:
            await _redraw_dm(self.bot, fresh)

    async def _on_delete(self, interaction: discord.Interaction) -> None:
        opened = self._opened()
        if opened is None:
            return
        try:
            validate_delete(opened, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        view = WhisperDeleteConfirmView(
            self.bot, opened.id, invoker_id=interaction.user.id, inbox=self,
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def _on_guess(self, interaction: discord.Interaction) -> None:
        opened = self._opened()
        if opened is None:
            return
        # Delegate to WhisperGuessButton's existing logic — it sends a new
        # ephemeral with the member picker, leaving the inbox intact.
        btn = WhisperGuessButton(self.bot, opened.id)
        await btn.callback(interaction)

    async def _on_reply(self, interaction: discord.Interaction) -> None:
        opened = self._opened()
        if opened is None:
            return
        reply_count = await asyncio.to_thread(
            _do_count_replies, self.bot.ctx.db_path, opened.id
        )
        try:
            validate_reply(
                opened,
                invoker_id=interaction.user.id,
                reply_count=reply_count,
            )
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        await interaction.response.send_modal(
            WhisperReplyModal(self.bot, opened.id)
        )

    async def _on_report(self, interaction: discord.Interaction) -> None:
        opened = self._opened()
        if opened is None:
            return
        if interaction.user.id != opened.target_id:
            await interaction.response.send_message(
                "❌ Only the recipient can report a whisper.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            WhisperReportModal(self.bot, opened.id)
        )


# ── Send form ────────────────────────────────────────────────────────────────


class WhisperSendModal(discord.ui.Modal, title="Send a Whisper"):
    """The whole send, in one form. **Paradigm 4 — request form.**

    Was pick-a-recipient (a paged, filterable list of the opt-in pool) then
    a compose modal. The two steps didn't depend on each other, so they are
    one modal: Discord's own member picker and the message box. The picker
    offers the whole server and can't mark who has opted in (Ben,
    2026-09-30: A+), so a pick outside the pool is refused at submit with
    the draft quoted back, and counted.
    """

    def __init__(self, cog: WhisperCog) -> None:
        super().__init__()
        self._cog = cog
        self.who: discord.ui.UserSelect[Any] = discord.ui.UserSelect(
            placeholder="Pick a member…", min_values=1, max_values=1, required=True,
        )
        self.add_item(discord.ui.Label(text="Who?", component=self.who))
        self.message_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            style=discord.TextStyle.long,
            required=True,
            max_length=MAX_MESSAGE_LENGTH,
            placeholder="Say it anonymously…",
        )
        self.add_item(discord.ui.Label(
            text="Your whisper",
            description="They get a few guesses at who sent it.",
            component=self.message_input,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Whisper can only be used in a server.", ephemeral=True
            )
            return
        values = list(self.who.values)
        if not values:
            await interaction.response.send_message(
                "❌ Pick someone to whisper.", ephemeral=True
            )
            return
        # A modal's resolved user may arrive as a plain User; the role
        # checks read the Member, so prefer the guild's own copy.
        member = interaction.guild.get_member(values[0].id)
        if member is None:
            await interaction.response.send_message(
                "❌ That member isn't in this server any more.", ephemeral=True
            )
            return
        content = str(self.message_input.value)
        await self._cog._send_impl(interaction, target=member, message=content)


# ── The launcher (persistent hub at the foot of the feed channel) ────────────


class WhisperLauncherView(discord.ui.LayoutView):
    """The feed channel's sticky launcher. **Paradigm 1 — hub panel.**

    ``art`` is the guild's lobby strip and footer with "Whisper" drawn in
    (``branding_service.PanelArt`` through ``with_lobby_title``); the bare
    instance registered at cog load carries none, since dispatch keys off
    the custom ids alone.
    """

    def __init__(self, bot: Bot, art: PanelArt | None = None) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        art = art or PanelArt(banner=None, footer=None)
        for item in build_launcher_layout(
            header_url=art.header, footer_url=art.footer,
            title_in_header=art.title_in_header,
        ):
            self.add_item(item)
        bind(self, SEND_ID, self._on_send_click)
        bind(self, INBOX_ID, self._on_check_click)
        bind(self, SENT_ID, self._on_check_sent_click)
        bind(self, HELP_ID, self._on_help_click)

    def _cog(self) -> WhisperCog | None:
        cog = self.bot.get_cog("WhisperCog")
        return cog if isinstance(cog, WhisperCog) else None

    async def _on_send_click(self, interaction: discord.Interaction) -> None:
        cog = self._cog()
        if cog is None:
            await interaction.response.send_message(
                "❌ Whisper cog isn't loaded. Tell an admin.", ephemeral=True
            )
            return
        await cog._open_send_form(interaction)

    async def _on_check_click(self, interaction: discord.Interaction) -> None:
        cog = self._cog()
        if cog is None:
            await interaction.response.send_message(
                "❌ Whisper cog isn't loaded. Tell an admin.", ephemeral=True
            )
            return
        await cog._open_inbox(interaction, mode="received")

    async def _on_check_sent_click(self, interaction: discord.Interaction) -> None:
        cog = self._cog()
        if cog is None:
            await interaction.response.send_message(
                "❌ Whisper cog isn't loaded. Tell an admin.", ephemeral=True
            )
            return
        await cog._open_inbox(interaction, mode="sent")

    async def _on_help_click(self, interaction: discord.Interaction) -> None:
        """The guild's Whisper doc, read now, as an ephemeral card."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "❌ Use this in the server.", ephemeral=True
            )
            return
        body = await asyncio.to_thread(
            load_doc_body, self.bot.ctx.db_path, guild.id, HELP_DOC_KEY
        )
        # An admin-written doc may carry a role or @everyone mention; the
        # card is an answer to one tap, never a ping.
        await interaction.response.send_message(
            view=card(build_help_layout(body)),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# ── Opt-in confirmation ──────────────────────────────────────────────────────


class WhisperOptinView(ExpiringLayoutView):
    """Ephemeral consent card shown by /whisper optin. **Paradigm 5.** The
    role is only granted once the member presses Opt In; Cancel is silent."""

    def __init__(self, bot: Bot, role: discord.Role) -> None:
        super().__init__(timeout=_PICKER_TIMEOUT)
        self.bot = bot
        self.role = role
        for item in build_optin_layout():
            self.add_item(item)
        bind(self, OPTIN_CONFIRM_ID, self.confirm)
        bind(self, OPTIN_CANCEL_ID, self.cancel)

    async def confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        if grant_block_reason(self.role, interaction.guild, feature="Whisper opt-in") is not None:
            await interaction.response.edit_message(view=card(build_line_layout(
                "❌ The Whisper role can't be handed out right now — "
                "ask an admin to check the Whisper config on the web dashboard."
            )))
            return
        try:
            await interaction.user.add_roles(self.role, reason="Whisper opt-in")  # type: ignore[union-attr]
        except discord.Forbidden:
            await interaction.response.edit_message(view=card(build_line_layout(
                "❌ I don't have permission to assign that role."
            )))
            return
        await interaction.response.edit_message(view=card(build_optin_done_layout()))

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


# ── Cog ──────────────────────────────────────────────────────────────────────

# Fallbacks matching WhisperConfig's defaults; actual enforcement reads
# cfg.cooldown_seconds / cfg.hourly_cap_per_target, configurable per-guild
# from the dashboard's Whisper config panel.
SEND_COOLDOWN_SECONDS = 30
SEND_PER_TARGET_HOURLY_CAP = 5


class WhisperCog(commands.Cog):
    whisper_group = app_commands.Group(name="whisper", description="Send anonymous whispers.")

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        # The channel-bottom Send / Inbox / Sent launcher, on the shared
        # machinery in core.sticky (per-guild lock, TTL id cache, known-guilds
        # fast path, post-before-delete, shielded placement). Until 2026-09
        # this cog carried the last-but-one hand-rolled copy: no debounce, a
        # threaded DB read for every message in every guild before it had
        # looked at the channel, and delete-then-post — a failed send left the
        # channel with no launcher at all. This cog now only says where the
        # ids live and what the launcher looks like.
        self.launcher = StickyPanel(
            "whisper launcher",
            bot,
            load_ids=self._launcher_ids,
            save_ids=self._save_launcher_ids,
            build=self._build_launcher,
            refresh_on_update=True,
        )
        self._last_send_at: dict[int, float] = {}  # sender_id -> ts
        self._target_sends: dict[tuple[int, int, int], list[float]] = {}  # (guild_id, sender_id, target_id) -> [ts...]

    # ── the launcher's sticky callbacks (core.sticky) ─────────────────────

    def _launcher_ids(self, guild_id: int) -> tuple[int, int]:
        """Where the launcher actually is, or (0, 0) for "not posted".

        Reads the launcher's own channel rather than ``channel_id`` so the
        placer's delete aims at the message's real channel after an admin
        repoints the feed. A launcher posted before ``launcher_channel_id``
        existed has a message id and no channel id — ``cog_load`` pins those
        to the feed channel (where they were posted) before any repoint can
        happen; the fallback here covers the same case read before boot ran.
        """
        cfg = _load_config(self.bot.ctx.db_path, guild_id)
        if not cfg.launcher_message_id:
            return 0, 0
        return cfg.launcher_channel_id or cfg.channel_id, cfg.launcher_message_id

    def _save_launcher_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        _do_set_launcher_ids(self.bot.ctx.db_path, guild_id, channel_id, message_id)

    async def _build_launcher(self, guild: discord.Guild) -> PanelContent:
        def _read() -> PanelArt:
            with open_db(self.bot.ctx.db_path) as conn:
                art = resolve_panel_art_conn(conn, guild.id)
            # Draws the strip's title on first use, after the connection closes.
            return with_lobby_title(art, TITLE)

        art = await asyncio.to_thread(_read)
        return PanelContent(layout=WhisperLauncherView(self.bot, art))

    async def cog_load(self) -> None:
        # The actor on a guess row is the member it named, not a sender, so
        # dm_batch must not drop it for no-contact: _deliver_guess_batch
        # degrades the name instead.
        register_handler(GUESS_BATCH_KIND, _deliver_guess_batch, recheck_actor=False)
        # Register persistent views so static-id buttons survive restart.
        self.bot.add_view(WhisperLauncherView(self.bot))
        # Register dynamic-id buttons so per-whisper Guess/Share/Delete
        # button clicks on existing DMs still route after a bot restart
        # (custom_ids embed the whisper_id).
        self.bot.add_dynamic_items(
            WhisperGuessButton,
            WhisperShareButton,
            WhisperDeleteButton,
            WhisperReplyButton,
            WhisperReportButton,
            WhisperReportReplyButton,
        )
        # Publish the launcher-guild set so the on_message listener rejects
        # the overwhelming majority of messages with a set lookup, not a DB
        # read — then make sure each of those guilds has its launcher at the
        # bottom of the feed. only_if_buried: a launcher that is already the
        # last message stays put across a restart instead of being churned.
        configured = await asyncio.to_thread(_do_launcher_guilds, self.bot.ctx.db_path)
        # A launcher posted before ``whisper_launcher_channel_id`` existed has
        # a message id and no channel; the bootstrap below only writes the
        # channel when it reposts, and a launcher already at the bottom never
        # reposts. Pin it now, while the feed channel is still where it lives,
        # so a later repoint deletes it there rather than in the new channel.
        await asyncio.to_thread(
            _do_backfill_launcher_channels, self.bot.ctx.db_path, configured
        )
        self.launcher.set_known_guilds(configured)
        sem = asyncio.Semaphore(5)

        async def _bootstrap_one(guild_id: int) -> None:
            async with sem:
                try:
                    await self.refresh_whisper_launcher(guild_id, only_if_buried=True)
                except Exception:
                    log.exception(
                        "Failed to bootstrap whisper launcher for guild %s", guild_id
                    )

        await asyncio.gather(
            *[_bootstrap_one(g.id) for g in self.bot.guilds if g.id in configured]
        )

    async def cog_unload(self) -> None:
        unregister_handler(GUESS_BATCH_KIND)
        self.launcher.cancel_all()

    async def refresh_whisper_launcher(
        self, guild_id: int, *, only_if_buried: bool = False
    ) -> None:
        """Move the launcher to the bottom of the configured whisper channel.

        Delegates to ``core.sticky`` — the per-guild lock, post-before-delete
        and the shielded placement all live there. Kept as a method because
        three call sites (send, share, the boot bootstrap) already speak it.
        ``only_if_buried`` skips the repost when the launcher is already the
        channel's last message.
        """
        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, guild_id)
        if cfg.channel_id == 0:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(cfg.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self.launcher.place(guild, channel, only_if_buried=only_if_buried)

    @commands.Cog.listener("on_guild_remove")
    async def _on_guild_remove(self, guild: discord.Guild) -> None:
        await asyncio.to_thread(self._clear_guild_config, guild.id)

    def _clear_guild_config(self, guild_id: int) -> None:
        from bot_modules.core.db_utils import delete_config_value  # noqa: PLC0415
        with open_db(self.bot.ctx.db_path) as conn:
            for key in (
                "whisper_role_id",
                "whisper_channel_id",
                "whisper_log_channel_id",
                "whisper_launcher_message_id",
                "whisper_launcher_channel_id",
            ):
                delete_config_value(conn, key, guild_id)
        self.launcher.forget(guild_id)

    @commands.Cog.listener("on_message")
    async def _on_message_launcher_bump(self, message: discord.Message) -> None:
        """Keep the launcher at the bottom of the whisper channel.

        One line now: the bot filter, the known-guilds gate, the TTL id cache
        and the debounce all live in ``core.sticky``.
        """
        await self.launcher.on_message(message)

    @commands.Cog.listener()
    async def on_whisper_config_change(self, guild_id: int) -> None:
        """A dashboard save of the Whisper config (``PUT /config/whisper``).

        The ``on_message`` fast path only knows the guilds read at boot, so a
        feed channel set afterwards would get no launcher until a restart —
        the hand-rolled listener this replaced re-read the config on every
        message and so never had the gap. Republish the set, then re-stick
        the launcher where the channel now points (``place`` deletes the old
        one through its stored channel if the feed was repointed).
        """
        configured = await asyncio.to_thread(_do_launcher_guilds, self.bot.ctx.db_path)
        self.launcher.set_known_guilds(configured)
        if guild_id not in configured:
            self.launcher.forget(guild_id)
            return
        await self.refresh_whisper_launcher(guild_id, only_if_buried=True)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_launcher_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear the launcher's ids if its channel was deleted."""
        await self.launcher.on_channel_delete(channel)

    async def _optin_impl(self, interaction: discord.Interaction) -> None:
        """Pure shared implementation, easy to test directly."""
        assert interaction.guild is not None
        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, interaction.guild.id)
        if cfg.role_id == 0:
            await interaction.response.send_message(
                "❌ Whisper role hasn't been configured yet.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(cfg.role_id)
        if role is None:
            await interaction.response.send_message(
                "❌ Whisper role no longer exists. Ask an admin to fix the config.",
                ephemeral=True,
            )
            return
        view = WhisperOptinView(self.bot, role)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def _optout_impl(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, interaction.guild.id)
        if cfg.role_id == 0:
            await interaction.response.send_message(
                "❌ Whisper role hasn't been configured yet.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(cfg.role_id)
        if role is not None:
            try:
                await interaction.user.remove_roles(role, reason="Whisper opt-out")  # type: ignore[union-attr]
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I don't have permission to remove that role.", ephemeral=True
                )
                return
        await interaction.response.send_message(
            view=card(build_optout_done_layout()), ephemeral=True
        )

    @whisper_group.command(name="optin", description="Opt in to send and receive whispers.")
    async def whisper_optin(self, interaction: discord.Interaction) -> None:
        await self._optin_impl(interaction)

    @whisper_group.command(name="optout", description="Opt out of whispers.")
    async def whisper_optout(self, interaction: discord.Interaction) -> None:
        await self._optout_impl(interaction)

    async def _open_inbox(self, interaction: discord.Interaction, *, mode: str) -> None:
        """The member's inbox (``received``) or sent list, as an ephemeral
        card. Shared by the launcher's two buttons and ``/whisper sent``."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.", ephemeral=True
            )
            return
        if mode == "received":
            whispers = await asyncio.to_thread(
                _do_list_received_in_states,
                self.bot.ctx.db_path,
                guild_id=interaction.guild.id,
                target_id=interaction.user.id,
                states=[STATE_PENDING, STATE_SHARED],
            )
        else:
            sent = await asyncio.to_thread(
                _do_list_sent,
                self.bot.ctx.db_path,
                guild_id=interaction.guild.id,
                sender_id=interaction.user.id,
            )
            whispers = [w for w in sent if not is_terminal_for_sender(w)]
        # Only the Sent list shows a name (the recipient's); the Inbox never
        # does, so it skips the lookup.
        name_fn: NameFn | None = None
        if mode != "received":
            name_fn = await build_name_fn(
                guild=interaction.guild,
                db_path=self.bot.ctx.db_path,
                guild_id=interaction.guild.id,
                user_ids=[w.target_id for w in whispers],
            )
        view = WhisperInboxView(
            self.bot, whispers, invoker_id=interaction.user.id, mode=mode, name_fn=name_fn,
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @whisper_group.command(
        name="sent",
        description="See the whispers you've sent in this server (active only).",
    )
    async def whisper_sent(self, interaction: discord.Interaction) -> None:
        await self._open_inbox(interaction, mode="sent")

    async def _refuse_pool_pick(
        self, interaction: discord.Interaction, *, message: str,
    ) -> None:
        """The send form's pick hasn't opted in: refuse with the draft quoted
        back, and count it (no text, no member id) so we can see how often it
        bites."""
        assert interaction.guild is not None
        try:
            await asyncio.to_thread(
                _do_count_pool_refusal, self.bot.ctx.db_path, interaction.guild.id,
            )
        except Exception:  # noqa: BLE001 — the count is telemetry; the refusal still goes out
            log.warning("Failed to count a whisper pool refusal", exc_info=True)
        await interaction.response.send_message(
            view=card(build_pool_refusal_layout(ERROR_TARGET_NEEDS_ROLE, message)),
            ephemeral=True,
        )

    async def _refuse_send(
        self, interaction: discord.Interaction, refusal: str, message: str,
    ) -> None:
        """An ordinary send refusal, with the draft quoted back. Never used
        for the no-contact / Take a Break paths, which answer as a success."""
        await interaction.response.send_message(
            view=card(build_send_refusal_layout(refusal, message)), ephemeral=True,
        )

    async def _send_impl(
        self,
        interaction: discord.Interaction,
        *,
        target: discord.Member,
        message: str,
    ) -> None:
        assert interaction.guild is not None
        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, interaction.guild.id)

        sender_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        target_role_ids = {r.id for r in getattr(target, "roles", [])}
        try:
            validate_send(
                cfg=cfg,
                sender_role_ids=sender_role_ids,
                target_role_ids=target_role_ids,
                sender_id=interaction.user.id,
                target_id=target.id,
                message=message,
            )
        except SendValidationError as e:
            if e.message == ERROR_TARGET_NEEDS_ROLE:
                await self._refuse_pool_pick(interaction, message=message)
                return
            await self._refuse_send(interaction, e.message, message)
            return

        import time as _t  # noqa: PLC0415
        now = _t.time()
        remaining = check_send_cooldown(
            self._last_send_at.get(interaction.user.id),
            now=now,
            cooldown_seconds=cfg.cooldown_seconds,
        )
        if remaining is not None:
            await self._refuse_send(interaction, format_cooldown_message(remaining), message)
            return

        rate_key = (interaction.guild.id, interaction.user.id, target.id)
        recent = prune_recent_target_sends(
            self._target_sends.get(rate_key, []), now=now,
        )
        if len(recent) >= cfg.hourly_cap_per_target:
            await self._refuse_send(
                interaction, format_hourly_cap_message(cfg.hourly_cap_per_target), message,
            )
            return
        self._last_send_at[interaction.user.id] = now
        self._target_sends[rate_key] = [*recent, now]

        if getattr(target, "is_timed_out", lambda: False)():
            await self._refuse_send(
                interaction, "❌ Can't whisper a member who's currently timed out.", message,
            )
            return

        feed_channel = interaction.guild.get_channel(cfg.channel_id)
        if not isinstance(feed_channel, discord.TextChannel):
            await self._refuse_send(
                interaction,
                "❌ Whisper feed channel is missing or invalid. Tell an admin to fix the config.",
                message,
            )
            return
        # No-contact gate. Deliberately the LAST check before the write: every
        # ordinary refusal above (missing role, cooldown, hourly cap, timed-out
        # target, broken feed channel) still reaches the sender exactly as it
        # normally would, so the only case that diverges is the one that would
        # otherwise have succeeded — and that case is made to look like success.
        # Telling him "blocked" would confirm she acted against him, which is
        # the escalation this feature exists to prevent.
        # A target on a Take a Break takes the same path: no DM reaches them,
        # and a refusal would tell the sender why.
        if await asyncio.to_thread(
            no_contact_service.check_and_record,
            self.bot.ctx.db_path,
            interaction.guild.id,
            actor_id=interaction.user.id,
            target_id=target.id,
            surface=SURFACE_WHISPER,
        ) or await asyncio.to_thread(
            is_on_break, self.bot.ctx.db_path, interaction.guild.id, target.id
        ):
            await interaction.response.send_message(
                SENT_CONFIRMATION,
                ephemeral=True,
            )
            # Fire the economy trigger too. A quest that failed to tick after
            # an apparently-delivered whisper would be a tell, and the payout
            # is the same one a real whisper would have earned — the rate caps
            # above already bound how often this path can be reached.
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            await fire_member_trigger(
                self.bot, interaction.guild.id, interaction.user.id, "whisper",
                occurrence=f"nc-{int(now * 1000)}",
            )
            return

        whisper_id = await asyncio.to_thread(
            _do_insert_whisper,
            self.bot.ctx.db_path,
            guild_id=interaction.guild.id,
            sender_id=interaction.user.id,
            target_id=target.id,
            message=message.strip(),
            guesses_left=cfg.guesses_per_whisper,
        )
        whisper = await asyncio.to_thread(_do_load_whisper, self.bot.ctx.db_path, whisper_id)
        if whisper is None:  # pragma: no cover — the row was just written
            await interaction.response.send_message(ERROR_BOT_DM_FAILED, ephemeral=True)
            return

        dm_msg = await send_branded_dm(
            target,
            db_path=self.bot.ctx.db_path,
            guild=interaction.guild,
            layout=card(build_whisper_dm_layout(whisper)),
        )
        if dm_msg is None:
            # rollback inserted row if DM fails
            await asyncio.to_thread(_do_delete_whisper, self.bot.ctx.db_path, whisper_id)
            await self._refuse_send(interaction, ERROR_BOT_DM_FAILED, message)
            return

        feed_msg = None
        try:
            # The recipient is already notified by the DM above (which is
            # awaited and rolls back on failure), so the feed post doesn't need
            # to ping. It names the recipient visibly, once, resolved.
            name_fn = await build_name_fn(
                guild=interaction.guild,
                db_path=self.bot.ctx.db_path,
                guild_id=interaction.guild.id,
                user_ids=[target.id],
            )
            feed_msg = await feed_channel.send(
                view=card(build_send_feed_layout(target.id, name_fn)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            spawn(self.refresh_whisper_launcher(interaction.guild.id), label="whisper launcher refresh", logger=log)
        except discord.HTTPException:
            log.warning("Failed to post whisper announcement to feed channel")

        await asyncio.to_thread(
            _do_set_message_ids,
            self.bot.ctx.db_path,
            whisper_id,
            channel_msg_id=feed_msg.id if feed_msg else 0,
            dm_msg_id=dm_msg.id,
        )

        await interaction.response.send_message(
            SENT_CONFIRMATION,
            ephemeral=True,
        )

        # Credit the sender's ``whisper`` economy quest trigger — one payout
        # per delivered whisper. Guarded/non-raising when the economy is off.
        from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

        await fire_member_trigger(
            self.bot, interaction.guild.id, interaction.user.id, "whisper",
            occurrence=str(whisper_id),
        )

    async def _open_send_form(self, interaction: discord.Interaction) -> None:
        """Open the send form. Used by both the launcher's Send button and
        the /whisper send slash command. The pre-checks that need no form
        (set up, role exists, the sender holds it) answer first; a modal
        must be the interaction's first response, so nothing defers."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Whisper can only be used in a server.", ephemeral=True
            )
            return
        cfg = await asyncio.to_thread(
            _load_config, self.bot.ctx.db_path, interaction.guild.id
        )
        if not is_configured(cfg):
            await interaction.response.send_message(ERROR_NOT_CONFIGURED, ephemeral=True)
            return
        role = interaction.guild.get_role(cfg.role_id)
        if role is None:
            await interaction.response.send_message(
                "❌ Whisper role no longer exists. Ask an admin to fix the config.",
                ephemeral=True,
            )
            return
        if cfg.role_id not in {r.id for r in getattr(interaction.user, "roles", [])}:
            await interaction.response.send_message(
                "❌ You need the Whisper role first. Use `/whisper optin` to join.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(WhisperSendModal(self))

    @whisper_group.command(
        name="send",
        description="Send an anonymous whisper — opens the send form.",
    )
    async def whisper_send(self, interaction: discord.Interaction) -> None:
        await self._open_send_form(interaction)


async def setup(bot: Bot) -> None:
    await bot.add_cog(WhisperCog(bot))
