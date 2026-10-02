"""Shared decision-card → receipt shape for a paid or reviewed request.

**Paradigm 5** (``docs/components_v2_style_guide.md`` § The paradigms): a
request renders as a decision card — what's being asked for, who's asking,
an Approve/Deny pair under it — and the *same builder*, redrawn in the
resolved state, becomes the receipt: no buttons, one line naming who decided
it and how. Quest sign-off (``economy/quest_layouts.py``,
``build_signoff_card_layout``) is the first caller.

Pins (``economy/pin_layouts.py``) and Flash Themes
(``economy/theme_layouts.py``, converted 2026-09-28) both build their review
cards through this module now — the last of them (theme) was, until its own conversion, still building
its own review ``Embed``; converting it onto :func:`build_review_card_layout`
is what let ``approval_views.py``'s ``_renderers()`` registry drop the
isinstance branching that used to tell an embed-based card apart from a
``LayoutView`` one.

Pure, like every ``*_layouts.py`` module: primitives in, a list of
``discord.ui.Item`` out. No IO, no accent colour, no callbacks — the cog
wires behaviour onto the returned :class:`discord.ui.ActionRow` by its
buttons' ``custom_id``/``id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import discord

from bot_modules.core.layout import card_strips

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_review_card_layout(
    *,
    state: str,
    pending_heading: str,
    approved_heading: str,
    denied_heading: str,
    body: str,
    quote: str = "",
    resolved_by: str = "",
    deny_reason_quote: str = "",
    facts: str = "",
    buttons: Optional["discord.ui.ActionRow[Any]"] = None,
    art: PanelArt | None = None,
) -> list[discord.ui.Item[Any]]:
    """One card, in whichever of its three states ``state`` names.

    * ``"pending"`` — the decision card: ``pending_heading``, ``body``, an
      optional ``quote`` (the criteria/blurb/message being reviewed) and
      ``buttons`` underneath.
    * ``"approved"`` — the same shape, headed ``approved_heading``, with
      ``buttons`` omitted and ``resolved_by`` as a trailing ``-#`` line.
    * ``"denied"`` — headed ``denied_heading``; ``deny_reason_quote`` (the
      reviewer's stated reason, already quoted by the caller) follows the
      body and ``resolved_by`` again trails as ``-#``.

    Every string arrives pre-formatted — member names already run through
    the caller's ``name_fn``, member-written text already quoted by the
    caller (member-typed text is a ``>`` block, guide § Words) — so this
    function stays a plain assembler with no naming or escaping logic of its
    own to keep in step with a second caller's.

    ``facts`` is one more ``-#`` line (e.g. a prior-denial count); it joins
    ``resolved_by`` with " • " when both are present, per the guide's
    subtext-joining convention.

    ``art`` frames the card in the guild's thin card strips
    (``core.layout.card_strips``; Ben, 2026-09-30) — passed only for the
    copy posted in the staff approvals channel. The todo board's ephemeral
    detail and the quest sign-off picker pass none: a private card stays
    plain (``view_helpers.surface_art`` makes that call per surface).
    """
    if state == "approved":
        heading = approved_heading
    elif state == "denied":
        heading = denied_heading
    else:
        heading = pending_heading

    items: list[discord.ui.Item[Any]] = [_text(f"## {heading}\n{body}")]
    if quote:
        items.append(_text(quote))
    if state == "denied" and deny_reason_quote:
        items.append(_text(deny_reason_quote))
    if state == "pending" and buttons is not None:
        items.append(buttons)

    sub_bits = [b for b in (resolved_by, facts) if b]
    if sub_bits:
        items.append(_text("-# " + " • ".join(sub_bits)))
    top, bottom = card_strips(art)
    return [*top, *items, *bottom]
