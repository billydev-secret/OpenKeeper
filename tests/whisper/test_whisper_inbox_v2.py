"""Cog-level: inbox v2 UI + reply + report flows."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.models import Whisper, WhisperConfig, WhisperState
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")




SENDER, TARGET, OTHER = 1001, 2001, 9999
FEED, LOG = 8001, 8002


def _w(
    *,
    wid: int = 42,
    state: WhisperState = "pending",
    sender_id: int = SENDER,
    target_id: int = TARGET,
    message: str = "hi there",
    created_at: float | None = None,
    solved: bool = False,
    guesses_left: int = 3,
    exposed: bool = False,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=wid,
        guild_id=9001,
        sender_id=sender_id,
        target_id=target_id,
        message=message,
        created_at=time.time() if created_at is None else created_at,
        state=state,
        solved=solved,
        exposed=exposed,
        guesses_left=guesses_left,
        channel_msg_id=88888,
        dm_msg_id=99999,
        deleted_at=deleted_at,
    )


def _cfg(*, log_channel_id: int = LOG) -> WhisperConfig:
    return WhisperConfig(
        guild_id=9001, role_id=7001, channel_id=FEED, log_channel_id=log_channel_id
    )


# ── _format_time_ago ─────────────────────────────────────────────────────────


def test_format_time_ago_seconds():
    from bot_modules.whisper.logic import format_time_ago as _format_time_ago
    assert _format_time_ago(0, now=30) == "30s ago"


def test_format_time_ago_minutes():
    from bot_modules.whisper.logic import format_time_ago as _format_time_ago
    assert _format_time_ago(0, now=125) == "2m ago"


def test_format_time_ago_hours():
    from bot_modules.whisper.logic import format_time_ago as _format_time_ago
    assert _format_time_ago(0, now=3600 * 4) == "4h ago"


def test_format_time_ago_days_plural_singular():
    from bot_modules.whisper.logic import format_time_ago as _format_time_ago
    assert _format_time_ago(0, now=86400) == "1 day ago"
    assert _format_time_ago(0, now=86400 * 7) == "7 days ago"


# ── WhisperInboxView ─────────────────────────────────────────────────────────
#
# The inbox is a paged list (ten rows, an Open beside each) that swaps in
# place to the opened whisper and back. The cards' text is pinned by the
# builder tests in tests/whisper/test_whisper_layouts.py; these cover the view's
# behaviour — what a press changes and what it sends.


def _make_inbox(whispers, *, mode: str = "received"):
    from bot_modules.whisper.cog import WhisperInboxView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperInboxView(bot, whispers, invoker_id=TARGET, mode=mode)


def _texts(view) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(view) -> list[str]:
    return [b.custom_id for b in view.walk_children() if isinstance(b, discord.ui.Button)]


def _open(view, whisper_id: int) -> None:
    view._open_id = whisper_id
    view.render()


def test_inbox_empty_view():
    view = _make_inbox([])
    assert "No whispers yet" in _texts(view)
    assert _ids(view) == []


def test_inbox_list_has_an_open_per_row_and_a_filter():
    """Three whispers: three Open buttons and Filter, no arrows (one page)."""
    from bot_modules.whisper.layouts import (
        INBOX_FILTER_ID, INBOX_NEXT_ID, INBOX_OPEN_ID_TEMPLATE, INBOX_PREV_ID,
    )
    whispers = [_w(wid=10 + i, message=f"msg {i}") for i in range(3)]
    view = _make_inbox(whispers)
    ids = _ids(view)
    assert [INBOX_OPEN_ID_TEMPLATE.format(whisper_id=w) for w in (10, 11, 12)] == ids[:3]
    assert INBOX_FILTER_ID in ids
    assert INBOX_PREV_ID not in ids and INBOX_NEXT_ID not in ids


def test_inbox_paginates_past_10():
    from bot_modules.whisper.layouts import INBOX_NEXT_ID, INBOX_PREV_ID
    whispers = [_w(wid=100 + i, message=f"m{i}") for i in range(30)]
    view = _make_inbox(whispers)
    ids = _ids(view)
    assert sum(1 for i in ids if i.startswith("whisper_inbox:open:")) == 10
    assert INBOX_PREV_ID in ids and INBOX_NEXT_ID in ids
    assert "Page 1 of 3" in _texts(view)


def _detail_actions(view) -> list[str]:
    from bot_modules.whisper.layouts import INBOX_ACTION_IDS
    by_id = {v: k for k, v in INBOX_ACTION_IDS.items()}
    return [by_id[i] for i in _ids(view) if i in by_id]


def test_inbox_open_shows_every_action_and_back():
    from bot_modules.whisper.layouts import INBOX_BACK_ID
    view = _make_inbox([_w(wid=10)])
    _open(view, 10)
    assert _detail_actions(view) == ["guess", "share", "reply", "report", "delete"]
    assert INBOX_BACK_ID in _ids(view)


def test_inbox_locked_whisper_omits_guess_button():
    from bot_modules.whisper.service import LOCK_DURATION_SECONDS
    old = _w(wid=10, created_at=time.time() - LOCK_DURATION_SECONDS - 1)
    view = _make_inbox([old])
    _open(view, 10)
    assert _detail_actions(view) == ["share", "reply", "report", "delete"]


def test_inbox_shared_whisper_omits_share_button():
    view = _make_inbox([_w(wid=10, state="shared")])
    _open(view, 10)
    assert _detail_actions(view) == ["guess", "reply", "report", "delete"]


def test_inbox_exhausted_guesses_omits_guess_button():
    view = _make_inbox([_w(wid=10, guesses_left=0)])
    _open(view, 10)
    assert _detail_actions(view) == ["share", "reply", "report", "delete"]


def test_inbox_sent_mode_is_reply_only():
    """Sender's own list shows Reply only: Delete is recipient-only, so it
    could only ever refuse there."""
    view = _make_inbox([_w(wid=10, sender_id=TARGET, target_id=999, message="my secret")], mode="sent")
    _open(view, 10)
    assert _detail_actions(view) == ["reply"]


def test_inbox_interaction_check_rejects_other_user():
    """Only the inbox owner can interact with the view."""
    view = _make_inbox([_w()])
    other = fake_interaction(user=FakeMember(id=OTHER))
    other.response.send_message = AsyncMock()
    import asyncio
    ok = asyncio.run(view.interaction_check(other))
    assert ok is False
    other.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_inbox_delete_confirms_then_removes_from_cache_and_redraws():
    """Delete is red and asks first: the press opens a confirm card, and its
    Yes soft-deletes, drops the row from the inbox and redraws the inbox
    back on its list through the stored message handle."""
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    view = _make_inbox([_w(wid=10), _w(wid=11)])
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    _open(view, 10)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()
    await view._on_delete(interaction)
    confirm = interaction.response.send_message.call_args.kwargs["view"]
    assert isinstance(confirm, WhisperDeleteConfirmView)
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True

    yes = fake_interaction(user=FakeMember(id=TARGET))
    yes.response.edit_message = AsyncMock()
    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(wid=10)), \
         patch("bot_modules.whisper.cog._do_soft_delete") as sd, \
         patch("bot_modules.whisper.cog._redraw_dm", AsyncMock()):
        await confirm._on_yes(yes)
    sd.assert_called_once_with(":memory:", 10)
    assert [w.id for w in view._all] == [11]
    assert view._open_id is None
    view.message.edit.assert_awaited_once()
    assert "Deleted" in _texts(yes.response.edit_message.call_args.kwargs["view"])


@pytest.mark.asyncio
async def test_inbox_delete_cancel_is_silent():
    """Cancel deletes the confirm and leaves the inbox exactly as it was."""
    from bot_modules.whisper.cog import WhisperDeleteConfirmView
    view = _make_inbox([_w(wid=10)])
    _open(view, 10)
    confirm = WhisperDeleteConfirmView(view.bot, 10, invoker_id=TARGET, inbox=view)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.delete_original_response = AsyncMock()
    with patch("bot_modules.whisper.cog._do_soft_delete") as sd:
        await confirm._on_cancel(interaction)
    sd.assert_not_called()
    interaction.response.defer.assert_awaited_once()
    interaction.delete_original_response.assert_awaited_once()
    assert [w.id for w in view._all] == [10]
    assert view._open_id == 10


@pytest.mark.asyncio
async def test_inbox_share_updates_cached_state_and_redraws():
    target_w = _w(wid=10, state="pending")
    view = _make_inbox([target_w])
    _open(view, 10)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=target_w), \
         patch("bot_modules.whisper.cog._do_update_state") as upd, \
         patch("bot_modules.whisper.cog._share_side_effects", AsyncMock()), \
         patch("bot_modules.whisper.cog._redraw_dm", AsyncMock()) as redraw:
        await view._on_share(interaction)
    upd.assert_called_once_with(":memory:", 10, "shared")
    assert view._all[0].state == "shared"
    # The feed repost is several REST calls: defer, then redraw in place.
    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once_with(view=view)
    assert "share" not in _detail_actions(view)
    redraw.assert_awaited_once()


# ── WhisperReplyButton + WhisperReplyModal ───────────────────────────────────


@pytest.mark.asyncio
async def test_reply_button_third_party_rejected():
    from bot_modules.whisper.cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReplyButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=OTHER))
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_reply_button_target_opens_modal():
    from bot_modules.whisper.cog import WhisperReplyButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReplyButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_modal = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0):
        await button.callback(interaction)

    interaction.response.send_modal.assert_called_once()


@pytest.mark.asyncio
async def test_reply_modal_target_dms_sender():
    from bot_modules.whisper.cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "thanks for the message"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0), \
         patch("bot_modules.whisper.cog._do_insert_reply", return_value=99) as ins:
        await modal.on_submit(interaction)

    sender_user.send.assert_awaited_once()
    ins.assert_called_once_with(
        ":memory:",
        whisper_id=42,
        from_user_id=TARGET,
        to_user_id=SENDER,
        content="thanks for the message",
    )
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_reply_modal_sender_dms_target():
    from bot_modules.whisper.cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "glad you liked it"  # type: ignore[attr-defined]

    target_user = MagicMock()
    target_user.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=target_user)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0), \
         patch("bot_modules.whisper.cog._do_insert_reply", return_value=99) as ins:
        await modal.on_submit(interaction)

    target_user.send.assert_awaited_once()
    ins.assert_called_once_with(
        ":memory:",
        whisper_id=42,
        from_user_id=SENDER,
        to_user_id=TARGET,
        content="glad you liked it",
    )


@pytest.mark.asyncio
async def test_reply_modal_dm_forbidden_does_not_persist():
    from bot_modules.whisper.cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "hi"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no dms")
    )
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0), \
         patch("bot_modules.whisper.cog._do_insert_reply", return_value=99), \
         patch("bot_modules.whisper.cog._do_delete_reply") as del_mock:
        await modal.on_submit(interaction)

    # Reply is inserted first (to get reply_id), then rolled back when DM fails.
    del_mock.assert_called_once_with(":memory:", 99)
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "deliver" in args[0].lower() or "DMs" in args[0]


# ── WhisperReportButton + WhisperReportModal ─────────────────────────────────


@pytest.mark.asyncio
async def test_report_button_non_target_rejected():
    from bot_modules.whisper.cog import WhisperReportButton
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    button = WhisperReportButton(bot, 42)
    interaction = fake_interaction(user=FakeMember(id=SENDER))  # sender, not target
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_report_modal_posts_to_mod_log():
    from bot_modules.whisper.cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "creepy content"  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()
    text = _texts(log_channel.send.call_args.kwargs["view"])
    assert "## 🚨 Whisper Reported" in text
    assert "**Sender**" in text
    assert "**Reporter**" in text
    assert "> creepy content" in text
    assert "Whisper #42" in text
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_report_modal_records_even_when_no_log_channel():
    from bot_modules.whisper.cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "x"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg(log_channel_id=0)), \
         patch("bot_modules.whisper.cog._do_insert_report", return_value=True) as ins:
        await modal.on_submit(interaction)

    # Report still gets recorded (visible on the web mod dashboard) even when
    # the Discord mod-log channel isn't set.
    ins.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "submitted" in args[0].lower() or "report" in args[0].lower()


@pytest.mark.asyncio
async def test_report_modal_empty_reason_uses_placeholder():
    from bot_modules.whisper.cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = ""  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    text = _texts(log_channel.send.call_args.kwargs["view"])
    assert "no reason" in text.lower()


# ── B4: Report dedupe ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_modal_duplicate_rejected():
    """A second report from same reporter should be rejected without posting to mod log."""
    from bot_modules.whisper.cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "again"  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_insert_report", return_value=False):
        await modal.on_submit(interaction)

    log_channel.send.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "already" in args[0].lower()


@pytest.mark.asyncio
async def test_report_modal_first_report_succeeds():
    """First report from a reporter should succeed and post to mod log."""
    from bot_modules.whisper.cog import WhisperReportModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReportModal(bot, whisper_id=42)
    modal.reason_input._value = "bad"  # type: ignore[attr-defined]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.guild.get_channel = MagicMock(return_value=log_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_insert_report", return_value=True):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()


# ── S6: Reply mod log ─────────────────────────────────────────────────────────


# ── S9: Reply DM identifies whisper id ───────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_modal_dm_includes_whisper_id():
    """The reply DM body must include 'Whisper #<id>' so recipient can map it."""
    from bot_modules.whisper.cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "great question"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(wid=42)), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0), \
         patch("bot_modules.whisper.cog._do_insert_reply", return_value=99), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await modal.on_submit(interaction)

    sender_user.send.assert_awaited_once()
    assert "Whisper #42" in _texts(sender_user.send.call_args.kwargs["view"])


@pytest.mark.asyncio
async def test_reply_modal_posts_to_mod_log():
    """After a successful reply, a mod log notice should be posted."""
    from bot_modules.whisper.cog import WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "anonymous reply text"  # type: ignore[attr-defined]

    sender_user = MagicMock()
    sender_user.send = AsyncMock()
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=log_channel)
    bot.get_guild = MagicMock(return_value=guild)

    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=sender_user)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0), \
         patch("bot_modules.whisper.cog._do_insert_reply", return_value=99), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await modal.on_submit(interaction)

    log_channel.send.assert_awaited_once()
    text = _texts(log_channel.send.call_args.kwargs["view"])
    assert "## 💬 Whisper Reply" in text
    assert "**From**" in text and "**To**" in text
    assert "Whisper #42" in text


@pytest.mark.asyncio
async def test_reply_to_a_member_on_a_break_is_quiet_success():
    """Take a Break: the reply row is written (the one-reply cap still trips)
    but nothing is DM'd, and the replier sees the ordinary confirmation."""
    from bot_modules.whisper.cog import REPLY_CONFIRMATION, WhisperReplyModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    modal = WhisperReplyModal(bot, whisper_id=42)
    modal.reply_input._value = "hi"  # type: ignore[attr-defined]

    target_user = MagicMock()
    target_user.send = AsyncMock()
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.client = MagicMock()
    interaction.client.get_user = MagicMock(return_value=target_user)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._do_count_replies", return_value=0), \
         patch("bot_modules.whisper.cog.is_on_break", return_value=True), \
         patch("bot_modules.whisper.cog._do_insert_reply", return_value=99) as ins:
        await modal.on_submit(interaction)

    target_user.send.assert_not_awaited()
    ins.assert_called_once()
    interaction.response.send_message.assert_awaited_once_with(
        REPLY_CONFIRMATION, ephemeral=True
    )
