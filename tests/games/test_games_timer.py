"""``GameTimer``'s one-time dynamic-timestamp install, both shapes.

``_install_field`` (embed) predates this file and has no dedicated coverage
of its own — the round games exercise it indirectly through their embed
tests. What is new here, for Components V2 wave 3b, is ``_install_item``:
the layout sibling that edits a card's stable-id ``TextDisplay`` in place
via ``LayoutView.from_message``/``find_item`` rather than ``embed.copy()``.
"""

from __future__ import annotations

import asyncio

import discord
import pytest
from discord.components import TextDisplay as RawTextDisplay
from unittest.mock import AsyncMock, MagicMock

from bot_modules.games.utils.timer import GameTimer, format_deadline


def _component_message(content: str, item_id: int = 5) -> MagicMock:
    """A message whose ``.components`` round-trip through
    ``LayoutView.from_message`` the way Discord's own gateway payload would
    — a real ``discord.components.TextDisplay``, not a ``discord.ui`` item."""
    message = MagicMock(spec=discord.Message)
    message.components = [RawTextDisplay({"type": 10, "content": content, "id": item_id})]
    message.edit = AsyncMock()
    return message


def _timer(message, timer_item_id: int = 5) -> GameTimer:
    async def _callback():
        pass

    return GameTimer(30, message, _callback, timer_item_id=timer_item_id)


# ── _install_item: the Components V2 arm ─────────────────────────────


def test_install_item_writes_the_deadline_into_the_named_item():
    message = _component_message("## Round 1")
    timer = _timer(message)
    timer.deadline = 1_800_000_000

    asyncio.run(timer._install_item())

    view = message.edit.await_args.kwargs["view"]
    item = view.find_item(5)
    assert item.content == format_deadline(1_800_000_000)


def test_install_item_nulls_embed_and_attachments_on_the_edit():
    """Turns an edit that used to carry an embed into a bare Components V2
    one; a no-op on a message that was already a layout."""
    message = _component_message("## Round 1")
    timer = _timer(message)
    timer.deadline = 1_800_000_000

    asyncio.run(timer._install_item())

    kwargs = message.edit.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["embed"] is None
    assert kwargs["attachments"] == []


def test_install_item_skips_when_the_builder_already_rendered_a_timestamp():
    """The layout builder may have already put a live ``<t:...:R>`` into the
    item (e.g. a redraw that ran after the timer started) — don't stomp it
    with a second, slightly different deadline."""
    message = _component_message("## Round 1\n⏰ ends <t:1700000000:R>")
    timer = _timer(message)
    timer.deadline = 1_800_000_000

    asyncio.run(timer._install_item())

    message.edit.assert_not_awaited()


def test_install_item_is_a_no_op_with_no_components():
    message = MagicMock(spec=discord.Message)
    message.components = []
    message.edit = AsyncMock()
    timer = _timer(message)
    timer.deadline = 1_800_000_000

    asyncio.run(timer._install_item())

    message.edit.assert_not_awaited()


def test_install_item_swallows_an_edit_failure():
    message = _component_message("## Round 1")
    message.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "unknown message")
    )
    timer = _timer(message)
    timer.deadline = 1_800_000_000

    asyncio.run(timer._install_item())  # must not raise


def test_install_item_skips_an_item_that_is_not_a_text_display():
    """``find_item`` returning the wrong shape (a stale id reused by a
    button, say) is treated like "nothing to install", not a crash."""
    message = MagicMock(spec=discord.Message)
    button = discord.components.Button(
        {"type": 2, "style": 1, "label": "Go", "id": 5}
    )
    message.components = [button]
    message.edit = AsyncMock()
    timer = _timer(message)
    timer.deadline = 1_800_000_000

    asyncio.run(timer._install_item())

    message.edit.assert_not_awaited()


# ── start(): routes on timer_item_id ─────────────────────────────────


@pytest.mark.asyncio
async def test_start_routes_to_install_item_when_an_id_is_set():
    message = _component_message("## Round 1")
    timer = _timer(message, timer_item_id=5)
    timer._task = None
    try:
        await timer.start()
        message.edit.assert_awaited_once()
    finally:
        timer.cancel()


@pytest.mark.asyncio
async def test_start_routes_to_install_field_when_no_id_is_set():
    """The default keeps every embed-shaped caller on the old path."""
    message = MagicMock(spec=discord.Message)
    message.embeds = []  # no embed: _install_field is a fast no-op
    message.edit = AsyncMock()
    timer = _timer(message, timer_item_id=None)
    try:
        await timer.start()
        message.edit.assert_not_awaited()  # no embed to install into
    finally:
        timer.cancel()
