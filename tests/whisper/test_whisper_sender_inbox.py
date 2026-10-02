"""Cog-level: sender's own inbox (/whisper sent + the launcher's Sent button)."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.models import Whisper
from bot_modules.whisper.service import LOCK_DURATION_SECONDS
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")



SENDER, TARGET, OTHER_TARGET = 1001, 2001, 3003
NOW = time.time()


def _w(
    *,
    wid: int = 42,
    sender_id: int = SENDER,
    target_id: int = TARGET,
    created_at: float | None = None,
    solved: bool = False,
    exposed: bool = False,
    guesses_left: int = 3,
    state: str = "pending",
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=wid,
        guild_id=9001,
        sender_id=sender_id,
        target_id=target_id,
        message=f"secret {wid}",
        created_at=NOW if created_at is None else created_at,
        state=state,  # type: ignore[arg-type]
        solved=solved,
        exposed=exposed,
        guesses_left=guesses_left,
        channel_msg_id=88888,
        dm_msg_id=99999,
        deleted_at=deleted_at,
    )


# ── Sent button on the launcher ──────────────────────────────────────────────


def _texts(view) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def _cog():
    from bot_modules.whisper.cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return WhisperCog(bot)


def test_launcher_carries_the_sent_button():
    from bot_modules.whisper.cog import WhisperLauncherView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperLauncherView(bot)
    custom_ids = [getattr(c, "custom_id", None) for c in view.walk_children()]
    assert "whisper:check_sent" in custom_ids
    # "Check Hidden Whispers" must NOT be there any more.
    assert "whisper:check_hidden" not in custom_ids


@pytest.mark.asyncio
async def test_sent_button_filters_terminal_whispers():
    """Out-of-guesses-no-solve / age-locked are hidden from the sender's
    inbox view."""
    cog = _cog()

    sent_rows = [
        _w(wid=10),  # pending → visible
        _w(wid=12, guesses_left=0, solved=False),  # out of guesses → hidden
        _w(wid=13, solved=True),  # solved → still visible (reply is open)
        _w(wid=14, created_at=NOW - LOCK_DURATION_SECONDS - 1),  # locked → hidden
    ]

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_list_sent", return_value=sent_rows):
        await cog._open_inbox(interaction, mode="sent")

    interaction.response.send_message.assert_awaited_once()
    passed_view = interaction.response.send_message.call_args.kwargs["view"]
    visible_ids = {w.id for w in passed_view._all}
    assert visible_ids == {10, 13}
    # The ephemeral's handle is kept so the timeout can grey the card out.
    assert passed_view.message is interaction.original_response.return_value


# ── /whisper sent slash command ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whisper_sent_slash_command_requires_guild():
    cog = _cog()
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = None
    interaction.response.send_message = AsyncMock()

    await cog.whisper_sent.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    args = interaction.response.send_message.call_args.args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_whisper_sent_slash_command_opens_sent_inbox():
    cog = _cog()
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    sent_rows = [_w(wid=10), _w(wid=11, target_id=OTHER_TARGET)]
    with patch("bot_modules.whisper.cog._do_list_sent", return_value=sent_rows):
        await cog.whisper_sent.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once()
    sent_kwargs = interaction.response.send_message.call_args.kwargs
    assert sent_kwargs.get("ephemeral") is True
    view = sent_kwargs["view"]
    assert view._mode == "sent"
    assert {w.id for w in view._all} == {10, 11}


# ── Sent-mode surface differences ────────────────────────────────────────────


def test_sent_mode_titles_and_names_the_target():
    """Sent mode uses the sent title and names the recipient on the row and
    on the opened whisper (the conftest resolver hands back ``<@id>``)."""
    from bot_modules.whisper.cog import WhisperInboxView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperInboxView(
        bot, [_w(wid=42, target_id=OTHER_TARGET)],
        invoker_id=SENDER, mode="sent", name_fn=lambda uid: f"<@{uid}>",
    )
    assert "Whispers You've Sent" in _texts(view)
    assert f"To <@{OTHER_TARGET}>" in _texts(view)
    view._open_id = 42
    view.render()
    assert f"Whisper #42 → <@{OTHER_TARGET}>" in _texts(view)


def test_sent_mode_empty_message_distinct_from_received():
    from bot_modules.whisper.cog import WhisperInboxView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    sent = WhisperInboxView(bot, [], invoker_id=SENDER, mode="sent")
    received = WhisperInboxView(bot, [], invoker_id=SENDER, mode="received")
    assert "haven't sent" in _texts(sent).lower()
    assert _texts(sent) != _texts(received)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "looks_up_names"),
    [("received", False), ("sent", True)],
)
async def test_only_the_sent_list_looks_up_names(mode, looks_up_names):
    """The Inbox never shows a name, so it must not pay for the lookup."""
    cog = _cog()
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_list_sent", return_value=[_w(wid=10)]), \
         patch("bot_modules.whisper.cog._do_list_received_in_states", return_value=[_w(wid=10)]), \
         patch("bot_modules.whisper.cog.build_name_fn", new=AsyncMock(return_value=lambda uid: "x")) as lookup:
        await cog._open_inbox(interaction, mode=mode)

    assert lookup.await_count == (1 if looks_up_names else 0)
    interaction.response.send_message.assert_awaited_once()
