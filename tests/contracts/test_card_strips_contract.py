"""Every public card that wears the thin card strips (Ben, 2026-09-30: "a
third thinner header with no title for use in the embeds") opens on the card
header and closes on the card footer, and is exactly its old self without
them — one row per builder. ``tests/contracts/test_layout_hub_banner.py``'s
``CARD_ART_BUILDERS`` pins who may draw them; this pins that each one does,
and in the right place.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from bot_modules.casino import pools_logic, pools_metrics
from bot_modules.casino.cog import layouts as casino
from bot_modules.core.branding_service import PanelArt
from bot_modules.dm.perms.layouts import build_audit_notice_layout as dm_audit
from bot_modules.economy import view_helpers
from bot_modules.economy.approval_layouts import build_review_card_layout
from bot_modules.economy.auction_layouts import build_auction_card_layout
from bot_modules.economy.bounty_layouts import build_bounty_card_layout
from bot_modules.economy.drops_layouts import (
    build_claimed_layout,
    build_drop_layout,
    build_expired_layout,
)
from bot_modules.economy.layouts import build_grant_receipt_layout
from bot_modules.economy.pin_layouts import build_pin_live_layout, build_pin_review_layout
from bot_modules.economy.service import EconSettings
from bot_modules.economy.theme_layouts import build_theme_review_layout
from bot_modules.economy.transfers import build_payment_receipt_layout

HEADER = "https://dash.example/static/panel-banners/" + "c" * 32 + ".webp"
FOOTER = "https://dash.example/static/panel-banners/" + "d" * 32 + ".webp"
ART = PanelArt(None, None, card_header=HEADER, card_footer=FOOTER)

ECON = EconSettings(currency_emoji="🪙", currency_name="coin", currency_plural="coins")
ICON_ECON = EconSettings(
    currency_emoji="🪙", currency_name="coin", currency_plural="coins",
    currency_icon_url="https://cdn.example/coin.png",
)
NAME_FN = lambda uid: f"N{uid}"  # noqa: E731
BUTTONS = discord.ui.ActionRow(discord.ui.Button(label="Approve", custom_id="x:approve"))


def _bounty(art):
    row = {
        "id": 1, "state": "open", "title": "Draw the mascot", "description": "",
        "poster_id": 5, "winner_id": 0, "payout": 0, "rake_amount": 0, "resolved_at": 0,
    }
    return build_bounty_card_layout(
        ECON, row, pot=100, contributors=2, name_fn=NAME_FN, art=art
    )


def _auction(art, state="open"):
    row = {
        "state": state, "title": "Rare hat", "description": "", "created_by": 5,
        "winner_id": None, "winning_bid": None, "high_bid": None,
        "high_bidder_id": None, "ends_at": 1_750_000_000, "opening_bid": 10,
        "min_increment": 1,
    }
    return build_auction_card_layout(
        settings=ECON, auction=row, bids=0, name_fn=NAME_FN,
        bid_button=discord.ui.Button(label="Bid", custom_id="x:bid") if state == "open" else None,
        art=art,
    )


def _big_win(art):
    built = casino.build_big_win_broadcast_layout(
        "recap", payout=5000, threshold=1000, stake=10, min_mult=5,
        game_label="Slots", winner_name="Brynja",
        winner_icon="https://cdn.example/av.png", art=art,
    )
    assert built is not None
    return built.items


POOLS_SPEC = pools_metrics.SPECS["messages"]

#: Each public card, as ``art -> items``.
CARDS = [
    pytest.param(_bounty, id="bounty-card"),
    pytest.param(_auction, id="auction-open"),
    pytest.param(lambda art: _auction(art, "cancelled"), id="auction-cancelled"),
    pytest.param(
        lambda art: build_drop_layout(
            settings=ECON, amount=50, expires_at=1_750_000_000,
            claim_button=discord.ui.Button(label="Claim", custom_id="x:claim"), art=art,
        ),
        id="drop",
    ),
    pytest.param(
        lambda art: build_claimed_layout(
            settings=ECON, credited=50, claimant_id=5, name_fn=NAME_FN, art=art
        ),
        id="drop-claimed",
    ),
    pytest.param(lambda art: build_expired_layout(art), id="drop-expired"),
    pytest.param(
        lambda art: build_pin_live_layout(
            sponsor_id=5, name_fn=NAME_FN, message="hi", expires_at=1_750_000_000, art=art
        ),
        id="pin-live",
    ),
    pytest.param(
        lambda art: build_grant_receipt_layout(
            ICON_ECON, credited=10, amount=10, booster=False, member_name="Ada",
            reason="", actor_name="Mod", art=art,
        ),
        id="grant-receipt",
    ),
    pytest.param(
        lambda art: build_payment_receipt_layout(
            ICON_ECON, "Ada", "Bo", 10, public=True, art=art
        ),
        id="pay-receipt-public",
    ),
    pytest.param(
        lambda art: casino.build_jackpot_celebration_layout(
            ECON, 5, 1000, name_fn=NAME_FN, art=art
        ),
        id="jackpot",
    ),
    pytest.param(_big_win, id="big-win"),
    pytest.param(
        lambda art: casino.build_pools_panel_layout(
            ECON, 1200.0, pools_logic.PoolSplit(400, 250), 1_750_000_000,
            "2026-08-03", spec=POOLS_SPEC, controls=BUTTONS, art=art,
        ),
        id="pools-panel",
    ),
    pytest.param(
        lambda art: casino.build_pools_result_layout(
            ECON, "2026-08-03", 1250, 1200.0, "over", [(5, 10, 20)], 0,
            spec=POOLS_SPEC, name_fn=NAME_FN, art=art,
        ),
        id="pools-result",
    ),
    pytest.param(
        lambda art: casino.build_pools_void_layout("2026-08-03", 250, art=art),
        id="pools-void",
    ),
    pytest.param(
        lambda art: build_pin_review_layout(
            ECON, sponsor_id=5, name_fn=NAME_FN, message="hi", price=200,
            state="pending", buttons=BUTTONS, art=art,
        ),
        id="pin-review",
    ),
    pytest.param(
        lambda art: build_theme_review_layout(
            ECON, sponsor_id=5, name_fn=NAME_FN, title="Cursed", blurb="Eat it.",
            price=200, state="denied", art=art,
        ),
        id="theme-review-resolved",
    ),
    pytest.param(
        lambda art: dm_audit(message="Ada asked Bo.", timestamp=1_750_000_000, art=art),
        id="dm-perms-audit",
    ),
]


def _urls(items) -> list[str]:
    return [
        g.items[0].media.url for g in items if isinstance(g, discord.ui.MediaGallery)
    ]


@pytest.mark.parametrize("build", CARDS)
def test_card_opens_and_closes_on_the_strips(build):
    items = build(ART)
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert items[0].items[0].media.url == HEADER
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert items[-1].items[0].media.url == FOOTER
    # Nothing is drawn onto the strips, so the card keeps its own heading.
    assert isinstance(items[1], (discord.ui.TextDisplay, discord.ui.Section))


@pytest.mark.parametrize("build", CARDS)
@pytest.mark.parametrize(
    "art",
    [
        pytest.param(None, id="no-art-read"),
        pytest.param(PanelArt(None, None), id="unset"),
        pytest.param(PanelArt(None, None, card_footer=FOOTER), id="footer-alone"),
    ],
)
def test_card_is_unchanged_without_a_card_header(build, art):
    """Ships dark: until a card header is uploaded, every card is exactly
    what it was — the footer alone never shows."""
    assert HEADER not in _urls(build(art)) and FOOTER not in _urls(build(art))
    assert len(build(art)) == len(build(None))


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_bounty, id="bounty"),
        pytest.param(_auction, id="auction"),
        pytest.param(
            lambda art: casino.build_pools_panel_layout(
                ECON, 1200.0, pools_logic.PoolSplit(400, 250), 1_750_000_000,
                "2026-08-03", spec=POOLS_SPEC, controls=BUTTONS, art=art,
            ),
            id="pools-panel",
        ),
        pytest.param(
            lambda art: build_review_card_layout(
                state="pending", pending_heading="Review", approved_heading="A",
                denied_heading="D", body="b", buttons=BUTTONS, art=art,
            ),
            id="review-pending",
        ),
    ],
)
def test_the_buttons_sit_inside_the_frame(build):
    """The footer closes the card below its buttons, not between the text
    and them."""
    items = build(ART)
    assert isinstance(items[-2], discord.ui.ActionRow)


def test_the_private_pay_receipt_stays_plain():
    """The builder, not each caller, holds the line: an ephemeral receipt
    never wears the strips, as it never shows the balance publicly."""
    items = build_payment_receipt_layout(ECON, "Ada", "Bo", 10, balance=90, art=ART)
    assert _urls(items) == []


def test_the_retired_pools_panel_keeps_its_frame_without_buttons():
    items = casino.build_pools_panel_layout(
        ECON, 1200.0, pools_logic.PoolSplit(400, 250), 1_750_000_000,
        "2026-08-03", spec=POOLS_SPEC, closed=True, art=ART,
    )
    assert _urls(items)[0] == HEADER and _urls(items)[-1] == FOOTER
    assert not [i for i in items if isinstance(i, discord.ui.ActionRow)]


def test_a_big_win_ping_sits_above_the_frame():
    """The caller puts ``@here`` in front of the built items, so the frame
    is the card's and the ping line stays outside it."""
    built = casino.build_big_win_broadcast_layout(
        "recap", payout=5000, threshold=1000, stake=10, min_mult=5,
        game_label="Slots", art=ART,
    )
    assert built is not None
    assert _urls(built.items)[0] == HEADER


# ── the review cards' surfaces ──────────────────────────────────────────


def _surface_art(ctx, surface):
    with patch(
        "bot_modules.core.card_art.read_card_art", AsyncMock(return_value=ART)
    ) as read:
        got = asyncio.run(view_helpers.surface_art(ctx, surface))
    return got, read


def test_the_approvals_channel_copy_wears_the_strips():
    guild = SimpleNamespace(id=1)
    got, read = _surface_art(SimpleNamespace(), SimpleNamespace(guild=guild))
    assert got is ART
    read.assert_awaited_once()


@pytest.mark.parametrize(
    "ctx, surface",
    [
        # The todo board's ephemeral detail: one mod's working surface.
        pytest.param(SimpleNamespace(), view_helpers.EphemeralCard(None), id="ephemeral"),
        pytest.param(None, SimpleNamespace(guild=SimpleNamespace(id=1)), id="no-ctx"),
        pytest.param(SimpleNamespace(), None, id="no-card"),
        pytest.param(SimpleNamespace(), SimpleNamespace(guild=None), id="no-guild"),
    ],
)
def test_other_surfaces_stay_plain(ctx, surface):
    got, read = _surface_art(ctx, surface)
    assert got is None
    read.assert_not_awaited()


@pytest.mark.parametrize(
    "card, expected",
    [
        pytest.param(
            SimpleNamespace(guild=SimpleNamespace(id=1), edit=AsyncMock()), ART,
            id="channel-card",
        ),
        pytest.param(
            view_helpers.EphemeralCard(SimpleNamespace(edit_original_response=AsyncMock())),
            None,
            id="board-ephemeral",
        ),
    ],
)
def test_a_review_repaint_keeps_the_frame_on_the_channel_card_only(card, expected):
    seen = []

    def _layout(settings, row, name_fn=None, *, art=None):
        seen.append(art)
        return discord.ui.LayoutView(timeout=None)

    with patch(
        "bot_modules.core.card_art.read_card_art", AsyncMock(return_value=ART)
    ):
        asyncio.run(
            view_helpers.edit_review_card_layout(
                card, ECON, {"id": 1}, build_layout=_layout, log_label="t",
                ctx=SimpleNamespace(),
            )
        )
    assert seen == [expected]
