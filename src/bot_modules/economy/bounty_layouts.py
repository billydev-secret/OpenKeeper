"""Components V2 card builders for Community Bounty — the board card and hub.

The V2 replacement for this package's ``render_bounty_card`` /
``build_bounty_hub_embed`` (``bot_modules/economy/bounty_views.py``, deleted
with this conversion — ``docs/plans/components_v2_migration.md`` wave 3a).
Every function here is **pure**: primitives and a ``name_fn`` in, a list of
``discord.ui.Item`` out. No IO, no accent lookup, no callbacks — the buttons
below carry only a stable ``custom_id`` and dispatch through the
``discord.ui.DynamicItem`` classes already registered in ``bounty_views.py``
(``bot.add_dynamic_items`` at cog load), which match a click by regex against
the id alone — independent of which view rendered the button. Emitting a
plain ``Button(custom_id=...)`` here is therefore enough; nothing in this
module needs to know about a ``DynamicItem`` at all.

Each builder's docstring names the paradigm it builds
(``docs/components_v2_style_guide.md`` § The paradigms).

Conventions this module follows (see ``dm/perms/layouts.py`` for the fuller
statement of each, first written there):

* **A card is bare** — no ``Container``, no accent colour.
* **A card's own heading is one glyph-led ``## Title`` line** in its first
  Text Display. The board reuses the feature's own established vocabulary
  rather than the repo-wide table: 🎯 open, 🏆 awarded, ✖️ cancelled (both
  already said by the ephemeral replies ``_handle_chip``/``_handle_cancel``
  send today), ⌛ expired (new — distinguishes it from a cancel, which the
  embed version only did in the title's trailing word).
* **A member is named through ``name_fn``**, never ``<@id>``.
* **Quoted text is a ``>`` block** — a bounty's description is the poster's
  own task text, so it is quoted and escaped exactly like a DM Perms reason
  (``dm/perms/layouts.py``'s ``_quote``): without that, a poster could write
  a description containing e.g. a fake ``## Bounty Awarded`` heading that
  visually forges the rest of the card, which the pre-conversion embed's
  boxed "Task" field made impossible by its own chrome.

**The hub wears the small lobby strip, not the tall banner** (Ben,
2026-09-28): a channel hub that had drawn no art of its own — as this one had
not — gets the same small header/footer strip a game lobby wears
(``core.layout.lobby_art``), title drawn into the header in place of the
``## `` heading, rather than a second tall banner. See
``docs/components_v2_style_guide.md`` § Media, and this module's entry in
``LOBBY_ART_BUILDERS`` (``tests/contracts/test_layout_hub_banner.py``) rather than
``HUB_BANNER_BUILDERS``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.layout import card_strips, lobby_art
from bot_modules.core.utils import jump_url
from bot_modules.economy.bounty_service import BountyBoardEntry
from bot_modules.shared.name_resolver import NameFn

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt
    from bot_modules.economy.service import EconSettings

# ── Persistent custom ids ────────────────────────────────────────────
#
# These survive a restart: the board card is an ordinary message that can
# sit in the channel for weeks, and the hub is a sticky panel re-registered
# at cog load. bounty_views.py's DynamicItem classes match these formats by
# regex, so a builder call and the class that handles its click are pinned
# to the same literal string here rather than duplicated in both files.

#: Card buttons — one persistent id per bounty, embedding its id.
CHIP_CUSTOM_ID_TEMPLATE = "econ_bounty:chip:{bounty_id}"
AWARD_CUSTOM_ID_TEMPLATE = "econ_bounty:award:{bounty_id}"
CANCEL_CUSTOM_ID_TEMPLATE = "econ_bounty:cancel:{bounty_id}"
#: The hub's per-row Chip In — a *different* id format from the card's own
#: (above), because the click's ``interaction.message`` is the hub panel, not
#: the bounty's card: the card that must be repainted can only be found by
#: its stored ids (``refresh_by_ids=True`` in ``_handle_chip``), never taken
#: from the interaction. Replaces the old bounty-picker select
#: (``_HubChipSelect``) now that every open row carries its own button.
HUB_CHIP_CUSTOM_ID_TEMPLATE = "econ_bounty:hub_chip:{bounty_id}"
#: The hub's one card-level action. Carries no per-message state.
HUB_POST_CUSTOM_ID = "econ_bounty:hub_post"

#: Titles are clipped for the *hub list* only; the bounty's own card shows it
#: in full. Trimmed well below the old embed's 60 — a Section row shares its
#: line with a button, and the guide's phone rule (~45 visible characters)
#: leaves much less room than a 1024-char embed field did.
HUB_TITLE_CLIP = 24

#: The hub's own name, drawn into the lobby strip's header in place of the
#: ``## `` heading when a guild has lobby header art set (Ben, 2026-09-28).
HUB_TITLE = "Bounty Board"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quote(text: str) -> str:
    """``text`` as a markdown quote block, every line prefixed and escaped.

    Mirrors ``dm/perms/layouts.py``'s ``_quote``: a bounty description is
    member-typed free text (``economy_bounty_service.MAX_DESC_LEN``-capped,
    otherwise unvalidated), and without escaping a poster could craft
    markdown that reads as if the bot itself authored the surrounding lines.
    """
    escaped = discord.utils.escape_markdown(text)
    return "\n".join(f"> {line}" for line in escaped.splitlines())


def _jump_url(guild_id: int, entry: BountyBoardEntry) -> str | None:
    """The row's jump link, or None when the card never posted (ids are 0)."""
    if not entry.card_channel_id or not entry.card_message_id:
        return None
    return jump_url(guild_id, entry.card_channel_id, entry.card_message_id)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


# ── 5 / 7. The board card ────────────────────────────────────────────


def build_bounty_card_layout(
    settings: EconSettings,
    bounty,
    *,
    pot: int,
    contributors: int,
    name_fn: NameFn,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """The board card for one bounty. **Paradigm 5** while open — the facts,
    then Chip In / Award / Cancel in the bottom row; **paradigm 7** once
    awarded, cancelled or expired — the same facts, no controls, edited in
    place with the buttons removed exactly like a DM Perms decision card
    answered.

    Poster and winner are named through ``name_fn``, never ``<@id>``: the
    board is public, and a Text Display mention renders from the *reading*
    client's cache. For a no-contact pair ``bounty_views._card_name_fn``
    hands over a resolver that prints bare ids **on purpose** (bug #221) —
    see its docstring before "fixing" a card full of numbers.

    ``art`` frames it in the guild's thin card strips (``core.layout.
    card_strips``; Ben, 2026-09-30), the controls inside the frame.
    """
    state = str(bounty["state"])
    title = discord.utils.escape_markdown(str(bounty["title"]))
    unit = settings.currency_plural or "coins"
    bounty_id = int(bounty["id"])

    if state == "awarded":
        head = f"## 🏆 Bounty Awarded — {title}"
    elif state == "cancelled":
        head = f"## ✖️ Bounty Cancelled — {title}"
    elif state == "expired":
        head = f"## ⌛ Bounty Expired — {title}"
    else:
        head = f"## 🎯 Bounty — {title}"

    lines = [head]
    if bounty["description"]:
        lines.append(_quote(str(bounty["description"])))
    lines.append(f"Posted by **{name_fn(int(bounty['poster_id']))}**")

    items: list[discord.ui.Item[Any]] = []

    if state == "awarded":
        lines.append(
            f"Won by **{name_fn(int(bounty['winner_id']))}** — "
            f"{int(bounty['payout']):,} {unit} paid out"
        )
        if int(bounty["rake_amount"]) > 0:
            lines.append(f"House kept {int(bounty['rake_amount']):,} {unit}.")
        items.append(_text("\n".join(lines)))
        items.append(_text(f"-# Awarded <t:{int(bounty['resolved_at'])}:f>"))
    elif state in ("cancelled", "expired"):
        lines.append("Everyone who chipped in got their coins back.")
        items.append(_text("\n".join(lines)))
        verb = "Cancelled" if state == "cancelled" else "Expired"
        items.append(_text(f"-# {verb} <t:{int(bounty['resolved_at'])}:f>"))
    else:
        rake = max(0, min(100, int(settings.bounty_rake_pct)))
        lines.append(
            f"Pot: **{pot:,}** {unit} · {_plural(contributors, 'contributor')}"
        )
        note = (
            "Chip in to grow the pot. Whoever posted it — or a mod — awards "
            "it to whoever gets it done"
        )
        if rake > 0:
            note += f" (the house keeps {rake}% on award)"
        lines.append(note + ".")
        items.append(_text("\n".join(lines)))
        items.append(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Chip In", emoji="💰",
                    style=discord.ButtonStyle.success,
                    custom_id=CHIP_CUSTOM_ID_TEMPLATE.format(bounty_id=bounty_id),
                ),
                discord.ui.Button(
                    label="Award", emoji="🏆",
                    style=discord.ButtonStyle.primary,
                    custom_id=AWARD_CUSTOM_ID_TEMPLATE.format(bounty_id=bounty_id),
                ),
                discord.ui.Button(
                    label="Cancel",
                    style=discord.ButtonStyle.danger,
                    custom_id=CANCEL_CUSTOM_ID_TEMPLATE.format(bounty_id=bounty_id),
                ),
            )
        )
    top, bottom = card_strips(art)
    return [*top, *items, *bottom]


# ── 6 (+1). The hub panel ────────────────────────────────────────────


def _bounty_row(
    guild_id: int, entry: BountyBoardEntry, unit: str
) -> discord.ui.Section[Any]:
    """One open bounty, its own Chip In button beside it. **Paradigm 6.**

    Two lines (Ben, 2026-09-28: "can that be a second line?"): the title,
    then a small line with the pot and the jump link — the pot no longer
    trails the title after a dash. The backer count the old embed's list
    carried is still left off (the guide's "Three facts rarely fit",
    § Screen use on a phone); it is on the bounty's own card, one tap away
    through the jump link.
    """
    title = discord.utils.escape_markdown(_clip(entry.title, HUB_TITLE_CLIP))
    detail = f"{entry.pot:,} {unit} in the pot"
    url = _jump_url(guild_id, entry)
    if url:
        detail += f" · [jump]({url})"
    text = f"**{title}**\n-# {detail}"
    return discord.ui.Section(
        _text(text),
        accessory=discord.ui.Button(
            label="Chip In", emoji="💰",
            style=discord.ButtonStyle.success,
            custom_id=HUB_CHIP_CUSTOM_ID_TEMPLATE.format(bounty_id=entry.bounty_id),
        ),
    )


def build_bounty_hub_layout(
    settings: EconSettings,
    guild_id: int,
    entries: list[BountyBoardEntry],
    *,
    open_total: int,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The board's sticky hub: what a bounty is, every open one with its own
    Chip In button, and the card-level Post a Bounty action in the bottom
    row. **Paradigm 6 — list with per-row actions**, topped by the single
    hub-level action the guide's Anatomy places in a bottom ``ActionRow``.

    Replaces the old hub's separate "Chip in" button, which opened a
    bounty-picker select (``_HubChipSelect``): once every row carries its own
    button, that picker duplicates what the list already shows — the same
    move the DM Perms pilot made dropping its user-select lookup once
    per-connection Remove buttons existed.

    Deliberately does **not** repeat the per-card "How it works" line — the
    blurb here is the first thing a member reads in the channel, so it
    explains the mechanic end to end (including the refund promise, which
    the open-state card doesn't mention); the card's version is the reminder
    next to a live pot.

    The list is **not paged**: it stays a truncate-with-count tail
    ("…and N more"), unlike the DM Perms settings panel's arrow pages. That
    panel is a member's own ephemeral view; this is one shared, public
    sticky message every viewer reads at once, so a page control would move
    for everyone simultaneously on every press — a bigger behavioural change
    than converting the card shape. ``HUB_LIST_LIMIT`` (the read bound) times
    a Section's 3 components leaves the hub comfortably under the 40-component
    ceiling without paging.

    ``header_url``/``footer_url``/``title_in_header`` thread this hub's own
    art: a hub that had no banner of its own wears the small lobby strip a
    game lobby wears (``core.layout.lobby_art``) — see the module docstring's
    "The hub wears the small lobby strip" note. Nothing draws unless
    ``header_url`` is set (ships dark), and the drawn title replaces the
    ``## `` heading below exactly like a titled lobby's does
    (``games/clapback/layouts.py``'s ``build_lobby_layout``).
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=HUB_TITLE if titled else None)

    rake = max(0, min(100, int(settings.bounty_rake_pct)))
    days = max(0, int(settings.bounty_expire_days))
    unit = settings.currency_plural or "coins"

    blurb = [] if titled else ["## 🎯 The Bounty Board"]
    blurb += [
        f"**Post a task** and seed it with {unit} — anyone can chip in to "
        "grow the pot.",
        "**The poster or a mod awards the pot** to whoever gets it done "
        "(posters can't award it to themselves).",
    ]
    if rake > 0:
        blurb.append(f"The house keeps **{rake}%** on award.")
    if days > 0:
        blurb.append(
            f"Nobody awarded it within **{days} days**? Everyone who chipped "
            "in is refunded in full."
        )
    items: list[discord.ui.Item[Any]] = [*top, _text("\n".join(blurb))]
    items.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

    if not entries:
        items.append(
            _text("### Open Bounties\nNothing on the board yet — post the first one.")
        )
    else:
        items.append(_text("### Open Bounties"))
        items.extend(_bounty_row(guild_id, entry, unit) for entry in entries)
        hidden = open_total - len(entries)
        if hidden > 0:
            items.append(_text(f"-# …and **{hidden}** more further up the channel."))

    items.append(
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Post Bounty", emoji="🎯",
                style=discord.ButtonStyle.primary,
                custom_id=HUB_POST_CUSTOM_ID,
            )
        )
    )
    items.extend(bottom)
    return items
