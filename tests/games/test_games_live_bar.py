"""The live vote meter's rate-limit gate.

``LiveBarUpdater`` exists because a popular round draws a vote every few
hundred milliseconds and Discord rate-limits message edits hard: one edit per
``min_interval``, and a second update arriving inside that window is
*dropped* rather than queued, because it would draw the same bars a moment
later anyway.

``schedule_render`` shares that gate with ``schedule_update``. It was split
out when the round games joined the Live Game Boards dial: under the dial the
board may have been re-posted since the click that triggered the redraw, so
the caller has to look its location up rather than edit a captured message —
which means the *caller* owns the edit and the updater only owns the gate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.games.utils.live_bar import LiveBarUpdater


def _updater() -> LiveBarUpdater:
    # Short enough to keep the test instant, long enough that two awaits
    # inside one event-loop tick land in the same window.
    return LiveBarUpdater(min_interval=0.05)


@pytest.mark.asyncio
async def test_the_first_update_goes_straight_out():
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()

    await _updater().schedule_update(message, lambda: discord.Embed(title="bars"))

    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_none_message_is_a_no_op():
    """Callers pass ``interaction.message`` without narrowing it."""
    await _updater().schedule_update(None, lambda: pytest.fail("never built"))


# ── schedule_update_layout: the Components V2 sibling ────────────────


@pytest.mark.asyncio
async def test_schedule_update_layout_edits_in_place():
    """No fresh object to hand over — the view *is* the message body, so the
    caller rebuilds it and this only re-sends that same instance, nulling
    ``embed``/``attachments`` so a prior embed edit doesn't linger."""
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()
    view = discord.ui.LayoutView(timeout=None)

    await _updater().schedule_update_layout(message, view)

    message.edit.assert_awaited_once_with(
        content=None, embed=None, attachments=[], view=view
    )


@pytest.mark.asyncio
async def test_schedule_update_layout_none_message_is_a_no_op():
    view = discord.ui.LayoutView(timeout=None)
    await _updater().schedule_update_layout(None, view)  # must not raise


@pytest.mark.asyncio
async def test_schedule_update_layout_shares_the_gate_with_schedule_update():
    """One updater per board — a layout bar and an embed bar never coexist
    on the same game, but the gate is still the updater's, not the method's."""
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()
    updater = _updater()
    view = discord.ui.LayoutView(timeout=None)

    await asyncio.gather(*(
        updater.schedule_update_layout(message, view) for _ in range(5)
    ))

    assert message.edit.await_count == 2


@pytest.mark.asyncio
async def test_schedule_update_layout_swallows_a_dead_message():
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "unknown message")
    )
    view = discord.ui.LayoutView(timeout=None)

    await _updater().schedule_update_layout(message, view)  # must not raise


@pytest.mark.asyncio
async def test_a_third_vote_inside_the_window_is_dropped():
    """Two edits, not three: the first claims the slot, the second waits out
    the interval, and everything arriving while that one is queued draws the
    same bars it will and so is dropped."""
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()
    updater = _updater()

    await asyncio.gather(*(
        updater.schedule_update(message, lambda: discord.Embed(title="bars"))
        for _ in range(5)
    ))

    assert message.edit.await_count == 2


@pytest.mark.asyncio
async def test_schedule_render_lets_the_caller_own_the_edit():
    """The board-sticky path: no message is handed over, because where the
    board is now is something only the caller can resolve."""
    drawn: list[int] = []
    updater = _updater()

    async def _render():
        drawn.append(1)

    await updater.schedule_render(_render)

    assert drawn == [1]


@pytest.mark.asyncio
async def test_schedule_render_shares_the_gate_with_schedule_update():
    """One updater per board, so a redraw and a bar edit racing must not both
    get through — the gate is the updater's, not each method's."""
    calls: list[str] = []
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock(side_effect=lambda **kw: calls.append("edit"))
    updater = _updater()

    async def _render():
        calls.append("render")

    await asyncio.gather(
        updater.schedule_update(message, lambda: discord.Embed(title="bars")),
        updater.schedule_render(_render),
        updater.schedule_render(_render),
    )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_failing_render_is_swallowed():
    """A redraw landing on a message that has just been deleted (a restick
    moved the board, or the round ended) is an ordinary race, not an error to
    propagate into a vote handler."""
    async def _boom():
        raise discord.HTTPException(MagicMock(), "unknown message")

    await _updater().schedule_render(_boom)
