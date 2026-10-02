"""Pure helpers for the data-deletion flow — no Discord, no DB calls.

These functions exist so the bookkeeping inside
``deletion_service.delete_discord_messages`` can be unit-tested without
standing up a fake guild. That engine owns the network calls
(``channel.delete_messages``, ``fetch_message``, etc.) and is driven by both
surfaces — ``/mydata`` and the dashboard's Member Data panel — while
everything testable about *what to delete* and *how to report progress*
lives here.

:data:`MEMBER_ERASABLE_STORES` also lives here: the list of bot-side stores a
member can clear themselves from the ``/mydata`` panel. It is the one place
that list is written — each entry carries its own eraser, so the cog builds a
button per entry and calls ``store.eraser`` with no second registry to keep in
step, and ``tests/privacy/test_privacy_member_data_panel.py`` checks the list against
the member-erasable rows of ``docs/data_register.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from bot_modules.core import meters
from bot_modules.games.guess import (
    cleanup_service as guess_cleanup_service,
    repo as guess_repo,
)
from bot_modules.whisper import repo as whisper_repo
from bot_modules.privacy.disclosure_copy import GUESS_ROUNDS_ERASED, GUESS_SOLVER_SURVIVES
from bot_modules.games.guess.cleanup_service import DeletePost

# Discord's bulk-delete API only accepts messages younger than 14 days; older
# messages have to be deleted one at a time. The partition helper below uses
# this to split a per-channel batch into "recent" (bulk-eligible) and "old"
# (single-shot) lists.
FOURTEEN_DAYS = timedelta(days=14)

# Default width of the textual progress bar shown while deletion runs.
_DEFAULT_BAR_WIDTH = 20

# Deletion scope — every mode only clears the member's *Discord* messages and
# leaves the server-side data (XP, activity, profile) intact. The modes differ
# solely in which messages go: "all" removes every message, "media"/"text"
# remove just that slice.
MODE_ALL = "all"
MODE_MEDIA = "media"
MODE_TEXT = "text"

# Discord auto-generates an embed for any link a member posts, so embed
# presence alone doesn't mean media. Only these types are real media; a
# link preview ("link"/"article"/"rich") leaves the message plain text.
_MEDIA_EMBED_TYPES = frozenset({"image", "video", "gifv"})


def message_has_media(message: object) -> bool:
    """True when *message* carries real media rather than plain text.

    Counts uploads (attachments, stickers) and media embeds. A posted image
    *URL* has no attachment but does produce an ``image``-type embed, so it
    counts too — from the member's point of view they posted a picture either
    way. A bare link with an article/rich preview does not.
    """
    if getattr(message, "attachments", None):
        return True
    if getattr(message, "stickers", None):
        return True
    for embed in getattr(message, "embeds", None) or ():
        if getattr(embed, "type", None) in _MEDIA_EMBED_TYPES:
            return True
    return False


def message_matches_mode(message: object, mode: str) -> bool:
    """True when *message* is in scope for *mode*.

    Used as the scan predicate, so an out-of-scope message is never collected
    and therefore never deleted.
    """
    if mode == MODE_MEDIA:
        return message_has_media(message)
    if mode == MODE_TEXT:
        return not message_has_media(message)
    return True


def is_forum_thread(channel: object) -> bool:
    """Return True if *channel* is a forum-thread (a Thread under a ForumChannel).

    Forum-thread OPs (where ``message_id == channel_id``) need special handling:
    deleting them would nuke the whole thread, so the cog re-posts a tombstone
    under the bot. The cog calls this to decide whether to take that branch.
    """
    if not isinstance(channel, discord.Thread):
        return False
    return isinstance(channel.parent, discord.ForumChannel)


def group_messages_by_channel(
    msg_rows: list[tuple[int, int]],
) -> dict[int, list[int]]:
    """Bucket ``(message_id, channel_id)`` pairs into ``{channel_id: [msg_ids]}``.

    The scan in ``find_user_messages`` returns a flat list per guild because
    that's the cheapest shape over the discord.py iteration API. The cog
    then needs them grouped per-channel so each channel is fetched at most
    once and its archive state managed in a single block.
    """
    by_channel: dict[int, list[int]] = {}
    for message_id, channel_id in msg_rows:
        by_channel.setdefault(channel_id, []).append(message_id)
    return by_channel


def partition_by_bulk_delete_window(
    message_ids: list[int],
    now: datetime | None = None,
) -> tuple[list[int], list[int]]:
    """Split message IDs by the 14-day bulk-delete cutoff.

    Returns ``(recent, old)`` where:
      - ``recent`` is bulk-delete eligible (Discord allows up to 100 per call).
      - ``old`` must be deleted one at a time.

    The split uses ``discord.utils.snowflake_time`` so the result is purely
    a function of the message ID — no need to fetch any Discord state.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - FOURTEEN_DAYS

    recent: list[int] = []
    old: list[int] = []
    for mid in message_ids:
        if discord.utils.snowflake_time(mid) > cutoff:
            recent.append(mid)
        else:
            old.append(mid)
    return recent, old


def chunk_for_bulk_delete(message_ids: list[int], chunk_size: int = 100) -> list[list[int]]:
    """Chunk message IDs into lists no longer than ``chunk_size``.

    Discord caps ``channel.delete_messages`` at 100 IDs per request. The
    cog uses 100; the param is exposed so tests can use a smaller cap
    without hand-crafting 100-element fixtures.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [
        message_ids[i : i + chunk_size]
        for i in range(0, len(message_ids), chunk_size)
    ]


def render_progress_bar(done: int, total: int, *, width: int = _DEFAULT_BAR_WIDTH) -> str:
    """Render a textual progress bar of the form ``` `▰▰▰▰▱▱▱▱ 12/40` ```.

    Matches the format used in ``_run_deletion`` so the cog can call this
    directly. A ``total`` of zero renders a full bar (defensive — there's
    nothing to delete, so the run is effectively complete).
    """
    if width <= 0:
        raise ValueError("width must be positive")
    if total <= 0:
        # Nothing to delete, so the run is effectively complete — the shared
        # fill reads a non-positive target as *empty*, which is the opposite
        # of what this card wants, so it's decided here.
        bar = meters.BAR_FILLED * width
    else:
        # fill() clamps, which matters here: done can exceed total when failed
        # + replaced + deleted double-counts a single message.
        bar = meters.fill(done, total, width)
    return meters.mono(f"{bar} {done}/{total}")


def render_scan_status(done: int, total: int, found: int) -> str:
    """Format the "Scanning the server…" status string.

    Extracted so the wording can be tested without mocking out the entire
    interaction edit path in the cog.
    """
    return (
        f"Scanning the server for your messages — channel **{done}/{total}** "
        f"(**{found}** found so far)…"
    )


def mode_noun(mode: str) -> str:
    """The member-facing word for what *mode* targets."""
    if mode == MODE_MEDIA:
        return "images & files"
    if mode == MODE_TEXT:
        return "text messages"
    return "messages"


def render_confirm_prompt(
    *,
    mode: str,
) -> str:
    """Copy shown immediately before the irreversible click.

    Always addressed to the member deleting their own messages. The mod-facing
    variant went with ``/delete_user``: an admin deleting someone else's
    messages does it from the dashboard's Member Data panel, which renders its
    own confirm and never calls this.

    This is the moment consent is given, so it states the real scope: only the
    member's Discord messages go, and their XP, activity and profile stay — the
    person is told that here rather than discovering it in the summary
    afterwards.

    It deliberately says **nothing** about the server keeping its own record of
    a message. Until 2026-09-18 it did, and the sentence ("the server keeps its
    own copy of your messages for moderation — as metadata (who, where, when —
    not the text)") was read as content retention however it was qualified: by
    the time the parenthetical arrived the member had already read "copy of
    your messages". The bot has held no message text since 2026-09-14
    (migration 228); what survives is a row in ``messages`` saying a post
    happened. That is disclosed where a member can read it properly — the
    privacy notice in ``manual.html`` §Your Data & Privacy today, and the
    ``privacy-tools`` doc once its rewritten body
    (``docs/proposals/privacy-tools-body.md``, staged but not yet saved and
    re-posted from the Docs panel) goes live — not in six words before an
    irreversible button.
    """
    who = "your"
    whose = "Your"
    noun = mode_noun(mode)

    detail = ""
    if mode == MODE_MEDIA:
        detail = " — every message with an attachment, sticker, or embedded image/video"
    elif mode == MODE_TEXT:
        detail = " — every message that carries no attachment or media"

    return (
        f"⚠️ **This will delete {who} {noun} from Discord**{detail}.\n\n"
        f"{whose} XP, activity, and profile stay exactly as they are.\n\n"
        "This cannot be undone. Are you sure?"
    )


def confirm_button_label(mode: str) -> str:
    """Label for the danger button — names the real scope (messages only).

    Always "my": this is the member's own button. The "their" variant went with
    ``/delete_user`` — see :func:`render_confirm_prompt`.
    """
    if mode == MODE_MEDIA:
        return "Yes, delete my images & files"
    if mode == MODE_TEXT:
        return "Yes, delete my text messages"
    return "Yes, delete my messages"


#: The second danger button every My Data deletion passes through. Billy,
#: 2026-09-27: *"All of the deletion buttons need to have a confirm too, just
#: in case"* — the first confirm already existed, so "too" became a second
#: one. Worded differently from every first-step label on purpose: a member
#: double-clicking through a button that stays under their cursor must not
#: land on an identical one.
FINAL_CONFIRM_LABEL = "Delete it, I'm sure"


def render_final_confirm(what: str) -> str:
    """The last-check copy, shown after the first confirm is pressed.

    *what* is the subject of the deletion as a sentence start — "Your whisper
    history", "Your messages". The first step already said what goes and what
    survives; this one only has to make the member stop once more.
    """
    return (
        f"🛑 **Last check.** {what} will be gone for good. There is no undo, "
        "and nobody on the server can bring it back.\n\n"
        f"Press **{FINAL_CONFIRM_LABEL}** to go ahead, or Cancel."
    )



# ── Delete everything ───────────────────────────────────────────────────────
#
# Not an ErasableStore: its eraser is the departure recipe
# (``self_erasure_service``), not a purge function, and the store contract is
# "the same function purge_user_data runs". It gets its own button, below the
# stores, and its own copy here.

EVERYTHING_LABEL = "Delete everything"
EVERYTHING_CONFIRM_LABEL = "Yes, delete everything"
EVERYTHING_SUBJECT = "Your messages, and everything the bot links to you"

#: Billy, 2026-09-27: *"just erase"* — a member mid-bet, mid-game, owning a
#: voice room or paired in Pen Pals loses it, with no check first. This
#: sentence is the only guard, so ``tests/privacy/test_privacy_modes.py`` pins it in
#: the rendered confirm.
EVERYTHING_IN_PROGRESS = (
    "**Anything in progress is lost:** a bet or casino stake, a game you're "
    "in, a voice room you own, a Pen Pals pairing."
)


def render_everything_confirm() -> str:
    """The first confirm for Delete everything: what goes, what is cut loose,
    what is lost mid-flight, and what stays."""
    return (
        "⚠️ **Delete everything?**\n\n"
        "This deletes every message you've posted in this server and your Guess "
        "Who data here. Everything else the bot holds on you — XP, coins, "
        "items, game history, whispers, your profile — is cut loose from your "
        "account for good: it stays in the server's totals, but it is no "
        "longer linked to you. You start again from nothing.\n\n"
        f"{EVERYTHING_IN_PROGRESS} If you're on a break, it ends first and "
        "your roles come back.\n\n"
        "Your moderation record (warnings, jails, tickets) and your "
        "no-contact list stay tied to you. The message scan runs first and "
        "can take a while; if any message won't delete, nothing else is "
        "touched."
    )


def render_everything_summary(
    status: str, *, deleted: int = 0, failed: int = 0, detail: str = "", all_guilds: bool = False
) -> str:
    """The card a Delete everything press ends on, one per outcome.

    *status* is a ``self_erasure_service.STATUS_*`` value, passed as a plain
    string so this module stays free of the service import.
    """
    if status == "done":
        where = (
            "everywhere this bot knows you"
            if all_guilds
            else "in this server — run it in any other server you share with "
            "this bot if you want the same there"
        )
        return (
            f"✅ **Done.** {deleted} message(s) deleted, your Guess Who data "
            f"deleted, and everything else cut loose from your account {where}."
        )
    if status == "held":
        what = "you're in jail" if detail == "jailed" else "you have an open ticket"
        if deleted:
            return (
                f"❌ **Stopped partway.** {deleted} message(s) were already deleted, "
                f"but {what} here, and that needs a mod before anything else can go. "
                "Ask one, and try again once it's resolved."
            )
        return (
            f"❌ **Nothing was deleted** — {what} here, and that needs a mod. "
            "Ask one, and try again once it's resolved."
        )
    if status == "messages_left":
        return (
            f"⚠️ {deleted} message(s) deleted, but {failed} wouldn't go, so "
            "nothing else was touched. Press **Delete everything** again in a "
            "few minutes to finish."
        )
    if status == "break_stuck":
        return (
            f"⚠️ {deleted} message(s) deleted, but your break couldn't be ended "
            "just now, so nothing else was touched. Try again in a minute."
        )
    return (
        "❌ Something went wrong partway. Your messages and Guess Who posts "
        "may already be gone; nothing else was changed. Try again in a "
        "minute, and if it keeps failing, open a ticket."
    )

def render_deletion_summary(
    *,
    deleted: int,
    failed: int,
    replaced: int,
    mode: str = MODE_ALL,
) -> str:
    """Build the final "All done. Here's what was removed:" report.

    Only Discord messages are ever removed; server-side data is retained. The
    copy is deliberately neutral (no "your") because the dashboard's Member Data
    panel shows this summary to the acting admin, not to the subject. Returning
    the same string the cog would render makes this trivially snapshot-testable,
    and lets ``deletion_status`` report a finished run in identical words.
    """
    lines = [
        "All done. Here's what was removed:",
        f"{mode_noun(mode).capitalize()} deleted from Discord: **{deleted}**",
        "XP, activity, and profile: **kept for moderation**.",
    ]
    if replaced:
        lines.append(f"Forum posts replaced with tombstone: **{replaced}**")
    if failed:
        lines.append(f"Messages that couldn't be deleted (no access): **{failed}**")
    return "\n".join(lines)


def render_empty_summary(*, mode: str = MODE_ALL) -> str:
    """Return the "no messages found" summary string.

    Used when the scan turns up nothing on Discord. Nothing server-side is ever
    touched, so this just reports that no messages were found.
    """
    return (
        f"All done. No {mode_noun(mode)} found in any channel I can read. "
        "Nothing else was touched — XP, activity, and profile stay as they are."
    )


def should_throttle(
    last_update: float,
    now: float,
    *,
    done: int,
    total: int,
    interval: float,
) -> bool:
    """Return True if a progress update should be skipped to respect rate limits.

    Discord rate-limits ``edit_original_response`` aggressively. The cog
    coalesces updates so at most one fires per ``interval`` seconds — but
    always lets the final update through (``done >= total``) so the user
    sees the completion state. Both the scan and delete phases use the same
    pattern with different intervals (2.0s and 1.5s respectively).
    """
    if done >= total:
        return False
    return (now - last_update) < interval


# ---------------------------------------------------------------------------
# The /mydata panel
# ---------------------------------------------------------------------------
#
# One member-facing surface for "what does this server hold on me, and what
# can I delete". It replaced two commands on 2026-09-17: ``/delete_me``
# (Discord messages) and ``/whisper forget-me`` (one feature's records). A
# feature shipping its own forget-me was how the second one happened, and it
# left an operator erasure reaching no whisper at all while the member could
# clear the lot themselves — bug #222, item 3. New features add an entry
# below instead of a command, and a guard test fails the suite if one tries.
#
# Not everything a member can delete belongs here. ``/birthday remove`` and
# ``/voice profile reset`` each delete a stored row, but they are also how the
# member turns that feature off, so the off switch would go with them; they
# stay on their feature. The rule is about a surface whose *only* job is
# erasure.


@dataclass(frozen=True)
class ErasableStore:
    """A bot-side store the member can clear from the ``/mydata`` panel.

    ``register_tables`` names the tables the button actually clears, and is
    what ties this entry to its row in ``docs/data_register.md``: a register
    row marked **Member-erasable** must appear here, and vice versa. That is
    the second half of the panel contract — the first is that no cog defines
    an erase-my-data command of its own.

    ``eraser`` rides on the entry rather than in a lookup table beside it, so
    a store is declared exactly once: a new feature adds one ``ErasableStore``
    and is wired, instead of adding a row here and remembering a second row in
    the cog, where the omission would surface as a ``KeyError`` under the
    member's finger rather than as a missing button.
    """

    key: str
    label: str
    #: Panel line — what the member is told the button covers.
    summary: str
    #: The irreversible-click copy. States what survives, because a member who
    #: reads "delete everything" and keeps seeing their reports was misled.
    confirm: str
    confirm_label: str
    #: The store as a sentence start, for the final "Last check" step —
    #: "Your whisper history". Written out rather than derived from ``label``,
    #: which is title-cased for a button and reads wrong mid-sentence.
    subject: str
    done: str
    register_tables: tuple[str, ...]
    #: The deletes themselves, ``(conn, *, guild_id, user_id)``. This is the
    #: *same* function ``purge_user_data`` runs for the store, not a parallel
    #: implementation — the member's button and an operator erasure drifting
    #: apart is what made bug #222's whisper item a bug rather than a wording
    #: fix. It takes a ``conn`` so the purge can run it inside its own
    #: transaction.
    eraser: Callable[..., None]
    #: What has to leave Discord and the disk before the rows go, as
    #: ``async (db_path, *, guild_id, user_id, delete_post)``, or ``None``.
    #: Run first and outside the eraser's transaction, because neither a
    #: Discord delete nor an unlink can be rolled back. It raises when
    #: something would not go, and the rows are then left for a retry.
    clear_media: Callable[..., Awaitable[None]] | None = None


WHISPER_STORE = ErasableStore(
    key="whispers",
    label="Whisper history",
    summary="every whisper you sent or received here, with its replies and guesses",
    confirm=(
        "⚠️ **This will permanently delete every whisper you sent or received "
        "in this server**, along with their replies, guesses and reports.\n\n"
        "Reports you filed on someone else's whisper stay — those are about "
        "their message, not yours.\n\n"
        "This cannot be undone. Are you sure?"
    ),
    confirm_label="Yes, delete my whispers",
    subject="Your whisper history",
    done="Your whisper history for this server has been deleted.",
    register_tables=(
        "whispers",
        "whisper_replies",
        "whisper_guesses",
        "whisper_reports",
        "whisper_reply_reports",
    ),
    eraser=whisper_repo.forget_member,
)


async def _clear_guess_media(
    db_path: Path, *, guild_id: int, user_id: int, delete_post: DeletePost
) -> None:
    await guess_cleanup_service.clear_member_media(
        db_path, user_id, guild_id=guild_id, delete_post=delete_post
    )


GUESS_STORE = ErasableStore(
    key="guess",
    label="Guess Who data",
    summary=(
        "every Guess Who round you submitted or were the answer to, with its "
        "photo and post, and the guesses you made"
    ),
    confirm=(
        "⚠️ **This will permanently delete your Guess Who data in this server.**\n\n"
        f"{GUESS_ROUNDS_ERASED[0].upper()}{GUESS_ROUNDS_ERASED[1:]} "
        "Guesses you made on other rounds, and guesses naming you, go too.\n\n"
        f"{GUESS_SOLVER_SURVIVES} Whether you are opted in to Guess Who does "
        "not change.\n\n"
        "This cannot be undone. Are you sure?"
    ),
    confirm_label="Yes, delete my Guess Who data",
    subject="Your Guess Who data",
    done="Your Guess Who data for this server has been deleted.",
    register_tables=("guess_rounds", "guess_guesses", "guess_audit_log"),
    eraser=guess_repo.forget_member,
    clear_media=_clear_guess_media,
)

#: Every store the panel offers, in button order.
MEMBER_ERASABLE_STORES: tuple[ErasableStore, ...] = (WHISPER_STORE, GUESS_STORE)


def render_panel(*, stores: tuple[ErasableStore, ...] = MEMBER_ERASABLE_STORES) -> str:
    """The ``/mydata`` panel body.

    Two sections, because "delete from Discord" and "delete from the bot" are
    genuinely different acts and the old flat command list blurred them: the
    top half removes the member's messages from Discord and touches nothing
    server-side, the bottom half deletes the server's own records.

    The panel is honest about what neither half reaches — XP, profile and the
    moderation record — rather than leaving the member to discover it in the
    summary afterwards.

    Like :func:`render_confirm_prompt`, it says nothing about the server's own
    metadata row for a message: that phrasing reads as content retention no
    matter how it's qualified, and this panel is the first thing a member
    sees in the flow — the same reasoning applies here even more than at the
    confirm step. The disclosure lives in the privacy notice instead.
    """
    lines = [
        "**Your data in this server.** Everything here deletes only *your* "
        "data, and nothing here can be undone.",
        "",
        "**Your Discord messages**",
        "Remove your own messages from the server's channels.",
        "",
        "**Data this server holds**",
    ]
    if stores:
        lines += [f"• **{store.label}** — {store.summary}" for store in stores]
    else:
        # Not reachable today, and cheaper than letting the panel render a
        # bare heading if the last store is ever retired.
        lines.append("• Nothing here is member-deletable right now.")
    lines += [
        "",
        f"**{EVERYTHING_LABEL}**",
        "Your messages and Guess Who data deleted, and everything else the bot "
        "holds on you — XP, coins, profile, game history — cut loose from your "
        "account for good.",
        "",
        "Your moderation record (warnings, jails, tickets) stays whatever you "
        "press — ask a mod if you need that looked at.",
    ]
    return "\n".join(lines)
