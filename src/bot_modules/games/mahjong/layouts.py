"""Meadow Mahjong cards — presentation only, no rules (stage 6, spec §6).

The Components V2 successor of ``games/mahjong/embeds.py`` (deleted with
this conversion; ``docs/components_v2_style_guide.md``). Every builder takes
pre-resolved inputs (state, names) and returns layout items; the cog owns
Discord lookups and the service owns truth. House style: bare cards, no
accent and no results-green — the words carry the meaning; Title Case
labels; the ``-# 🀄 Meadow Mahjong`` footer line; monospace payout tables.
Tiles render through tile_render only — racks can never blank.

The table card's controls are :func:`phase_controls` — one row per phase,
``mmj:{table_id}:{action}`` custom ids, stable across restarts — which the
``views.TableView`` binds. **Nothing host-only is on it**: Cancel (lobby)
and Close Table (after a hand) are on the ephemeral host panel
(:func:`build_host_panel_layout`, opened by ``/games host``; Ben,
2026-10-01), and the lobby and settle cards carry the one ``-#`` line that
points there. Every other card is text alone; the ephemeral panels in
``views.py`` add their own pickers under it.

Not cards, but living here with them: the plain-content turn nudges
(:func:`nudge_lines`), the Game Lobby line (:func:`build_table_open_ping`)
and the closed-table copy.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, NamedTuple

import discord

from bot_modules.core.branding import (
    code_safe,
    escape_to_fit,
    trim_escaped,
)
from bot_modules.games.mahjong.bot_logic import is_bot_id
from bot_modules.games.mahjong.card_logic import Card
from bot_modules.games.mahjong.game_logic import (
    AUTO_PASS,
    STRIKES_TO_FALLOW,
    AssistReadout,
    Event,
    GameState,
    Outcome,
    Phase,
)
from bot_modules.games.mahjong.tiles import FULL_RANK, Tile, sort_rack
from bot_modules.games.utils.host_panel import HOST_COMMAND, HOST_HINT
from bot_modules.games.mahjong.tile_render import back_str, chip, rack_str, tile_str

__all__ = ["tile_str"]  # the cog renders winning groups through it

ICON = "🀄"
FOOTER = "🀄 Meadow Mahjong"
MODE_NAMES = {2: "Duel", 4: "Full Table"}

#: The most an admin-uploaded name (card, hand line) may spend of a card,
#: escaped — ``card_logic.py`` caps none of them.
NAME_LIMIT = 300
#: A Text Display is capped at 4,000 characters and a card at 40 components;
#: the card viewer pages itself well inside both.
VIEWER_BLOCK_LIMIT = 1800
VIEWER_PAGE_LIMIT = 3600
#: The rack's Closest Hands block keeps the 1,024-character bound the embed
#: field gave it — ``_assist_field`` degrades emoji to text chips to fit.
ASSIST_LIMIT = 1024

#: The member panel's buttons (ephemeral ids; the view binds them).
PANEL_CREATE_ID = "mmj_panel_create"
PANEL_CARD_ID = "mmj_panel_card"
PANEL_STATS_ID = "mmj_panel_stats"
PANEL_SETTINGS_ID = "mmj_panel_settings"
PANEL_HELP_ID = "mmj_panel_help"

#: The host panel's buttons (ephemeral; the cog binds them).
HOST_CANCEL_ID = "mmj_host_cancel"
HOST_CLOSE_ID = "mmj_host_close"

#: Every action a table card ever carried, for resume-time registration:
#: ``cancel`` and ``close`` left the card for the host panel, but a sticky
#: message on disk from before that move still shows them until its next
#: repost, and an unregistered id dies as "interaction failed".
TABLE_ACTIONS = ("join", "cancel", "rack", "vote_y", "vote_n", "claim_mj",
                 "claim_call", "claim_pass", "rematch", "close", "add_bot")

_PASS_NAMES = {
    (1, 0): "Right", (1, 1): "Across", (1, 2): "Left",
    (2, 0): "Left", (2, 1): "Across", (2, 2): "Right",
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _footer(extra: str = "") -> discord.ui.TextDisplay[Any]:
    return _text(f"-# {FOOTER} • {extra}" if extra else f"-# {FOOTER}")


def _grey(label: str, custom_id: str, emoji: str | None = None) -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label=label, style=discord.ButtonStyle.secondary, emoji=emoji, custom_id=custom_id,
    )


def _red(label: str, custom_id: str) -> discord.ui.Button[Any]:
    return discord.ui.Button(label=label, style=discord.ButtonStyle.danger, custom_id=custom_id)


def table_cid(table_id: int, action: str) -> str:
    return f"mmj:{table_id}:{action}"


def _clock(deadline_at: float | None) -> str:
    """Markdown countdown for a card line — Discord renders <t:…:R> in
    body text."""
    if not deadline_at or deadline_at <= time.time():
        return ""
    return f" — ⏱ <t:{int(deadline_at)}:R>"


def _seat_line(
    state: GameState, seat: int, name: str, *, turn_arrow: bool
) -> str:
    s = state.seats[seat]
    bits = []
    if turn_arrow and state.turn == seat and state.phase in (
        Phase.AWAIT_DISCARD, Phase.CLAIM_WINDOW
    ):
        bits.append("▶")
    bits.append(f"**{name}**")
    if s.fallow:
        bits.append("💤 folded")
    elif s.strikes:
        bits.append("⚠" * s.strikes)
    line = " ".join(bits)
    if s.exposures:
        shown = "   ".join(
            " ".join([tile_str(e.natural)] * (e.count - e.jokers)
                     + [f"{tile_str(Tile.JOKER)}"] * e.jokers)
            for e in s.exposures
        )
        line += f"\n{shown}"
    return line


# ── the /mahjong panel ───────────────────────────────────────────────────────


def build_member_panel(
    card: Card | None, stakes: tuple[int, ...], balance: int,
    escrow_for: Callable[[int, int], int] | None = None,
) -> list[discord.ui.Item[Any]]:
    """The /mahjong panel. **Paradigm 2 — ephemeral panel.**
    ``escrow_for(seat_count, stake)`` puts the hold per table size on the
    panel itself (mahjong-150): it used to appear three clicks in, on the
    stake picker, after a member had already decided to open a table."""
    if card is None:
        desc = "No Meadow Card is active right now — check back soon."
    else:
        # card.display_name is admin-uploaded free text (card_logic.py has no
        # length cap on it) — escape first, then fit.
        desc = (
            f"Card in season: **{escape_to_fit(card.display_name, NAME_LIMIT)}** "
            f"({len(card.hands)} hands)\n"
            f"Stakes: {', '.join(str(s) for s in stakes)} coins per point\n"
            f"Your balance: **{balance}** coins"
        )
    items: list[discord.ui.Item[Any]] = [_text(f"## {ICON} Meadow Mahjong\n{desc}")]
    if card is not None and escrow_for is not None and stakes:
        low = min(stakes)
        items.append(_text(
            "### Escrow per Seat\n"
            f"At {low}/point: **{escrow_for(2, low)}** coins for a "
            f"Duel, **{escrow_for(4, low)}** for a Full Table — held "
            "when you sit, and everything you don't lose comes back "
            "at the settle. Higher stakes hold proportionally more; "
            "practice tables hold nothing."
        ))
    items.append(discord.ui.ActionRow(
        _grey("Create Table", PANEL_CREATE_ID),
        _grey("Card Viewer", PANEL_CARD_ID),
        _grey("My Stats", PANEL_STATS_ID),
        _grey("My Settings", PANEL_SETTINGS_ID),
        _grey("How to Play", PANEL_HELP_ID),
    ))
    items.append(_footer("American-style, card-driven"))
    return items


#: Why a table closed, in the words the final card shows (mahjong-151).
#: Keyed by ``mahjong_tables.closed_reason``; anything unlisted falls back
#: to the bare "Table closed".
CLOSE_COPY = {
    "dissolved": "the lobby never filled",
    "expired": "nobody rematched in time",
    "rematch_unfunded": "a seat couldn't cover the next hand's escrow",
    "cancelled": "cancelled before the deal",
    "closed": "closed from the settle screen",
    "purged": "closed by the house",
    "unloadable": "closed by the house",
}


def close_copy(reason: str | None) -> str:
    why = CLOSE_COPY.get(str(reason or ""))
    return f"Table closed — {why}." if why else "Table closed."


def build_table_open_ping(
    role_id: int, seat_count: int, stake: int, open_seats: int,
    jump: str, *, quick: bool = False,
) -> str:
    """The one Game Lobby line a new real table posts (mahjong-149).
    Content, never a card — a role mention only notifies from message
    content — and the cog allow-lists exactly this role."""
    mode = MODE_NAMES.get(seat_count, str(seat_count))
    if quick:
        mode = f"Quick {mode}"
    seats = f"{open_seats} seat{'s' if open_seats != 1 else ''} open"
    return (
        f"<@&{role_id}> {ICON} A **Meadow Mahjong** {mode} just opened at "
        f"{stake}/point — {seats}. Jump in: {jump}"
    )


# ── the table card ───────────────────────────────────────────────────────────


def phase_controls(
    table_id: int, phase: Phase | None, *, add_bot: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The table card's buttons for *phase* — all grey, all things a player
    or an onlooker may press. Cancel and Close Table are the host panel's
    (:func:`build_host_panel_layout`). A closed table carries none."""
    cid = lambda action: table_cid(table_id, action)  # noqa: E731
    if phase is Phase.LOBBY:
        buttons = [_grey("Join", cid("join"))]
        if add_bot:  # host seats a house bot (bots plan B6; fill dial)
            buttons.append(_grey("Add Bot", cid("add_bot"), emoji="🌱"))
    elif phase is Phase.CHARLESTON_VOTE:
        buttons = [
            _grey("Run It Again", cid("vote_y")),
            _grey("No Thanks", cid("vote_n")),
            _grey("Open Rack", cid("rack")),
        ]
    elif phase is Phase.CLAIM_WINDOW:
        buttons = [
            _grey("Mahjong", cid("claim_mj"), emoji="🀄"),
            _grey("Call", cid("claim_call"), emoji="✋"),
            _grey("Pass", cid("claim_pass")),
            _grey("Open Rack", cid("rack")),
        ]
    elif phase is Phase.SETTLE:
        buttons = [_grey("Rematch", cid("rematch"), emoji="🔁")]
    elif phase is Phase.CLOSED or phase is None:
        return []
    else:  # charleston / courtesy / await_discard: self-serve via rack
        buttons = [_grey("Open Rack", cid("rack"))]
    return [discord.ui.ActionRow(*buttons)]


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The table's host controls, on an ephemeral card. **Paradigm 2 — panel.**

    Cancel scraps a lobby (the host's, or a mod's; every escrow returns);
    Close Table ends a table after a hand (any seated player, or a mod). Both
    stay red — the destructive presses — and both are always drawn: the
    panel outlives the phase it was opened in, so each press checks the
    table as it is *then* and says plainly when it has moved on.
    """
    return [
        _text(
            f"## {ICON} Table Controls\n"
            "**Cancel** scraps the table before the deal and returns every "
            "escrow (host or a mod). **Close Table** ends it after a hand "
            "(any player at the table, or a mod).\n"
            f"-# Only you can see this. {HOST_COMMAND} brings it back."
        ),
        discord.ui.ActionRow(
            _red("Cancel", HOST_CANCEL_ID),
            _red("Close Table", HOST_CLOSE_ID),
        ),
    ]


def build_table_panel(
    state: GameState,
    names: dict[int, str],
    stake: int,
    escrow: int,
    deadline_at: float | None = None,
    *,
    practice: bool = False,
    closed_reason: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The public table card's body, every phase. **Paradigm 8 — live
    board.** ``names`` arrive escaped (``mahjong_cog._escaped``). The
    phase's buttons are :func:`phase_controls`, added by the view."""
    mode = MODE_NAMES.get(state.seat_count, str(state.seat_count))
    # A short deck has to be on the card itself. Players notice missing
    # numbers within a turn or two, and discovering it mid-hand reads as a
    # bug rather than the option the host chose.
    if state.config.max_rank != FULL_RANK:
        mode = f"Quick {mode} · 1–{state.config.max_rank}"
    title = f"## {ICON} " + (f"Practice Table — {mode}" if practice else f"Mahjong Table — {mode}")
    clock = _clock(deadline_at)

    if state.phase is Phase.LOBBY:
        rows = [
            f"✅ **{names.get(s.member_id, s.member_id)}**"
            + ("" if practice else " — escrow locked")
            for s in state.seats
        ]
        open_seats = state.seat_count - len(state.seats)
        rows += ["◻ *open seat*"] * open_seats
        stake_line = ("Practice — no stakes, no stats" if practice
                      else f"{stake} coins per point ({escrow} escrow per seat)")
        status = (f"Waiting for {open_seats} more{clock}" if open_seats
                  else f"Everyone's in — dealing{clock}")
        return [
            _text(f"{title}\n" + "\n".join(rows)),
            _text(f"**Stake** — {stake_line}\n**Status** — {status}\n-# {HOST_HINT}"),
            _footer("Lobby"),
        ]

    seat_names = {i: names.get(s.member_id, str(s.member_id))
                  for i, s in enumerate(state.seats)}
    lines = [
        _seat_line(state, i, seat_names[i], turn_arrow=True)
        for i in range(len(state.seats))
    ]
    head = _text(f"{title}\n" + "\n".join(lines))

    if state.phase is Phase.CHARLESTON:
        rnd = "First" if state.charleston_round == 1 else "Second"
        direction = _PASS_NAMES[(state.charleston_round, state.pass_index)]
        if state.seat_count == 2:
            direction = "to your opponent"
        ticks = " ".join(
            "✅" if i in state.pending_picks else "…"
            for i in range(len(state.seats))
        )
        return [
            head,
            _text(f"**{rnd} Charleston — Pass {state.pass_index + 1} ({direction})**\nTiles in: {ticks}{clock}"),
            _footer("Charleston"),
        ]

    if state.phase is Phase.CHARLESTON_VOTE:
        ticks = " ".join(
            "🗳" if i in state.votes else "…" for i in range(len(state.seats))
        )
        return [
            head,
            _text(f"**Second Charleston?**\nUnanimous yes runs it again. Votes: {ticks}{clock}"),
            _footer("Vote"),
        ]

    if state.phase in (Phase.COURTESY_PROPOSE, Phase.COURTESY_PICK):
        if state.phase is Phase.COURTESY_PROPOSE:
            ticks = " ".join(
                "✅" if i in state.proposals else "…"
                for i in range(len(state.seats))
            )
            block = f"**Courtesy Pass**\nOffers in: {ticks}{clock}"
        else:
            owe = ", ".join(
                f"{seat_names[i]}: {n}" for i, n in state.courtesy_owed.items()
            )
            block = f"**Courtesy Exchange**\nTrading tiles — {owe}{clock}"
        return [head, _text(block), _footer("Courtesy")]

    # play phases share the pit/wall furniture
    pit = ""
    if state.discards:
        older = " ".join(tile_str(t) for _, t in state.discards[:-1][-16:])
        latest_seat, latest = state.discards[-1]
        pit = (older + "   " if older else "") + f"__{tile_str(latest)}__"
    furniture = (
        f"**Discards**\n{pit or '*none yet*'}\n"
        f"**Wall** — {back_str()} × {len(state.wall)}"
    )

    if state.phase is Phase.CLAIM_WINDOW:
        assert state.live_discard is not None and state.live_discarder is not None
        responders = [s for s in state.live_seats() if s != state.live_discarder]
        # Only a seat's own tap earns a tick. An auto-passed seat (no legal
        # route to the tile) renders exactly like an undecided one: ticking
        # it the instant the discard landed told the whole table which
        # seats could call or Mahjong the tile (mahjong-145).
        ticks = " ".join(
            "✅" if state.claims.get(s, (AUTO_PASS,))[0] != AUTO_PASS else "…"
            for s in responders
        )
        return [
            head,
            _text(furniture),
            _text(
                "**Claim Window**\n"
                f"{seat_names[state.live_discarder]} threw "
                f"{tile_str(state.live_discard)} — 🀄 Mahjong · ✋ Call · Pass\n"
                f"Responses: {ticks}{clock}"
            ),
            _footer("Claim window"),
        ]

    if state.phase is Phase.AWAIT_DISCARD:
        return [
            head,
            _text(f"{furniture}\n**Turn** — {seat_names[state.turn]} to discard{clock}"),
            _footer(f"Hand {state.hand_no}"),
        ]

    if state.phase is Phase.SETTLE:
        out = state.outcome
        if out is not None and out.kind == "mahjong" and out.winner is not None:
            # out.line_name is admin-uploaded free text (card_logic.py has no
            # length cap on it) — seat_names is already escaped by the caller
            # (mahjong_cog._escaped), so only line_name needs it here.
            # ``or ""`` is defensive: only wall_game, fallow_end and all_fallow
            # carry line_name=None, and this branch is inside kind == "mahjong".
            name = escape_to_fit(out.line_name or "", NAME_LIMIT)
            hand_over = f"🎉 {seat_names[out.winner]} went Mahjong — **{name}**"
        elif out is not None and out.kind == "fallow_end":
            survivor = seat_names.get(out.winner, "—") if out.winner is not None else "—"
            hand_over = f"{survivor} stands alone; the folded seats pay out."
        else:
            hand_over = "Nobody went out — escrow returns."
        ready = " ".join(
            "🔁" if i in state.rematch_votes else "…"
            for i in range(len(state.seats))
        )
        return [
            head,
            _text(
                f"**Hand Over**\n{hand_over}\n"
                f"**Rematch?**\nEveryone must press it: {ready}{clock}\n"
                f"-# {HOST_HINT}"
            ),
            _footer("Settled"),
        ]

    # CLOSED: the final card says why (mahjong-151) — it used to be
    # whatever the last live render was, buttons stripped, so a settle card
    # kept asking for a Rematch that could never come.
    seats = "\n".join(
        f"**{names.get(s.member_id, s.member_id)}**" for s in state.seats
    )
    return [
        _text(f"{title}\n{seats}" if seats else title),
        _text(f"**Closed**\n{close_copy(closed_reason)}"),
        _footer("Closed"),
    ]


# ── turn nudges (plain content, never a card) ────────────────────────────────


def turn_key(state: GameState) -> tuple | None:
    """Identity of the turn a human is being nudged for, or None outside
    one. Two transitions inside the same turn (a joker redeem, an assist
    refresh) share a key, so the ping is posted once per turn, not once per
    render."""
    if state.phase is not Phase.AWAIT_DISCARD:
        return None
    return (state.hand_no, state.turn, state.discard_count, state.turn_source)


class Nudge(NamedTuple):
    """One plain-content ping (mahjong-144). ``warning`` rows are the
    second-strike line, which outlives the turn it was posted in."""

    member_id: int
    content: str
    warning: bool = False


def warning_live(state: GameState, member_id: int) -> bool:
    """Is the second-strike warning still true for this member? It stands
    while the seat is one missed turn from folding; a timely act resets the
    strikes and the fold itself ends the question, so the cog deletes the
    line the moment this goes False."""
    for seat in state.seats:
        if seat.member_id == member_id:
            return not seat.fallow and seat.strikes == STRIKES_TO_FALLOW - 1
    return False


def nudge_lines(
    state: GameState, events: list[Event], *, turn_changed: bool
) -> list[Nudge]:
    """The plain-content pings a transition owes (mahjong-144): the seat
    whose turn just began, and any seat one missed turn from folding.
    Content, never a card — a mention only pings from message content —
    and never a bot seat. The cog restricts ``allowed_mentions`` to the one
    member; a draw line is deleted when the next turn begins and a warning
    while ``warning_live`` holds, so the channel never accumulates them."""
    out: list[Nudge] = []
    for kind, data in events:
        if kind != "strike" or data.get("strikes") != STRIKES_TO_FALLOW - 1:
            continue
        seat = state.seats[data["seat"]]
        if seat.fallow or is_bot_id(seat.member_id):
            continue
        out.append(Nudge(
            seat.member_id,
            f"<@{seat.member_id}> — one more missed turn and your seat folds.",
            warning=True,
        ))
    if turn_changed and state.phase is Phase.AWAIT_DISCARD:
        member_id = state.seats[state.turn].member_id
        if not is_bot_id(member_id):
            out.append(Nudge(member_id, f"<@{member_id}> — your draw."))
    return out


# ── the rack panel ───────────────────────────────────────────────────────────


def _assist_field(assist: AssistReadout) -> str:
    """The Closest Hands block (plans/mahjong-assist.md stage 3). One
    renderer for every panel that shows a rack, so the modes can't drift.

    Two rules from the stage-4 review round: "Dead weight" is the
    intersection across the hands actually shown — the copy promises
    "tiles none of your closest hands can use", so a tile another shown
    hand still needs must never print as dead. And the block guarantees
    its own bound (the embed field's 1,024, kept), degrading emoji to text
    chips (then trimming hands) rather than blind-slicing — a cut used to
    land mid-<:mm_…:id> token and silently drop the coach suggestion, the
    one line coach mode exists to deliver.
    """
    claim = _claim_advice_lines(assist)
    if not assist.prospects:
        base = ("No line on the card is still reachable from your tiles — "
                "play for the wall.")
        return "\n".join(claim + [base]) if claim else base

    def tiles_str(pairs, render) -> str:
        return " ".join(
            render(tile) if n == 1 else f"{render(tile)}×{n}"
            for tile, n in pairs
        )

    def render_block(shown_n: int, render) -> str:
        shown = assist.prospects[:shown_n]
        # the live discard is the decision in front of the member RIGHT
        # now — it leads, and the fit loop never trims it
        lines: list[str] = list(claim)
        for rank, p in enumerate(shown, start=1):
            away = "ready!" if p.distance == 0 else (
                "1 tile away" if p.distance == 1 else f"{p.distance} tiles away")
            lines.append(
                f"**{rank}. {p.hand.name}** — {away} · {p.hand.value} pts"
            )
            if assist.mode in ("gap", "coach") and p.needed:
                lines.append(f"    need {tiles_str(p.needed, render)}")
        if assist.live_count > len(shown):
            lines.append(f"*…of {assist.live_count} lines still live*")
        if assist.mode == "coach":
            # dead for EVERY shown hand; prospects[0]'s order is kept
            common = list(shown[0].dead_weight)
            for p in shown[1:]:
                dw = dict(p.dead_weight)
                common = [
                    (tile, min(n, dw[tile])) for tile, n in common if tile in dw
                ]
            if common:
                lines.append(f"Dead weight: {tiles_str(common, render)}")
            if assist.suggestion is not None:
                lines.append(f"Consider discarding {render(assist.suggestion)}")
            elif assist.prospects[0].dead_weight:
                lines.append("*No clearly safe discard — your call.*")
        return "\n".join(lines)

    attempts = [(len(assist.prospects), tile_str), (len(assist.prospects), chip),
                (2, chip), (1, chip)]
    for shown_n, render in attempts:
        text = render_block(shown_n, render)
        if len(text) <= ASSIST_LIMIT:
            return text
    # unreachable — one chip-rendered hand is far under the bound — but if a
    # future card breaks the assumption, a clean tail beats a cut token.
    return render_block(1, chip)[:ASSIST_LIMIT]


def _claim_advice_lines(assist: AssistReadout) -> list[str]:
    """Coach's verdict on the live discard (plans/mahjong-bots.md shares the
    judgement with the bot brain): take it to win, call it for a named
    gain, or let it go. Silent in every other mode and phase."""
    if assist.claim_win is not None:
        return [f"🀄 **That tile wins it — {assist.claim_win.name}. "
                f"Press Mahjong.**"]
    advice = assist.claim_call
    if advice is None:
        return []
    group = {3: "pung", 4: "kong", 5: "quint"}.get(advice.count, "set")
    return [
        f"✋ **Call it** — exposes a {group}, "
        f"{advice.distance_before}→{advice.distance} away "
        f"for {advice.hand.name}."
    ]


def build_rack_panel(
    state: GameState, seat: int, deadline_at: float | None = None,
    *, context: str | None = None, assist: AssistReadout | None = None,
) -> list[discord.ui.Item[Any]]:
    """The member's rack, ephemeral. **Paradigm 2 — panel.** The phase's
    picker (``views.py``) rides under it."""
    s = state.seats[seat]
    rack = list(s.rack)
    drawn = state.drawn if (state.turn == seat and state.drawn in rack) else None
    if drawn is not None:
        rack.remove(drawn)
    shown = rack_str(sort_rack(rack))
    if drawn is not None:
        shown += f"   ➜ **{tile_str(drawn)}**"
    items: list[discord.ui.Item[Any]] = [_text(f"## {ICON} Your Rack\n{shown or '*empty*'}")]
    if s.exposures:
        items.append(_text(
            "**Your Exposures**\n"
            + "   ".join(
                " ".join([tile_str(x.natural)] * (x.count - x.jokers)
                         + [tile_str(Tile.JOKER)] * x.jokers)
                for x in s.exposures
            )
        ))
    turn_note = context or (
        "**It's your turn.**" if state.turn == seat
        and state.phase is Phase.AWAIT_DISCARD
        else ""
    )
    if turn_note:
        items.append(_text(f"**Now**\n{turn_note}{_clock(deadline_at)}"))
    if assist is not None:
        items.append(_text(f"### Closest Hands\n{_assist_field(assist)}"))
    items.append(_footer(f"Hand {state.hand_no}"))
    return items


# ── the announcements ────────────────────────────────────────────────────────


def build_mahjong_reveal(
    out: Outcome, winner_name: str, winning_tiles: str,
) -> list[discord.ui.Item[Any]]:
    """The winning hand. **Paradigm 7 — notice.** The embed era's
    results-green is gone with every other colour; 🎉 and the words say it."""
    how = {
        "discard": "claimed from the discard pit",
        "self_pick": "self-picked",
    }.get(out.won_by or "", "won")
    # out.line_name is admin-uploaded free text (card_logic.py has no length
    # cap on it) — escape first, then fit. Reached only for a mahjong
    # outcome, which always names its line, so ``or ""`` guards the raise.
    line = escape_to_fit(out.line_name or "", NAME_LIMIT)
    return [
        _text(
            f"## {ICON} Mahjong — {line}\n"
            f"🎉 **{winner_name}** {how} for **{out.value}** points"
            + (" — **jokerless, doubled!**" if out.jokerless_double else "")
            + f"\n\n{winning_tiles}"
        ),
        _footer(out.line_id or ""),
    ]


def build_settlement(
    out: Outcome, names: dict[int, str], stake: int,
) -> list[discord.ui.Item[Any]]:
    """The payout table. **Paradigm 7 — notice.**"""
    if out.kind == "wall_game":
        return [
            _text(
                f"## {ICON} Wall Game\n"
                "The wall ran dry with nobody out — every escrow returns untouched."
            ),
            _footer("Rematch?"),
        ]
    if out.kind == "all_fallow":
        return [
            _text(
                f"## {ICON} Table Timed Out\n"
                "Every seat folded together — the hand voids and escrow returns."
            ),
            _footer(),
        ]

    width = max((len(n) for n in names.values()), default=4)
    rows = []
    for seat in sorted(out.point_deltas):
        # names is escaped nowhere in this builder on purpose — this row
        # renders inside a ``` fence below, where escape_markdown would only
        # print literal backslashes. What the fence IS vulnerable to is a
        # member name closing it early with a run of backticks; code_safe
        # neutralises just that (fences_only — a lone backtick is inert here
        # and left as typed) without touching length, so `width` above still
        # lines the columns up.
        name = code_safe(names.get(seat, f"Seat {seat + 1}"), fences_only=True)
        delta = out.point_deltas[seat] * stake
        rows.append(f"{name:<{width}}  {delta:+7d} coins")
    note = []
    duel = len(out.point_deltas) == 2
    if out.won_by == "discard" and out.discarder is not None:
        # The note sits after the closing ``` fence, in live markdown —
        # unlike the per-seat rows above, which render inside the fence and
        # must stay unescaped.
        discarder = discord.utils.escape_markdown(names.get(out.discarder, "?"))
        note.append(
            f"discard win 2× — {discarder} fed the tile"
            if duel else
            f"discard win — {discarder} pays 2×, others 1×"
        )
    if out.won_by == "self_pick":
        note.append("self-pick 3×" if duel else "self-pick — everyone pays 2×")
    if out.jokerless_double:
        note.append("jokerless: doubled")
    return [
        _text(
            f"## {ICON} Settlement\n```\n" + "\n".join(rows) + "\n```"
            + (" · ".join(note) if note else "")
        ),
        _footer(f"{out.value} points × {stake}/point"),
    ]


def build_joker_redeemed(
    redeemer: str, owner: str, tile: Tile,
) -> list[discord.ui.Item[Any]]:
    """**Paradigm 7 — notice.** Names arrive escaped."""
    return [
        _text(
            f"## {ICON} Joker Redeemed\n"
            f"**{redeemer}** swapped a natural {tile_str(tile)} for the joker "
            f"in **{owner}**'s exposure — it stands natural now."
        ),
        _footer(),
    ]


# ── the study surfaces ───────────────────────────────────────────────────────


def build_how_to_play(
    card: Card | None, settings, manual_url: str = "",
) -> list[discord.ui.Item[Any]]:
    """The /mahjong How to Play panel — enough to sit down and play one
    hand, in the order a newcomer meets it. Deliberately not the rules
    reference: the Card Viewer holds the season's hands and the manual
    holds the detail, both linked from the last block."""
    helpers = [
        "**Card Viewer** — every hand in season, with its point value.",
        "**My Settings** — how much help you want: from nothing, up to a "
        "coach naming your closest hand and a tile to throw.",
        "**Open Rack** on the table card — your tiles, your options, and "
        "whose turn it is. It keeps itself up to date.",
    ]
    if card is not None:
        # card.display_name is admin-uploaded free text (no length cap) —
        # escape first, then fit.
        name = escape_to_fit(card.display_name, NAME_LIMIT)
        helpers.insert(0, f"This season: **{name}**, {len(card.hands)} hands.")
    if manual_url:
        # served at /static/manual.html (server.py gates exactly this
        # path behind login); the §10 heading anchors as #mahjong
        helpers.append(
            f"Full guide: {manual_url}/static/manual.html#mahjong")
    return [
        _text(
            f"## {ICON} How to Play Meadow Mahjong\n"
            "Build **fourteen tiles** into one of the patterns on the season's "
            "card. You never invent a hand — you pick a line off the card and "
            "chase it."
        ),
        _text(
            "### A Hand, Start to Finish\n"
            "**1. Sit down.** Create a table or press Join on someone's; the "
            "stake is escrowed when you sit.\n"
            "**2. Charleston.** Three rounds of passing three unwanted tiles "
            "around the table, then an optional second Charleston if everyone "
            "agrees, then a courtesy swap of up to three with the player "
            "opposite.\n"
            "**3. Play.** On your turn you draw, then discard. Everyone else "
            "gets a moment to claim what you throw.\n"
            "**4. Win.** Complete your line and press 🀄 Mahjong."
        ),
        _text(
            "### Claiming a Discard\n"
            "Any discard is fair game for a moment. **✋ Call** takes it to "
            "complete a set of three or more, which you then expose face-up "
            "for everyone to see. **🀄 Mahjong** takes it to win. A pair can "
            "never be called — only the winning tile breaks that rule. "
            "Mahjong beats a call, and a call beats waiting your turn."
        ),
        _text(
            "### Jokers\n"
            "A joker stands in for any tile inside a group of **three or "
            "more** — never in a pair or a single. If someone's exposed set "
            "contains a joker and you hold the real tile, you can swap for it "
            "on your turn. Winning with no jokers at all pays double."
        ),
        _text(
            "### Clocks\n"
            f"Your turn: **{int(settings.turn_timer)}s**. Passing and other "
            f"shared phases: **{int(settings.phase_timer)}s**. Claiming a "
            f"discard: **{int(settings.claim_window(2))}–"
            f"{int(settings.claim_window(4))}s**, so tap quickly. "
            "Miss three of your own turns and your seat folds for the hand."
        ),
        _text("### Where to Look\n" + "\n".join(helpers)),
        _footer("New here? Try 🌱 Practice — free, against house bots"),
    ]


def build_card_viewer(card: Card) -> list[list[discord.ui.Item[Any]]]:
    """The active card by section, for study — paged to respect the card
    budget, one message per page.

    A hand's notes and parity lock ride every line (grammar-verify G1: the
    web viewer showed notes but Discord silently didn't, so a member could
    build an odd-locked hand at an even x and be refused with no visible
    reason). Long sections split across continuation blocks rather than
    being sliced mid-hand.
    """
    def line(h) -> str:
        # h.name/h.notes are admin-uploaded free text (card_logic.py has no
        # markdown sanitization or length cap on either) — h.display is a
        # generated string sitting inside the code span, exempt. Escape the
        # member halves where they enter, then trim the composed line as a
        # safety net so one escaped-oversized hand can never exceed a block.
        name = discord.utils.escape_markdown(h.name)
        out = f"`{h.display}` — **{name}** · {h.value}"
        if h.concealed:
            out += " · C"
        extra = discord.utils.escape_markdown(h.notes)
        if h.x_parity and h.x_parity not in extra:
            lock = f"{h.x_parity} x only"
            extra = f"{extra}; {lock}" if extra else lock
        if extra:
            out += f" — *{extra}*"
        return trim_escaped(out, VIEWER_BLOCK_LIMIT)

    title = escape_to_fit(card.display_name, NAME_LIMIT)
    heading = (
        f"## {ICON} {title}\nSeason {card.season} — {len(card.hands)} hands. "
        "`C` hands must stay concealed (claim only for Mahjong)."
    )
    pages: list[list[discord.ui.Item[Any]]] = []
    page: list[discord.ui.Item[Any]] = [_text(heading)]
    used = len(heading)

    def flush() -> None:
        nonlocal page, used
        page.append(_footer(card.season))
        pages.append(page)
        page = [_text(f"## {ICON} {title} (cont.)")]
        used = len(title) + 20

    for section in card.sections():
        hands = [h for h in card.hands if h.section == section]
        chunks: list[str] = []
        current = ""
        for h in hands:
            piece = line(h)
            if current and len(current) + 1 + len(piece) > VIEWER_BLOCK_LIMIT - 40:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece
        if current:
            chunks.append(current)
        for i, chunk in enumerate(chunks):
            name = section if i == 0 else f"{section} (cont.)"
            block = f"### {name}\n{chunk}"
            if used + len(block) > VIEWER_PAGE_LIMIT and len(page) > 1:
                flush()
            page.append(_text(block))
            used += len(block)
    page.append(_footer(card.season))
    pages.append(page)
    return pages


def build_my_stats(rows: list[dict]) -> list[discord.ui.Item[Any]]:
    """**Paradigm 2 — panel.**"""
    if not rows:
        return [
            _text(f"## {ICON} Your Mahjong Record\nNo hands on record yet — pull up a chair."),
            _footer(),
        ]
    lines = []
    for row in rows:
        mode = MODE_NAMES.get(row["mode"], str(row["mode"]))
        lines.append(
            f"**{mode}** — {row['wins']}/{row['hands_played']} hands won"
            f" · {row['jokerless_wins']} jokerless\n"
            f"+{row['coins_won']} / -{row['coins_lost']} coins"
            f" · best win {row['biggest_win']}"
        )
    return [_text(f"## {ICON} Your Mahjong Record\n" + "\n\n".join(lines)), _footer()]
