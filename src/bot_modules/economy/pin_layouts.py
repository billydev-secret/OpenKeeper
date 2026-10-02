"""Components V2 card builders for Pin of the Day.

The V2 replacement for the ``Embed(``-building code that used to live in
``pin_views.py``. Both functions here are **pure**: primitives and a
``name_fn`` in, a list of ``discord.ui.Item`` out. No IO, no accent colour,
no callbacks — ``pin_views.py``'s view builders add the items to a
``LayoutView`` and bind behaviour by persistent ``custom_id``.

Each builder's docstring names the paradigm it builds
(``docs/components_v2_style_guide.md`` § The paradigms).

The review card reuses ``approval_layouts.build_review_card_layout`` rather
than inventing a second near-identical Approve/Decline shape — the same
reuse ``quest_layouts.build_signoff_card_layout`` established, and the module
that shape lives in names pin by name as an intended caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import discord

from bot_modules.core.layout import card_strips
from bot_modules.economy.approval_layouts import build_review_card_layout
from bot_modules.economy.shop import shop_pointer_line
from bot_modules.economy.view_helpers import coins as _reward_text
from bot_modules.economy.service import EconSettings
from bot_modules.shared.name_resolver import NameFn, mention, named_or_anonymous

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _quote(text: str) -> str:
    """A member's own words, quoted and escaped — guide § Words."""
    return "\n".join(
        f"> {line}" for line in discord.utils.escape_markdown(text).splitlines()
    )


# ── mod-review card ─────────────────────────────────────────────────────


def build_pin_review_layout(
    settings: EconSettings,
    *,
    sponsor_id: int,
    name_fn: NameFn = mention,
    message: str,
    price: int,
    state: str,
    resolver_id: Optional[int] = None,
    deny_reason: Optional[str] = None,
    expires_at: Optional[int] = None,
    refunded: bool = False,
    buttons: Optional["discord.ui.ActionRow[Any]"] = None,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """Build the mod-review view of a pin submission in the given state.

    **Paradigm 5 — decision card, redrawn as its own receipt once resolved**
    (``approval_layouts.build_review_card_layout``). Pin's five submission
    states collapse to that function's three: ``live`` is the approved
    receipt, ``denied``/``expired``/``superseded`` all render the shape that
    carries no more buttons (none of the three offers a decision any more),
    anything else is the pending decision card — but only ``denied`` and an
    unreviewed ``expired`` are actually a *decline*, so those two alone keep
    the ``denied_heading`` slot's default "❌ Pin Declined"; a pin that went
    live and lapsed, or lost to a newer approval, gets "📌 Pin Ended" instead
    (same split Flash Themes uses for its own ``expired``,
    ``theme_layouts.build_theme_review_layout``'s ``ended_unrefunded``) — the
    opposite of a decline, and the card's own "no refund" facts line below
    would otherwise flatly contradict a "Declined" heading above it.

    ``expired`` covers both a pending request nobody reviewed (refunded) and
    a live pin that ran its day or was pulled early (not refunded,
    ``resolver_id`` left holding whichever mod originally *approved* it —
    ``economy_pin_service``'s ``expire_live_pins``/``go_live`` never touch
    the column on that transition), and ``superseded`` is never refunded
    either. ``refunded`` — read by the caller from the row's
    ``refunded_at IS NOT NULL`` — is what actually knows whether money went
    back, the same fix ``theme_layouts.build_theme_review_layout`` applies for
    the identical ambiguity; branching the facts line (or the heading) on
    state alone would tell a mod a lapsed or superseded pin was refunded
    when it wasn't. For the same reason ``resolved_by`` only attributes a
    "Declined by" to ``state == "denied"`` — crediting the approving mod's
    id with declining a pin that simply ran out or lost to a newer one would
    name the wrong action to the wrong mod.

    The sponsor is named through ``name_fn`` via ``named_or_anonymous``,
    which renders "a member" rather than the raw id when the submitter has
    since been erased (``services/name_resolver``) — never ``<@id>``, which
    an embed or a layout alike leaves as a bare number for a mod who hasn't
    seen that member. ``buttons`` is the Approve/Decline row, passed in
    rather than built here, since it carries the two persistent
    ``DynamicItem`` buttons — a layout builder returns components, not
    behaviour.
    """
    card_state = (
        "approved" if state == "live"
        else "denied" if state in ("denied", "expired", "superseded")
        else "pending"
    )
    body = (
        f"**{named_or_anonymous(sponsor_id, name_fn)}** paid "
        f"{_reward_text(settings, price)} to pin a message."
    )
    # Prefixed so this quote reads as the sponsor's words even when the
    # denied-with-reason card stacks the moderator's own unmarked
    # deny_reason_quote right underneath it — same fix quest_layouts.py
    # applies to its own first quote in this shared card shape.
    quote = _quote(f"📝 {message}")
    resolved_by = ""
    facts = ""
    deny_reason_quote = ""
    if card_state == "approved":
        if resolver_id:
            resolved_by = f"Approved by **{name_fn(resolver_id)}**"
        facts = f"Expires <t:{expires_at}:R>" if expires_at else ""
    elif card_state == "denied":
        if state == "denied" and resolver_id:
            resolved_by = f"Declined by **{name_fn(resolver_id)}**"
        facts = (
            f"↩️ {_reward_text(settings, price)} refunded" if refunded
            else "➖ It had its day — no refund"
        )
        if deny_reason:
            deny_reason_quote = _quote(deny_reason)
    # A pin that actually ran — went live and lapsed, or lost to a newer
    # approval — is not a decline, and the ``facts`` line above already says
    # "no refund" rather than claiming one; a "Declined" heading over that
    # would flatly contradict its own subtext. Same split Flash Themes uses
    # for its own three-way ``expired`` (``theme_layouts.build_theme_review_layout``'s
    # ``ended_unrefunded``): only an unreviewed, refunded lapse or an actual
    # mod decline keeps the decline heading.
    lapsed_after_live = state in ("expired", "superseded") and not refunded
    denied_heading = "📌 Pin Ended" if lapsed_after_live else "❌ Pin Declined"
    return build_review_card_layout(
        state=card_state,
        pending_heading="📋 Pin Requested",
        approved_heading="📌 Pin Approved",
        denied_heading=denied_heading,
        body=body,
        quote=quote,
        resolved_by=resolved_by,
        deny_reason_quote=deny_reason_quote,
        facts=facts,
        buttons=buttons,
        art=art,
    )


# ── public pinned card ──────────────────────────────────────────────────


def build_pin_live_layout(
    *,
    sponsor_id: int,
    name_fn: NameFn = mention,
    message: str,
    expires_at: int,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """The card that actually gets pinned in the pin channel.

    **Paradigm 7 — receipt/notice.** Carries the shop pointer
    (``shop.shop_pointer_line``) for the same reason the flash-theme card
    does: a member reading a pin somebody paid for is the one reader already
    demonstrably interested in buying one.

    ``expires_at`` is a Unix time rendered ``<t:…:R>`` in the subtext, per
    the guide's "count down, don't state" rule — the embed it replaces said
    "up for 24 hours", true only at the moment it was posted. ``art`` frames
    it in the guild's thin card strips (``core.layout.card_strips``).
    """
    body = (
        f"## 📌 Pinned by a Member\n{_quote(message)}\n\n"
        f"Paid to pin this: **{named_or_anonymous(sponsor_id, name_fn)}**"
    )
    pointer = shop_pointer_line("Pin a message of your own for a day")
    subtext = f"-# Expires <t:{expires_at}:R> • {pointer}"
    top, bottom = card_strips(art)
    return [*top, _text(body), _text(subtext), *bottom]
