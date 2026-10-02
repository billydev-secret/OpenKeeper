"""Wallet card helpers — memo parsing, memo shortening, rental attribution.

Pure-function tests that moved here with ``build_wallet_layout`` when it left
``economy_cog.py``. The wallet *command* wiring (the ephemeral send, the
leaderboard panel's Wallet button) stays in ``test_economy_cog.py``; the
card's own shape (heading, sections, budget on the fullest wallet the builder
can produce) is pinned at the bottom of this file.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from types import SimpleNamespace

import discord

from bot_modules.core.layout import check_budget
from bot_modules.economy.wallet import build_wallet_layout, ellipsis, memo_of, rental_lines
from bot_modules.economy.service import DEFAULT_ECON_SETTINGS


def test_memo_of_tolerates_missing_and_malformed_meta():
    assert memo_of('{"to": 1, "memo": "hi"}') == "hi"
    assert memo_of('{"to": 1}') is None
    assert memo_of(None) is None
    assert memo_of("") is None
    assert memo_of("not json") is None
    # A non-string memo must not crash the render.
    assert memo_of('{"memo": 5}') is None


def test_ellipsis_trims_trailing_space_before_the_dot():
    assert ellipsis("short memo") == "short memo"
    long_memo = "w" * 60
    assert ellipsis(long_memo).endswith("…")
    assert len(ellipsis(long_memo)) == 40
    # A cut landing on a space must not leave " …".
    assert not ellipsis(("ab " * 30)).endswith(" …")


def _rental(perk, *, owner, beneficiary, state="active"):
    return {
        "perk": perk, "price": 50, "next_bill_at": 1_700_000_000,
        "user_id": owner, "beneficiary_id": beneficiary, "state": state,
    }


def _settings():
    return SimpleNamespace(currency_emoji="🪙")


def _name(uid: int) -> str:
    return f"Member {uid}"


def test_rental_lines_attribute_gifts_in_both_directions():
    """A gift reads differently to the payer and to the wearer."""
    settings = _settings()

    received = rental_lines(
        settings, [_rental("role_color", owner=1, beneficiary=500)], 500, _name
    )
    assert "(gift received)" in received[0]

    sent = rental_lines(
        settings, [_rental("role_color", owner=500, beneficiary=1)], 500, _name
    )
    # Named, never <@1>: the line renders inside the wallet card.
    assert "(gift to Member 1)" in sent[0]

    # Self-rented: no attribution at all.
    own = rental_lines(
        settings, [_rental("role_color", owner=500, beneficiary=500)], 500, _name
    )
    assert "gift" not in own[0]


def test_rental_lines_mark_grace_state():
    line = rental_lines(
        _settings(),
        [_rental("role_name", owner=500, beneficiary=500, state="grace")],
        500,
        _name,
    )[0]
    assert "⏳ in grace" in line
    assert "Custom Role Name" in line


def test_fullest_card_fits_the_budget():
    """The wallet is the only card here with three independently-capped,
    combinable sections (Recent Activity / Active Rentals / At the Tables,
    each up to ``fit_lines``'s 1024-char field cap) — their *combined* total
    against the message-wide budget is asserted here, not assumed from each
    section fitting on its own. Measured, not assumed (guide § The budget)."""
    settings = dataclasses.replace(
        DEFAULT_ECON_SETTINGS, currency_icon_url="https://example.invalid/coin.png"
    )
    ledger = [
        {
            "amount": -250 if i % 2 else 300,
            "kind": "rental" if i % 2 else "quest",
            "meta": '{"memo": "a fairly long memo describing this transaction"}',
            "created_at": 1_700_000_000 - i * 60,
        }
        for i in range(30)
    ]
    rentals = [
        _rental(perk, owner=500, beneficiary=(501 if i % 2 else 500))
        for i, perk in enumerate(
            ["role_color", "role_name", "role_icon", "role_gradient", "role_holo"] * 3
        )
    ]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    casino = conn.execute(
        "SELECT 40 AS plays, 12000 AS wagered, 15500 AS returned, 5 AS streak, "
        "3000 AS biggest_win, 'Blackjack' AS biggest_win_game"
    ).fetchone()
    items = build_wallet_layout(
        settings,
        balance=1_234_567,
        ledger=ledger,
        rentals=rentals,
        shields=1,
        casino=casino,
        viewer_id=500,
        name_fn=_name,
    )
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    check_budget(view)


def _texts(items) -> list[str]:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return [
        c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)
    ]


def _minimal(settings, **kw):
    args = dict(
        balance=0, ledger=[], rentals=[], shields=0, casino=None,
        viewer_id=500, name_fn=_name,
    )
    args.update(kw)
    return build_wallet_layout(settings, **args)


def test_card_leads_with_a_glyph_led_title_and_says_when_empty():
    """Guide § Card anatomy: every card opens on a ``##`` title led by its
    glyph. A new member's wallet still says so rather than rendering a bare
    heading, and the optional sections stay out."""
    settings = dataclasses.replace(
        DEFAULT_ECON_SETTINGS, currency_emoji="💎", wallet_name="Vault",
        currency_icon_url="",
    )
    items = _minimal(settings)
    texts = _texts(items)
    assert texts[0].startswith("## 💎 Vault")
    assert "Streak shield" not in texts[0]
    assert any("_No activity yet._" in t for t in texts)
    assert not any("Active Rentals" in t or "At the Tables" in t for t in texts)
    assert not any(isinstance(i, discord.ui.Container) for i in items)
    assert not any(isinstance(i, discord.ui.Section) for i in items)


def test_currency_icon_rides_beside_the_heading_and_shield_is_noted():
    settings = dataclasses.replace(
        DEFAULT_ECON_SETTINGS, currency_icon_url="https://example.invalid/coin.png"
    )
    items = _minimal(settings, shields=1)
    head = items[0]
    assert isinstance(head, discord.ui.Section)
    assert isinstance(head.accessory, discord.ui.Thumbnail)
    assert "🛡️ Streak shield held" in _texts(items)[0]
