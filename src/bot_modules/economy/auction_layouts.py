"""Components V2 card builder for the live-auction sticky card.

The V2 replacement for ``render_auction_card`` in ``economy/auction_views.py``
(deleted with this conversion — ``docs/plans/components_v2_migration.md``
wave 3a). This module has **one** builder: the auction card is a single
sticky message that is fully rebuilt and re-sent on every state change (a
bid, a close, a cancel), so the states below are branches of one function
rather than separate cards the way a hub's receipts are.

Conventions carried over from the pilot (``docs/components_v2_style_guide.md``
§ Anatomy, § Words):

* **A card is bare** — no ``Container``, no accent colour.
* **A card's own heading is one glyph-led ``## Title`` line.** The auction
  keeps its own established glyph vocabulary (🔨 the hammer, ✖️ cancelled,
  🏆 winner, 🙋 high bidder, ⏳ the clock) rather than the guide's outcome set
  — those glyphs predate the conversion and are this feature's own, per
  ``embed_style_guide.md`` § Card anatomy's "keep the glyph vocabulary
  consistent within a feature" rule.
* **Host, winner and high bidder are named through ``name_fn``**, never
  ``<@id>``: a mention in a Text Display renders from the *reading* client's
  cache, so it shows as a bare number to anyone who hasn't seen that member.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.layout import card_strips
from bot_modules.economy.view_helpers import coins as _coins
from bot_modules.economy.auction_service import min_next_bid
from bot_modules.economy.service import EconSettings
from bot_modules.shared.name_resolver import NameFn

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_auction_card_layout(
    *,
    settings: EconSettings,
    auction: Any,
    bids: int,
    name_fn: NameFn,
    bid_button: discord.ui.Item[Any] | None = None,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """The sticky auction card in its current state. **Paradigm 8 — live
    board.**

    The same card is rebuilt from the row and re-sent on every state change
    — a bid, a close, a cancel — rather than edited part by part, so it needs
    no stable component ids of its own; the state that survives a restart
    lives on ``bid_button``'s ``custom_id`` (an ``AuctionBidButton``
    ``DynamicItem``, built by the caller so this stays pure), not on
    anything here.

    ``bid_button`` is the card-level action (guide § Anatomy: "card-level
    actions stay in an ActionRow at the bottom") and is omitted once the
    auction is no longer ``open`` — a closed or cancelled card has nothing
    left to press. Passing ``None`` for an open auction (there is always a
    button while one is open) is a caller error, not a state this builder
    renders.

    ``auction`` is a ``sqlite3.Row`` (or anything subscriptable the same
    way) from ``economy_auction_service``. ``art`` frames the card in the
    guild's thin card strips (``core.layout.card_strips``; Ben, 2026-09-30).
    """
    state = str(auction["state"])
    title = discord.utils.escape_markdown(str(auction["title"]))
    description = discord.utils.escape_markdown(str(auction["description"] or ""))
    host = name_fn(int(auction["created_by"]))

    if state == "closed":
        heading = (
            f"## 🔨 Sold — {title}"
            if auction["winner_id"] is not None
            else f"## 🔨 Auction Closed — {title}"
        )
    elif state == "cancelled":
        heading = f"## ✖️ Auction Cancelled — {title}"
    else:
        heading = f"## 🔨 Auction — {title}"

    lead = heading
    if description:
        lead += f"\n🎁 {description}"
    lead += f"\n🎙️ Hosted by **{host}**"

    items: list[discord.ui.Item[Any]] = [_text(lead)]

    if state == "closed":
        winner = auction["winner_id"]
        if winner is not None:
            items.append(
                _text(
                    f"🏆 Winner: **{name_fn(int(winner))}** · 🔨 Winning Bid: "
                    f"{_coins(settings, int(auction['winning_bid']))}"
                )
            )
            items.append(_text("-# The host will hand over the prize."))
        else:
            items.append(_text("Nobody bid — nothing changes hands."))
    elif state == "cancelled":
        items.append(_text("↩️ The standing high bid was returned in full."))
    else:
        high = auction["high_bid"]
        if high is not None:
            items.append(
                _text(
                    f"🔨 Current Bid: {_coins(settings, int(high))} · 🙋 High "
                    f"Bidder: **{name_fn(int(auction['high_bidder_id']))}**"
                )
            )
        else:
            items.append(
                _text(
                    f"🔨 Opening Bid: {_coins(settings, min_next_bid(settings, auction))}"
                )
            )
        bid_word = "Bid" if bids == 1 else "Bids"
        items.append(
            _text(f"⏳ Ends <t:{int(float(auction['ends_at']))}:R> · 🙌 {bids} {bid_word}")
        )
        if bid_button is not None:
            items.append(discord.ui.ActionRow(bid_button))

    top, bottom = card_strips(art)
    return [*top, *items, *bottom]
