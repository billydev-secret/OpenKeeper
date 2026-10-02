"""Shared review-card rendering mechanics behind the economy approval cards.

A product shows a mod a card with Approve/Deny buttons and re-renders it when
the row moves. The mechanics are shared; each product's card is not, so
``build_layout`` is injected rather than branched on.

Theme and pin both bind through this trio (``edit_review_card_layout`` etc.)
— see the wiring tests at the bottom of this file. Sponsor hand-rolls its own
local repaint helpers (``sponsor_views``' ``_edit_card_view`` etc.) and calls
neither. An earlier embed-flavored twin of these same three mechanics existed
for theme's own card before its 2026-09-28 Components V2 conversion; it has
no callers left and was removed with that conversion.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.economy.view_helpers import (
    edit_review_card_layout,
    refresh_review_card_layout,
)


def _card(*, raises: Exception | None = None):
    card = MagicMock(spec=discord.Message)
    card.edit = AsyncMock(side_effect=raises)
    return card


def _ctx(row=None, *, read_raises: Exception | None = None):
    ctx = MagicMock()

    @contextmanager
    def _open_db():
        if read_raises:
            raise read_raises
        yield MagicMock()

    ctx.open_db = _open_db
    return ctx


def _layout(settings, row, name_fn=None, *, art=None):
    """The build_layout contract: positional (settings, row, name_fn), plus
    the card strips as ``art`` (``view_helpers.surface_art``)."""
    who = name_fn(int(row["id"])) if name_fn else "?"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(f"row {row['id']} {who}"))
    return view


# ── edit_review_card_layout ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_card_is_edited_with_the_callers_layout():
    """Pins and themes render different cards from different columns; only
    the edit-and-swallow mechanics are shared."""
    card = _card()
    await edit_review_card_layout(
        card, None, {"id": 5}, build_layout=_layout, log_label="econ pin"
    )
    card.edit.assert_awaited_once()
    kwargs = card.edit.await_args.kwargs
    assert kwargs["embed"] is None
    assert kwargs["content"] is None
    assert kwargs["attachments"] == []
    assert isinstance(kwargs["view"], discord.ui.LayoutView)


@pytest.mark.asyncio
async def test_no_card_is_not_an_error():
    """The card may have been deleted; the row is still resolved."""
    await edit_review_card_layout(
        None, None, {"id": 5}, build_layout=_layout, log_label="x"
    )


@pytest.mark.asyncio
async def test_a_failed_edit_is_swallowed(caplog):
    """Losing the card isn't worth raising over — the member was already told."""
    card = _card(raises=discord.HTTPException(MagicMock(status=500), "nope"))
    with caplog.at_level(logging.DEBUG, logger="dungeonkeeper.economy"):
        await edit_review_card_layout(
            card, None, {"id": 5}, build_layout=_layout, log_label="econ pin"
        )
    assert "econ pin" in caplog.text


# ── refresh_review_card_layout ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_row_is_reread_before_rendering():
    """The row can move underneath the card — resolved from the dashboard, or
    by a second mod. Re-reading is what stops a card claiming the wrong
    outcome."""
    card = _card()
    reads: list[int] = []

    def _read(conn, submission_id):
        reads.append(submission_id)
        return {"id": submission_id}

    await refresh_review_card_layout(
        card, _ctx(), None, 9,
        read_row=_read, build_layout=_layout, log_label="econ pin",
    )
    assert reads == [9]
    assert isinstance(card.edit.await_args.kwargs["view"], discord.ui.LayoutView)


@pytest.mark.asyncio
async def test_a_failed_read_leaves_the_card_alone(caplog):
    """Stale beats wrong-and-confident: don't blank a card we couldn't verify."""
    card = _card()
    with caplog.at_level(logging.DEBUG, logger="dungeonkeeper.economy"):
        await refresh_review_card_layout(
            card, _ctx(read_raises=RuntimeError("db gone")), None, 9,
            read_row=lambda c, i: None, build_layout=_layout, log_label="econ pin",
        )
    card.edit.assert_not_awaited()
    assert "failed to reload" in caplog.text


@pytest.mark.asyncio
async def test_a_vanished_row_leaves_the_card_alone():
    """Deleted underneath us — nothing to re-render."""
    card = _card()
    await refresh_review_card_layout(
        card, _ctx(), None, 9,
        read_row=lambda c, i: None, build_layout=_layout, log_label="x",
    )
    card.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_card_skips_the_read_entirely():
    reads: list[int] = []
    await refresh_review_card_layout(
        None, _ctx(), None, 9,
        read_row=lambda c, i: reads.append(i), build_layout=_layout, log_label="x",
    )
    assert reads == []


@pytest.mark.asyncio
async def test_theme_binds_its_own_label_over_the_layout_trio():
    """theme_views is a partial over the layout trio (Components V2,
    2026-09-28), so the wiring is what could break rather than the logic."""
    from bot_modules.economy import theme_views

    assert theme_views._edit_card.keywords["log_label"] == "econ theme"
    assert "build_layout" in theme_views._refresh_card.keywords


@pytest.mark.asyncio
async def test_pin_binds_its_own_label_over_the_layout_trio():
    """Pin moved to this same trio in its own 2026-09-28 Components V2
    conversion. Sponsor hand-rolls its own repaint helpers and binds through
    neither."""
    from bot_modules.economy import pin_views

    assert pin_views._edit_card.keywords["log_label"] == "econ pin"
    assert "build_layout" in pin_views._refresh_card.keywords
