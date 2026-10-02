"""Cog-level: the one-modal send form (``WhisperSendModal``).

The paged recipient picker (``WhisperSendTargetSelectView``) and its compose
modal went with the 2026-09-30 Components V2 conversion: the two steps never
depended on each other, so they are one form — Discord's own member picker
and the message box. What is left to pin here is the form's own gate: it
refuses outside a guild, refuses a pick who has since left, and otherwise
hands the resolved member and the text to ``_send_impl``, which owns every
other check (``test_whisper_send.py``).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.fakes import FakeMember, fake_interaction

SENDER, OTHER = 1001, 2001


def _modal():
    from bot_modules.whisper.cog import WhisperCog, WhisperSendModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)
    cog._send_impl = AsyncMock()  # type: ignore[method-assign]
    modal = WhisperSendModal(cog)
    modal.message_input._value = "an anonymous message"  # type: ignore[attr-defined]
    return cog, modal


def _picked(modal, user_id: int) -> None:
    """Simulate the member picker's pick: a modal's resolved user may arrive
    as a plain User, so only the id is trusted."""
    picked = MagicMock()
    picked.id = user_id
    modal.who._values = [picked]  # type: ignore[attr-defined]


# ── WhisperSendModal.on_submit → _send_impl ───────────────────────────────────


@pytest.mark.asyncio
async def test_send_modal_submit_delegates_to_send_impl():
    cog, modal = _modal()
    _picked(modal, OTHER)

    target_member = FakeMember(id=OTHER, display_name="bob")
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.get_member = MagicMock(return_value=target_member)

    await modal.on_submit(interaction)

    # The guild's own Member is what reaches _send_impl — the role checks
    # read it, and a bare User carries none.
    interaction.guild.get_member.assert_called_once_with(OTHER)
    cog._send_impl.assert_awaited_once()
    call_kwargs = cog._send_impl.call_args.kwargs
    assert call_kwargs["target"] is target_member
    assert call_kwargs["message"] == "an anonymous message"


@pytest.mark.asyncio
async def test_send_modal_rejects_missing_guild():
    cog, modal = _modal()
    _picked(modal, OTHER)

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = None

    await modal.on_submit(interaction)

    cog._send_impl.assert_not_awaited()
    args = interaction.response.send_message.call_args.args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_send_modal_rejects_left_target():
    """Target who left the server between opening the form and submit → error."""
    cog, modal = _modal()
    _picked(modal, OTHER)

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.get_member = MagicMock(return_value=None)  # left server

    await modal.on_submit(interaction)

    cog._send_impl.assert_not_awaited()
    args = interaction.response.send_message.call_args.args
    assert "any more" in args[0].lower() or "not in" in args[0].lower()


@pytest.mark.asyncio
async def test_send_modal_rejects_an_empty_pick():
    """The picker is required, but a submit with nothing picked still answers
    rather than raising into the modal handler."""
    cog, modal = _modal()
    modal.who._values = []  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()

    await modal.on_submit(interaction)

    cog._send_impl.assert_not_awaited()
    interaction.guild.get_member.assert_not_called()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
