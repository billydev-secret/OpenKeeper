"""Pure decisions for Needle (Auto-Thread).

The cog is glue: it finds a thread's starter, reads their reply-DM opt-in and
the no-contact pair, and asks here what to do with them.
"""

from __future__ import annotations

from typing import Literal, TypedDict

import discord

from bot_modules.core.utils import jump_url
from bot_modules.dm.batch.logic import join_names

MuteAction = Literal["mute", "unmute"]

#: The reply DM's toggle button. Carries everything the press needs, because it
#: is clicked in a DM — there is no guild or channel on the interaction.
#:
#: Since reply DMs went opt-in (migration 244) the press changes the member's
#: one server-wide switch, so only ``guild_id`` is read: ``mute`` turns reply
#: DMs off and ``unmute`` turns them back on. The channel and thread fields
#: stay in the format because DMs sent since 2026-09-12 carry it, and their
#: buttons must still dispatch after a restart.
REPLY_DM_BUTTON_TEMPLATE = (
    r"needle_dm:(?P<action>mute|unmute):(?P<guild_id>\d+)"
    r":(?P<channel_id>\d+):(?P<thread_id>\d+)"
)


def reply_dm_custom_id(
    action: MuteAction, *, guild_id: int, channel_id: int, thread_id: int
) -> str:
    return f"needle_dm:{action}:{guild_id}:{channel_id}:{thread_id}"


def resolve_starter_id(
    *, starter_author_id: int | None, owner_id: int, bot_id: int
) -> int:
    """Who started the thread, as a member id (0 if unknown).

    The starter message's author wins. Failing that (the post was deleted, or a
    member opened the thread by hand), ``owner_id`` stands in — unless it is the
    bot's own account, which owns every thread Needle opens and must never be
    taken for the poster.
    """
    if starter_author_id:
        return starter_author_id
    if owner_id and owner_id != bot_id:
        return owner_id
    return 0


def can_manage_thread(
    *,
    user_id: int,
    owner_id: int,
    starter_author_id: int,
    has_manage_threads: bool,
) -> bool:
    """May this member archive or rename the thread?

    Needle opens the thread itself, so Discord records the *bot* as its owner:
    checking ``owner_id`` alone locked every poster out of their own thread.
    The member who wrote the starter message counts as the owner. ``owner_id``
    still counts, for a thread a member opened by hand in the channel. An id of
    0 means "unknown" and never matches.
    """
    if has_manage_threads:
        return True
    return user_id != 0 and user_id in {owner_id, starter_author_id}


def should_dm_starter(
    *,
    notify_enabled: bool,
    starter_author_id: int,
    replier_id: int,
    replier_is_bot: bool,
    opted_in: bool,
    no_contact: bool,
) -> bool:
    """Should this reply DM the member who started the thread?

    Both switches must be on: the channel's dial (an admin's) and the
    starter's own opt-in (Help & Settings → Roles & Pings). Reply DMs are
    opt-IN since migration 244 — a member who never chose is not DMed.

    A no-contact pair drops the DM, and only the DM: the reply is the replier's
    own message and stays up, and nothing tells either side a DM was withheld.
    """
    return (
        notify_enabled
        and opted_in
        and starter_author_id > 0
        and not replier_is_bot
        and replier_id != starter_author_id
        and not no_contact
    )


#: The ``dm_batch`` kind for reply DMs; ``group_key`` is the thread id.
REPLY_BATCH_KIND = "needle_reply"


def build_reply_digest_text(
    *,
    replier_names: list[str],
    reply_count: int,
    thread_name: str,
    guild_name: str,
    guild_id: int,
    thread_id: int,
    first_reply_id: int,
) -> str:
    """One DM for a thread's replies over the batch window.

    ``replier_names`` arrive already escaped (``build_name_fn``), each once, in
    the order they first replied; the thread name is member-authored, so it is
    escaped here. A single reply reads exactly as the one-per-reply DM did.
    """
    safe_thread = discord.utils.escape_markdown(thread_name)
    who = join_names(replier_names)
    link = jump_url(guild_id, thread_id, first_reply_id)
    if reply_count <= 1:
        return (
            f"{who} replied to your thread **{safe_thread}** in **{guild_name}**.\n"
            f"Reply: {link}"
        )
    return (
        f"{who} replied to your thread **{safe_thread}** in **{guild_name}** "
        f"({reply_count} replies).\n"
        f"First new reply: {link}"
    )


StarterVerdict = Literal["thread", "remove"]

#: The four auto-archive durations Discord accepts for a thread, in minutes:
#: 1 hour, 1 day, 3 days, 1 week. The same Literal discord.py types
#: ``create_thread(auto_archive_duration=...)`` with, so a normalised value
#: passes the type check as well as Discord.
AutoArchiveMinutes = Literal[60, 1440, 4320, 10080]
_AUTO_ARCHIVE_CHOICES: dict[int, AutoArchiveMinutes] = {
    60: 60, 1440: 1440, 4320: 4320, 10080: 10080
}

#: One day, which is what every Needle thread got before the dial existed
#: (migration 252). The dial ships at this value, so it ships dark.
DEFAULT_AUTO_ARCHIVE_MINUTES: AutoArchiveMinutes = 1440


def auto_archive_minutes(value: object) -> AutoArchiveMinutes:
    """The channel's **Threads archive after** dial, as a value Discord takes.

    Discord rejects any ``auto_archive_duration`` outside its four choices, so
    an out-of-range value would mean no thread at all. Anything that is not
    exactly one of the four as an ``int`` (0, 90, ``None``, a negative, the
    string ``"1440"``, a bool) falls back to one day: the old behaviour, never
    a failed thread. The store clamps on write (``_upsert_channel``) and on
    read (``_row_to_config``), and ``thread_create_options`` clamps once more
    on the way to Discord, so the route needs no clamp of its own.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_AUTO_ARCHIVE_MINUTES
    return _AUTO_ARCHIVE_CHOICES.get(value, DEFAULT_AUTO_ARCHIVE_MINUTES)


class ThreadCreateOptions(TypedDict):
    auto_archive_duration: AutoArchiveMinutes
    slowmode_delay: int | None


def thread_create_options(*, archive_minutes: int, slowmode: int) -> ThreadCreateOptions:
    """The keyword arguments Needle opens a thread with, from the channel's row.

    The cog unpacks this straight into ``message.create_thread``; the name is
    its own argument. Kept here so a test can prove the channel's **Threads
    archive after** value is what reaches Discord: before the dial the cog
    hard-coded one day, and a quiet return to that would otherwise leave every
    test green while the dial saved values nothing applied. A slowmode of 0
    is sent as ``None``, which is "no slowmode" to discord.py.
    """
    return {
        "auto_archive_duration": auto_archive_minutes(archive_minutes),
        "slowmode_delay": slowmode or None,
    }


#: How long the "posts need an upload" note stays up before the bot removes it.
TEXT_ONLY_NOTICE_SECONDS = 15

#: A member told once is not told again inside this window. Their next
#: text-only post is still removed; only the note is skipped, so one member
#: typing five lines costs the channel one note rather than five.
TEXT_ONLY_NOTICE_COOLDOWN_SECONDS = 60


def starter_post_verdict(
    *,
    require_attachment: bool,
    has_attachment: bool,
    author_is_bot: bool,
    author_can_manage_messages: bool,
) -> StarterVerdict:
    """Does this post in an auto-threaded channel get a thread, or get removed?

    With the channel's **require an upload** dial on, a member's post must
    carry at least one Discord attachment. A caption rides along with the
    upload and is kept. "Attachment" is the auto-delete media-only rule's
    definition (``docs/specs/auto_delete_spec.md``): a real upload. A link that
    unfurls, a GIF-picker GIF, a sticker, a poll and a forwarded message all
    have none, and the cog passes ``bool(message.attachments)``.

    Two exemptions (Ben, 2026-09-29): a member with Manage Messages in the
    channel, so a moderator can post the channel's rules as text, and every
    bot or webhook, so a game prompt another bot posts is never eaten. Whether
    a bot's post opens a thread at all is still ``include_bots``, checked
    before this.

    The dial ships off, and off is exactly the old behaviour: every post gets
    a thread.
    """
    if not require_attachment or has_attachment:
        return "thread"
    if author_is_bot or author_can_manage_messages:
        return "thread"
    return "remove"


def removal_blocked(
    *, require_attachment: bool, bot_can_manage_messages: bool | None
) -> bool:
    """Is the dial on in a channel where the bot cannot delete a post?

    The dashboard's warning. Without Manage Messages every delete fails, so
    the ticked box would remove nothing, and a setting that isn't enforced
    must say so. ``None`` means the permission could not be read (the bot is
    down, or the channel isn't cached): unknown is not a fault, and no
    warning is shown on a guess.
    """
    return require_attachment and bot_can_manage_messages is False


def should_post_text_only_notice(
    *,
    now: float,
    last_notice_at: float | None,
    cooldown: float = TEXT_ONLY_NOTICE_COOLDOWN_SECONDS,
) -> bool:
    """Tell this member why their post went, unless they were told a moment ago."""
    return last_notice_at is None or now - last_notice_at >= cooldown


# Plain message text, not an embed: the ping is how the poster learns why
# their post went, so it mentions them on purpose.
def build_text_only_notice(author_mention: str) -> str:  # embed-mention-ok: author_mention
    """The short note posted after a text-only post is removed.

    It names nobody but the poster and quotes nothing they wrote: the removed
    text is not stored, and repeating it would undo the removal.
    """
    return (
        f"{author_mention} Posts here need an image or file attached, so your "
        "message was removed. Add your text as the caption on an upload, or "
        "reply to someone's post inside its thread."
    )


ThreadFate = Literal["delete", "archive", "nothing"]


def starter_deleted_fate(
    *,
    behavior: str,
    can_delete: bool,
    history_read: bool = True,
    others_posted: bool = False,
) -> ThreadFate:
    """What happens to a thread when the message that started it is deleted.

    ``archive_if_empty`` deletes only a thread nobody else posted in, and only
    when the whole history was actually read: a failed read is not evidence of
    an empty thread, and deleting on it would take members' replies with it.
    Every uncertain case falls back to archiving, which loses nothing.
    """
    if behavior == "nothing":
        return "nothing"
    if behavior == "archive":
        return "archive"
    if behavior == "delete":
        return "delete" if can_delete else "archive"
    if history_read and not others_posted and can_delete:
        return "delete"
    return "archive"


def mute_button_label(action: MuteAction) -> str:
    """The button reads as what pressing it does. After a press it offers the
    undo, so a mis-tap is one more tap away from fixed. It changes the
    member's switch for the whole server, and says so — not "this thread"."""
    if action == "mute":
        return "🔕 Turn off reply DMs"
    return "🔔 Turn reply DMs back on"


def dm_button_may_write(action: MuteAction, *, is_member: bool) -> bool:
    """May a press on the reply DM's button change the presser's opt-in?

    Turning reply DMs off is always allowed: it only deletes. Turning them
    back on writes a row, and only a current member may — the button lives in
    an old DM, so a member who left (and may have been erased) could otherwise
    re-create personal data for a server they're no longer in.
    """
    return action == "mute" or is_member


def toggled(action: MuteAction) -> MuteAction:
    """The action the button offers once this one has been done."""
    return "unmute" if action == "mute" else "mute"
