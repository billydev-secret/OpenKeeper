"""Components V2 card builders for the ``/bank`` cog's own embeds.

The V2 replacement for the ``discord.Embed`` builders that used to live
inline in ``economy/economy_cog.py`` (deleted with this conversion —
``docs/plans/components_v2_migration.md`` wave 3a): the grant receipt, the
mute toggle receipt and the pay confirm card (the QOTD-post fallback card went
with `/qotd` in 2026-09).
Every function here is **pure**: primitives in, a list of ``discord.ui.Item``
out. No IO, no accent lookup. The wallet/register/leaderboard/transfers/guide
cards keep their own builders in their own modules (``wallet.py``,
``register.py``, ``leaderboard.py``, ``transfers.py``, ``guide.py``) — this
file only holds the cards that had no home of their own.

Each builder's docstring names the paradigm it builds
(``docs/components_v2_style_guide.md`` § The paradigms).

Conventions this module follows, matching the pilot (``dm/perms/layouts.py``)
and the rest of this feature's conversion (``quest_layouts.py``):

* **A card is bare** — no ``Container``, no accent colour.
* **A card's own heading is one glyph-led ``## Title`` line.**
* **A member is named through a pre-escaped, resolved string**, never
  ``<@id>`` — the cog escapes ``display_name`` before handing it in, the same
  way it always did for these four embeds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.branding import FIELD_VALUE_LIMIT, escape_to_fit
from bot_modules.core.layout import card_strips
from bot_modules.economy.view_helpers import unit as _unit
from bot_modules.economy.service import EconSettings

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt

#: Transient component ids for the pay-confirm card's two buttons — looked up
#: with ``find_item`` by ``_PayConfirmView``, which binds the callbacks.
#: Nothing outside that one ephemeral message ever dispatches to them.
PAY_CONFIRM_ID = 1
PAY_CANCEL_ID = 2


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _with_icon(
    settings: EconSettings, text: discord.ui.TextDisplay[Any]
) -> list[discord.ui.Item[Any]]:
    """Wrap ``text`` in a Thumbnail Section when the guild set a currency icon.

    Matches ``quest_layouts.build_quest_claimed_layout`` — the icon is the
    one thing on a currency card worth a picture, and only when a guild
    actually configured one.
    """
    if settings.currency_icon_url:
        return [
            discord.ui.Section(
                text, accessory=discord.ui.Thumbnail(settings.currency_icon_url)
            )
        ]
    return [text]


def build_grant_receipt_layout(
    settings: EconSettings,
    *,
    credited: int,
    amount: int,
    booster: bool,
    member_name: str,
    reason: str,
    actor_name: str,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """``/bank grant``'s public receipt. **Paradigm 7 — receipt.**

    ``member_name`` arrives already resolved and markdown-escaped (the cog
    escapes ``member.display_name`` before calling this, as it always did) —
    this card is posted publicly, so an unescaped name could inject markup
    into the channel. ``reason`` is free text a staff member typed, with no
    ``Range`` on the command param — it is escaped and then capped to
    ``FIELD_VALUE_LIMIT`` (escaping first, since it can double the length and
    a naive raw-length cap upstream would still overflow after escaping),
    matching ``register.py``'s rendering of the same free-text field.
    ``actor_name`` is a staff member's own display name and is escaped here
    too, for the same reason. ``art`` frames it in the guild's thin card
    strips (``core.layout.card_strips``).
    """
    emoji = settings.currency_emoji
    lines = [
        f"## {emoji} Currency Granted",
        f"{emoji} **{credited:,}** {_unit(settings, credited)} → **{member_name}**",
    ]
    if booster and credited != amount:
        lines.append(
            f"Booster bonus: base {amount:,} × {settings.booster_multiplier:g}"
        )
    if reason:
        lines.append(f"> {escape_to_fit(reason, FIELD_VALUE_LIMIT)}")
    lines.append(f"-# Granted by {discord.utils.escape_markdown(actor_name)}")
    top, bottom = card_strips(art)
    return [*top, *_with_icon(settings, _text("\n".join(lines))), *bottom]


def build_mute_toggle_layout(*, muted: bool) -> list[discord.ui.Item[Any]]:
    """``/bank mute``'s ephemeral receipt. **Paradigm 7 — receipt.**"""
    if muted:
        body = (
            "## 🔔 Notifications Muted\n"
            "You won't get economy DMs anymore. Run this again to turn them "
            "back on."
        )
    else:
        body = (
            "## 🔔 Notifications On\n"
            "You'll get economy DMs again — milestones, streak saves, and more."
        )
    return [_text(body)]


def build_pay_confirm_layout(
    settings: EconSettings,
    *,
    amount: int,
    recipient_name: str,
    memo: str | None,
    public: bool,
) -> list[discord.ui.Item[Any]]:
    """The Confirm/Cancel gate for a transfer over the threshold.

    **Paradigm 5 — decision card.** ``_PayConfirmView`` edits this very
    message into :func:`bot_modules.economy.transfers.build_payment_receipt_layout`'s
    receipt on Confirm; Cancel deletes it silently (guide § Controls). Buttons
    carry transient ids (``PAY_CONFIRM_ID`` / ``PAY_CANCEL_ID``) rather than
    ``custom_id``s — nothing outside this one ephemeral message ever
    dispatches to them, same as the dm_perms confirm gate.

    ``recipient_name`` arrives pre-escaped, matching every other name in this
    module.
    """
    emoji = settings.currency_emoji
    lines = [
        f"## {emoji} Confirm Payment",
        f"Send {emoji} **{amount:,}** {_unit(settings, amount)} to "
        f"**{recipient_name}**?",
    ]
    if memo:
        lines.append(f"> {discord.utils.escape_markdown(memo)}")
    if public:
        lines.append("The receipt will be posted in this channel.")
    return [
        *_with_icon(settings, _text("\n".join(lines))),
        discord.ui.ActionRow(
            discord.ui.Button(
                label="Confirm",
                style=discord.ButtonStyle.success,
                id=PAY_CONFIRM_ID,
            ),
            discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.secondary,
                id=PAY_CANCEL_ID,
            ),
        ),
    ]

