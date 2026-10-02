"""``bot_modules/economy/layouts.py`` — the Components V2 cards.

Three pure builders, offline-renderable exactly like the embed builders they
replaced (the grant receipt, the mute toggle receipt and the pay confirm card;
a fourth, the QOTD-post fallback card, went with ``/qotd`` in 2026-09). What's
worth its own assertion here: the paradigm shape, the budget, no
``Container`` anywhere, and every member named through a pre-escaped string
rather than a raw mention.
"""

from __future__ import annotations

import dataclasses

import discord
import pytest

from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.economy.layouts import (
    PAY_CANCEL_ID,
    PAY_CONFIRM_ID,
    build_grant_receipt_layout,
    build_mute_toggle_layout,
    build_pay_confirm_layout,
)
from bot_modules.economy.service import DEFAULT_ECON_SETTINGS


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _all_text(items) -> str:
    return "\n".join(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _grant_items(**overrides):
    kwargs = dict(
        settings=DEFAULT_ECON_SETTINGS,
        credited=30,
        amount=30,
        booster=False,
        member_name="Alex",
        reason="staff pick",
        actor_name="Mod",
    )
    kwargs.update(overrides)
    return build_grant_receipt_layout(**kwargs)


def _mute_items(**overrides):
    kwargs = dict(muted=True)
    kwargs.update(overrides)
    return build_mute_toggle_layout(**kwargs)


def _confirm_items(**overrides):
    kwargs = dict(
        settings=DEFAULT_ECON_SETTINGS,
        amount=200,
        recipient_name="Bob",
        memo=None,
        public=False,
    )
    kwargs.update(overrides)
    return build_pay_confirm_layout(**kwargs)


ALL_CARDS = [
    pytest.param(_grant_items, id="grant-receipt"),
    pytest.param(lambda: _mute_items(muted=True), id="mute-on"),
    pytest.param(lambda: _mute_items(muted=False), id="mute-off"),
    pytest.param(_confirm_items, id="pay-confirm"),
]


# ── Every card renders, fits, and stays bare ────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


def _lead_heading(items) -> str:
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


def _heading_faults(heading: str) -> list[str]:
    """What is wrong with a card's lead line, per guide § Anatomy."""
    if not heading.startswith("## "):
        return [f"a card-level heading is a `## Title` line: {heading!r}"]
    if len(heading) < 4 or heading[3].isascii():
        return [f"lead the title with the feature's glyph: {heading!r}"]
    return []


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    assert not _heading_faults(_lead_heading(build()))


@pytest.mark.parametrize(
    ("heading", "faulty"),
    [
        pytest.param("## 💰 Currency Granted", False, id="glyph-led-title"),
        pytest.param("### Currency Granted", True, id="section-heading-as-title"),
        pytest.param("## Currency Granted", True, id="no-glyph"),
        pytest.param("Currency Granted", True, id="no-heading"),
    ],
)
def test_the_heading_sweep_can_actually_see_a_violation(heading, faulty):
    """The sweep above is born green; this is what proves it can go red."""
    assert bool(_heading_faults(heading)) is faulty


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_writes_a_raw_mention(build):
    """Every card here is mention-clean (docs/embed_style_guide.md § no bare
    mentions)."""
    assert "<@" not in _all_text(build())


# ── Grant receipt — paradigm 7 ──────────────────────────────────────────


def test_grant_receipt_names_the_actor_and_recipient():
    text = _all_text(_grant_items())
    assert "Alex" in text
    assert "-# Granted by Mod" in text


def test_grant_receipt_shows_booster_math_only_when_it_changed_the_payout():
    text = _all_text(_grant_items(booster=True, amount=20, credited=30))
    assert "Booster bonus" in text
    assert "20" in text and "1.5" in text

    # Booster flagged but credited == amount (e.g. rounding landed even):
    # no line claiming a bonus that didn't show up in the number.
    text = _all_text(_grant_items(booster=True, amount=30, credited=30))
    assert "Booster bonus" not in text


def test_grant_receipt_escapes_a_free_text_reason():
    text = _all_text(_grant_items(reason="*shady* markdown"))
    assert "\\*shady\\*" in text


def test_grant_receipt_escapes_the_actor_name():
    text = _all_text(_grant_items(actor_name="*Mod*"))
    assert "-# Granted by \\*Mod\\*" in text


def test_grant_receipt_omits_the_reason_line_when_none_given():
    text = _all_text(_grant_items(reason=""))
    assert "> " not in text


def test_grant_receipt_thumbnails_a_configured_currency_icon():
    settings = dataclasses.replace(
        DEFAULT_ECON_SETTINGS, currency_icon_url="https://example.invalid/coin.png"
    )
    items = _grant_items(settings=settings)
    section = next(i for i in items if isinstance(i, discord.ui.Section))
    assert section.accessory.media.url == "https://example.invalid/coin.png"


# ── Mute toggle — paradigm 7 ─────────────────────────────────────────────


def test_mute_toggle_reads_differently_muted_vs_unmuted():
    muted_text = _all_text(_mute_items(muted=True))
    unmuted_text = _all_text(_mute_items(muted=False))
    assert "Muted" in muted_text
    assert "On" in unmuted_text
    assert muted_text != unmuted_text


# ── Pay confirm — paradigm 5: decision card ──────────────────────────────


def test_pay_confirm_carries_both_buttons_with_transient_ids():
    items = _confirm_items()
    row = next(i for i in items if isinstance(i, discord.ui.ActionRow))
    buttons = {b.label: b.id for b in row.children if isinstance(b, discord.ui.Button)}
    assert buttons == {"Confirm": PAY_CONFIRM_ID, "Cancel": PAY_CANCEL_ID}


def test_pay_confirm_shows_the_memo_when_given():
    text = _all_text(_confirm_items(memo="rent money"))
    assert "rent money" in text


def test_pay_confirm_warns_the_receipt_will_be_public():
    public_text = _all_text(_confirm_items(public=True))
    private_text = _all_text(_confirm_items(public=False))
    assert "posted in this channel" in public_text
    assert "posted in this channel" not in private_text


def test_pay_confirm_escapes_a_typed_memo():
    text = _all_text(_confirm_items(memo="*bold* memo"))
    assert "\\*bold\\*" in text


# ── Budget ──────────────────────────────────────────────────────────────


def test_fullest_card_fits_the_budget():
    """The pay-confirm card (heading + memo + note + two buttons) is the
    fullest this module builds. Measured, not assumed (guide § The budget)."""
    view = _view(
        _confirm_items(memo="a reasonably long memo about what this is for", public=True)
    )
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS
