"""Components V2 mechanics: the budget, the expiring base, binding, the sample ids.

``docs/components_v2_style_guide.md`` is the design standard and
``docs/plans/components_v2_migration.md`` the order of work; this module is
the one place the *mechanics* live, so a converted feature imports from here
rather than re-deriving them.

There is deliberately **no** ``send_layout`` / ``edit_layout`` wrapper. The
client is constructed with ``allowed_mentions=AllowedMentions.none()``
(``core.app_context.Bot``), so a wrapper would have nothing to add, and the
send surface a card can land on is too varied for one to be honest:
``channel.send``, ``interaction.response.send_message``, ``followup.send``,
``edit_message``, ``edit_original_response``, ``message.edit`` and
``user.send`` all take ``view=`` and differ in what else they need. An edit
that converts an existing embed message still has to pass
``content=None, embed=None, attachments=[]`` explicitly — for a sticky panel
:class:`core.sticky.PanelContent` does that; anywhere else it is the caller's
line to write.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

import discord

if TYPE_CHECKING:
    from bot_modules.core.branding_service import PanelArt

#: Discord's ceiling on components in one message, **nested ones counted**.
#: A ``Section`` costs three (itself, its text display, its accessory), an
#: ``ActionRow`` one plus its buttons, everything else one. The pilot's full
#: shop listing — seven perk rows — came to 37.
MAX_COMPONENTS = 40

#: Discord's ceiling on characters across every ``TextDisplay`` in one
#: message. Embeds were allowed 6000; a layout is not.
MAX_CHARS = 4000

#: Columns a Section's Button accessory may take before a phone drops it
#: under its row instead of rendering it beside it — Billy's phone
#: screenshot of the Perk Shop, 2026-09-24: "Browse Colors" (``text_columns``
#: 13) dropped under its row while "Set Color" (9) stayed beside it.
MAX_ACCESSORY_LABEL = 10

#: A variation selector picks a presentation (text vs. emoji) for the glyph
#: before it and draws no width of its own; a zero-width joiner glues two
#: glyphs into one rendered cluster and likewise draws nothing on its own —
#: both are dropped rather than counted.
_ZERO_WIDTH = frozenset({"︎", "️", "‍"})


def text_columns(text: str) -> int:
    """The visible width of ``text`` as a phone draws it, one codepoint at a
    time: an emoji (any character outside the Basic Multilingual Plane, or
    whose Unicode category is ``So`` — Symbol, other) counts 2 columns, a
    variation selector or zero-width joiner counts 0, everything else counts
    1. This is a working number, not a font metric — it does not fold a
    multi-codepoint emoji sequence (a ZWJ family, a flag) into the one glyph
    it renders as, so such a sequence is over-counted rather than measured
    exactly. Good enough is the point: it is what turned "Browse Colors"
    (13) into the label that dropped under its row on Billy's phone
    screenshot (2026-09-24) while "Set Color" (9) stayed put, and
    ``MAX_ACCESSORY_LABEL`` / ``shop_layouts.MAX_ROW_CHARS`` are held to the
    number it reports, not to ``len()``.
    """
    total = 0
    for ch in text:
        if ch in _ZERO_WIDTH:
            continue
        if ord(ch) > 0xFFFF or unicodedata.category(ch) == "So":
            total += 2
        else:
            total += 1
    return total


def layout_budget(view: discord.ui.LayoutView) -> tuple[int, int]:
    """``(components, characters)`` for ``view``, counted the way Discord does.

    ``LayoutView.walk_children()`` yields every top-level item *and* every
    nested one — a ``Section``'s text displays and its accessory, an
    ``ActionRow``'s buttons — which is exactly the set Discord counts, and
    exactly what discord.py accumulates into the private ``_total_children``
    it checks against 40. Counting the walk keeps this off that private name
    while agreeing with it item for item.

    The character half is :meth:`LayoutView.content_length`, i.e. the total
    length of every ``TextDisplay``'s content.
    """
    return sum(1 for _ in view.walk_children()), view.content_length()


def check_budget(view: discord.ui.LayoutView) -> None:
    """Raise ``ValueError`` naming the ceiling ``view`` overflows.

    A builder test calls this on the **fullest card the feature can
    produce** — the equivalent of the old ``len(embed) <= 6000`` checks —
    and a builder that pages may call it before a send to decide it must.

    The two ceilings are not guarded equally by the library. discord.py
    counts components as they are added, nested ones included, and refuses
    the 41st itself; what it raises is a bare "maximum number of children
    exceeded (40)" from inside whichever ``add_item`` happened to be last,
    naming neither the card nor how far over it went. Nothing at all checks
    the 4,000-character ceiling before Discord rejects the send. So this
    reports both, together, against the finished card.
    """
    components, chars = layout_budget(view)
    problems = []
    if components > MAX_COMPONENTS:
        problems.append(f"{components} components (max {MAX_COMPONENTS})")
    if chars > MAX_CHARS:
        problems.append(f"{chars} characters of text display (max {MAX_CHARS})")
    if problems:
        raise ValueError(
            f"{type(view).__name__} overflows its Components V2 budget: "
            + "; ".join(problems)
            + " — page the card (ten rows per page) or cut text."
        )


def card(items: Sequence[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one send.

    A receipt, a notice or a finished board carries no buttons, so nothing
    dispatches to it and nothing has to expire. A view instance belongs to
    one message, so every surface gets its own build.
    """
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def disable_all(view: discord.ui.LayoutView) -> None:
    """Disable every interactive item in ``view``, however deeply nested.

    Two differences from ``core.utils.disable_all_items``, which stays as it
    is for ordinary views:

    * it walks ``view.children`` only, which is the whole of a
      ``discord.ui.View``. A layout nests — a row's button is a ``Section``
      accessory, a bottom row's buttons live in an ``ActionRow`` — so the
      walk has to recurse.
    * it tests ``isinstance(item, (Button, Select))``, and
      ``discord.ui.UserSelect`` and friends do **not** subclass ``Select``
      (they share a private ``BaseSelect``), so a typed picker would be left
      live. Asking for the ``disabled`` attribute instead catches every
      interactive item and no layout item: ``TextDisplay``, ``Section``,
      ``Thumbnail``, ``Separator``, ``MediaGallery`` and ``ActionRow`` have
      no such attribute.
    """
    for item in view.walk_children():
        if hasattr(item, "disabled"):
            item.disabled = True  # type: ignore[attr-defined]


#: Alt text for the hub banner and footer — the images are the guild's own
#: art, so the bot can't describe them; this at least tells a screen reader
#: what each is.
HUB_BANNER_ALT = "Server banner"
HUB_FOOTER_ALT = "Server footer art"
LOBBY_HEADER_ALT = "Server lobby header art"
CARD_HEADER_ALT = "Server card header art"
CARD_FOOTER_ALT = "Server card footer art"


def _hub_image(url: str | None, alt: str) -> list[discord.ui.Item[Any]]:
    if not url:
        return []
    return [discord.ui.MediaGallery(discord.MediaGalleryItem(media=url, description=alt))]


def hub_banner(url: str | None, *, title: str | None = None) -> list[discord.ui.Item[Any]]:
    """The guild's banner image as the first item of a hub panel, or nothing.

    **Channel-resident hub panels only** (Billy, 2026-09-26: "let's not use
    it on the ephemeral messages, just the main panels that live in the
    channel"). A hub is the poster a channel keeps at its foot, so the art
    brands the place; an ephemeral card is one member's working surface and
    stays plain. That is the guide's one exception to § Media's "never
    decoration", and ``tests/contracts/test_layout_hub_banner.py`` pins which builders
    may call this (and :func:`hub_footer`).

    ``url`` is ``branding_service.resolve_panel_art_conn(...).banner`` —
    a public https URL, never an ``attachment://``: a sticky panel reposts on
    channel activity, and an attachment would be re-uploaded on every send
    and edit. ``None`` (no banner set — the default) returns ``[]``, so a
    builder splices the result in unconditionally and an unconfigured guild
    renders exactly the panel it did before.

    ``title`` is set when ``url`` is the banner with the hub's title drawn in
    (``panel_title_service``): it becomes the alt text, because the builder
    has dropped its text heading and a screen reader would otherwise never
    hear the panel's name.
    """
    return _hub_image(url, title or HUB_BANNER_ALT)


def hub_footer(url: str | None) -> list[discord.ui.Item[Any]]:
    """The guild's footer strip as the last item of a hub panel, or nothing.

    The banner's partner: a thin strip that closes the card the way the
    banner opens it (Billy, 2026-09-26: the art does the framing, not a
    coloured bar). Same rules as :func:`hub_banner` —
    channel-resident hubs only, a hosted URL
    (``resolve_panel_art_conn(...).footer``), ``[]`` when unset.
    """
    return _hub_image(url, HUB_FOOTER_ALT)


def lobby_art(
    header: str | None, footer: str | None, *, title: str | None = None
) -> tuple[list[discord.ui.Item[Any]], list[discord.ui.Item[Any]]]:
    """A game lobby's ``(top, bottom)`` art, or ``([], [])``.

    Billy, 2026-09-27: "give the game lobby the header and footer treatment
    from panels … a small header … same height as the footer with a simpler
    flower accent". A lobby is a public card in a games channel, not one
    member's working surface, so it takes the hubs' exception to § Media —
    but a lobby is not a hub: the tall banner would push the Join row off a
    phone screen, so it gets its own strip (``resolve_panel_art_conn(...)
    .header``) and closes with the hubs' footer.

    **The header switches the art on.** The footer alone draws nothing, so a
    guild that already has a hub footer sees its lobbies unchanged until an
    admin uploads a lobby header. ``tests/contracts/test_layout_hub_banner.py`` pins
    which lobby builders may call this.

    ``title`` is set when ``header`` has the game's name drawn between its
    leaves (``panel_title_service.with_lobby_title``): it becomes the alt
    text, as :func:`hub_banner`'s does, since the builder drops its heading.
    """
    if not header:
        return [], []
    return (
        _hub_image(header, title or LOBBY_HEADER_ALT),
        _hub_image(footer, HUB_FOOTER_ALT),
    )


def card_strips(
    art: PanelArt | None,
) -> tuple[list[discord.ui.Item[Any]], list[discord.ui.Item[Any]]]:
    """A public card's ``(top, bottom)`` thin strips, or ``([], [])``.

    Ben, 2026-09-30: "a third thinner header with no title for use in the
    embeds" — the posted cards that are neither hubs nor lobbies (the
    economy and casino posts, the staff-channel review and audit cards) take the hubs' exception to § Media with a strip half the
    lobby header's height and nothing drawn onto it, so the art needs no
    ``panel_title_service`` pass and the builder keeps its own heading.

    ``art`` is ``branding_service.resolve_panel_art_conn(...)`` (``card_header``
    and ``card_footer``), or ``None`` where the caller read none. **The header
    switches the art on**, as :func:`lobby_art`'s does: a card footer alone
    draws nothing. A builder that is also sent privately (a review card's
    picker, the /pay receipt) takes ``art=None`` on that path — an ephemeral
    card stays plain. ``tests/contracts/test_layout_hub_banner.py`` pins which builders
    may call this.
    """
    if art is None or not art.card_header:
        return [], []
    return (
        _hub_image(art.card_header, CARD_HEADER_ALT),
        _hub_image(art.card_footer, CARD_FOOTER_ALT),
    )


def bind(
    view: discord.ui.LayoutView,
    custom_id: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
) -> None:
    """Attach ``callback`` to the persistent item carrying ``custom_id``.

    Layout builders return components and no behaviour (guide § Code
    conventions), so the view wires itself up after adding them. Persistent
    items are found by ``custom_id`` rather than ``find_item`` — that one
    takes the numeric ``id``, which is a different field. Raises ``KeyError``
    when no item carries it, so a renamed id fails at construction, not on a
    member's press.
    """
    for item in view.walk_children():
        if getattr(item, "custom_id", None) == custom_id:
            item.callback = callback  # type: ignore[method-assign]
            return
    raise KeyError(f"no component with custom_id {custom_id!r} in {view!r}")


def bind_present(
    view: discord.ui.LayoutView,
    callbacks: Mapping[str, Callable[[discord.Interaction], Awaitable[None]]],
) -> set[str]:
    """Attach each callback to the item carrying its ``custom_id``, for the
    ids the card currently shows; returns the ids it bound.

    :func:`bind` raises on a missing id, which is right for a card whose
    controls never change. A live board's do: its builder drops the vote
    buttons while a round waits for a prompt, and every control on the
    closed frame. The view binds whatever the builder rendered, and a test
    on the builder pins which ids each state carries.
    """
    bound: set[str] = set()
    for item in view.walk_children():
        custom_id = getattr(item, "custom_id", None)
        if isinstance(custom_id, str) and custom_id in callbacks:
            item.callback = callbacks[custom_id]  # type: ignore[method-assign]
            bound.add(custom_id)
    return bound


#: What an edit passes beside ``view=`` to turn a message into (or keep it
#: as) a Components V2 card: a V2 message takes no ``content`` and no
#: ``embeds``. Needed wherever the message on screen may predate a
#: conversion — a board posted as an embed is still there after the restart
#: that brings the new code, and its first redraw has to clear both.
V2_EDIT: dict[str, Any] = {"content": None, "embed": None, "attachments": []}


class ExpiringLayoutView(discord.ui.LayoutView):
    """The ``LayoutView`` sibling of :class:`core.utils.ExpiringView`.

    ``discord.py``'s default ``on_timeout`` is a no-op: past the timeout the
    view stops listening, but its buttons keep rendering as live and a press
    fails with Discord's generic "This interaction failed" — the member gets
    no signal anything expired. Subclassing this instead disables every item
    in place and edits the stored message to show that, so the card visibly
    goes dead rather than silently failing on the next click.

    The contract is ``ExpiringView``'s, verbatim:

    * The caller sets ``self.message`` right after sending or editing the
      view onto a message — usually
      ``view.message = await interaction.original_response()``. That handle
      stays valid and editable for the view's whole timeout window even for
      an ephemeral response. Until it is set, ``on_timeout`` disables the
      in-memory view (so a caller inspecting it sees disabled items) but has
      nothing to edit, so it returns without making a request.
    * A press that swaps a *new* view instance onto the same message (a page
      turn that rebuilds the view, a picker replaced by its confirm gate)
      must ``stop()`` the view it replaces before the edit. discord.py keeps
      the replaced view's timeout timer running, and once it fires this
      ``on_timeout`` would edit the greyed-out old view back over the live
      one. Re-rendering the same instance in place needs nothing: its timer
      is the live view's own.

    The one difference is the walk: a layout's items nest, so disabling goes
    through ``walk_children()`` rather than ``children``. The edit passes
    only ``view=`` — a V2 message has no ``content`` or ``embeds`` to null
    out, and re-uploading its media on a timeout would be a wasted upload of
    bytes the message already shows.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        disable_all(self)
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


def sample_prefix_ids(view: discord.ui.LayoutView) -> None:
    """Prefix every ``custom_id`` in ``view`` with ``sample:``, in place.

    A sample gallery is posted by a throwaway REST login while the **running**
    bot still holds the old code (guide § Verifying a conversion). Its
    persistent ids would otherwise be live: a press on a sample card would
    reach the real handler and act on real data. Prefixed, no handler matches
    and Discord answers "This interaction failed", which is what a sample
    should do.

    Link buttons carry no ``custom_id`` and are left alone, and an id already
    prefixed is not prefixed twice, so the helper is idempotent. A persistent
    :class:`discord.ui.DynamicItem` (auctions' Bid button) rejects a prefixed
    id through its own ``custom_id`` setter — it only accepts one matching
    its ``template`` — so for one of those the prefix is written to the
    wrapped item beneath it instead, the same place discord.py itself reads
    from; the outer ``DynamicItem`` never validates that write. Used only by
    the sample-gallery script — never on a card the bot actually ships.
    """
    for item in view.walk_children():
        custom_id = getattr(item, "custom_id", None)
        if not isinstance(custom_id, str) or custom_id.startswith("sample:"):
            continue
        target = item.item if isinstance(item, discord.ui.DynamicItem) else item
        target.custom_id = f"sample:{custom_id}"  # type: ignore[attr-defined]
