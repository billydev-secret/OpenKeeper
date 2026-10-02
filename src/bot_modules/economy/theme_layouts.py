"""Components V2 card builders for Flash Themes — the review/receipt card and
the live announcement.

The V2 replacement for the ``Embed(``-building code that used to live in
``theme_views.py``. Both builders are **pure**: primitives and a ``name_fn``
in, a list of ``discord.ui.Item`` out. No IO, no accent lookup, no callbacks
— ``theme_views.py`` wraps the returned items in a ``LayoutView`` and, for
the review card, attaches the persistent Approve/Decline buttons by handing
them in as ``buttons`` (a layout builder returns components, not behaviour,
and those two *are* the behaviour).

The review card reuses ``approval_layouts.build_review_card_layout``
(**paradigm 5** — decision card, redrawn as its own receipt once resolved)
rather than inventing a second approval shape, per
``docs/plans/components_v2_migration.md`` § 5 and the module's own docstring.

The live announcement (**paradigm 7 — receipt/notice**) is also the message
that gets pinned, and stays plain the way every card here does — except that
it is the one **exception to the hub banner's "channel-resident hub panels
only" rule** (Ben, 2026-09-28, "option B"): it is a public post that lives in
the theme channel for its whole window, not an ephemeral card, so it opens on
the guild's tall hub banner with "Today's Theme" drawn in and closes on the
hub footer — the same art a hub panel wears, on a card that isn't one. See
``docs/components_v2_style_guide.md`` § Media and
``tests/contracts/test_layout_hub_banner.py``'s ``HUB_BANNER_BUILDERS`` for the
documented exception. Review cards, DMs and ephemeral replies never carry it;
a review card in the staff approvals channel wears only the thin card strips
(``core.layout.card_strips``, through ``build_review_card_layout``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import discord

from bot_modules.core.layout import hub_banner, hub_footer
from bot_modules.economy.approval_layouts import build_review_card_layout
from bot_modules.economy.shop import shop_pointer_line
from bot_modules.economy.view_helpers import coins as _reward_text
from bot_modules.economy.service import EconSettings
from bot_modules.shared.name_resolver import NameFn, mention, named_or_anonymous

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt

#: The label drawn into the tall hub banner for the live announcement — a
#: fixed caption, not a per-guild title, so it is always the same image text
#: regardless of that day's theme (``panel_title_service.titled_banner_url``
#: draws it; the member-supplied theme title stays text-only, in the card's
#: own ``## 🎨`` heading below, per Ben's decision — never drawn into art).
ANNOUNCEMENT_BANNER_TITLE = "Today's Theme"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _quote(text: str) -> str:
    """A member-written line as an escaped ``>`` block (guide § Words)."""
    escaped = discord.utils.escape_markdown(text)
    return "\n".join(f"> {line}" for line in escaped.splitlines())


def _safe_title(title: str) -> str:
    """A member-written title, escaped for the ``##`` heading and inline bold
    it lands in — a ``TextDisplay`` renders markdown where an embed title
    never did, so this gets the same treatment as ``_quote`` (guide § Words).
    """
    return discord.utils.escape_markdown(title[:200])


def build_theme_review_layout(
    settings: EconSettings,
    *,
    sponsor_id: int,
    name_fn: NameFn = mention,
    title: str,
    blurb: str,
    price: int,
    state: str,
    resolver_id: Optional[int] = None,
    deny_reason: Optional[str] = None,
    refunded: bool = False,
    expires_at: Optional[float] = None,
    buttons: Optional["discord.ui.ActionRow[Any]"] = None,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """The approval card for a submission, in whichever of its four endings
    ``state``/``refunded`` names. **Paradigm 5 — decision card**, via
    ``approval_layouts.build_review_card_layout``.

    ``expired`` is three different endings — a request nobody reviewed in
    time (refunded), a theme that ran its whole window, and one a mod ended
    early (neither refunded) — so the state alone cannot say whether money
    went back. ``refunded`` carries that, read from the row's
    ``refunded_at``, which is the only thing that actually knows. Rendering
    the refund line off the state alone would tell a mod a themed day was
    refunded when it ran — the worst thing a card about money can be wrong
    about, so it stays a direct read of ``refunded`` rather than a second
    inference from ``state``.

    ``buttons`` is the Approve/Decline row, passed in rather than built here:
    it carries the persistent ``DynamicItem`` buttons, which are the actual
    behaviour a pure builder cannot own.

    ``expires_at`` is the row's own clock (``econ_theme_submissions.expires_at``,
    set once by ``go_live``), not the *configured* window — this card stays
    visible, re-rendered, for the whole time a theme is live, so a duration
    stated once would go stale the moment it's read again (guide § Words,
    "Count down, don't state"). Only meaningful when ``state == "live"``.
    """
    ended_unrefunded = state == "expired" and not refunded
    if state in ("approved", "live"):
        card_state = "approved"
    elif state in ("denied", "expired"):
        card_state = "denied"
    else:
        card_state = "pending"

    who = named_or_anonymous(sponsor_id, name_fn)
    body = f"**{who}** wants to run a themed day: **{_safe_title(title)}**, for {_reward_text(settings, price)}."
    if state == "live":
        if expires_at is not None:
            body += f"\nRunning — announced and pinned, ends <t:{int(expires_at)}:R>."
        else:
            body += "\nRunning — announced and pinned."
    elif state == "approved":
        body += "\nQueued — it runs the next time the channel is free."
    elif ended_unrefunded:
        body += "\nIt had its day — no refund."

    quote = _quote(blurb) if blurb else ""

    deny_reason_quote = ""
    facts_bits: list[str] = []
    if card_state == "denied" and not ended_unrefunded:
        # A "❌ Theme Declined" heading over a bare `>` quote reads as *why*
        # it was declined (guide § Words, "unlabelled they read as why") —
        # true even before the mod's own reason quote joins it underneath.
        # Same fix the pilot uses for a solo note under a decline heading
        # (dm/perms/layouts.py's `build_denial_layout_for_requester`).
        if quote:
            quote = f"**Their idea**\n{quote}"
        if deny_reason:
            deny_reason_quote = _quote(deny_reason)
        facts_bits.append(f"↩️ {_reward_text(settings, price)} returned")

    resolved_by = ""
    if resolver_id:
        if card_state == "approved":
            resolved_by = f"Approved by **{name_fn(resolver_id)}**"
        elif ended_unrefunded:
            resolved_by = f"Ended by **{name_fn(resolver_id)}**"
        elif card_state == "denied":
            resolved_by = f"Declined by **{name_fn(resolver_id)}**"

    denied_heading = "🎨 Theme Ended" if ended_unrefunded else "❌ Theme Declined"

    return build_review_card_layout(
        state=card_state,
        pending_heading="📋 Theme Requested",
        approved_heading="🎨 Theme Approved",
        denied_heading=denied_heading,
        body=body,
        quote=quote,
        resolved_by=resolved_by,
        deny_reason_quote=deny_reason_quote,
        facts=" • ".join(facts_bits),
        buttons=buttons,
        art=art,
    )


def build_theme_live_layout(
    settings: EconSettings,
    *,
    sponsor_id: int,
    name_fn: NameFn = mention,
    title: str,
    blurb: str,
    expires_at: float,
    banner_url: Optional[str] = None,
    footer_url: Optional[str] = None,
) -> list[discord.ui.Item[Any]]:
    """The announcement — which is also the message that gets pinned.
    **Paradigm 7 — receipt / notice.** No controls; it is taken down whole
    when the window ends (``unpin_and_delete``), never edited in place.

    One message, not two: the announcement and the pin are the same thing,
    so the channel gets a single card that stays at the top for the window
    rather than an announcement scrolling away from a separate pinned copy.

    It carries the shop pointer (``economy/shop.py``'s ``shop_pointer_line``):
    this card is the only place most members ever see that a themed day is
    purchasable, and it reaches them at the moment they are looking at one.

    ``banner_url``/``footer_url`` are the guild's own hub art
    (``branding_service.resolve_panel_art_conn``), drawn and resolved by the
    caller (``theme_views.announce_theme``) — this stays a pure builder, so it
    takes finished URLs rather than reading art itself. ``banner_url`` is
    expected to already carry :data:`ANNOUNCEMENT_BANNER_TITLE` drawn in when
    the caller could draw it (``panel_title_service.titled_banner_url``); the
    alt text names it either way, since it describes the art's own caption,
    not whether the drawing happened to succeed. ``None`` for either — the
    default, and the only state possible before a guild uploads hub art —
    adds nothing (``core.layout.hub_banner``/``hub_footer``), so the card
    renders exactly as it did before this art existed. This is the hub
    banner's one documented exception to "channel-resident hub panels only"
    — see the module docstring.

    ``expires_at`` is computed by the caller (``theme_views.announce_theme``,
    before posting) and handed to ``go_live`` afterwards as its own ``now``,
    so the countdown printed here and the row's stored clock agree exactly
    — the pinned card is read anywhere in its whole window, not just the
    instant it goes up, so a stated duration would go stale (guide § Words,
    "Count down, don't state").
    """
    who = named_or_anonymous(sponsor_id, name_fn)
    heading = f"## 🎨 {_safe_title(title)}"
    body = f"{heading}\n{_quote(blurb)}" if blurb else heading
    items: list[discord.ui.Item[Any]] = [
        *hub_banner(banner_url, title=ANNOUNCEMENT_BANNER_TITLE),
        _text(f"{body}\nThemed by **{who}**."),
        _text(f"-# Ends <t:{int(expires_at)}:R>."),
        _text(f"-# {shop_pointer_line('Buy a themed day of your own')}"),
        *hub_footer(footer_url),
    ]
    return items
