"""Guess cog — the guess-the-member image game (Phase 2)."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import os
import socket
import tempfile
import time
import urllib.request
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse, urlunparse

import discord

from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.core.branding_service import PanelArt
from bot_modules.core.db_utils import open_db
from bot_modules.core.layout import ExpiringLayoutView, bind, card, disable_all
from bot_modules.core.role_safety import grant_block_reason
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.docs.help_card import load_doc_body
from bot_modules.duels.filters import contains_disallowed_content
from bot_modules.games.utils.lobby_art import (
    NO_ART,
    read_guild_lobby_art,
    read_lobby_art,
)
from bot_modules.games.guess import layouts
from bot_modules.games.guess.layouts import (
    build_confession_preview_layout,
    build_consent_layout,
    build_crop_editor_layout,
    build_help_layout,
    build_inspector_layout,
    build_leaderboard_layout,
    build_notice_layout,
    build_picker_layout,
    build_prompt_layout,
    build_round_layout,
    build_solved_layout,
    reveal_names,
)
from bot_modules.games.guess.submit_logic import (
    _has_guess_role,
    _validate_size,
    picker_candidates,
    submit_refusal,
    too_large_text,
)
from bot_modules.games.guess.models import BoundingBox, GuessConfig, GuessGuess, GuessRound
from bot_modules.shared.name_resolver import build_name_fn
from bot_modules.no_contact import service as no_contact_service
from bot_modules.no_contact.logic import (
    KIND_ATTEMPT,
    SURFACE_GUESS,
    SURFACE_GUESS_SUBMIT,
    guess_round_blocked_for,
    guess_scores_correct,
)
from bot_modules.games.guess.pipeline import (
    compute_padded_crop,
    enforce_min_size,
    move_crop_box,
    run_pipeline,
    zoom_crop_box,
)
from bot_modules.games.guess.crop_renderer import render_crop, render_crop_editor, render_reveal
from bot_modules.shared.quote_renderer import render_quote
from bot_modules.games.guess.expiry_service import (
    claim_expired_rounds,
    clear_expired_round,
    pending_expired_rounds,
)
from bot_modules.games.guess.cleanup_service import (
    ORIGINAL_MAX_AGE_DAYS,
    age_out_originals,
    guess_orig_dir,
    rollback_unposted_round,
    sweep_orphan_originals,
    withdraw_departed_member,
)
from bot_modules.games.guess.repo import (
    count_guesses_for_round,
    count_unique_guessers_for_round,
    count_user_guesses_for_round,
    flag_user_open_rounds_optout,
    record_guess_consent,
    withdraw_guess_consent,
    get_last_guess_by_user_for_round,
    get_round,
    get_top_guessers,
    get_top_posters,
    get_unsolved_round_ids,
    get_guess_config,
    insert_audit_event,
    insert_guess,
    insert_round,
    mark_round_solved,
    set_round_crop_box,
    set_round_original_path,
    set_round_reroll_count,
    set_guess_config_value,
    soft_delete_round,
    update_round_message,
)

# Hard cap on per-(user, round) guesses — kills brute-force-by-dropdown.
# Fallback matching GuessConfig's default; actual enforcement reads
# config.max_guesses_per_round, configurable per-guild from the dashboard.
MAX_GUESSES_PER_USER_ROUND = 5

# Maximum number of persistent GameViews to re-register at startup. Bounds the
# discord.py view-matching cost as the round backlog grows. Unsolved rounds
# only — a solved round's card carries no button.
_COG_LOAD_VIEW_CAP = 1000

# An ephemeral card can only be edited through its interaction for 15
# minutes, so a longer view timeout's grey-out edit would fail silently.
MAX_EPHEMERAL_TIMEOUT = 840

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.guess")

# Shared by the ordinary wrong-guess path and the no-contact gate: the
# refused guess must read identically to a genuine miss (see
# docs/specs/no_contact_spec.md).
WRONG_GUESS_TEXT = "❌ Not it. Keep trying!"
# ⌛, not ✅: the member was right, but the round was already over — the
# guide's glyph for "no longer active".
ALREADY_SOLVED_TEXT = "⌛ Right name — but someone already solved this one."
# A wrong pick on a round solved while the picker was open. It used to get
# "Not it. Keep trying!" on a round nobody could play any more.
SOLVED_WHILE_PICKING_TEXT = "⌛ Someone solved this round while you were picking."
# A round that timed out (``guess_round_lifetime_days``) while the member held a
# picker, or whose stale Guess button they pressed.
EXPIRED_TEXT = "⌛ This round has ended."


# Originals are persisted per-round under ``guess_orig_dir(db_path)`` and
# deleted after the first correct guess reveals them. Submitters are warned at
# submit time that the file is kept until the round is solved.

# Per-user submission rate limit (in-memory flood protection; resets on
# restart). Fallbacks matching GuessConfig's defaults; actual enforcement
# reads config.submit_max_per_window / config.submit_window_seconds,
# configurable per-guild from the dashboard's Guess config panel.
_SUBMIT_WINDOW_S = 3600
_SUBMIT_MAX_PER_WINDOW = 5
_submit_history: dict[int, list[float]] = {}


def _submit_rate_limited(
    user_id: int, *, max_per_window: int = _SUBMIT_MAX_PER_WINDOW, window_seconds: int = _SUBMIT_WINDOW_S
) -> bool:
    """Return True (and record the attempt) if the user has exceeded the
    submission cap within the rolling window."""
    now = time.time()
    hist = [t for t in _submit_history.get(user_id, []) if now - t < window_seconds]
    if len(hist) >= max_per_window:
        _submit_history[user_id] = hist
        return True
    hist.append(now)
    _submit_history[user_id] = hist
    return False


def _submit_gate(
    interaction: discord.Interaction,
    config: GuessConfig,
    *,
    content_type: str | None = None,
    size: int | None = None,
) -> str | None:
    """Both submit doors' one check (``submit_logic.submit_refusal``), with
    this submitter's member record and rate limiter filled in."""
    assert interaction.guild
    return submit_refusal(
        config,
        interaction.guild.get_member(interaction.user.id),
        rate_limited=lambda: _submit_rate_limited(
            interaction.user.id,
            max_per_window=config.submit_max_per_window,
            window_seconds=config.submit_window_seconds,
        ),
        content_type=content_type,
        size=size,
    )


def _validate_dimensions(image_bytes: bytes, min_px: int) -> tuple[bool, int, int]:
    from PIL import Image  # noqa: PLC0415
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    return (min(w, h) >= min_px, w, h)


# ── DB helpers (sync, called via asyncio.to_thread) ──────────────────────────

def _load_config(db_path: Path, guild_id: int) -> GuessConfig:
    with open_db(db_path) as conn:
        return get_guess_config(conn, guild_id)


def _do_insert_round(
    db_path: Path,
    *,
    guild_id: int,
    submitter_id: int,
    answer_id: int,
    channel_id: int,
    difficulty: str,
    candidate_count: int,
    round_type: str = "photo",
    confession_text: str = "",
    confession_prompt_text: str = "",
) -> int:
    with open_db(db_path) as conn:
        return insert_round(
            conn,
            guild_id=guild_id,
            submitter_id=submitter_id,
            answer_id=answer_id,
            channel_id=channel_id,
            difficulty=difficulty,
            candidate_count=candidate_count,
            round_type=round_type,
            confession_text=confession_text,
            confession_prompt_text=confession_prompt_text,
        )


def _do_update_round_message(
    db_path: Path, round_id: int, message_id: int, crop_url: str, crop_path: str
) -> None:
    with open_db(db_path) as conn:
        update_round_message(conn, round_id, message_id=message_id, crop_url=crop_url, crop_path=crop_path)


def _do_set_reroll_count(db_path: Path, round_id: int, count: int) -> None:
    with open_db(db_path) as conn:
        set_round_reroll_count(conn, round_id, count)


def _do_set_original_path(db_path: Path, round_id: int, original_path: str) -> None:
    with open_db(db_path) as conn:
        set_round_original_path(conn, round_id, original_path)


def _do_set_crop_box(
    db_path: Path, round_id: int, x1: float, y1: float, x2: float, y2: float
) -> None:
    with open_db(db_path) as conn:
        set_round_crop_box(conn, round_id, x1, y1, x2, y2)


def _do_get_top_posters(
    db_path: Path, guild_id: int
) -> list[tuple[int, int, int]]:
    with open_db(db_path) as conn:
        return get_top_posters(conn, guild_id)


def _do_get_top_guessers(
    db_path: Path, guild_id: int
) -> list[tuple[int, int]]:
    with open_db(db_path) as conn:
        return get_top_guessers(conn, guild_id)


def _do_load_round(db_path: Path, round_id: int) -> GuessRound | None:
    with open_db(db_path) as conn:
        return get_round(conn, round_id)


def _do_insert_guess(
    db_path: Path,
    *,
    round_id: int,
    guesser_id: int,
    guessed_user_id: int,
    correct: bool,
) -> None:
    with open_db(db_path) as conn:
        insert_guess(conn, round_id=round_id, guesser_id=guesser_id,
                     guessed_user_id=guessed_user_id, correct=correct)


def _do_get_last_guess(
    db_path: Path, round_id: int, guesser_id: int
) -> GuessGuess | None:
    with open_db(db_path) as conn:
        return get_last_guess_by_user_for_round(conn, round_id, guesser_id)


def _do_mark_solved(
    db_path: Path, round_id: int, *, solver_id: int
) -> tuple[int, int, int]:
    """Returns (rowcount, guess_count, unique_count). rowcount==0 means we lost
    the race — another correct guess marked solved first."""
    with open_db(db_path) as conn:
        guess_count = count_guesses_for_round(conn, round_id)
        unique_count = count_unique_guessers_for_round(conn, round_id)
        rowcount = mark_round_solved(
            conn, round_id,
            solver_id=solver_id,
            guesses_to_solve=guess_count,
            unique_guessers_to_solve=unique_count,
        )
    return rowcount, guess_count, unique_count


def _do_count_round_guesses(db_path: Path, round_id: int) -> tuple[int, int]:
    with open_db(db_path) as conn:
        return (
            count_guesses_for_round(conn, round_id),
            count_unique_guessers_for_round(conn, round_id),
        )


def _do_set_config(db_path: Path, guild_id: int, key: str, value: str) -> None:
    with open_db(db_path) as conn:
        set_guess_config_value(conn, guild_id, key, value)


def _do_load_unsolved_round_ids(db_path: Path, *, limit: int) -> list[int]:
    with open_db(db_path) as conn:
        return get_unsolved_round_ids(conn, limit=limit)


#: How long the role-loss listener waits for a jail or inactive move to
#: finish writing its hold (row after strip) before it decides.
HOLD_SETTLE_SECONDS = 5.0


def _is_held(db_path: Path, guild_id: int, user_id: int, role_ids: set[int]) -> bool:
    from bot_modules.roles.grant.audit_service import member_is_held

    with open_db(db_path) as conn:
        return member_is_held(conn, guild_id, user_id, role_ids)


def _do_flag_user_open_rounds_optout(
    db_path: Path, *, guild_id: int, user_id: int
) -> int:
    with open_db(db_path) as conn:
        return flag_user_open_rounds_optout(conn, guild_id=guild_id, user_id=user_id)


def _do_record_consent(
    db_path: Path, guild_id: int, user_id: int, when: float
) -> None:
    with open_db(db_path) as conn:
        record_guess_consent(
            conn, guild_id=guild_id, user_id=user_id, consented_at=when
        )


def _do_withdraw_consent(
    db_path: Path, guild_id: int, user_id: int, when: float
) -> int:
    with open_db(db_path) as conn:
        return withdraw_guess_consent(
            conn, guild_id=guild_id, user_id=user_id, withdrawn_at=when
        )


def _do_soft_delete_round(db_path: Path, round_id: int) -> None:
    with open_db(db_path) as conn:
        soft_delete_round(conn, round_id)


def _do_count_user_guesses(db_path: Path, round_id: int, guesser_id: int) -> int:
    with open_db(db_path) as conn:
        return count_user_guesses_for_round(conn, round_id, guesser_id)


def _do_count_guesses_for_round(db_path: Path, round_id: int) -> int:
    with open_db(db_path) as conn:
        return count_guesses_for_round(conn, round_id)


def _do_count_unique_guessers_for_round(db_path: Path, round_id: int) -> int:
    with open_db(db_path) as conn:
        return count_unique_guessers_for_round(conn, round_id)


def _do_audit(
    db_path: Path,
    *,
    guild_id: int,
    actor_id: int,
    action: str,
    round_id: int | None = None,
    details: dict | None = None,
) -> None:
    """Best-effort audit write. Logs and swallows DB errors so the audit log
    never blocks user-facing flows."""
    try:
        with open_db(db_path) as conn:
            insert_audit_event(
                conn,
                guild_id=guild_id,
                actor_id=actor_id,
                action=action,
                round_id=round_id,
                details=details,
            )
    except Exception:
        log.exception("guess audit write failed for action=%s round=%s", action, round_id)


async def _post_round_or_rollback(
    interaction: discord.Interaction,
    channel: "discord.TextChannel | discord.VoiceChannel | discord.Thread",
    *,
    db_path: Path,
    round_id: int,
    guild_id: int,
    submitter_id: int,
    file: discord.File,
    view: discord.ui.LayoutView,
    label: str,
) -> "discord.Message | None":
    """Post a round's card, rolling the whole submission back if it fails.

    Returns the posted message, or ``None`` when it rolled back — the caller
    stops its own view and returns. Both Post handlers (photo and confession)
    share this: by the time the send runs the round row is committed and, on
    the photo path, the member's full-size original is on disk, so a raised
    send has to undo both or it leaves an open round that never posted and a
    private photo nothing will ever clean up (#227).
    """
    try:
        # ``view`` is the whole card; its gallery shows ``file`` by name.
        return await channel.send(file=file, view=view)
    except Exception:
        log.exception(
            "guess: %s round %d failed to post in guild %d — rolling back",
            label, round_id, guild_id,
        )
        await asyncio.to_thread(
            rollback_unposted_round, db_path, round_id,
            guild_id=guild_id, actor_id=submitter_id,
        )
        await _tell_post_failed(interaction)
        return None


async def _tell_post_failed(interaction: discord.Interaction) -> None:
    """Close the submitter's ephemeral panel after a rolled-back post.

    "Nothing was saved" is the literal truth after the rollback, and it is the
    part the submitter cares about: their photo is not sitting on the bot."""
    try:
        await interaction.edit_original_response(
            view=card(build_notice_layout(
                "❌ Couldn't Post That Round",
                "Nothing was saved. Try again in a moment.",
            )),
            attachments=[],
        )
    except discord.HTTPException:
        log.warning("guess: could not tell the submitter their post failed")


async def _close_silently(interaction: discord.Interaction) -> None:
    """Cancel on an ephemeral card: delete it, say nothing (style guide
    § Words — a receipt for a non-event is one more thing to read)."""
    await interaction.response.defer()
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


# ── Reading and editing a posted round card ──────────────────────────────────


async def _card_art(bot: "Bot", channel: Any) -> PanelArt:
    """The lobby strip the public Guess cards wear, titled "Guess Who".

    Never lets the art stop a round: a failed read (a channel with no
    guild, a branding table mid-migration) draws nothing rather than
    leaving a solve unrevealed.
    """
    try:
        return await read_lobby_art(bot, channel, layouts.TITLE)
    except Exception:
        log.exception("guess: could not read the lobby art")
        return NO_ART


def _art_kwargs(art: PanelArt | None) -> dict[str, Any]:
    art = art or NO_ART
    return {
        "header_url": art.header,
        "footer_url": art.footer,
        "title_in_header": art.title_in_header,
    }


def _is_confession(round_row: GuessRound | None) -> bool:
    return bool(round_row and round_row.round_type == "confession")


def _embed_era_attachments(message: Any) -> list[Any]:
    """The uploads on a round posted before the Components V2 conversion.

    Those messages are an embed with the crop as a plain attachment. A V2
    card's uploads are referenced by its gallery and hidden from
    ``message.attachments``, so a non-empty list here *is* the embed era.
    """
    attachments = getattr(message, "attachments", None)
    return attachments if isinstance(attachments, list) else []


def _carried_media(message: Any, *, confession: bool) -> str | None:
    """The ``attachment://`` reference for the image a round card already
    carries, so an edit can keep showing it without uploading it again.

    An embed-era round names its own upload; a V2 round's filename is the one
    this cog posted it under. ``None`` for an embed-era message with no
    upload at all: there is nothing to reference, and a gallery naming a file
    that isn't there would fail the whole edit.
    """
    legacy = _embed_era_attachments(message)
    if legacy:
        return layouts.attachment_ref(str(legacy[0].filename))
    flags = getattr(message, "flags", None)
    if flags is not None and getattr(flags, "components_v2", True) is False:
        return None
    return layouts.attachment_ref(
        layouts.CONFESSION_FILENAME if confession else layouts.CROP_FILENAME
    )


def _posted_media_url(message: Any, filename: str) -> str:
    """The CDN URL of the image a freshly posted round card shows.

    ``filename`` is the upload the card was posted with. The card's galleries
    include the guild's lobby header and footer, so the round's own image is
    the one whose path ends in that name, not simply the first gallery.
    """
    urls = layouts.gallery_urls(getattr(message, "components", None))
    for url in urls:
        if urlparse(url).path.endswith("/" + filename):
            return url
    if len(urls) == 1:
        return urls[0]
    attachments = _embed_era_attachments(message)
    return str(attachments[0].url) if attachments else ""


async def _reveal_names_for(
    db_path: Path,
    guild: discord.Guild | None,
    round_row: GuessRound,
    *,
    extra_user_ids: Sequence[int] = (),
) -> tuple[Any, str, str]:
    """``(name_fn, answer_name, submitter_name)`` for a round's public reveal.

    Don't let the bot author a message that puts a no-contact pair side by
    side. The card pings nobody, but the reveal still names them together in
    the bot's own voice, which is the bot manufacturing the association the
    pair exists to prevent. Degrade to plain ids so the reveal still reads
    correctly.
    """
    no_contact = (
        round_row.submitter_id != round_row.answer_id
        and await asyncio.to_thread(
            no_contact_service.is_no_contact,
            db_path,
            round_row.guild_id,
            round_row.answer_id,
            round_row.submitter_id,
        )
    )
    name_fn = await build_name_fn(
        guild=guild,
        db_path=db_path,
        guild_id=round_row.guild_id,
        user_ids=[round_row.answer_id, round_row.submitter_id, *extra_user_ids],
    )
    answer_name, submitter_name = reveal_names(
        round_row.answer_id,
        round_row.submitter_id,
        no_contact=bool(no_contact),
        name_fn=name_fn,
    )
    return name_fn, answer_name, submitter_name


async def _reveal_file(round_row: GuessRound) -> tuple[discord.File | None, Path | None]:
    """The full-size reveal image from the round's cached original, and the
    path to delete once the card shows it; ``(None, path-or-None)`` when the
    file is already gone (a confession, or an original that aged out)."""
    if not round_row.original_path:
        return None, None
    orig_path = Path(round_row.original_path)
    if not orig_path.exists():
        return None, orig_path
    full_bytes = await asyncio.to_thread(orig_path.read_bytes)
    if all(
        v is not None for v in (
            round_row.crop_box_x1, round_row.crop_box_y1,
            round_row.crop_box_x2, round_row.crop_box_y2,
        )
    ):
        box = BoundingBox(
            round_row.crop_box_x1,  # type: ignore[arg-type]
            round_row.crop_box_y1,  # type: ignore[arg-type]
            round_row.crop_box_x2,  # type: ignore[arg-type]
            round_row.crop_box_y2,  # type: ignore[arg-type]
        )
        full_bytes = await asyncio.to_thread(render_reveal, full_bytes, box)
    return (
        discord.File(io.BytesIO(full_bytes), filename=layouts.FULL_FILENAME),
        orig_path,
    )


async def _edit_round_card(
    message: discord.Message,
    view: discord.ui.LayoutView,
    *,
    files: list[discord.File] | None = None,
) -> None:
    """Edit a round's public card, converting an embed-era one on the way.

    ``content`` and ``embed`` are nulled on every edit: an embed message
    becomes a V2 one the first time it is edited with a layout, and a V2
    message can carry neither. ``files`` replaces the card's uploads (the
    reveal); without it the uploads stay and the layout's gallery references
    the one already there.

    An embed-era round whose upload Discord won't let the new gallery
    reference is converted the slow way instead — download the crop, upload
    it again under the same name — so the round is never left unconvertible.
    """
    if files is not None:
        await message.edit(view=view, content=None, embed=None, attachments=files)
        return
    try:
        await message.edit(view=view, content=None, embed=None)
    except discord.HTTPException:
        legacy = _embed_era_attachments(message)
        if not legacy:
            raise
        data = await legacy[0].read()
        await message.edit(
            view=view, content=None, embed=None,
            attachments=[
                discord.File(io.BytesIO(data), filename=str(legacy[0].filename))
            ],
        )


# ── Views ─────────────────────────────────────────────────────────────────────

SELECT_TIMEOUT_SECONDS = 60


_PAGE_SIZE = layouts.PICKER_PAGE_SIZE


class _FilterModal(discord.ui.Modal, title="Filter names"):
    """Modal that accepts a search query and narrows the guess select."""

    query: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Search",
        placeholder="Type a name…",
        required=True,
        max_length=50,
    )

    def __init__(self, parent: GuessSelectView) -> None:
        super().__init__()
        self._parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        q = self.query.value.strip()
        q_lower = q.lower()

        def _score(m: discord.Member) -> int:
            name = m.display_name.lower()
            if name == q_lower:
                return 4
            if name.startswith(q_lower):
                return 3
            if q_lower in name:
                return 2
            it = iter(name)
            if all(c in it for c in q_lower):
                return 1
            return 0

        scored = sorted(
            ((m, _score(m)) for m in self._parent._all_members),
            key=lambda x: -x[1],
        )
        self._parent._display_members = [m for m, s in scored if s > 0]
        self._parent._filter_query = q
        self._parent._page = 0
        self._parent._rebuild()
        await interaction.response.edit_message(view=self._parent)


class GuessSelectView(ExpiringLayoutView):
    """Ephemeral picker shown when a user clicks the Guess button.

    One card for the whole exchange: ``_status`` is the line under the
    heading — the question at first, then the answer to each pick — and
    every change redraws the card in place (``build_picker_layout``).
    """

    def __init__(
        self,
        bot: "Bot",
        round_id: int,
        guess_members: Sequence[discord.Member],
        game_message: discord.Message,
        *,
        cooldown_seconds: int = 0,
        max_guesses_per_round: int = MAX_GUESSES_PER_USER_ROUND,
        guess_placeholder: str = layouts.PHOTO_QUESTION,
    ) -> None:
        super().__init__(timeout=SELECT_TIMEOUT_SECONDS)
        self.bot = bot
        self.round_id = round_id
        self.game_message = game_message
        self.cooldown_seconds = cooldown_seconds
        self.max_guesses_per_round = max_guesses_per_round
        self._all_members = list(guess_members)
        self._display_members = self._all_members
        self._filter_query = ""
        self._base_placeholder = guess_placeholder
        self._page = 0
        self._status = layouts.picker_prompt(SELECT_TIMEOUT_SECONDS)
        self._locked = False
        self._select: discord.ui.Select  # type: ignore[type-arg]
        self._rebuild()

    def _page_count(self) -> int:
        return max(1, (len(self._display_members) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _rebuild(self) -> None:
        self.clear_items()
        start = self._page * _PAGE_SIZE
        page_members = self._display_members[start : start + _PAGE_SIZE]
        page_count = self._page_count()
        for item in build_picker_layout(
            self.round_id,
            [(m.display_name, str(m.id)) for m in page_members],
            status=self._status,
            placeholder=layouts.picker_placeholder(
                self._base_placeholder,
                filter_query=self._filter_query,
                match_count=len(self._display_members),
                page=self._page,
                page_count=page_count,
            ),
            page=self._page,
            page_count=page_count,
            filter_query=self._filter_query,
        ):
            self.add_item(item)

        self._select = cast(
            "discord.ui.Select[Any]", self.find_item(layouts.PICKER_SELECT_ID)
        )
        self._select.callback = self._on_select  # type: ignore[method-assign]
        for component_id, callback in (
            (layouts.PICKER_PREV_ID, self._on_prev),
            (layouts.PICKER_NEXT_ID, self._on_next),
            (layouts.PICKER_FILTER_ID, self._on_filter),
            (layouts.PICKER_CLEAR_ID, self._on_clear_filter),
        ):
            item = self.find_item(component_id)
            if item is not None:
                item.callback = callback  # type: ignore[attr-defined]
        if self._locked:
            disable_all(self)

    def _disable_all(self) -> None:
        self._locked = True
        disable_all(self)

    async def _show(self, interaction: discord.Interaction, status: str) -> None:
        """Redraw the card with ``status`` as the answer to this pick."""
        self._status = status
        self._rebuild()
        await interaction.edit_original_response(view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self._page = min(self._page_count() - 1, self._page + 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_filter(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_FilterModal(self))

    async def _on_clear_filter(self, interaction: discord.Interaction) -> None:
        self._display_members = self._all_members
        self._filter_query = ""
        self._page = 0
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        # Ack within Discord's 3s budget before any DB / file / Discord-edit work.
        await interaction.response.defer(ephemeral=True)

        guessed_user_id = int(self._select.values[0])
        db_path = self.bot.ctx.db_path

        prior_guesses = await asyncio.to_thread(
            _do_count_user_guesses, db_path, self.round_id, interaction.user.id
        )
        if prior_guesses >= self.max_guesses_per_round:
            self._disable_all()
            guild_id = interaction.guild.id if interaction.guild else 0
            await asyncio.to_thread(
                _do_audit, db_path,
                guild_id=guild_id, actor_id=interaction.user.id,
                action="guess_cap_hit", round_id=self.round_id,
                details={"prior_guesses": prior_guesses},
            )
            await self._show(
                interaction,
                f"❌ You're out of guesses on this round "
                f"(cap: {self.max_guesses_per_round}).",
            )
            return

        if self.cooldown_seconds > 0:
            last_guess = await asyncio.to_thread(
                _do_get_last_guess, db_path, self.round_id, interaction.user.id
            )
            if last_guess is not None:
                remaining = self.cooldown_seconds - (time.time() - last_guess.created_at)
                if remaining > 0:
                    ready_at = int(last_guess.created_at) + self.cooldown_seconds
                    await self._show(
                        interaction,
                        f"⏳ On cooldown — you can guess again <t:{ready_at}:R>.",
                    )
                    return

        round_row = await asyncio.to_thread(_do_load_round, db_path, self.round_id)
        if round_row is None:
            self.stop()
            await interaction.edit_original_response(
                view=card(build_notice_layout("❌ Round Not Found"))
            )
            return

        # No-contact on a Guess Who round. A round belongs to two members — the
        # ANSWER (whose likeness is on display) and the SUBMITTER (who chose to
        # put it there).
        #
        # The real protection is the candidate filter above: his partner is not
        # in his picker, so he CANNOT name her and his guess on her round is
        # always genuinely wrong. Given that, the guess is left to run its
        # ordinary course rather than being discarded.
        #
        # Discarding it was worse than useless. The per-round cap and the
        # cooldown both count rows in ``guess_guesses``, so a guess that was
        # never written left him uncapped and never on cooldown: he could hold
        # down the button on her round and get "Not it" forever, while every
        # other round in the server put him on cooldown after one guess. That
        # is a tell anyone bored enough finds in a minute, and it also wrote a
        # log row per click. A wrong guess he could never have got right costs
        # her nothing; being distinguishable costs her everything.
        #
        # One event per round still goes to the moderator log, on his first
        # guess only, so the evidence trail survives without the flood.
        guess_partners = await asyncio.to_thread(
            no_contact_service.no_contact_partners,
            db_path,
            round_row.guild_id,
            interaction.user.id,
        )
        if prior_guesses == 0 and guess_round_blocked_for(
            viewer_id=interaction.user.id,
            submitter_id=round_row.submitter_id,
            answer_id=round_row.answer_id,
            partners=guess_partners,
        ):
            for target in {round_row.answer_id, round_row.submitter_id} & guess_partners:
                await asyncio.to_thread(
                    no_contact_service.record_event,
                    db_path,
                    round_row.guild_id,
                    actor_id=interaction.user.id,
                    target_id=target,
                    kind=KIND_ATTEMPT,
                    surface=SURFACE_GUESS,
                )

        # A pick on a round his partner is part of is a forced miss, the right
        # one included (the picker filter can't cover a round she submitted).
        correct = guess_scores_correct(
            guessed_id=guessed_user_id,
            answer_id=round_row.answer_id,
            submitter_id=round_row.submitter_id,
            partners=guess_partners,
        )
        await asyncio.to_thread(
            _do_insert_guess,
            db_path,
            round_id=self.round_id,
            guesser_id=interaction.user.id,
            guessed_user_id=guessed_user_id,
            correct=correct,
        )

        # A scored guess is "playing the round" for the economy's quest
        # trigger (right or wrong — the guess is the participation).
        from bot_modules.economy.game_rewards import fire_member_trigger

        await fire_member_trigger(
            self.bot, round_row.guild_id, interaction.user.id,
            "guess", occurrence=str(self.round_id),
        )

        if correct and round_row.solved_at is None and round_row.expired_at is None:
            self._disable_all()
            rowcount, guess_count, unique_count = await asyncio.to_thread(
                _do_mark_solved, db_path, self.round_id, solver_id=interaction.user.id
            )
            if rowcount == 0:
                # Lost the race — a concurrent correct guess marked solved
                # first, or the expiry sweep closed the round.
                latest = await asyncio.to_thread(_do_load_round, db_path, self.round_id)
                expired = (
                    latest is not None
                    and latest.expired_at is not None
                    and latest.solved_at is None
                )
                await self._show(
                    interaction, EXPIRED_TEXT if expired else ALREADY_SOLVED_TEXT
                )
                return
            # The race is won: this member solved the round. guess_win is the
            # stretch twin of the participation `guess` kind above.
            await fire_member_trigger(
                self.bot, round_row.guild_id, interaction.user.id,
                "guess_win", occurrence=str(self.round_id),
            )

            name_fn, answer_name, submitter_name = await _reveal_names_for(
                db_path, interaction.guild, round_row, extra_user_ids=[interaction.user.id]
            )
            confession = _is_confession(round_row)
            full_file, orig_path = await _reveal_file(round_row)
            full_attachments = [full_file] if full_file is not None else []

            # The reveal shows the full image when the original was still on
            # disk; otherwise it keeps the crop (or confession card) the
            # round already carries.
            art = await _card_art(self.bot, self.game_message.channel)

            def _reveal(media: str | None) -> discord.ui.LayoutView:
                return card(build_solved_layout(
                    self.round_id, answer_name, submitter_name,
                    interaction.user.id, guess_count, unique_count,
                    name_fn=name_fn, media=media, confession=confession,
                    self_submitted=round_row.answer_id == round_row.submitter_id,
                    **_art_kwargs(art),
                ))

            # The round is already marked solved, so nothing below may stop
            # the rest of the solve — the original's deletion, the audit row,
            # the solver's "Correct!". A card that can't show its image still
            # gets the reveal in words, or the channel shows an open round no
            # guess can ever solve; a card a mod deleted mid-pick gets
            # nothing, since there is nothing left to edit (code review,
            # 2026-09-30: it used to raise there and strand the photo).
            try:
                if full_attachments:
                    await _edit_round_card(
                        self.game_message,
                        _reveal(layouts.attachment_ref(layouts.FULL_FILENAME)),
                        files=full_attachments,
                    )
                else:
                    await _edit_round_card(
                        self.game_message,
                        _reveal(_carried_media(
                            self.game_message, confession=confession
                        )),
                    )
            except discord.NotFound:
                log.info(
                    "guess: round %d card was deleted before its reveal",
                    self.round_id,
                )
            except discord.HTTPException:
                log.exception(
                    "guess: round %d reveal could not keep its image",
                    self.round_id,
                )
                try:
                    await _edit_round_card(self.game_message, _reveal(None))
                except discord.HTTPException:
                    log.exception(
                        "guess: round %d reveal could not be posted at all",
                        self.round_id,
                    )

            if orig_path is not None:
                await asyncio.to_thread(orig_path.unlink, missing_ok=True)
                await asyncio.to_thread(_do_set_original_path, db_path, self.round_id, "")
            guild_id = interaction.guild.id if interaction.guild else round_row.guild_id
            await asyncio.to_thread(
                _do_audit, db_path,
                guild_id=guild_id, actor_id=interaction.user.id,
                action="solve", round_id=self.round_id,
                details={
                    "guesses_to_solve": guess_count,
                    "unique_guessers": unique_count,
                },
            )
            await self._show(
                interaction, f"✅ **Correct!** You solved Round #{self.round_id}!"
            )
        elif round_row.expired_at is not None and round_row.solved_at is None:
            self._disable_all()
            await self._show(interaction, EXPIRED_TEXT)
        elif round_row.solved_at is not None:
            # The guess is still recorded above (the cap and the cooldown
            # count rows, and the no-contact rule needs every guess to run
            # the same course); only the answer differs, and it depends on
            # the round alone, never on who is guessing. So the text keys on
            # the raw name match, not ``correct``: a forced miss (no-contact)
            # naming the real answer must read the same as anyone else doing so.
            self._disable_all()
            await self._show(
                interaction,
                ALREADY_SOLVED_TEXT
                if guessed_user_id == round_row.answer_id
                else SOLVED_WHILE_PICKING_TEXT,
            )
        else:
            new_count = await asyncio.to_thread(
                _do_count_guesses_for_round, db_path, self.round_id
            )
            confession = _is_confession(round_row)
            new_view = GameView(
                self.bot, self.round_id, guess_count=new_count,
                media=_carried_media(self.game_message, confession=confession),
                confession=confession,
                art=await _card_art(self.bot, self.game_message.channel),
            )
            try:
                await _edit_round_card(self.game_message, new_view)
            except discord.HTTPException:
                log.exception(
                    "guess: chip counter bump failed for round %d", self.round_id
                )
            # Extend view lifetime so the user can guess again after cooldown expires.
            if self.cooldown_seconds > 0:
                self.timeout = min(
                    self.cooldown_seconds + SELECT_TIMEOUT_SECONDS,
                    MAX_EPHEMERAL_TIMEOUT,
                )
            await self._show(interaction, WRONG_GUESS_TEXT)


class GameView(discord.ui.LayoutView):
    """Persistent public card for an open round (``build_round_layout``).

    ``media`` is the gallery's reference to the round's image. The default
    is what a fresh photo round is posted with; the restart-time
    registration in ``cog_load`` passes nothing either, since a view that is
    only being registered is never sent — all that matters there is the
    Guess button's custom id, which is the embed-era one, so rounds posted
    before the conversion still answer. ``art`` is the channel's lobby strip
    (``_card_art``), none at registration for the same reason.
    """

    def __init__(
        self,
        bot: "Bot",
        round_id: int,
        *,
        guess_count: int = 0,
        media: str | None = layouts.attachment_ref(layouts.CROP_FILENAME),
        confession: bool = False,
        art: PanelArt | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.round_id = round_id
        self.guess_count = guess_count
        for item in build_round_layout(
            round_id, guess_count, media=media, confession=confession,
            **_art_kwargs(art),
        ):
            self.add_item(item)
        bind(
            self,
            layouts.GUESS_ID_TEMPLATE.format(round_id=round_id),
            self._guess_callback,
        )

    async def _guess_callback(self, interaction: discord.Interaction) -> None:
        assert interaction.guild and interaction.message
        await interaction.response.defer(ephemeral=True)
        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        round_row = await asyncio.to_thread(_do_load_round, db_path, self.round_id)
        if round_row and round_row.answer_optout:
            await interaction.followup.send(
                # Not "the answer opted out": in Guess the answer is the
                # submitter, so that told the channel who had left the pool.
                "❌ This round can't be solved any more.",
                ephemeral=True,
            )
            return
        if round_row and round_row.expired_at is not None:
            await interaction.followup.send(EXPIRED_TEXT, ephemeral=True)
            return
        if round_row and interaction.user.id == round_row.submitter_id:
            await interaction.followup.send(
                "❌ You can't guess on your own round.", ephemeral=True
            )
            return

        guess_role = interaction.guild.get_role(config.guess_role_id)
        if guess_role is None:
            await interaction.followup.send(
                "❌ Guess role not found — ask an admin to configure it.", ephemeral=True
            )
            return

        guess_members = [m for m in guess_role.members if not m.bot]
        # Drop no-contact partners from the candidate picker. Removing a name
        # from a long, paginated, searchable member list is unnoticeable — he
        # has no baseline for who "should" be in it — so this costs nothing on
        # the leak side.
        #
        # It also closes the one hole in the silent-discard rule below. The
        # tell we were worried about was: guess her correctly, watch the round
        # get solved later with the name you gave, and deduce your guess was
        # filtered. If her name is not selectable, he can never guess her
        # correctly, so that deduction can never be made.
        # (That holds for a fresh picker. One opened before the pair existed
        # still lists her; the pick is re-scored against current partners,
        # so it is a forced miss -- see guess_scores_correct.)
        nc_partners = await asyncio.to_thread(
            no_contact_service.no_contact_partners,
            db_path,
            interaction.guild.id,
            interaction.user.id,
        )
        guess_members = picker_candidates(
            guess_members, nc_partners, interaction.user.id
        )
        if not guess_members:
            await interaction.followup.send(
                "❌ No opted-in members to guess from.", ephemeral=True
            )
            return

        picker = GuessSelectView(
            self.bot, self.round_id, guess_members, interaction.message,
            cooldown_seconds=config.guess_cooldown_seconds,
            max_guesses_per_round=config.max_guesses_per_round,
            guess_placeholder=(
                layouts.CONFESSION_QUESTION
                if _is_confession(round_row)
                else layouts.PHOTO_QUESTION
            ),
        )
        picker.message = await interaction.followup.send(
            view=picker, ephemeral=True, wait=True
        )


CROP_EDITOR_ZOOM_IN: float = 0.8
CROP_EDITOR_ZOOM_OUT: float = 1.25


class CropEditorView(ExpiringLayoutView):
    """D-pad grid for interactive crop framing (``build_crop_editor_layout``).

    Row 0:  [  ·  ]  [  ↑  ]  [  ·  ]
    Row 1:  [  ←  ]  [  ·  ]  [  →  ]
    Row 2:  [  ·  ]  [  ↓  ]  [  ·  ]
    Row 3:  [ 🔍+ ]  [ Auto]  [ 🔍− ]
    Row 4:  [✓Post]           [  ✗  ]

    · = disabled spacer button to maintain the cross shape.
    Auto cycles through pipeline detections (first press → top candidate,
    subsequent presses cycle, wraps around). Disabled when no candidates.
    Step size is 1/5 of the current crop box so precision scales with zoom level.
    The round is only inserted to DB when Post is clicked.
    """

    def __init__(
        self,
        bot: "Bot",
        image_bytes: bytes,
        img_w: int,
        img_h: int,
        crop_box: BoundingBox,
        guild_id: int,
        guess_channel_id: int,
        *,
        submitter_id: int,
        answer_id: int,
        difficulty: str,
        candidate_count: int,
        guess_role_id: int = 0,
        original_bytes: bytes = b"",
        original_ext: str = ".jpg",
        candidate_boxes: list[BoundingBox] | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.image_bytes = image_bytes
        self.img_w = img_w
        self.img_h = img_h
        self.crop_box = crop_box
        self.guild_id = guild_id
        self.guess_channel_id = guess_channel_id
        self._submitter_id = submitter_id
        self._answer_id = answer_id
        self._difficulty = difficulty
        self._candidate_count = candidate_count
        self._guess_role_id = guess_role_id
        self._original_bytes = original_bytes
        self._original_ext = original_ext
        self._post_lock = asyncio.Lock()
        self._posted = False
        self._candidate_boxes: list[BoundingBox] = candidate_boxes or []
        self._candidate_idx = -1  # -1 so first Auto press snaps to index 0

        for item in build_crop_editor_layout(
            detections=bool(self._candidate_boxes)
        ):
            self.add_item(item)
        for component_id, callback in (
            (layouts.EDITOR_UP_ID, self._on_up),
            (layouts.EDITOR_DOWN_ID, self._on_down),
            (layouts.EDITOR_LEFT_ID, self._on_left),
            (layouts.EDITOR_RIGHT_ID, self._on_right),
            (layouts.EDITOR_ZOOM_IN_ID, self._on_zoom_in),
            (layouts.EDITOR_ZOOM_OUT_ID, self._on_zoom_out),
            (layouts.EDITOR_AUTO_ID, self._on_auto),
            (layouts.EDITOR_POST_ID, self._on_post),
            (layouts.EDITOR_CANCEL_ID, self._on_cancel),
        ):
            button = cast("discord.ui.Button[Any]", self.find_item(component_id))
            button.callback = callback  # type: ignore[method-assign]
        self._auto_btn = cast(
            "discord.ui.Button[Any]", self.find_item(layouts.EDITOR_AUTO_ID)
        )

    async def _rerender(self, interaction: discord.Interaction) -> None:
        editor_bytes = await asyncio.to_thread(
            render_crop_editor, self.image_bytes, self.crop_box
        )
        preview_file = discord.File(
            io.BytesIO(editor_bytes), filename=layouts.EDITOR_PREVIEW_FILENAME
        )
        # The card is unchanged — only the preview it references is new.
        await interaction.response.edit_message(
            attachments=[preview_file], view=self
        )

    async def _on_up(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, 0, -self.crop_box.height / 5, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_down(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, 0, self.crop_box.height / 5, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_left(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, -self.crop_box.width / 5, 0, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_right(self, interaction: discord.Interaction) -> None:
        self.crop_box = move_crop_box(self.crop_box, self.crop_box.width / 5, 0, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_zoom_in(self, interaction: discord.Interaction) -> None:
        self.crop_box = zoom_crop_box(self.crop_box, CROP_EDITOR_ZOOM_IN, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_zoom_out(self, interaction: discord.Interaction) -> None:
        self.crop_box = zoom_crop_box(self.crop_box, CROP_EDITOR_ZOOM_OUT, self.img_w, self.img_h)
        await self._rerender(interaction)

    async def _on_auto(self, interaction: discord.Interaction) -> None:
        if not self._candidate_boxes:
            return
        self._candidate_idx = (self._candidate_idx + 1) % len(self._candidate_boxes)
        self.crop_box = self._candidate_boxes[self._candidate_idx]
        await self._rerender(interaction)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await _close_silently(interaction)

    async def _on_post(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        async with self._post_lock:
            if self._posted:
                await interaction.response.send_message("❌ Already posted.", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)

        async with self._post_lock:
            if self._posted:
                await interaction.followup.send("❌ Already posted.", ephemeral=True)
                return
            self._posted = True

        guess_channel = interaction.guild.get_channel(self.guess_channel_id)
        if guess_channel is None or not isinstance(
            guess_channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            self.stop()
            await interaction.followup.send(
                "❌ Guess channel not found — ask an admin to check the config.", ephemeral=True
            )
            return

        db_path = self.bot.ctx.db_path
        # No-contact gate on submission. Today both call sites set answer_id to
        # the submitter (you can only post a photo of yourself), so this is
        # unreachable — but submitter_id and answer_id are separate columns and
        # the day they diverge, this is the gate that stops someone posting a
        # cropped image of the member who blocked them for the server to guess.
        # Cheap to hold now, expensive to notice missing later.
        if self._answer_id != self._submitter_id and await asyncio.to_thread(
            no_contact_service.check_and_record,
            db_path,
            self.guild_id,
            actor_id=self._submitter_id,
            target_id=self._answer_id,
            surface=SURFACE_GUESS_SUBMIT,
        ):
            self.stop()
            await interaction.followup.send(
                "❌ Couldn't post that round — try a different photo.", ephemeral=True
            )
            return

        crop_bytes = await asyncio.to_thread(render_crop, self.image_bytes, self.crop_box)
        round_id = await asyncio.to_thread(
            _do_insert_round,
            db_path,
            guild_id=self.guild_id,
            submitter_id=self._submitter_id,
            answer_id=self._answer_id,
            channel_id=self.guess_channel_id,
            difficulty=self._difficulty,
            candidate_count=self._candidate_count,
        )

        if self._original_bytes:
            orig_dir = guess_orig_dir(db_path)
            orig_path = orig_dir / f"{round_id}{self._original_ext}"

            def _write_original() -> None:
                orig_dir.mkdir(parents=True, exist_ok=True)
                orig_path.write_bytes(self._original_bytes)

            await asyncio.to_thread(_write_original)
            await asyncio.to_thread(_do_set_original_path, db_path, round_id, str(orig_path))

        await asyncio.to_thread(
            _do_set_crop_box, db_path, round_id,
            self.crop_box.x1, self.crop_box.y1, self.crop_box.x2, self.crop_box.y2,
        )

        crop_file = discord.File(io.BytesIO(crop_bytes), filename=layouts.CROP_FILENAME)
        game_view = GameView(
            self.bot, round_id, art=await _card_art(self.bot, guess_channel)
        )
        game_msg = await _post_round_or_rollback(
            interaction, guess_channel,
            db_path=db_path, round_id=round_id, guild_id=self.guild_id,
            submitter_id=self._submitter_id,
            file=crop_file, view=game_view, label="photo",
        )
        # Posted or rolled back, the editor is done: stopping it keeps its
        # timeout from greying the editor back over the card that replaces it.
        self.stop()
        if game_msg is None:
            return

        # Persistent-view registration waits for the send: a round that never
        # posted must not leave its custom_ids listening for a message that
        # does not exist (they would sit in the view store until a restart).
        self.bot.add_view(game_view)

        crop_url = _posted_media_url(game_msg, layouts.CROP_FILENAME)
        await asyncio.to_thread(
            _do_update_round_message, db_path, round_id, game_msg.id, crop_url, ""
        )

        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=self.guild_id, actor_id=self._submitter_id,
            action="submit", round_id=round_id,
            details={"difficulty": self._difficulty},
        )

        # Quest hook: submitting a round is the producer half of the
        # guess-who quest pair (the guesses themselves fire "guess"). Fires
        # only once the round is actually up — a post that failed and rolled
        # back is not a submitted round, and must not pay for one.
        from bot_modules.economy.game_rewards import fire_member_trigger

        await fire_member_trigger(
            self.bot, self.guild_id, self._submitter_id,
            "guess_post", occurrence=str(round_id),
        )

        await interaction.edit_original_response(
            view=card(build_notice_layout(
                "✅ Round Posted", f"It's up in {guess_channel.mention}."
            )),
            attachments=[],
        )

        try:
            await _repost_prompt(self.bot, guess_channel, self.guild_id)
        except Exception:
            log.exception("guess: prompt repost after game post failed for guild %d", self.guild_id)


# ── Sticky channel prompt ────────────────────────────────────────────────────

# Trailing-edge debounce: after the last channel message, wait this long before
# re-posting. Cancels and reschedules on each new message so the prompt lands
# under the final message of a burst rather than flickering through each one.
PROMPT_REPOST_DELAY_SEC = 2.0


def _resolve_safe_url(url: str) -> tuple[str, str]:
    """Resolve URL hostname to an IP, validate it's not private/loopback/link-local.

    Returns (ip_url, host_header) where ip_url has the hostname substituted with the
    resolved IP. Callers must connect using ip_url and send the original Host header to
    prevent DNS rebinding (the hostname is resolved once here, not again at fetch time).
    Raises ValueError if the host is unsafe or unresolvable.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise ValueError("no host in URL")

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            addr = ipaddress.ip_address(socket.gethostbyname(host))
        except socket.gaierror:
            raise ValueError(f"could not resolve host: {host!r}")

    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        raise ValueError("host resolves to a private or internal address")

    port_str = f":{parsed.port}" if parsed.port else ""
    # IPv6 literals need brackets in the netloc
    addr_str = f"[{addr}]" if addr.version == 6 else str(addr)
    ip_netloc = f"{addr_str}{port_str}"
    host_header = f"{host}{port_str}"
    return urlunparse(parsed._replace(netloc=ip_netloc)), host_header


def _fetch_url_bytes(url: str, max_bytes: int) -> bytes:
    """Synchronous URL fetch with a hard byte cap.

    Resolves the hostname once and connects directly to the IP to prevent DNS rebinding.
    Raises ValueError if the host is unsafe, unresolvable, or the response is too large.
    """
    ip_url, host_header = _resolve_safe_url(url)
    req = urllib.request.Request(
        ip_url,
        headers={"User-Agent": "DungeonKeeper-Bot/1.0", "Host": host_header},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"response exceeds {max_bytes // (1024 * 1024)} MB limit")
    return data


async def _pipeline_and_open_editor(
    interaction: discord.Interaction,
    bot: "Bot",
    config: GuessConfig,
    image_bytes: bytes,
    *,
    original_ext: str = ".jpg",
) -> None:
    """Run the detection pipeline and open the crop-editor view.

    Expects the interaction to already be deferred (ephemeral).
    Uses ``interaction.followup`` for all responses.
    """
    assert interaction.guild

    try:
        dim_ok, img_w, img_h = await asyncio.to_thread(
            _validate_dimensions, image_bytes, config.min_image_dimension_px
        )
    except Exception:  # noqa: BLE001 — PIL's open-ended raise set on arbitrary user-supplied bytes; anything means "not a valid image"
        await interaction.followup.send("❌ That doesn't appear to be a valid image.", ephemeral=True)
        return
    if not dim_ok:
        await interaction.followup.send(
            f"❌ Image too small. Minimum dimension is {config.min_image_dimension_px}px.",
            ephemeral=True,
        )
        return

    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".jpg")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(image_bytes)
        pipeline_result = await asyncio.to_thread(
            run_pipeline, tmp_path, image_bytes, config.crop_difficulty, candidate_count=10
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    sorted_cands = sorted(pipeline_result.candidates, key=lambda d: d.score, reverse=True)
    candidate_boxes: list[BoundingBox] = []
    for det in sorted_cands:
        _exp = enforce_min_size(
            compute_padded_crop(det.box, config.crop_difficulty, img_w, img_h)
        )
        candidate_boxes.append(BoundingBox(
            max(0.0, _exp.x1), max(0.0, _exp.y1),
            min(float(img_w), _exp.x2), min(float(img_h), _exp.y2),
        ))

    if candidate_boxes:
        initial_crop = candidate_boxes[0]
    else:
        mx, my = img_w * 0.2, img_h * 0.2
        _exp = enforce_min_size(BoundingBox(mx, my, img_w - mx, img_h - my))
        initial_crop = BoundingBox(
            max(0.0, _exp.x1), max(0.0, _exp.y1),
            min(float(img_w), _exp.x2), min(float(img_h), _exp.y2),
        )

    editor_bytes = await asyncio.to_thread(render_crop_editor, image_bytes, initial_crop)
    editor = CropEditorView(
        bot,
        image_bytes=image_bytes,
        img_w=img_w,
        img_h=img_h,
        crop_box=initial_crop,
        guild_id=interaction.guild.id,
        guess_channel_id=config.guess_channel_id,
        submitter_id=interaction.user.id,
        answer_id=interaction.user.id,
        difficulty=config.crop_difficulty,
        candidate_count=len(pipeline_result.candidates),
        guess_role_id=config.guess_role_id,
        original_bytes=image_bytes,
        original_ext=original_ext,
        candidate_boxes=candidate_boxes,
    )
    editor.message = await interaction.followup.send(
        file=discord.File(
            io.BytesIO(editor_bytes), filename=layouts.EDITOR_PREVIEW_FILENAME
        ),
        view=editor,
        ephemeral=True,
        wait=True,
    )


_MAX_URL_BYTES = 25 * 1024 * 1024


class _GuessSubmitModal(discord.ui.Modal, title="Submit a Guess Photo"):
    """The prompt's Submit form: a photo upload or an image link.

    One of the two is required and an upload wins when both are given. It
    runs the same checks as ``/guess submit`` — role, rate limit, type, size
    — before the crop editor opens. Until 2026-09-30 it took a link only and
    sent members off to the slash command to upload.
    """

    def __init__(self, bot: "Bot") -> None:
        super().__init__()
        self._bot = bot
        self.upload: discord.ui.FileUpload = discord.ui.FileUpload(  # type: ignore[type-arg]
            required=False, max_values=1
        )
        self.image_url: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[type-arg]
            placeholder="https://…",
            required=False,
            max_length=512,
        )
        self.add_item(
            discord.ui.Label(
                text="Photo",
                description="SFW or NSFW. You frame the crop before anything posts.",
                component=self.upload,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Or an image link",
                description="Used only when no photo is uploaded.",
                component=self.image_url,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        uploads = list(self.upload.values)
        url = (self.image_url.value or "").strip()
        if not uploads and not url:
            await interaction.followup.send(
                "❌ Add a photo or paste an image link.", ephemeral=True
            )
            return
        parsed = urlparse(url)
        if not uploads and parsed.scheme not in ("http", "https"):
            await interaction.followup.send(
                "❌ Please provide an http or https URL.", ephemeral=True
            )
            return

        db_path = self._bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        image = uploads[0] if uploads else None
        refusal = _submit_gate(
            interaction, config,
            content_type=image.content_type if image else None,
            size=image.size if image else None,
        )
        if refusal:
            await interaction.followup.send(refusal, ephemeral=True)
            return

        if image is not None:
            image_bytes = await image.read()
            original_ext = (Path(image.filename).suffix or ".jpg").lower()
        else:
            try:
                image_bytes = await asyncio.to_thread(
                    _fetch_url_bytes, url, _MAX_URL_BYTES
                )
            except Exception as exc:  # noqa: BLE001 — arbitrary-URL fetch has an open-ended raise set (DNS, TLS, socket, HTTP); anything means "could not download"
                await interaction.followup.send(
                    f"❌ Could not download that URL: {exc}", ephemeral=True
                )
                return

            if not _validate_size(len(image_bytes), config.max_image_size_mb):
                await interaction.followup.send(too_large_text(config), ephemeral=True)
                return
            original_ext = (Path(parsed.path).suffix or ".jpg").lower()

        await _pipeline_and_open_editor(
            interaction, self._bot, config, image_bytes, original_ext=original_ext
        )


class GuessPromptView(discord.ui.LayoutView):
    """Persistent channel-bottom prompt (``build_prompt_layout``).

    ``art`` is the guild's lobby strip, already titled; the restart-time
    registration passes none — it only needs the two custom ids.
    """

    def __init__(self, bot: "Bot", *, art: PanelArt | None = None) -> None:
        super().__init__(timeout=None)
        self._bot = bot
        art = art or NO_ART
        for item in build_prompt_layout(
            header_url=art.header,
            footer_url=art.footer,
            title_in_header=art.title_in_header,
        ):
            self.add_item(item)
        bind(self, layouts.PROMPT_SUBMIT_ID, self._on_submit)
        bind(self, layouts.PROMPT_HELP_ID, self._on_help)

    async def _on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_GuessSubmitModal(self._bot))

    async def _on_help(self, interaction: discord.Interaction) -> None:
        """The guild's ``guess`` doc, read now; the built-in how-to until an
        admin writes one."""
        body = None
        if interaction.guild is not None:
            body = await asyncio.to_thread(
                load_doc_body,
                self._bot.ctx.db_path,
                interaction.guild.id,
                layouts.HELP_DOC_KEY,
            )
        # An admin-written doc may carry a role or @everyone mention; the
        # card is an answer to one tap, never a ping.
        await interaction.response.send_message(
            view=card(build_help_layout(body)),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _repost_prompt(
    bot: "Bot",
    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread,
    guild_id: int,
) -> None:
    """Move the prompt to the bottom of ``channel``.

    Delegates to the cog's ``StickyPanel``. Before that (2026-08-06) this was
    the last hand-rolled copy of the sticky placer and it still carried both
    bugs the shared module exists to prevent:

    * it **deleted the old prompt before posting the new one**, so a failed
      send left the channel with no prompt at all and the stored id naming a
      message that no longer existed — gone until someone reposted by hand;
    * the placement was **not shielded**, and ``on_message`` cancel-and-rearmed
      on every message, so a cancel could land on the task parked in ``send()``.
      Discord has accepted the message by then but the id is never recorded:
      an orphaned prompt with live buttons, and a stored id frozen on the
      already-deleted one. That is the mechanism of the 2026-07-26 casino storm,
      fixed in ``6fb53e73`` for every migrated caller.

    Kept as a module function because four call sites (and three test modules)
    already speak it: after posting a round, after a confession, and from the
    dashboard's panel poster.
    """
    cog = cast("GuessCog | None", bot.get_cog("GuessCog"))
    if cog is None:  # cog unloaded mid-flight
        return
    await cog.prompt_panel.place(channel.guild, channel)


class ConfessionPreviewView(ExpiringLayoutView):
    """Ephemeral preview shown before a confession is posted
    (``build_confession_preview_layout``)."""

    def __init__(
        self,
        bot: "Bot",
        text: str,
        guild_id: int,
        guess_channel_id: int,
        *,
        submitter_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._bot = bot
        self._text = text
        self._guild_id = guild_id
        self._guess_channel_id = guess_channel_id
        self._submitter_id = submitter_id
        self._post_lock = asyncio.Lock()
        self._posted = False
        for item in build_confession_preview_layout():
            self.add_item(item)
        for component_id, callback in (
            (layouts.CONFESSION_POST_ID, self._on_post),
            (layouts.CONFESSION_CANCEL_ID, self._on_cancel),
        ):
            button = cast("discord.ui.Button[Any]", self.find_item(component_id))
            button.callback = callback  # type: ignore[method-assign]

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await _close_silently(interaction)

    async def _on_post(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        async with self._post_lock:
            if self._posted:
                await interaction.response.send_message("❌ Already posted.", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)

        async with self._post_lock:
            if self._posted:
                await interaction.followup.send("❌ Already posted.", ephemeral=True)
                return
            self._posted = True

        guess_channel = interaction.guild.get_channel(self._guess_channel_id)
        if guess_channel is None or not isinstance(
            guess_channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            self.stop()
            await interaction.followup.send(
                "❌ Guess channel not found — ask an admin to check the config.", ephemeral=True
            )
            return

        db_path = self._bot.ctx.db_path
        round_id = await asyncio.to_thread(
            _do_insert_round,
            db_path,
            guild_id=self._guild_id,
            submitter_id=self._submitter_id,
            answer_id=self._submitter_id,
            channel_id=self._guess_channel_id,
            difficulty="confession",
            candidate_count=0,
            round_type="confession",
            confession_text=self._text,
        )

        card_bytes = await asyncio.to_thread(
            render_quote, self._text, footer=f"Guess #{round_id}"
        )
        card_file = discord.File(
            io.BytesIO(card_bytes), filename=layouts.CONFESSION_FILENAME
        )
        game_view = GameView(
            self._bot, round_id,
            media=layouts.attachment_ref(layouts.CONFESSION_FILENAME),
            confession=True,
            art=await _card_art(self._bot, guess_channel),
        )
        # No file is cached for a confession, so what the rollback undoes here
        # is the orphaned row — a round that never posted but still counts as
        # open.
        game_msg = await _post_round_or_rollback(
            interaction, guess_channel,
            db_path=db_path, round_id=round_id, guild_id=self._guild_id,
            submitter_id=self._submitter_id,
            file=card_file, view=game_view, label="confession",
        )
        # As in the crop editor: done either way, so the timeout must not
        # grey this preview back over the card that replaces it.
        self.stop()
        if game_msg is None:
            return

        # As on the photo path: the view is only worth registering once the
        # message it belongs to exists.
        self._bot.add_view(game_view)

        crop_url = _posted_media_url(game_msg, layouts.CONFESSION_FILENAME)
        await asyncio.to_thread(
            _do_update_round_message, db_path, round_id, game_msg.id, crop_url, ""
        )

        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=self._guild_id, actor_id=self._submitter_id,
            action="confess", round_id=round_id,
            details={},
        )

        # Quest hook: a confession round is still a submitted round — once it
        # is up. See the photo path for why this waits for the post.
        from bot_modules.economy.game_rewards import fire_member_trigger

        await fire_member_trigger(
            self._bot, self._guild_id, self._submitter_id,
            "guess_post", occurrence=str(round_id),
        )

        await interaction.edit_original_response(
            view=card(build_notice_layout(
                "✅ Round Posted", f"It's up in {guess_channel.mention}."
            )),
            attachments=[],
        )

        try:
            await _repost_prompt(self._bot, guess_channel, self._guild_id)
        except Exception:
            log.exception("guess: prompt repost after confession failed for guild %d", self._guild_id)


# ── GuessCog ──────────────────────────────────────────────────────────────────

# Shown before the consent role is granted. This is the disclosure moment, so
# it states what joining stores and for how long — mirroring the Whisper
# opt-in pattern (2026-08 review, guess U1). Plain facts, no hedging.
GUESS_CONSENT_TEXT = layouts.consent_text(ORIGINAL_MAX_AGE_DAYS)


class GuessOptinConsentView(ExpiringLayoutView):
    """Ephemeral consent card for /guess optin (``build_consent_layout``) —
    the role is granted only once the member explicitly presses Join, never
    on command invocation."""

    def __init__(
        self,
        member: discord.Member,
        role: discord.Role,
        db_path: Path | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.member = member
        self.role = role
        self.db_path = db_path
        for item in build_consent_layout(ORIGINAL_MAX_AGE_DAYS):
            self.add_item(item)
        for component_id, callback in (
            (layouts.CONSENT_JOIN_ID, self.confirm),
            (layouts.CONSENT_CANCEL_ID, self.cancel),
        ):
            button = cast("discord.ui.Button[Any]", self.find_item(component_id))
            button.callback = callback  # type: ignore[method-assign]

    async def _answer(
        self, interaction: discord.Interaction, title: str, body: str
    ) -> None:
        """Replace the consent card with its outcome. The card is done, so
        its timeout is retired before the edit."""
        self.stop()
        await interaction.response.edit_message(
            view=card(build_notice_layout(title, body))
        )

    async def confirm(self, interaction: discord.Interaction) -> None:
        if grant_block_reason(self.role, interaction.guild, feature="Guess opt-in") is not None:
            await self._answer(
                interaction,
                "❌ Couldn't Join the Pool",
                "The Guess role can't be handed out right now. "
                "Ask an admin to check the Guess config on the web dashboard.",
            )
            return
        try:
            await self.member.add_roles(self.role, reason="Guess opt-in (consented)")
        except discord.Forbidden:
            await self._answer(
                interaction,
                "❌ Couldn't Join the Pool",
                "I don't have permission to add that role. "
                "Ask an admin to check my role permissions and hierarchy.",
            )
            return
        # Record the evidence *after* the grant succeeds — a consent row for a
        # member who never got the role would misrepresent what happened. A
        # failure to write it must not cost the member their opt-in, so it is
        # logged rather than raised (Art 7(1) evidence, 2026-08 GDPR pass).
        if self.db_path is not None:
            try:
                await asyncio.to_thread(
                    _do_record_consent,
                    self.db_path,
                    interaction.guild_id or 0,
                    self.member.id,
                    time.time(),
                )
            except Exception:
                log.exception(
                    "guess: could not record consent for %s", self.member.id
                )
        await self._answer(
            interaction,
            "✅ Welcome to the Guess Pool",
            "You can now submit images and be guessed at. "
            "Leave anytime with `/guess optout`.",
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await _close_silently(interaction)


class GuessCog(commands.Cog):
    guess = app_commands.Group(
        name="guess",
        description="Guess guessing game commands.",
        guild_only=True,
    )

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        # The channel-bottom Submit/Help prompt, on the shared machinery in
        # core.sticky (per-guild lock, TTL id cache, known-guilds fast path,
        # post-before-delete, shielded placement). This cog only says where the
        # ids live and what the prompt looks like.
        #
        # target_types is the reason this migration waited: the Guess channel is
        # allowed to be a thread or a voice channel's text view, and the shared
        # placer only accepted TextChannel. It is per-panel rather than global
        # because the auction card's channel warning relies on threads staying
        # out of the default set.
        self.prompt_panel = StickyPanel(
            "guess prompt",
            bot,
            load_ids=self._panel_ids,
            save_ids=self._save_panel_ids,
            build=self._build_prompt_panel,
            # Preserves the hand-rolled debounce's timing exactly rather than
            # quietly moving this panel to core's 6s default.
            delay=PROMPT_REPOST_DELAY_SEC,
            target_types=(
                discord.TextChannel, discord.VoiceChannel, discord.Thread
            ),
            refresh_on_update=True,
        )
        super().__init__()

    # ── the prompt's sticky callbacks (core.sticky) ───────────────────────

    def _panel_ids(self, guild_id: int) -> tuple[int, int]:
        """Where the prompt actually is, or (0, 0) for "not posted".

        Reads the prompt's own channel rather than ``guess_channel_id`` so the
        placer's delete aims at the message's real channel. An admin repointing
        the Guess channel therefore leaves the prompt where it is until the next
        round or an explicit repost moves it, instead of orphaning it.

        **Legacy fallback.** ``guess_prompt_channel_id`` arrived with the
        ``core.sticky`` migration, so every guild that already had a prompt has a
        message id and no channel id. Without the fallback those prompts would
        read ``(0, live_id)``: ``should_restick`` bails on a falsy channel so the
        prompt would stop re-sticking altogether, and the first placement could
        not resolve a channel to delete the old one through — leaving two
        prompts, the stale one with live buttons. Before the key existed the
        prompt was always posted into ``guess_channel_id``, so that is the right
        answer for exactly those rows, and the next save records it properly.
        """
        with self.bot.ctx.open_db() as conn:
            config = get_guess_config(conn, guild_id)
        channel_id = config.prompt_channel_id or (
            config.guess_channel_id if config.prompt_message_id else 0
        )
        return channel_id, config.prompt_message_id

    def _save_panel_ids(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        with self.bot.ctx.open_db() as conn:
            set_guess_config_value(
                conn, guild_id, "guess_prompt_channel_id", str(channel_id)
            )
            set_guess_config_value(
                conn, guild_id, "guess_prompt_message_id", str(message_id)
            )

    async def _build_prompt_panel(self, guild: discord.Guild) -> PanelContent:
        try:
            art = await read_guild_lobby_art(self.bot, guild, layouts.TITLE)
        except Exception:
            # Same rule as the round cards (``_card_art``): a failed art
            # read draws a plain panel, never a prompt that can't repaint.
            log.exception("guess: could not read the lobby art for the prompt")
            art = NO_ART
        return PanelContent(layout=GuessPromptView(self.bot, art=art))

    def _prompt_guilds(self) -> set[int] | None:
        """Guilds with a Guess channel set — the only ones whose messages can
        move a prompt. Without this the listener paid a DB read for every
        message in every guild (2026-08-06 review, F3).

        Returns **None** when a legacy ``guild_id = 0`` row exists. This query
        sees guild-scoped rows only, but ``get_guess_config`` reads through
        ``get_config_value``, which falls back to that legacy row — so any guild
        could be inheriting a Guess channel this query cannot enumerate, and
        publishing an incomplete set would drop those guilds off the fast path
        permanently (the restick is the only automatic path, so they would never
        re-stick and never self-heal). None leaves ``_known`` unset and the TTL
        cache carrying the load, which is slower and always correct.
        """
        with self.bot.ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT guild_id FROM config WHERE key = 'guess_channel_id'"
                " AND value NOT IN ('', '0')"
            ).fetchall()
        guilds = {int(r[0]) for r in rows}
        if 0 in guilds:
            return None
        return guilds

    async def cog_load(self) -> None:
        """Re-register persistent GameViews for unsolved rounds (capped) and
        the channel-prompt view."""
        db_path = self.bot.ctx.db_path
        round_ids = await asyncio.to_thread(
            _do_load_unsolved_round_ids, db_path, limit=_COG_LOAD_VIEW_CAP
        )
        for rid in round_ids:
            count = await asyncio.to_thread(
                _do_count_guesses_for_round, db_path, rid
            )
            self.bot.add_view(GameView(self.bot, rid, guess_count=count))
        self.bot.add_view(GuessPromptView(self.bot))
        # Publish the panel-guild set so the on_message listener rejects the
        # overwhelming majority of messages with a set lookup, not a DB read.
        # None means "cannot be computed exactly" — see _prompt_guilds.
        prompt_guilds = await asyncio.to_thread(self._prompt_guilds)
        if prompt_guilds is not None:
            self.prompt_panel.set_known_guilds(prompt_guilds)
        log.info("guess: re-registered %d persistent GameViews (cap %d)",
                 len(round_ids), _COG_LOAD_VIEW_CAP)
        self._age_out_loop.start()

    async def cog_unload(self) -> None:
        self._age_out_loop.cancel()
        self.prompt_panel.cancel_all()

    @tasks.loop(hours=24)
    async def _age_out_loop(self) -> None:
        try:
            cleared = await asyncio.to_thread(
                age_out_originals, self.bot.ctx.db_path
            )
            if cleared:
                log.info("guess: aged out %d stale cached originals", cleared)
        except Exception:
            log.exception("guess: original age-out sweep failed")

        try:
            await self._expire_due_rounds()
        except Exception:
            log.exception("guess: round expiry sweep failed")

        # The age-out above reads the rounds, so it can only see a file some
        # row still points at. This second pass reads the directory, and is
        # the only thing that ever finds a photo whose round was deleted out
        # of band or which landed a moment before its path was recorded.
        try:
            swept = await asyncio.to_thread(
                sweep_orphan_originals,
                self.bot.ctx.db_path,
                guess_orig_dir(self.bot.ctx.db_path),
            )
            if swept:
                log.info("guess: swept %d orphaned cached originals", swept)
        except Exception:
            log.exception("guess: orphaned-original sweep failed")

    async def _expire_due_rounds(self) -> None:
        """Resolve every guild's rounds that outlived ``guess_round_lifetime_days``
        (0 = off), then delete the card of every expired round still standing.
        The claim is in the DB first (``expiry_service``); a delete that fails is
        retried by the next sweep, even with the dial back at 0."""
        db_path = self.bot.ctx.db_path
        for guild in list(self.bot.guilds):
            config = await asyncio.to_thread(_load_config, db_path, guild.id)
            if config.round_lifetime_days > 0:
                claimed = await asyncio.to_thread(
                    claim_expired_rounds, db_path,
                    guild_id=guild.id, lifetime_days=config.round_lifetime_days,
                )
                if claimed:
                    log.info("guess: %d rounds expired in guild %d", len(claimed), guild.id)
            pending = await asyncio.to_thread(
                pending_expired_rounds, db_path, guild_id=guild.id
            )
            for round_row in pending:
                try:
                    await self._clear_expired_card(guild, round_row)
                except Exception:  # noqa: BLE001 - one card must not stop the sweep
                    log.exception("guess: round %d expiry clean-up failed", round_row.id)

    async def _clear_expired_card(
        self, guild: discord.Guild, round_row: GuessRound
    ) -> None:
        """Delete an expired round's card and cached original. Nothing is
        revealed: members consented to a reveal only when a round is solved."""
        channel = guild.get_channel_or_thread(round_row.channel_id)
        delete_message = None
        if isinstance(
            channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            delete_message = channel.get_partial_message(round_row.message_id).delete
        done = await clear_expired_round(
            self.bot.ctx.db_path, round_row, delete_message
        )
        if done:
            await asyncio.to_thread(
                _do_audit, self.bot.ctx.db_path,
                guild_id=guild.id, actor_id=0, action="expire", round_id=round_row.id,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Keep the prompt at the bottom of the Guess channel.

        One line now: the debounce, the per-guild lock, the id cache and the
        known-guilds fast path all live in ``core.sticky``. This used to open a
        fresh DB connection for every message in every guild before it had even
        checked the channel.
        """
        await self.prompt_panel.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_prompt_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear the prompt's ids if its channel was deleted."""
        await self.prompt_panel.on_channel_delete(channel)

    @commands.Cog.listener("on_thread_delete")
    async def _forget_deleted_prompt_thread(self, thread: discord.Thread) -> None:
        """Same, for a thread — which is the case ``target_types`` was widened
        for, and which ``on_guild_channel_delete`` is never dispatched for."""
        await self.prompt_panel.on_channel_delete(thread)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """When a member loses the Guess role, flag their open rounds as
        answer_optout so they can never be guessed again — even if they
        re-acquire the role later.

        Not when a hold took it: a jail, inactive move or Take a Break strips
        every role and gives this one back on release, and none of them is the
        member withdrawing consent. Jail and inactive write their row after
        the strip, so the check waits ``HOLD_SETTLE_SECONDS`` and reads the
        member as they are then — row or hold role.
        """
        before_role_ids = {r.id for r in before.roles}
        after_role_ids = {r.id for r in after.roles}
        removed = before_role_ids - after_role_ids
        if not removed:
            return
        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, after.guild.id)
        if config.guess_role_id == 0 or config.guess_role_id not in removed:
            return
        await asyncio.sleep(HOLD_SETTLE_SECONDS)
        live = after.guild.get_member(after.id)
        role_ids = {r.id for r in live.roles} if live is not None else after_role_ids
        if await asyncio.to_thread(_is_held, db_path, after.guild.id, after.id, role_ids):
            log.info("guess: role loss for user %d is a hold, not an opt-out", after.id)
            return
        flagged = await asyncio.to_thread(
            _do_flag_user_open_rounds_optout,
            db_path,
            guild_id=after.guild.id,
            user_id=after.id,
        )
        if flagged:
            log.info(
                "guess: %d open rounds flagged answer_optout for user %d (role removed)",
                flagged, after.id,
            )
        # Losing the role is the one choke point both /guess optout and a mod
        # removal pass through, so the consent record is stamped withdrawn here
        # rather than in the command (Art 7(3)).
        await asyncio.to_thread(
            _do_withdraw_consent, db_path, after.guild.id, after.id, time.time()
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """A member who leaves takes their consent with them: flag their open
        rounds unsolvable, stamp consent withdrawn and delete their cached
        originals now (``cleanup_service.withdraw_departed_member``), rather
        than leaving rounds nobody can win and a photo on disk until the
        age-out. Never raises into the other leave listeners."""
        if member.bot:
            return
        db_path = self.bot.ctx.db_path
        try:
            flagged, deleted = await asyncio.to_thread(
                withdraw_departed_member,
                db_path,
                guild_id=member.guild.id,
                user_id=member.id,
                when=time.time(),
            )
        except Exception:  # noqa: BLE001 - a leave must never break the other listeners
            log.exception("guess: departure cleanup failed in guild %d", member.guild.id)
            return
        if flagged or deleted:
            log.info(
                "guess: departure closed %d open rounds and deleted %d originals (guild %d)",
                flagged, deleted, member.guild.id,
            )

    @guess.command(name="submit", description="Submit an image to start a Guess round.")
    @app_commands.describe(
        image="The image to submit (SFW or NSFW)",
    )
    async def guess_submit(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        refusal = _submit_gate(
            interaction, config, content_type=image.content_type, size=image.size
        )
        if refusal:
            await interaction.followup.send(refusal, ephemeral=True)
            return

        image_bytes = await image.read()
        original_ext = (Path(image.filename).suffix or ".jpg").lower()

        await _pipeline_and_open_editor(
            interaction, self.bot, config, image_bytes, original_ext=original_ext
        )


    @guess.command(name="round", description="Inspect a Guess round (mods only).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(round_id="Round ID to inspect")
    async def guess_round(
        self,
        interaction: discord.Interaction,
        round_id: int,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        member = interaction.guild.get_member(interaction.user.id)
        if not (member and member.guild_permissions.manage_guild):
            await interaction.followup.send(
                "❌ Only mods (manage_guild permission) can inspect rounds.",
                ephemeral=True,
            )
            return

        db_path = self.bot.ctx.db_path
        round_row = await asyncio.to_thread(_do_load_round, db_path, round_id)
        if round_row is None or round_row.guild_id != interaction.guild.id:
            await interaction.followup.send(
                f"❌ Round #{round_id} not found.", ephemeral=True
            )
            return

        guess_count = await asyncio.to_thread(
            _do_count_guesses_for_round, db_path, round_id
        )
        unique_count = await asyncio.to_thread(
            _do_count_unique_guessers_for_round, db_path, round_id
        )

        name_fn = await build_name_fn(
            guild=interaction.guild,
            db_path=db_path,
            guild_id=interaction.guild.id,
            user_ids=[
                round_row.submitter_id,
                round_row.answer_id,
                round_row.solver_id or 0,
            ],
        )
        await interaction.followup.send(
            view=card(build_inspector_layout(
                round_row, guess_count, unique_count, name_fn=name_fn
            )),
            ephemeral=True,
        )


    @guess.command(name="delete", description="Delete a Guess round (submitter or mod only).")
    @app_commands.describe(round_id="Round ID to delete")
    async def guess_delete(
        self,
        interaction: discord.Interaction,
        round_id: int,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        round_row = await asyncio.to_thread(_do_load_round, db_path, round_id)
        if round_row is None or round_row.guild_id != interaction.guild.id:
            await interaction.followup.send(
                f"Round #{round_id} not found.", ephemeral=True
            )
            return

        if round_row.deleted_at is not None:
            await interaction.followup.send(
                f"❌ Round #{round_id} is already deleted.", ephemeral=True
            )
            return

        is_submitter = interaction.user.id == round_row.submitter_id
        member = interaction.guild.get_member(interaction.user.id)
        is_mod = bool(member and member.guild_permissions.manage_guild)
        if not (is_submitter or is_mod):
            await interaction.followup.send(
                "❌ Only the submitter or a mod can delete this round.", ephemeral=True
            )
            return

        await asyncio.to_thread(_do_soft_delete_round, db_path, round_id)
        await asyncio.to_thread(
            _do_audit, db_path,
            guild_id=interaction.guild.id, actor_id=interaction.user.id,
            action="delete", round_id=round_id,
            details={"by_mod": is_mod and not is_submitter},
        )

        if round_row.original_path:
            orig_path = Path(round_row.original_path)
            if orig_path.exists():
                await asyncio.to_thread(orig_path.unlink, missing_ok=True)
            # Clear the reference too. A soft-deleted round still recording a
            # path is invisible to both cleanup passes — the age-out skips
            # deleted rounds, and the orphan sweep counts the path as live —
            # so a failed unlink here would strand the file for good.
            await asyncio.to_thread(_do_set_original_path, db_path, round_id, "")

        if round_row.channel_id and round_row.message_id:
            channel = interaction.guild.get_channel(round_row.channel_id)
            if isinstance(
                channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
            ):
                try:
                    msg = await channel.fetch_message(round_row.message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

        await interaction.followup.send(
            f"Round #{round_id} deleted.", ephemeral=True
        )


    @guess.command(name="optin", description="Join the Guess pool — add the Guess role to yourself.")
    async def guess_optin(self, interaction: discord.Interaction) -> None:
        await self._optin_impl(interaction)

    async def _optin_impl(self, interaction: discord.Interaction) -> None:
        """Shared by ``/guess optin`` and the ``/info`` panel's Join button.

        Factored out for the same reason ``WhisperCog._optin_impl`` was: the
        consent gate below is the whole feature's compliance story, and a
        second caller must go through it rather than around it.
        """
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        if config.guess_role_id == 0:
            await interaction.followup.send(
                "❌ Guess role is not configured. Ask an admin to set it in the web dashboard.",
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(config.guess_role_id)
        if role is None:
            await interaction.followup.send(
                "❌ Guess role is configured but no longer exists. "
                "Ask an admin to fix the Guess config in the web dashboard.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.followup.send(
                "❌ Couldn't find you in this server.", ephemeral=True
            )
            return

        if _has_guess_role(member, config.guess_role_id):
            await interaction.followup.send(
                f"❌ You're already in the Guess pool ({role.mention}).",
                ephemeral=True,
            )
            return

        # Consent gate: the role is granted by the view's Join button, not
        # here — the member reads what joining stores before anything changes.
        consent = GuessOptinConsentView(member, role, self.bot.ctx.db_path)
        consent.message = await interaction.followup.send(
            view=consent, ephemeral=True, wait=True
        )

    @guess.command(
        name="optout",
        description="Leave the Guess pool — removes the Guess role from you.",
    )
    async def guess_optout(self, interaction: discord.Interaction) -> None:
        await self._optout_impl(interaction)

    async def _optout_impl(self, interaction: discord.Interaction) -> None:
        """Shared by ``/guess optout`` and the ``/info`` panel's Leave button."""
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        if config.guess_role_id == 0:
            await interaction.followup.send(
                "❌ Guess role is not configured. Ask an admin to set it in the web dashboard.",
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(config.guess_role_id)
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.followup.send(
                "❌ Couldn't find you in this server.", ephemeral=True
            )
            return

        if role is None or not _has_guess_role(member, config.guess_role_id):
            await interaction.followup.send(
                "❌ You're not in the Guess pool.", ephemeral=True
            )
            return

        try:
            await member.remove_roles(role, reason="Guess opt-out (self-service)")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to remove that role. "
                "Ask a mod to remove it for you.",
                ephemeral=True,
            )
            return

        # The on_member_update listener sees the role removal and flags the
        # member's open rounds answer_optout — no extra bookkeeping here.
        await interaction.followup.send(
            "You've left the Guess pool. Any open rounds where you're the "
            "answer are no longer solvable; your past rounds and stats stay. "
            "Rejoin anytime with `/guess optin`.",
            ephemeral=True,
        )

    @guess.command(name="confess", description="Submit an anonymous text confession.")
    @app_commands.describe(text="Your anonymous confession.")
    async def guess_confess(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        assert interaction.guild
        await interaction.response.defer(ephemeral=True)

        db_path = self.bot.ctx.db_path
        config = await asyncio.to_thread(_load_config, db_path, interaction.guild.id)

        if config.guess_role_id == 0 or config.guess_channel_id == 0:
            await interaction.followup.send(
                "❌ Guess is not fully configured. Ask an admin to set it up in the web dashboard.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not _has_guess_role(member, config.guess_role_id):
            await interaction.followup.send(
                "❌ You need the Guess role to submit a confession.", ephemeral=True
            )
            return

        text = text.strip()
        if not text:
            await interaction.followup.send("❌ Confession text cannot be empty.", ephemeral=True)
            return

        if contains_disallowed_content(text):
            await interaction.followup.send(
                "❌ That confession contains disallowed content. Please rephrase.", ephemeral=True
            )
            return

        card_bytes = await asyncio.to_thread(render_quote, text)
        preview = ConfessionPreviewView(
            self.bot,
            text,
            interaction.guild.id,
            config.guess_channel_id,
            submitter_id=interaction.user.id,
        )
        preview.message = await interaction.followup.send(
            file=discord.File(
                io.BytesIO(card_bytes),
                filename=layouts.CONFESSION_PREVIEW_FILENAME,
            ),
            view=preview,
            ephemeral=True,
            wait=True,
        )


    @guess.command(name="leaderboard", description="Show the top Guess posters and guessers.")
    async def guess_leaderboard(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        await interaction.response.defer()

        db_path = self.bot.ctx.db_path
        posters, guessers = await asyncio.gather(
            asyncio.to_thread(_do_get_top_posters, db_path, interaction.guild.id),
            asyncio.to_thread(_do_get_top_guessers, db_path, interaction.guild.id),
        )

        name_fn = await build_name_fn(
            guild=interaction.guild,
            db_path=db_path,
            guild_id=interaction.guild.id,
            user_ids=[r[0] for r in posters] + [r[0] for r in guessers],
        )
        art = await _card_art(self.bot, interaction.channel)
        await interaction.followup.send(
            view=card(build_leaderboard_layout(
                posters, guessers, name_fn=name_fn, **_art_kwargs(art),
            ))
        )


    async def post_prompt_panel(self, guild, channel=None):
        """Place the channel-bottom Submit/Help prompt.

        Entry point for the dashboard's panel poster (``panel_registry``);
        replaced /guess prompt on 2026-07-28. ``channel`` is ignored — the
        prompt belongs in the configured Guess channel, and posting it anywhere
        else would leave the submit button outside the flow it drives. Returns
        None when no Guess channel is set or it can't be posted to.
        """
        config = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, guild.id)
        if config.guess_channel_id == 0:
            return None
        target = guild.get_channel_or_thread(config.guess_channel_id)
        if not isinstance(
            target, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            return None
        # place_or_refresh, not place: re-running this after a re-brand should
        # repaint the prompt where it is rather than hop it to the bottom.
        await self.prompt_panel.place_or_refresh(guild, target)
        return target


async def setup(bot: "Bot") -> None:
    await bot.add_cog(GuessCog(bot))
