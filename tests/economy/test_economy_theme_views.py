"""Tests for the flash-theme DM receipt (economy/theme_views.py).

The cards themselves moved to Components V2 builders in
``bot_modules/economy/theme_layouts.py`` — see
``tests/economy/test_economy_theme_layouts.py`` for its coverage. This file keeps the
plain-text DM receipt, untouched by that conversion.

The scope is one thing, and it is a money claim: ``expired`` is three
different endings — a request nobody reviewed in time (refunded), a theme that
ran its whole window, and one a mod ended early (neither refunded) — so the
state alone cannot say whether coins went back. The DM reads ``refunded_at``
instead. Branching on the state told a member their coins had been returned
when a mod ended their running theme, which is the worst thing a bot can be
wrong about.
"""

from __future__ import annotations

from bot_modules.economy.theme_views import theme_resolution_dm_text
from bot_modules.economy.service import EconSettings

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems", theme_hours=24
)


def _row(state: str, *, refunded_at: float | None) -> dict:
    return {
        "title": "Cursed Cooking",
        "blurb": "Post the worst thing you have ever eaten.",
        "price": 300,
        "state": state,
        "deny_reason": "too close to last week's",
        "refunded_at": refunded_at,
        "user_id": 42,
        "resolver_id": 7,
    }


def test_ending_a_running_theme_never_claims_a_refund():
    """The regression: a mod pressing End Early moved the row to `expired`,
    which the old state-based branch read as 'declined and refunded'."""
    text = theme_resolution_dm_text(_SETTINGS, _row("expired", refunded_at=None))
    assert "refunded" not in text.lower()
    assert "no refund" in text.lower()
    assert "Cursed Cooking" in text


def test_a_theme_nobody_reviewed_does_promise_the_refund():
    text = theme_resolution_dm_text(_SETTINGS, _row("expired", refunded_at=123.0))
    assert "have been refunded" in text


def test_a_declined_theme_promises_the_refund_and_gives_the_reason():
    text = theme_resolution_dm_text(_SETTINGS, _row("denied", refunded_at=123.0))
    assert "have been refunded" in text
    assert "too close to last week's" in text


def test_the_declined_receipt_escapes_the_moderators_deny_reason():
    row = _row("denied", refunded_at=123.0)
    row["deny_reason"] = "**not** on brand"
    text = theme_resolution_dm_text(_SETTINGS, row)
    assert "**not** on brand" not in text
    assert "\\*\\*not\\*\\* on brand" in text


def test_an_approved_theme_says_it_is_queued_not_live():
    """Approval posts nothing, so the receipt must not imply it is up."""
    text = theme_resolution_dm_text(_SETTINGS, _row("approved", refunded_at=None))
    assert "runs the next time the channel is free" in text


def test_a_live_theme_says_it_is_up_now():
    text = theme_resolution_dm_text(_SETTINGS, _row("live", refunded_at=None))
    assert "live now" in text and "24 hours" in text
