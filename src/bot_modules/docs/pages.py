"""A dashboard doc read into the pages of an ephemeral Components V2 card.

A docs pane (``docs/specs/docs_cog_spec.md`` § Docs panes) opens a doc privately, and a doc
may run far past one message: prod's etiquette guide is 4,832 characters
against a card's 4,000. Where ``help_card`` stops at the budget with a cut
note, this never drops text — it packs the doc into as few pages as fit, and
the card turns them with ◀ / ▶ (Ben, 2026-09-28: *auto-fill to budget*).

The doc is read the way ``help_card`` reads it, reusing its pieces:

- The leading heading is lifted into a ``##`` title, repeated on every page
  so a reader always knows which doc this is.
- A ``---`` line is a section break. Sections that share a page are divided
  by a drawn ``Separator``; the break decides nothing about where a page
  ends — the budget does.
- An image rides with its section as a ``MediaGallery``, on the section's
  first piece when the section spans pages (the channel render's rule).
- Text is packed by ``render.atomic_blocks``. A block too long for any page
  is split at a line break; a fenced block split that way is re-fenced on
  each piece, so no page renders half a fence as code.

Pure: no database, no interaction. The pane's view turns the pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import discord

from bot_modules.core.layout import MAX_COMPONENTS
from bot_modules.docs.help_card import HELP_TEXT_BUDGET, HelpSection, lift_heading
from bot_modules.docs.render import (
    EMPTY_DOC_TEXT,
    FENCE_RE,
    IMAGE_RE,
    atomic_blocks,
    hard_split,
    split_sections,
)

#: Components a page's controls take, so the body gets the rest of the 40:
#: the title's own Text Display when the first section is image-only (1),
#: the ``Page N of M`` line (1), the arrow row and its two buttons (3), and
#: the category select and its row (2).
PAGE_CONTROL_ITEMS = 7

#: What a page's sections may spend: each costs its Text Display, its
#: gallery, and the separator above it when it is not the page's first.
PAGE_BODY_ITEMS = MAX_COMPONENTS - PAGE_CONTROL_ITEMS



@dataclass(frozen=True)
class DocPages:
    """A doc's title and its pages, each an ordered run of sections."""

    title: str
    pages: list[list[HelpSection]]

    def __len__(self) -> int:
        return len(self.pages)


def title_line(title: str) -> str:
    return f"## {title}"


def page_caption(index: int, count: int) -> str:
    """The ``Page N of M`` subtext for 0-based ``index``."""
    return f"-# Page {index + 1} of {count}"


#: Below this much room per piece, an oversized fence is split as plain text
#: rather than re-fenced on every piece.
MIN_FENCE_ROOM = 200


def _fit_block(block: str, limit: int) -> list[str]:
    """``block`` in pieces of at most ``limit`` characters.

    A fenced block keeps its fence on every piece: the opening line (with its
    language tag) above, the closing marker below.
    """
    if len(block) <= limit:
        return [block]
    fence = FENCE_RE.match(block)
    if fence is None:
        return hard_split(block, limit)
    lines = block.split("\n")
    opener = lines[0]
    marker = fence.group(1)
    closed = len(lines) > 1 and lines[-1].strip().startswith(marker[0] * 3)
    closer = lines[-1] if closed else marker
    inner = "\n".join(lines[1:-1] if closed else lines[1:])
    room = limit - len(opener) - len(closer) - 2
    if room < MIN_FENCE_ROOM:
        # An opening line nearly as long as the page leaves no room to
        # re-fence each piece (at 0 or below hard_split never ended — ship
        # review, 2026-10-01); split the block as plain text instead.
        return hard_split(block, limit)
    return [f"{opener}\n{piece}\n{closer}" for piece in hard_split(inner, room)]


class _Packer:
    """Fills pages in order, opening a new one when text or items run out."""

    def __init__(self, text_budget: int) -> None:
        self.text_budget = text_budget
        self.pages: list[list[HelpSection]] = []
        self.page: list[HelpSection] = []
        self.chars = 0
        self.items = 0

    def _open_cost(self, *, text: bool, image: bool) -> int:
        return (1 if self.page else 0) + int(text) + int(image)

    def new_page(self) -> None:
        if self.page:
            self.pages.append(self.page)
        self.page, self.chars, self.items = [], 0, 0

    def add_section(self, blocks: list[str], image_url: Optional[str]) -> None:
        kept: list[str] = []
        image = image_url
        for block in blocks:
            if kept:
                if self.chars + 2 + len(block) <= self.text_budget:
                    kept.append(block)
                    self.chars += 2 + len(block)
                    continue
                # The section runs on: what fits stays, the rest carries on
                # overleaf as the next page's first piece.
                self.page.append(HelpSection("\n\n".join(kept), image))
                image = None
                kept = []
                self.new_page()
            cost = self._open_cost(text=True, image=image is not None)
            if (
                self.chars + len(block) > self.text_budget
                or self.items + cost > PAGE_BODY_ITEMS
            ):
                self.new_page()
                cost = self._open_cost(text=True, image=image is not None)
            kept.append(block)
            self.chars += len(block)
            self.items += cost
        if kept:
            self.page.append(HelpSection("\n\n".join(kept), image))
        elif image:
            cost = self._open_cost(text=False, image=True)
            if self.items + cost > PAGE_BODY_ITEMS:
                self.new_page()
                cost = self._open_cost(text=False, image=True)
            self.page.append(HelpSection("", image))
            self.items += cost


def plan_doc_pages(body_md: Optional[str], *, fallback_title: str) -> DocPages:
    """Read a doc into pages that each fit one card. Pure.

    Nothing is dropped: every block of the body lands on some page, in order.
    A blank or heading-only doc is one page saying it is empty.
    """
    body = (body_md or "").replace("\r\n", "\n").replace("\r", "\n")
    heading, body = lift_heading(body)
    title = heading or fallback_title
    # The title leads every page, so it is paid for on every page.
    budget = HELP_TEXT_BUDGET - len(title_line(title)) - 1

    packer = _Packer(budget)
    for raw in split_sections(body):
        found = IMAGE_RE.findall(raw)
        image_url = found[0] if found else None
        blocks = [
            piece
            for block in atomic_blocks(IMAGE_RE.sub("", raw).strip("\n"))
            for piece in _fit_block(block, budget)
        ]
        if blocks or image_url:
            packer.add_section(blocks, image_url)
    packer.new_page()

    if not packer.pages:
        return DocPages(title, [[HelpSection(EMPTY_DOC_TEXT)]])
    return DocPages(title, packer.pages)


def build_doc_page_items(doc: DocPages, index: int) -> list[discord.ui.Item[Any]]:
    """Page ``index`` of ``doc`` as card items: the title, then its sections.

    The caller adds the controls (``page_caption``, the arrows, the select)
    that ``PAGE_CONTROL_ITEMS`` reserves room for. Send with
    ``AllowedMentions.none()``: an admin-written doc may hold a role mention.
    """
    head = title_line(doc.title)
    items: list[discord.ui.Item[Any]] = []
    for i, section in enumerate(doc.pages[index]):
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
    return items
