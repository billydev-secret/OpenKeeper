"""Components V2 card builders for the Coin Drops feature.

The V2 replacement for the three ``discord.Embed`` builders
``economy_drops_loop.py`` used to hold (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3a). Every function here is
**pure**: primitives and a ``name_fn`` in, a list of ``discord.ui.Item`` out.
No IO, no accent lookup — ``economy_drops_loop.py`` builds the persistent
``DropClaimButton`` (a ``DynamicItem`` that already carries its own callback,
unrelated to the ``find_item`` binding this module's caller would otherwise
need) and hands it in, so this module never has to know about drop ids.

Each builder's docstring names the paradigm it builds
(``docs/components_v2_style_guide.md`` § The paradigms).

* **A card is bare** — no ``Container``, no accent colour. 🪂 stays on the
  announcement only — it is the event notice, not an outcome. The two
  receipts take distinct outcome glyphs from the guide's set (guide § Words:
  "One glyph per outcome"): ✅ for the claim, ⌛ for the expiry, so a win and
  a non-event don't share a heading glyph.
* **A card's own heading is one glyph-led ``## Title`` line.**
* **Framed in the guild's thin card strips** (``art``,
  ``core.layout.card_strips``; Ben, 2026-09-30), on the pouch and on both
  receipts it edits into, so the card keeps its frame through the edit.
* **The claimant is named through ``name_fn``**, never as ``<@id>``: a
  mention in a Text Display renders from the *reading* client's cache, and
  the pouch card is read by everyone in the channel, most of whom have not
  necessarily seen the winner recently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.layout import card_strips
from bot_modules.economy.service import EconSettings
from bot_modules.shared.name_resolver import NameFn

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _unit(settings: EconSettings, amount: int) -> str:
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


def build_drop_layout(
    *,
    settings: EconSettings,
    amount: int,
    expires_at: float,
    claim_button: discord.ui.Item[Any],
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """The pouch as it lands in the channel. **Paradigm 5 — decision card.**

    Facts, then the one decision button in the bottom row — Claim is the
    only choice the card offers, same shape as a two- or three-button
    decision card with the count at its minimum. ``claim_button`` is built
    by the caller (``DropClaimButton``, a persistent ``DynamicItem`` whose
    ``custom_id`` carries the drop id) rather than by this function, which
    keeps the builder a function of the drop's own data alone. A winning
    click edits this very card into :func:`build_claimed_layout`'s receipt
    with the button gone; the expiry sweep edits it into
    :func:`build_expired_layout` instead — both are the "edits into a
    receipt with the buttons removed" half of the paradigm.
    """
    top, bottom = card_strips(art)
    return [
        *top,
        _text(
            "## 🪂 Coin Drop!\n"
            f"A pouch of {settings.currency_emoji} **{amount:,} "
            f"{_unit(settings, amount)}** just landed.\n"
            "First to press **Claim** grabs it — "
            f"it vanishes <t:{int(expires_at)}:R>."
        ),
        discord.ui.ActionRow(claim_button),
        *bottom,
    ]


def build_claimed_layout(
    *,
    settings: EconSettings,
    credited: int,
    claimant_id: int,
    name_fn: NameFn,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """The pouch, claimed. **Paradigm 7 — receipt.**

    Replaces the decision card's button with nothing: the race the card
    offered is over, and there is nothing left to press.
    """
    top, bottom = card_strips(art)
    return [
        *top,
        _text(
            "## ✅ Drop Claimed!\n"
            f"**{name_fn(claimant_id)}** grabbed {settings.currency_emoji} "
            f"**{credited:,} {_unit(settings, credited)}**."
        ),
        *bottom,
    ]


def build_expired_layout(art: PanelArt | None = None) -> list[discord.ui.Item[Any]]:
    """Nobody claimed the pouch before it timed out. **Paradigm 7 — receipt.**"""
    top, bottom = card_strips(art)
    return [*top, _text("## ⌛ Drop Vanished\nNobody grabbed it in time."), *bottom]
