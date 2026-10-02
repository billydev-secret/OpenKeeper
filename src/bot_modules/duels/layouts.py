"""Components V2 cards for the shared duel layer: the terminal notices, and
now (``docs/plans/components_v2_migration.md`` wave 3b, "Duels: live lobby
and challenge cards") the *live* challenge and lobby cards themselves.

**Paradigm 7** (``docs/components_v2_style_guide.md`` § The paradigms):
:func:`build_duel_notice_layout` is the one bare-receipt shape every
terminal off-ramp shares — no buttons, one heading, a line or two of body
text — for a challenge, lobby or in-progress game that ended without a
result card to show (declined, expired, called off, cancelled, or a rename
that didn't happen).

**Paradigm 5** (decision card): :func:`build_challenge_layout` is the
pending-challenge card — facts, then Accept/Decline underneath (the row is
:class:`duels.views.ChallengeView`'s job, not this builder's — see
``DuelLayoutView``'s ``render(items)`` contract). **Paradigm 8** (live
board): :func:`build_lobby_layout` is the join lobby, re-rendered on every
join/leave.

Both used to stay ``discord.Embed`` on purpose: every mini-game cog's own
card was still an embed too, so the accept / lobby-start edit that turns
this message into the ACTIVE game's card (``render_game_state``, an
abstract hook — see ``base_game.py``'s module docstring) would have had to
downgrade a Components V2 message back to one, which Discord refuses
outright (the ``IS_COMPONENTS_V2`` flag is one-way). That blocker is gone:
every mini-game cog wave 3b touches now renders its own card as items, so
the edit takes the layout arm everywhere this shared layer is reachable
from, and the pre-game card can convert alongside it. The *terminal* cards
built by :func:`build_duel_notice_layout` never reach that edit — a
declined, expired, called-off or cancelled game is finished, and nothing
edits its message again — which is why they converted first, in an earlier
commit.
``ResultView`` (``duels/views.py``) stays a plain ``discord.ui.View`` on
purpose still: it is the embed twin :meth:`base_game.BaseGame._result_view`
builds for the (now hypothetical, until a seventh mini-game needs it) cog
whose ``render_result_state`` still returns an embed — the dual-armed
plumbing in ``base_game.py`` (``_active_game_edit_kwargs``,
``_edit_continue_state``, ``_result_send_kwargs``) is out of scope for this
commit and stays exactly as it is.

Pure, like every ``*_layouts.py`` module: primitives + ``name_fn`` in, a
list of ``discord.ui.Item`` out. No IO, no accent colour, no callbacks, no
controls — a builder's caller (the owning ``LayoutView``'s ``render``) adds
its own control row last.
"""
from __future__ import annotations

from typing import Any, Sequence

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.core.branding_service import PanelArt
from bot_modules.shared.embeds import take_to_fit
from bot_modules.shared.name_resolver import NameFn

# The roster's share of the lobby card's 4,000-character text budget
# (``core.layout.MAX_CHARS``, counted across every text item). An escaped
# display name can reach 64 characters and ``max_players`` has no server-side
# ceiling, so a full lobby of markdown-heavy names could otherwise overrun the
# whole card, and Discord rejects the send outright: the lobby never appears.
LOBBY_ROSTER_BUDGET = 1500


def card_art(
    art: PanelArt | None, title: str
) -> tuple[list[discord.ui.Item[Any]], list[discord.ui.Item[Any]]]:
    """The ``(top, bottom)`` frame every PvP card wears: the guild's lobby
    header (``title`` — the game's name — drawn into it when
    ``art.title_in_header``) and the hubs' footer, or ``([], [])`` without a
    header (``core.layout.lobby_art``). One game is one card from challenge
    or lobby to result, so the challenge, the live board, the result and the
    terminal notice all frame the same way the lobby does."""
    if art is None:
        return [], []
    return lobby_art(
        art.header, art.footer, title=title if art.title_in_header else None
    )


def card(
    items: Sequence[discord.ui.Item[Any]],
    *,
    art: PanelArt | None = None,
    title: str = "",
) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one send.

    Every card built here is a receipt with no buttons, so nothing needs to
    expire and no per-caller subclass is worth carrying — one shared
    throwaway view, same shape as ``dm_perms_cog._card``. A view instance
    belongs to one message, so every call site builds its own. ``art`` and
    ``title`` frame it (:func:`card_art`) — a game's terminal notice lands on
    the game's own card, so it keeps that card's header and footer.
    """
    view = discord.ui.LayoutView(timeout=None)
    top, bottom = card_art(art, title)
    for item in (*top, *items, *bottom):
        view.add_item(item)
    return view


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def build_players_line(winner: str, loser: str) -> discord.ui.TextDisplay[Any]:
    """Who won and who lost, by resolved name — the first line of a result
    card (``BaseGame._result_layout``) and of a notice that replaces one.
    Names, not mentions: the result is an edit of the game's own card, and
    an edit notifies nobody (``docs/specs/dk_pvp_games_suite_spec.md`` "One card")."""
    return discord.ui.TextDisplay(f"🏆 **Winner:** {winner}\n💀 **Loser:** {loser}")


def build_duel_notice_layout(heading: str, body: str) -> list[discord.ui.Item[Any]]:
    """A bare receipt: ``## {heading}`` then ``body``. No buttons — every
    caller is a terminal edit that also drops the message's view.

    ``heading`` carries its own glyph (``⏱️``, ``❌``, …) — this builder adds
    none of its own, so each call site picks the one glyph naming what
    actually happened (guide § Words). ``body`` arrives pre-formatted and
    pre-escaped by the caller, same contract as ``approval_layouts``.
    """
    return [_text(f"## {heading}\n{body}")]


def build_nick_reverted_layout(game_display_name: str, restored: str) -> list[discord.ui.Item[Any]]:
    """The DM :meth:`BaseGame._revert_nick` sends once a nickname sentence
    lapses and the member's nickname is restored.

    ``restored`` is the member's own prior nickname (or their username, when
    they had none) — a nickname is member- or admin-set text, so it is
    escaped here same as any other free text landing in a Text Display
    (guide § Words). The embed this replaces did not escape it; doing so now
    is a small fix-forward, not a behaviour this card is trying to keep.
    """
    safe = discord.utils.escape_markdown(restored)
    return [
        _text(
            f"Your {game_display_name} nickname sentence has expired. Your "
            f"nickname has been restored to **{safe}**."
        )
    ]


def build_challenge_layout(
    game_display_name: str,
    challenger_id: int,
    target_id: int,
    stakes_text: str,
    *,
    wager: int | None = None,
    expires_at: int,
    name_fn: NameFn,
    titled: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The pending-challenge card. **Paradigm 5 — decision card.**

    ``stakes_text`` already lists every live stake, one line each and
    already in the guild's currency vocabulary (custom text, wager, nickname
    forfeit — see :func:`filters.resolve_stakes_text`; a caller with no
    custom stakes passes ``BaseGame.nick_forfeit_copy(sentence_hours)``), so
    a separate wager line here would only repeat the amount. ``wager``
    survives purely to decide whether to add the escrow caveat, which is
    true only while a challenge is pending and so is never persisted.

    The target's ping is the **first** item — a bare ``<@{target_id}>`` Text
    Display, since a Components V2 message has no ``content=`` to carry one
    (guide § Anatomy / § Mentions) — followed by the challenger and target
    named through ``name_fn`` in the card body itself, never a second raw
    mention (an embed-era ``<@id>`` in body text only shows digits to a
    reader whose client hasn't cached that member).

    ``titled`` is set when the card's header already says
    ``game_display_name`` (:func:`card_art`), so the heading drops the name.
    """
    heading = "Challenge" if titled else f"{game_display_name} Challenge"
    stakes = stakes_text
    if wager:
        stakes += "\n_Nothing is charged unless the challenge is accepted._"
    return [
        # Not routed through the _text() wrapper: this is a deliberate ping
        # (guide § Mentions), and calling TextDisplay directly here is what
        # lets tests/contracts/test_embed_mention_guard.py's AST sweep actually see it
        # and require the ledger row below — a mention passed through a
        # local helper is invisible to that sweep's sink recognizer.
        discord.ui.TextDisplay(f"<@{target_id}>"),
        _text(
            f"## ⚔️ {heading}\n"
            f"{name_fn(challenger_id)} has challenged {name_fn(target_id)}!"
        ),
        _text(f"### 📋 Stakes\n{stakes}"),
        _text(f"### ⏱️ Expires\n<t:{expires_at}:R>"),
    ]


def build_lobby_layout(
    game_display_name: str,
    how_to_play: str | None,
    roster: Sequence[int],
    host_id: int,
    min_players: int,
    max_players: int,
    stakes_text: str,
    *,
    ante: int = 0,
    pot_text: str | None = None,
    closes_at: float | None = None,
    name_fn: NameFn,
    controls: discord.ui.Item[Any] | None = None,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The join lobby. **Paradigm 8 — live board**, re-rendered on every
    join, leave and clock reset; :class:`duels.lobby.LobbyView` appends the
    Join/Leave/Start/Cancel row (this builder returns no controls).

    ``stakes_text`` and ``pot_text`` arrive pre-formatted by the caller —
    same contract as :func:`build_challenge_layout` — so this stays a plain
    assembler with no currency-vocabulary or nickname-copy logic of its own.
    ``pot_text`` is only used when ``ante`` is positive (a wagered lobby);
    passing one without the other is a caller error, not a state this
    builder renders. Every roster member is named through ``name_fn``, never
    a raw mention (guide § Mentions), and the host is named **once**: marked
    in place on their roster line (" — host"), not repeated in the subtext
    (guide § Mentions and names: once is per card, per fact). Only a host who
    isn't in the roster — a state no mini-game produces today, since
    ``_base_lobby`` seats the host first — gets named in the subtext instead.

    The old embed's footer ("Host: … • Need N+ players to start.") becomes
    the trailing ``-# `` subtext line (guide § Anatomy: "Footer jobs move to
    a ``-# subtext`` line"), now carrying just the one job the roster row
    can't: the start threshold.

    ``controls`` is :class:`duels.lobby.LobbyView`'s button row, placed here
    so the lobby's closing art can sit below it; without it the view appends
    the row itself. ``header_url``/``footer_url``/``title_in_header`` are the
    guild's lobby art, as Clapback's lobby takes them
    (``core.layout.lobby_art``): nothing draws without a header, and a titled
    header already says ``game_display_name``, so the ``##`` heading goes.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(
        header_url, footer_url, title=game_display_name if titled else None
    )
    heading = "" if titled else f"## 🎮 {game_display_name} — Lobby\n"
    items: list[discord.ui.Item[Any]] = [
        *top,
        _text(
            f"{heading}"
            "Press **✋ Join** to get in. Host presses **▶️ Start** when "
            f"ready — or it starts on its own once {max_players} are in."
        ),
    ]
    if how_to_play:
        items.append(_text(f"### 📖 How to Play\n{how_to_play}"))
    tail_room = 24  # "_…and N more_" on its own line
    shown, remaining = take_to_fit(
        [f"• {name_fn(uid)}" + (" — host" if uid == host_id else "") for uid in roster],
        LOBBY_ROSTER_BUDGET - tail_room,
        sep="\n",
    )
    names = "\n".join(shown)
    if remaining:
        names = f"{names}\n_…and {remaining} more_" if names else f"_…and {remaining} more_"
    names = names or "—"
    items.append(_text(f"### 👥 Players ({len(roster)}/{max_players})\n{names}"))
    items.append(_text(f"### 📋 Stakes\n{stakes_text}"))
    if ante > 0 and pot_text:
        items.append(
            _text(
                f"### 💰 Pot\n{pot_text} so far, and it grows with each player.\n"
                "_Leaving the lobby refunds you._"
            )
        )
    if closes_at is not None:
        items.append(
            _text(
                f"### ⏱️ Closes\n<t:{int(closes_at)}:R> — every join resets the clock."
            )
        )
    footer = f"Need {min_players}+ players to start."
    if host_id not in roster:
        footer = f"Host: {name_fn(host_id)} • {footer}"
    items.append(_text(f"-# {footer}"))
    if controls is not None:
        items.append(controls)
    items.extend(bottom)
    return items
