"""Contract: every public paid-perk card advertises the shop, and truthfully.

Two members ever see a themed day or a Pin of the Day: the one who bought it,
and everyone else. The card in the channel is the only place the second group
learns the thing was purchasable at all, so it carries a pointer back to the
shop (todo #181).

The pointer is the fragile half. ``/bank theme``, ``/bank pin`` and
``/bank sponsor`` were **deleted** when the shop was reorganised into sections
(2026-08-29, commit 093b118f) — a card still naming one would be advertising a
command Discord answers with "unknown integration", and nothing else in the
suite would notice, because a string in a layout's Text Display compiles fine
either way. So the command in the pointer is checked against the cog that
actually defines it, and the section caption against the table the shop
renders from.

Both cards are Components V2 ``LayoutView``s (pin since its 2026-09-28
conversion, theme since the same wave) — a ``LayoutView`` has no field to
carry the pointer in, so both carry the same text (``shop.shop_pointer_line``)
in a ``-#`` subtext line instead.
"""

from __future__ import annotations

import re

import discord
import pytest

from bot_modules.economy.pin_layouts import build_pin_live_layout
from bot_modules.economy.shop import SECTION_CAPTIONS, SECTION_SERVER, SHOP_POINTER
from bot_modules.economy.theme_layouts import build_theme_live_layout
from bot_modules.economy.service import EconSettings
from tests.paths import REPO_ROOT

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems", theme_hours=24
)

_COG = REPO_ROOT / "src/bot_modules/economy/economy_cog.py"


def _name_fn(user_id: int) -> str:
    return f"Member{user_id}"


def _theme_card() -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in build_theme_live_layout(
        _SETTINGS,
        sponsor_id=42,
        name_fn=_name_fn,
        title="Cursed Cooking",
        blurb="Post the worst thing you have ever eaten.",
        expires_at=1_800_000_000,
    ):
        view.add_item(item)
    return view


def _pin_card() -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in build_pin_live_layout(
        sponsor_id=42,
        name_fn=_name_fn,
        message="Remember the movie night is Friday.",
        expires_at=1_800_000_000,
    ):
        view.add_item(item)
    return view


def _card_text(card: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in card.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _pointer_text(card: discord.ui.LayoutView) -> str:
    """The shop-pointer's own line — the only one carrying ``SHOP_POINTER``."""
    texts = [
        item.content
        for item in card.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    matches = [t for t in texts if SHOP_POINTER in t]
    assert len(matches) == 1, "exactly one shop pointer per card"
    return matches[0]


@pytest.mark.parametrize(
    "build, offer",
    [
        pytest.param(_theme_card, "themed day", id="flash-theme"),
        pytest.param(_pin_card, "Pin a message", id="pin-of-the-day"),
    ],
)
def test_public_paid_card_points_at_the_shop(build, offer: str) -> None:
    """Both cards carry the pointer, naming their own product."""
    value = _pointer_text(build())
    assert offer in value
    assert SHOP_POINTER in value


@pytest.mark.parametrize(
    "build",
    [pytest.param(_theme_card, id="flash-theme"), pytest.param(_pin_card, id="pin-of-the-day")],
)
def test_pointer_is_the_last_line(build) -> None:
    """The advertisement sits under the card's own content, never above it.

    The reader came for the theme or the pin; the offer is the footnote.
    """
    text = _card_text(build())
    assert SHOP_POINTER in text.rstrip().splitlines()[-1]


def test_pin_pointer_is_the_last_line() -> None:
    text = _card_text(_pin_card())
    assert text.rstrip().splitlines()[-1].startswith("-# Expires")
    assert SHOP_POINTER in text.rstrip().splitlines()[-1]


def test_pointer_names_a_command_that_still_exists() -> None:
    """The trap this file exists for: a pointer outliving its command.

    Read from the cog source rather than by importing it — a cog import drags
    in the whole bot, and what is being asserted is a fact about the declared
    command surface, which the source states directly.
    """
    source = _COG.read_text(encoding="utf-8")
    cited = re.findall(r"/bank ([a-z_]+)", SHOP_POINTER)
    assert cited, "the pointer should name a command at all"
    for sub in cited:
        assert f'@bank.command(name="{sub}"' in source, (
            f"the shop pointer cites /bank {sub}, which economy_cog no longer "
            "defines — /bank theme, /bank pin and /bank sponsor were deleted "
            "in 093b118f; point members at a command that exists"
        )


def test_pointer_section_matches_the_shop_it_describes() -> None:
    """Rename the aisle and the signpost moves with it, or this fails."""
    assert SECTION_CAPTIONS[SECTION_SERVER] in SHOP_POINTER
