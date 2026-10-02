"""A challenge that was just accepted must still answer the next press.

The Golden Meadow, 2026-09-28: a Pressure Cooker target pressed Accept, the
game went ACTIVE server-side — and he typed "It's not letting me accept"
twenty-one seconds later. ``_accept_callback`` called ``stop()`` *before*
awaiting the accept handler, and discord.py's ``stop()`` also hands the view
to the store's ``remove_view``: for the second or so it takes to go ACTIVE
and repaint the card, ``challenge_accept:`` was no longer a key anyone was
listening for, so a second press was dropped by ``ViewStore.dispatch_view``
with no log line, no reply, and a red "This interaction failed".

``base_duel`` has had the right words for that press since game night
2026-08-21 ("That challenge has already been accepted — the game is
running") — the guard was simply unreachable. These tests pin the view open
so the press can reach it.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from bot_modules.duels.db import CHALLENGE_RESPONSE_SECONDS
from bot_modules.duels.views import ChallengeView
from tests.fakes import fake_interaction

TARGET = 2


def _view(calls: list, *, deadline: float | None = None) -> ChallengeView:
    async def on_accept(interaction, game_id):
        calls.append(("accept", game_id))

    async def on_decline(interaction, game_id):
        calls.append(("decline", game_id))

    return ChallengeView(
        98, target_id=TARGET, on_accept=on_accept, on_decline=on_decline, deadline=deadline
    )


def _press(interaction=None):
    interaction = interaction or fake_interaction()
    interaction.user.id = TARGET
    return interaction


# ── the view stays registered ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "press, expected",
    [
        pytest.param("_accept_callback", ("accept", 98), id="accept"),
        pytest.param("_decline_callback", ("decline", 98), id="decline"),
    ],
)
async def test_a_resolved_challenge_is_not_stopped(press, expected):
    """``is_finished()`` is what ``_dispatch_item`` checks before running a
    callback, and ``stop()`` is also what de-registers the custom_ids — so a
    stopped view is deaf twice over."""
    calls: list = []
    view = _view(calls)

    await getattr(view, press)(_press())

    assert calls == [expected]
    assert view._resolved is True
    assert view.timeout is None  # the response window is retired…
    assert not view.is_finished()  # …without the view going deaf


@pytest.mark.parametrize("press", ["_accept_callback", "_decline_callback"])
async def test_a_second_press_still_reaches_the_handler(press):
    """The handler is where the stale-state guard lives
    (``base_duel._stale_challenge_message``), so the view's job is to let the
    press through rather than to answer it."""
    calls: list = []
    view = _view(calls)

    await getattr(view, press)(_press())
    await getattr(view, press)(_press())

    assert len(calls) == 2


# ── the repaint hazard the old stop() was guarding ────────────────────────────

async def test_a_resolved_challenge_never_repaints_over_the_board():
    """One card carries the challenge, the game and the result, so
    ``ExpiringLayoutView.on_timeout`` greying the Accept/Decline row in place
    would paint a dead challenge over a live game board. ``stop()`` used to
    prevent that; ``_resolved`` does now."""
    view = _view([])
    view.message = AsyncMock()

    await view._accept_callback(_press())
    await view.on_timeout()

    view.message.edit.assert_not_awaited()


async def test_an_unanswered_challenge_still_greys_itself_out():
    """The other side of that guard: nothing pressed, so the expiry repaint
    must happen exactly as it always did."""
    view = _view([])
    view.message = AsyncMock()

    await view.on_timeout()

    view.message.edit.assert_awaited_once()
    assert all(
        getattr(item, "disabled", True) for item in view.walk_children()
    )


# ── unchanged behaviour ───────────────────────────────────────────────────────

async def test_a_press_from_the_wrong_member_resolves_nothing():
    calls: list = []
    view = _view(calls)
    interaction = fake_interaction()
    interaction.user.id = TARGET + 1

    await view._accept_callback(interaction)

    assert calls == []
    assert view._resolved is False
    assert view.timeout == CHALLENGE_RESPONSE_SECONDS


async def test_a_press_past_a_reattached_deadline_resolves_nothing():
    calls: list = []
    view = _view(calls, deadline=time.time() - 1)

    await view._accept_callback(_press())

    assert calls == []
    assert view._resolved is False
