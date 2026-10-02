"""Components V2 card builders for the Marry/Fornicate/Kiss cog.

The V2 replacement for ``games_mfk/embeds.py`` (deleted with this
conversion — ``docs/plans/components_v2_migration.md`` wave 3b). Pure
builders: plain dicts/primitives in, ``discord.ui.Item`` lists out, no IO
and no Discord calls, so they're testable with no network and no mocks of
the Bot/Guild API.

Two card shapes are exposed, matching the two the embed module carried:

* :func:`build_lobby_layout` (+ :func:`build_lobby_action_row`) — the
  join-the-pool card shown alongside the host/player buttons. **Paradigm
  8 — live board**: the same instance is rebuilt in place on every Join
  toggle, not re-sent, so its buttons' ``custom_id``\\ s (frozen —
  ``mfk_join``, ``mfk_assign``, ``mfk_htp``) must survive a restart.
* :func:`build_assignments_layout` — the post-close card listing each
  player's three randomly-drawn targets with the call-to-action
  ("Reply with your <Marry, Fornicate, Kiss> picks!"). **Paradigm 7 —
  receipt/notice**: bare text, no controls. A V2 message has no
  ``content=`` to carry a ping (``docs/components_v2_style_guide.md``
  § What the pilot established), so the card body is the only place
  the reveal ping can live: each player's own line opens with a raw
  ``mention(player_id)`` — the deliberate ping, allow-listed by the
  cog to exactly that player — while their drawn *targets* are named
  through ``name_fn`` (``services/name_resolver``), never
  ``<@id>``-mentioned, since a ``TextDisplay`` mention renders from the
  *reading* client's own cache and degrades to a bare number for a
  target who isn't pinged. One identity, one representation per line:
  a player is pinged once in their own line and may still be *named*
  in another player's target list — a different purpose, not a
  duplicate.

``host_name`` and every resolved participant display name are
member-controlled text with no prior escaping (``games.utils.
game_manager.resolve_name`` returns the raw ``display_name``), so both
builders run them through ``discord.utils.escape_markdown`` before they
reach a ``TextDisplay`` — unlike an embed field, a Text Display renders
markdown, so an un-escaped name could break the card's own formatting or
mask a link (``docs/reviews/2026-09-21-components-v2-wave-3a.md``, the
wave's #1 repeated defect).

The slot-line formatter (:func:`format_assignment_value`) is split out
so tests can assert on the bold-name layout without constructing a
``LayoutView``.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit, trim_escaped
from bot_modules.core.layout import MAX_CHARS, lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.mfk.logic import DEFAULT_LABELS
from bot_modules.shared.embeds import take_to_fit
from bot_modules.shared.name_resolver import NameFn, mention

JOIN_CUSTOM_ID = "mfk_join"
CLOSE_ASSIGN_CUSTOM_ID = "mfk_assign"
HOW_TO_PLAY_CUSTOM_ID = "mfk_htp"


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


#: The most one category may spend of a card, escaped. ``labels`` is the
#: host's ``/mfk options:`` free text: ``parse_labels`` bounds how *many*
#: categories it yields (three) and nothing about how long each is, and every
#: card quotes them more than once — three 2,000-char categories, escaped,
#: overflow the 4,000-char card on their own and Discord rejects the send.
LABEL_LIMIT = 100

#: The most the lobby's pool line may spend. 25 joiners (``MAX_PARTICIPANTS``)
#: whose 32-char names are pure markdown escape to 64 chars each — 1,650
#: joined — so a full lobby of them needs the cap; ordinary names never meet it.
POOL_BUDGET = 1500


def _fitted_labels(labels: list[str]) -> list[str]:
    """Each category escaped, then fitted — fitted *after* escaping, since
    escaping can double the text, and never by a slice that splits an escape
    pair (``escape_to_fit``)."""
    return [escape_to_fit(lbl, LABEL_LIMIT) for lbl in labels]


def _title_for(labels: list[str]) -> str:
    """The card title from the labels list (e.g. ``"Marry, Fornicate, Kiss"``),
    escaped and fitted — a Text Display renders markdown."""
    return ", ".join(_fitted_labels(labels))


def _fit_pool(participants: list[str], limit: int = POOL_BUDGET) -> str:
    """Escape and comma-join the pool, dropping trailing names past ``limit``
    and counting them. Budgets whole *escaped* names (``take_to_fit``), never
    a character slice, which could split an escape pair."""
    if not participants:
        return "—"
    escaped = [discord.utils.escape_markdown(p) for p in participants]
    suffix_budget = 16  # " (+24 more)" is the longest a 25-name pool needs
    shown, remaining = take_to_fit(escaped, limit - suffix_budget)
    if not shown:
        # One name overflows on its own — unreachable under Discord's 32-char
        # name ceiling, but degrade rather than 400 if that ceiling moves.
        return trim_escaped(escaped[0], limit)
    joined = ", ".join(shown)
    return f"{joined} (+{remaining} more)" if remaining else joined


def build_lobby_action_row() -> discord.ui.ActionRow[Any]:
    """The three lobby buttons, with the ids that survive a restart.

    Built on its own as well as inside the card so the cog can register a
    recovered view at load without inventing a placeholder pool — a
    restart-recovered lobby carries only this row until the next Join
    redraws it (mirrors ``dm_perms``' ``build_consent_action_row``).
    """
    return discord.ui.ActionRow(
        discord.ui.Button(
            label="Join", style=discord.ButtonStyle.success, custom_id=JOIN_CUSTOM_ID
        ),
        discord.ui.Button(
            label="Close & Assign",
            style=discord.ButtonStyle.primary,
            custom_id=CLOSE_ASSIGN_CUSTOM_ID,
        ),
        discord.ui.Button(
            label="❓ Help",
            style=discord.ButtonStyle.secondary,
            custom_id=HOW_TO_PLAY_CUSTOM_ID,
        ),
    )


#: The name a titled lobby header wears (``read_lobby_art``) — the game's,
#: whatever categories the host picked; the card's categories line says those.
LOBBY_TITLE = GAME_NAMES["mfk"]


def build_lobby_layout(
    host_name: str,
    participants: list[str],
    labels: list[str] | None = None,
    start_at: int | None = None,
    *,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The lobby card shown while players are joining. **Paradigm 8 — live
    board.**

    ``participants`` is a list of pre-resolved display names (the cog runs
    the resolution against the guild before calling) — escaped here, not by
    the caller. Empty list renders as ``"—"`` so the line always has a
    value.

    ``labels`` overrides the default categories — when supplied, both the
    title and the categories line swap to the host's choices.

    ``start_at`` is an optional UTC epoch shown as a live Discord relative
    timestamp — the host's advertised start time. The host still presses
    the button; the countdown is advertising, not automation.

    Includes the button row (:func:`build_lobby_action_row`) — the lobby is
    always sent whole, card and controls together, so a caller doesn't
    assemble the two separately on every redraw.

    ``header_url``/``footer_url``/``title_in_header`` are the guild's lobby
    art (``core.layout.lobby_art``): the header tops the card and the footer
    closes it below the buttons, only when a header is set. A titled header
    already says :data:`LOBBY_TITLE`, so the ``##`` heading goes.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=LOBBY_TITLE if titled else None)
    labels = labels or DEFAULT_LABELS
    title_str = _title_for(labels)
    host = discord.utils.escape_markdown(host_name)
    # Each label escaped *inside* the bold, so a category of ``**`` can't
    # close the wrapper and restyle the rest of the line.
    categories = " · ".join(f"**{lbl}**" for lbl in _fitted_labels(labels))

    lead = "" if titled else f"## {GAME_ICONS['mfk']} {title_str}\n"
    lead += f"🎙️ Hosted by **{host}**"
    lead += f"\n🗂️ Categories: {categories}"
    if start_at:
        lead += f"\n⏰ Starting <t:{start_at}:R>"

    pool = f"👥 Pool ({len(participants)})\n{_fit_pool(participants)}"

    return [*top, _text(lead), _text(pool), build_lobby_action_row(), *bottom]


def format_assignment_value(target_names: list[str]) -> str:
    """Render the three target names as a bold ``·``-separated list.

    The cog assembles one of these per player and drops it into the
    assignments card's combined text. Split out so tests can assert on the
    layout without constructing the surrounding card. ``target_names`` is
    already resolved/escaped by the caller (through ``name_fn``).
    """
    return " · ".join(f"**{name}**" for name in target_names)


def build_assignments_layout(
    assignments: dict[int, list[int]],
    labels: list[str] | None = None,
    *,
    name_fn: NameFn = mention,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The post-close card announcing each player's three targets.
    **Paradigm 7 — receipt/notice.**

    ``assignments`` is the ``{player_id: [target_ids]}`` map from
    ``assign_targets``. Each line opens with a raw ``<@player_id>`` mention
    — the deliberate reveal ping, since a V2 message has no ``content=``
    left to carry it (the card body is the only place it can live) — and
    lists that player's targets through ``name_fn`` (the resolver from
    ``build_name_fn``), whose output is already markdown-escaped. The
    default ``name_fn=mention`` keeps an un-wired caller rendering a
    mention for targets too rather than crashing; the cog always passes a
    real resolver.

    ``labels`` matches the call sent to :func:`build_lobby_layout`; the
    heading tracks the host's chosen categories.

    ``payout_line`` is the economy's own text (``economy.game_rewards.
    payout_footer_line``) — a ``-#`` subtext line under the facts, the same
    place the old embed's footer carried it. ``None`` renders nothing,
    matching the old footer's silent-no-payout behaviour.

    One combined ``TextDisplay`` rather than a line per player as separate
    components: 25 players as ``Section`` rows would blow the 40-component
    ceiling on its own (3 each), where packed text fits the 4,000-character
    ceiling — ordinary names with room to spare, markdown-heavy ones by
    dropping whole trailing rows into an "…and N more" line (``check_budget``
    in the test asserts both worst cases). Capped at 25 rows defensively —
    the lobby caps joins at ``MAX_PARTICIPANTS`` (25) already, but a
    stale/oversized roster must never overflow the card.
    """
    labels = labels or DEFAULT_LABELS
    title_str = _title_for(labels)
    head = [
        f"## {GAME_ICONS['mfk']} {title_str} — Your Three Names",
        f"Reply with your {title_str} picks!",
    ]
    tail = [f"-# {payout_line}"] if payout_line else []
    rows = [
        f"{mention(player_id)}: {format_assignment_value([name_fn(uid) for uid in targets])}"
        for player_id, targets in list(assignments.items())[:25]
    ]
    # 25 rows of three markdown-heavy names (64 chars escaped) run to ~5,800
    # characters, past the card's 4,000; whole rows are dropped and counted
    # rather than the send being rejected. A dropped player still has their
    # targets in the game state — only this card's line is lost.
    overflow_room = 24  # "_…and N more_" on its own line
    fixed = sum(len(line) + 1 for line in head + tail) + overflow_room
    shown, remaining = take_to_fit(rows, MAX_CHARS - fixed, sep="\n")
    if remaining:
        shown.append(f"_…and {remaining} more_")
    return [_text("\n".join(head + shown + tail))]
