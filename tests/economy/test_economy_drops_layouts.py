"""``bot_modules/economy/drops_layouts.py`` — the Components V2 cards.

Three builders, pure functions renderable offline exactly like the embed
builders they replaced. The things worth their own assertions: the budget
(trivially small here, but the guide asks every feature to measure rather
than assume), the paradigm shape (a decision card with one button that
disappears on both of its receipts), no ``Container`` anywhere, and the
claimant named through ``name_fn`` rather than a raw mention.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.economy.drops_layouts import (
    build_claimed_layout,
    build_drop_layout,
    build_expired_layout,
)
from bot_modules.economy.service import DEFAULT_ECON_SETTINGS

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731

CLAIMANT_ID = 222222222222222222
EXPIRES_AT = 1_750_086_400


def _button(custom_id: str = "econ_drop:claim:1") -> discord.ui.Button:
    return discord.ui.Button(
        label="Claim", emoji="🪙", style=discord.ButtonStyle.success,
        custom_id=custom_id,
    )


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


def _drop_items():
    return build_drop_layout(
        settings=DEFAULT_ECON_SETTINGS,
        amount=25,
        expires_at=EXPIRES_AT,
        claim_button=_button(),
    )


def _claimed_items():
    return build_claimed_layout(
        settings=DEFAULT_ECON_SETTINGS,
        credited=25,
        claimant_id=CLAIMANT_ID,
        name_fn=NAME_FN,
    )


ALL_CARDS = [
    pytest.param(_drop_items, id="drop"),
    pytest.param(_claimed_items, id="claimed"),
    pytest.param(lambda: build_expired_layout(), id="expired"),
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
        pytest.param("## 🪂 Coin Drop!", False, id="glyph-led-title"),
        pytest.param("### Coin Drop!", True, id="section-heading-as-title"),
        pytest.param("## Coin Drop!", True, id="no-glyph"),
        pytest.param("Coin Drop!", True, id="no-heading"),
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
    assert "<@" not in _all_text(build())


def test_claimed_card_names_the_winner_through_the_resolver():
    assert f"N{CLAIMANT_ID}" in _all_text(_claimed_items())


# ── Paradigm 5: decision card in, receipt out, same one button gone ────


def test_drop_card_carries_exactly_the_claim_button_it_was_given():
    """Paradigm 5 — the one decision button lives in the bottom ActionRow,
    and it is the very item the caller built (a persistent ``DynamicItem``
    in the shipped code), not a copy the builder constructed itself."""
    button = _button()
    items = build_drop_layout(
        settings=DEFAULT_ECON_SETTINGS,
        amount=25,
        expires_at=EXPIRES_AT,
        claim_button=button,
    )
    rows = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    assert len(rows) == 1
    assert list(rows[0].children) == [button]


@pytest.mark.parametrize("build", [_claimed_items, lambda: build_expired_layout()])
def test_receipts_carry_no_buttons(build):
    """The two ways a pouch settles are bare receipts — paradigm 7 — with the
    decision card's button gone, not disabled."""
    assert not any(
        isinstance(item, (discord.ui.ActionRow, discord.ui.Button))
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize(
    ("amount", "unit"),
    [
        pytest.param(1, "Coin", id="singular"),
        pytest.param(2, "Coins", id="plural"),
        pytest.param(0, "Coins", id="zero-is-plural"),
    ],
)
def test_drop_card_pluralizes_the_currency_unit(amount, unit):
    text = _all_text(
        build_drop_layout(
            settings=DEFAULT_ECON_SETTINGS,
            amount=amount,
            expires_at=EXPIRES_AT,
            claim_button=_button(),
        )
    )
    assert f"{amount:,} {unit}" in text


def test_drop_card_counts_down_the_expiry():
    text = _all_text(_drop_items())
    assert f"<t:{EXPIRES_AT}:R>" in text


# ── Budget ──────────────────────────────────────────────────────────────


def test_fullest_card_fits_the_budget():
    """The pouch card is the fullest this feature can send: one heading and
    one button. Measured, not assumed (guide § The budget)."""
    view = _view(_drop_items())
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS
