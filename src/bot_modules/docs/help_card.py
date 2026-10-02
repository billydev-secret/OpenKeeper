"""A dashboard doc as an ephemeral Components V2 help card.

A hub panel's **Help** button answers with the feature's FAQ doc — read from
the ``docs`` table at click time, so the card can never disagree with the doc
admins edit and post from the dashboard's Docs panel
(``docs/components_v2_style_guide.md`` § Controls). Every hub that grows a
Help button builds it here rather than copying one feature's version.

The doc's markdown is read the way ``render.render_doc`` reads it for a
channel, reusing its pieces rather than re-deriving them:

- A ``---`` line is a break: a drawn ``Separator`` here, where a posted doc
  starts a new message.
- An image ``![alt](url)`` is pulled out of the text and shown as a
  ``MediaGallery`` under its section, never printed as literal markdown.
- Text is packed by ``render``'s paragraph blocks, so a fenced code block is
  kept whole or left out — a cut never lands inside one.

What differs is the target: one ephemeral message, not a run of them. So the
doc's leading heading becomes the card's glyph-led ``##`` title (a bigger
``#`` under it would read as a second card), and a body past the budget stops
at a block boundary with a ``-#`` line saying it was cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.docs.db import get_doc
from bot_modules.docs.render import (
    HEADING_RE,
    IMAGE_RE,
    FENCE_RE,
    atomic_blocks,
    hard_split,
    split_sections,
)

#: The heading line lifted into the title gets the dashboard title field's
#: cap (``docs.db.TITLE_MAX_LEN``); the line inside the body has none.
HELP_TITLE_MAX = 200

#: Room for the title and body text together. A message's Text Displays share
#: one 4,000-character budget, and a doc body may run to 40,000
#: (``docs.db.BODY_MAX_LEN``); the rest is headroom for the glyph and the
#: cut note.
HELP_TEXT_BUDGET = 3_800

#: Sections past this are cut like text past the budget: each costs up to
#: three components (text, separator, gallery) of the message's forty.
HELP_MAX_SECTIONS = 10

HELP_CUT_NOTE = "-# That's the start of it — ask a moderator for the rest."


@dataclass(frozen=True)
class HelpSection:
    """One ``---``-delimited part of the doc: its text and first image."""

    text: str
    image_url: Optional[str] = None


@dataclass(frozen=True)
class HelpCard:
    """What the card shows, before it becomes components."""

    title: str
    sections: list[HelpSection] = field(default_factory=list)
    truncated: bool = False


def load_doc_body(db_path: Path, guild_id: int, doc_key: str) -> Optional[str]:
    """The guild's doc markdown for ``doc_key``, or ``None`` if it has none."""
    with open_db(db_path) as conn:
        doc = get_doc(conn, guild_id, doc_key)
    return doc["body_md"] if doc else None


def lift_heading(body: str) -> tuple[Optional[str], str]:
    """``(heading, rest)`` if the first non-blank line is a heading."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        match = HEADING_RE.match(line)
        if match is None:
            return None, body
        return match.group(2).strip()[:HELP_TITLE_MAX], "\n".join(lines[i + 1 :])
    return None, body


def plan_help_card(
    body_md: Optional[str], *, fallback_title: str, missing_text: str
) -> HelpCard:
    """Read a doc into a card that fits one message. Pure.

    ``missing_text`` stands in when there is no doc, or nothing in it
    survives: a blank doc, or one that is only a heading.
    """
    body = (body_md or "").replace("\r\n", "\n").replace("\r", "\n")
    heading, body = lift_heading(body)
    title = heading or fallback_title

    budget = HELP_TEXT_BUDGET - len(title)
    sections: list[HelpSection] = []
    truncated = False
    for raw in split_sections(body):
        found = IMAGE_RE.findall(raw)
        image_url = found[0] if found else None
        blocks = atomic_blocks(IMAGE_RE.sub("", raw).strip("\n"))
        if not blocks and not image_url:
            continue
        if len(sections) == HELP_MAX_SECTIONS:
            truncated = True
            break
        kept: list[str] = []
        for block in blocks:
            cost = len(block) + (2 if kept else 0)
            if cost <= budget:
                kept.append(block)
                budget -= cost
                continue
            truncated = True
            # A first paragraph too long for the whole card is split at a
            # line break rather than dropped, so the card is never empty. A
            # code block is never split: half a fence renders the rest of
            # the card as code.
            if not sections and not kept and not FENCE_RE.match(block):
                kept.append(hard_split(block, budget)[0])
            break
        if kept or image_url:
            sections.append(HelpSection("\n\n".join(kept), image_url))
        if truncated:
            break

    if not any(s.text for s in sections) and not any(s.image_url for s in sections):
        return HelpCard(title, [HelpSection(missing_text)])
    return HelpCard(title, sections, truncated)


def build_doc_help_layout(
    body_md: Optional[str],
    *,
    glyph: str,
    fallback_title: str,
    missing_text: str,
) -> list[discord.ui.Item[Any]]:
    """A doc as an ephemeral help card. **Paradigm 7 — notice.**

    ``glyph`` leads the ``##`` title the way the calling feature's other
    cards are led. Send the result with ``AllowedMentions.none()``: an
    admin-written doc may hold a role mention, and the card answers one tap.
    """
    card = plan_help_card(
        body_md, fallback_title=fallback_title, missing_text=missing_text
    )
    head = f"## {glyph} {card.title}"
    items: list[discord.ui.Item[Any]] = []
    for i, section in enumerate(card.sections):
        if i:
            items.append(
                discord.ui.Separator(
                    visible=True, spacing=discord.SeparatorSpacing.large
                )
            )
        text = "\n".join(t for t in (head if i == 0 else "", section.text) if t)
        if text:
            items.append(discord.ui.TextDisplay(text))
        if section.image_url:
            items.append(
                discord.ui.MediaGallery(discord.MediaGalleryItem(section.image_url))
            )
    if card.truncated:
        items.append(discord.ui.TextDisplay(HELP_CUT_NOTE))
    return items
