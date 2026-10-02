"""Render tests for the mahjong cards — stage 6 of docs/plans/meadow-mahjong.md,
Components V2 since 2026-09-30.

Cards are pure (state + names in, layout items out), so every phase's table
panel is rendered here against real engine states — a render crash is the
classic cog bug, and none of these need a Discord mock. Rules stay untested
here; stage 3 owns them.
"""

from __future__ import annotations

import random
from typing import Any

import discord
import pytest

from bot_modules.core.layout import MAX_CHARS, card, check_budget
from bot_modules.games.mahjong import game_logic as G
from bot_modules.games.mahjong import layouts as mj_layouts
from bot_modules.games.mahjong.card_logic import load_card, load_first_light
from bot_modules.games.mahjong.game_logic import Phase, TableConfig
from bot_modules.games.mahjong.tiles import Tile, shuffled_wall

CARD = load_first_light()


@pytest.fixture(autouse=True)
def _text_chips(monkeypatch):
    # Chip-text assertions are written against the empty-map launch state.
    # Once prod registration populates tile_emoji.json (committed, so the
    # runner sees it too), the ambient map would render <:mm_5b:…> markup
    # instead of "5B" — every test pins the map it means to test. The
    # overflow test overrides this with its own full fake map.
    from bot_modules.games.mahjong import tile_render
    monkeypatch.setattr(tile_render, "_map_cache", {})
NAMES = {100: "Wren", 101: "Moss", 102: "Fern", 103: "Sage"}


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _block(items, heading: str, stops=("\n### ", "\n**", "\n-# ")) -> str | None:
    """The text after ``heading`` up to the next block, or None."""
    text = _text(items)
    if heading not in text:
        return None
    after = text.split(heading, 1)[1]
    for stop in stops:
        if stop in after:
            after = after.split(stop, 1)[0]
    return after.strip("\n")


def rng():
    return random.Random(5)


def state_seq():
    """One state per phase, driven through the real engine."""
    out = {}
    state = G.create_table(TableConfig(seat_count=2), 100)
    out["lobby_open"] = state
    state, _ = G.join_table(state, 101)
    out["lobby_full"] = state
    state, _ = G.deal(state, shuffled_wall(rng()))
    out["charleston"] = state
    r = rng()
    for _ in range(3):
        for s in range(2):
            picks = [t for t in state.seats[s].rack if t is not Tile.JOKER][:3]
            state, _ = G.charleston_pick(state, s, picks, 0, r)
    out["vote"] = state
    state, _ = G.vote_second_charleston(state, 0, False)
    out["courtesy_propose"] = state
    state, _ = G.courtesy_propose(state, 0, 1)
    state, _ = G.courtesy_propose(state, 1, 2)
    out["courtesy_pick"] = state
    give0 = [t for t in state.seats[0].rack if t is not Tile.JOKER][:1]
    give1 = [t for t in state.seats[1].rack if t is not Tile.JOKER][:1]
    state, _ = G.courtesy_pick(state, 0, give0)
    state, _ = G.courtesy_pick(state, 1, give1)
    out["await_discard"] = state
    tile = state.seats[0].rack[0]
    state, _ = G.discard(state, 0, tile, CARD)
    out["claim_window"] = state
    state, _ = G.timeout(state, CARD, r)
    out["mid_play"] = state
    return out


STATES = state_seq()


@pytest.mark.parametrize("phase_name", sorted(STATES))
def test_table_panel_renders_every_phase(phase_name):
    state = STATES[phase_name]
    items = mj_layouts.build_table_panel(state, NAMES, stake=1, escrow=450, deadline_at=None)
    text = _text(items)
    assert text.startswith(f"## {mj_layouts.ICON} Mahjong Table — Duel")
    assert text.rstrip().splitlines()[-1].startswith("-# 🀄 Meadow Mahjong")
    check_budget(card(items))


def test_lobby_panel_shows_open_seats_and_escrow():
    text = _text(mj_layouts.build_table_panel(STATES["lobby_open"], NAMES, stake=2, escrow=900))
    assert "open seat" in text and "900" in text


def test_claim_window_panel_names_the_discarder():
    text = _text(mj_layouts.build_table_panel(STATES["claim_window"], NAMES, stake=1, escrow=450))
    assert "Claim Window" in text and "Wren" in text


def test_settled_and_closed_panels_render():
    settled = G.GameState(
        config=TableConfig(seat_count=2), host=100,
        seats=[G.SeatState(member_id=100, rack=[Tile("1d")] * 13),
               G.SeatState(member_id=101, rack=[Tile("9c")] * 13)],
        phase=Phase.SETTLE, hand_no=1,
        outcome=G.Outcome(kind="mahjong", winner=1, line_id="gh-1",
                          line_name="Golden Hour", value=25, won_by="discard",
                          jokerless_double=True, discarder=0,
                          point_deltas={0: -100, 1: 100}),
    )
    text = _text(mj_layouts.build_table_panel(settled, NAMES, 1, 450))
    assert "Golden Hour" in text and "Rematch" in text
    closed, _ = G.close_table(settled, "finished")
    text = _text(mj_layouts.build_table_panel(closed, NAMES, 1, 450))
    assert text.endswith("-# 🀄 Meadow Mahjong • Closed")


def test_phase_controls_are_all_grey():
    """The destructive presses (Cancel, Close Table) are on the host panel
    now (test_mahjong_host_panel.py); nothing on the card is red."""
    styles = {}
    for phase in Phase:
        for b in card(mj_layouts.phase_controls(42, phase, add_bot=True)).walk_children():
            if isinstance(b, discord.ui.Button):
                styles[b.custom_id] = b.style
    assert set(styles.values()) == {discord.ButtonStyle.secondary}
    assert mj_layouts.phase_controls(42, Phase.CLOSED) == [] and mj_layouts.phase_controls(42, None) == []


def test_rack_panel_highlights_the_drawn_tile():
    state = STATES["mid_play"]
    seat = state.turn
    assert state.drawn is not None
    assert "➜" in _text(mj_layouts.build_rack_panel(state, seat))
    other = (seat + 1) % 2
    assert "➜" not in _text(mj_layouts.build_rack_panel(state, other))


def test_settlement_card_monospace_table_and_notes():
    out = G.Outcome(kind="mahjong", winner=1, line_id="gh-1",
                    line_name="Golden Hour", value=25, won_by="discard",
                    jokerless_double=True, discarder=0,
                    point_deltas={0: -100, 1: 100})
    text = _text(mj_layouts.build_settlement(out, {0: "Wren", 1: "Moss"}, stake=2))
    assert "```" in text and "+200" in text and "-200" in text
    assert "jokerless: doubled" in text
    assert "discard win 2×" in text  # §6.1: the exact line used, Duel form

    wall = G.Outcome(kind="wall_game", winner=None, line_id=None,
                     line_name=None, value=0, won_by=None,
                     jokerless_double=False, discarder=None,
                     point_deltas={0: 0, 1: 0})
    assert "escrow" in _text(mj_layouts.build_settlement(wall, NAMES, 1))
    void = G.Outcome(kind="all_fallow", winner=None, line_id=None,
                     line_name=None, value=0, won_by=None,
                     jokerless_double=False, discarder=None,
                     point_deltas={0: 0, 1: 0})
    assert "voids" in _text(mj_layouts.build_settlement(void, NAMES, 1))


def test_settlement_note_escapes_the_discarder_but_not_the_fenced_rows():
    """The discard-win note sits after the closing ``` fence — live
    markdown, so the discarder's name there must be escaped. The per-seat
    rows inside the fence render no markdown at all, so they must not be —
    escaping there would show members literal backslashes."""
    out = G.Outcome(kind="mahjong", winner=1, line_id="gh-1",
                    line_name="Golden Hour", value=25, won_by="discard",
                    jokerless_double=False, discarder=0,
                    point_deltas={0: -100, 1: 100})
    text = _text(mj_layouts.build_settlement(out, {0: "**Bold**", 1: "Moss"}, stake=2))
    fence = text.split("```")[1]
    note = text.split("```")[2]
    assert "**Bold**" in fence  # unescaped inside the code fence
    assert r"\*\*Bold\*\*" in note  # escaped in the live markdown note
    assert "**Bold**" not in note


def test_settlement_fenced_rows_neutralize_backtick_breakout_not_lone_backticks():
    """The fenced rows render no markdown (the note above this documents why
    they stay unescaped) but a run of 3+ backticks in a name closes the
    fence early and puts everything after it back in live markdown —
    code_safe(fences_only=True) neutralises just that run and leaves a lone
    backtick, which is inert inside a fence, exactly as typed."""
    out = G.Outcome(kind="mahjong", winner=1, line_id="gh-1",
                    line_name="Golden Hour", value=25, won_by=None,
                    jokerless_double=False, discarder=None,
                    point_deltas={0: -100, 1: 100})
    text = _text(mj_layouts.build_settlement(out, {0: "Evil```# pwned", 1: "Moss"}, stake=2))
    assert text.count("```") == 2  # only the builder's own opening/closing fence
    assert "Evil" in text and "pwned" in text  # name still renders, neutralised

    text2 = _text(mj_layouts.build_settlement(out, {0: "O`Brien", 1: "Moss"}, stake=2))
    assert "O`Brien" in text2.split("```")[1]  # a lone backtick is inert in a fence


def test_reveal_and_table_panel_escape_and_fit_line_name():
    """Hand.name (out.line_name) is admin-uploaded card JSON free text —
    card_logic.py caps neither its markdown nor its length — so both render
    sites must escape it, and since escaping can double an oversized name
    past the surface cap, both must fit too."""
    injected = "**Grand Slam**\n# EVERYONE READ THIS"
    out = G.Outcome(kind="mahjong", winner=0, line_id="gs-1",
                    line_name=injected, value=25, won_by="self_pick",
                    jokerless_double=False, discarder=None,
                    point_deltas={0: 100, 1: -100})

    reveal = _text(mj_layouts.build_mahjong_reveal(out, "Wren", "1D 1D 3D 3D"))
    assert r"\*\*Grand Slam\*\*" in reveal and "**Grand Slam**" not in reveal

    settled = G.GameState(
        config=TableConfig(seat_count=2), host=100,
        seats=[G.SeatState(member_id=100, rack=[Tile("1d")] * 13),
               G.SeatState(member_id=101, rack=[Tile("9c")] * 13)],
        phase=Phase.SETTLE, hand_no=1, outcome=out,
    )
    hand_over = _block(mj_layouts.build_table_panel(settled, NAMES, 1, 450), "**Hand Over**\n")
    assert hand_over and r"\*\*Grand Slam\*\*" in hand_over and "**Grand Slam**" not in hand_over

    # all-markdown and oversized: escaping doubles it, both sites must still fit
    huge = "*" * 500
    out2 = G.Outcome(kind="mahjong", winner=0, line_id="gs-2", line_name=huge,
                     value=25, won_by="self_pick", jokerless_double=False,
                     discarder=None, point_deltas={0: 100, 1: -100})
    reveal2 = mj_layouts.build_mahjong_reveal(out2, "Wren", "tiles")
    assert len(_text(reveal2).splitlines()[0]) <= mj_layouts.NAME_LIMIT + 40

    settled2 = G.GameState(config=settled.config, host=100, seats=settled.seats,
                           phase=Phase.SETTLE, hand_no=1, outcome=out2)
    check_budget(card(mj_layouts.build_table_panel(settled2, NAMES, 1, 450)))


def test_card_viewer_escapes_hand_free_text_and_keeps_every_block_in_budget():
    """Hand.name/Hand.notes are admin-uploaded card JSON free text — a
    spoiler or an unclosed backtick in either must not reach the reader
    live, and since escaping can double an oversized name+notes pair, the
    composed line must still fit a block even then."""
    data = {
        "card_id": "inj", "display_name": "Injection Card", "season": "2026",
        "hands": [{
            "id": "h1", "section": "Test", "concealed": False, "value": 30,
            "name": "Straight ||Flush||", "notes": "*" * 1200,
            "groups": [{"count": 4, "rank": "F"}],
        }],
    }
    pages = mj_layouts.build_card_viewer(load_card(data))
    text = " ".join(_text(p) for p in pages)
    assert r"\|\|Flush\|\|" in text and "||Flush||" not in text
    for page in pages:
        check_budget(card(page))


def test_display_name_is_escaped_and_fit_at_every_site():
    """Card.display_name is admin-uploaded card JSON free text with no
    length cap — every render site (viewer title, member-panel body,
    how-to-play block) must escape it, and an oversized name must still fit
    its own cap."""
    from bot_modules.games.mahjong.mahjong_service import MahjongSettings

    data = {
        "card_id": "inj2",
        "display_name": "Season __One__ [Click](https://evil.example)",
        "season": "2026",
        "hands": [{
            "id": "h1", "section": "Test", "concealed": False, "value": 30,
            "name": "Hand", "groups": [{"count": 4, "rank": "F"}],
        }],
    }
    season = load_card(data)
    for text in (
        _text(mj_layouts.build_card_viewer(season)[0]),
        _text(mj_layouts.build_member_panel(season, (1,), 100)),
        _text(mj_layouts.build_how_to_play(season, MahjongSettings(), "")),
    ):
        assert r"\_\_One\_\_" in text and "__One__" not in text

    data["display_name"] = "_" * 500
    huge = load_card(data)
    for items in (
        mj_layouts.build_card_viewer(huge)[0],
        mj_layouts.build_member_panel(huge, (1,), 100),
        mj_layouts.build_how_to_play(huge, MahjongSettings(), ""),
    ):
        check_budget(card(items))


def test_reveal_and_redeem_and_stats_and_viewer():
    out = G.Outcome(kind="mahjong", winner=0, line_id="qp-1",
                    line_name="Quiet Pairs", value=50, won_by="self_pick",
                    jokerless_double=False, discarder=None,
                    point_deltas={0: 150, 1: -150})
    reveal = _text(mj_layouts.build_mahjong_reveal(out, "Wren", "1D 1D 3D 3D"))
    assert reveal.startswith(f"## {mj_layouts.ICON} Mahjong — Quiet Pairs") and "self-picked" in reveal

    assert "5B" in _text(mj_layouts.build_joker_redeemed("Wren", "Moss", Tile("5b")))

    stats = _text(mj_layouts.build_my_stats(
        [{"mode": 2, "hands_played": 4, "wins": 2, "jokerless_wins": 1,
          "coins_won": 300, "coins_lost": 100, "biggest_win": 150}]))
    assert "**Duel** — 2/4 hands won" in stats
    assert "chair" in _text(mj_layouts.build_my_stats([]))

    pages = mj_layouts.build_card_viewer(CARD)
    text = "\n".join(_text(p) for p in pages)
    for section in CARD.sections():
        assert f"### {section}" in text
    assert "Golden Hour" in text  # a hand name made it through
    for page in pages:
        check_budget(card(page))
        assert card(page).content_length() <= MAX_CHARS


def test_member_panel_variants():
    text = _text(mj_layouts.build_member_panel(CARD, (1, 2, 5), 250))
    assert "First Light" in text and "250" in text
    assert "No Meadow Card" in _text(mj_layouts.build_member_panel(None, (1,), 0))
    ids = [b.custom_id for b in card(mj_layouts.build_member_panel(None, (1,), 0)).walk_children() if isinstance(b, discord.ui.Button)]
    assert ids == [
        mj_layouts.PANEL_CREATE_ID, mj_layouts.PANEL_CARD_ID, mj_layouts.PANEL_STATS_ID,
        mj_layouts.PANEL_SETTINGS_ID, mj_layouts.PANEL_HELP_ID,
    ]


# ── Assist block on the rack panel (plans/mahjong-assist.md stage 3) ─────────


def _assist_state():
    from tests.games.test_mahjong_game_logic import play_state
    return play_state(2, {0: "flower*4 2d*4 6b*2 9d wn", 1: "9c*13"})


def _readout(mode: str):
    state = _assist_state()
    return state, G.assist_readout(state, 0, CARD, mode)


def _rack_block(items) -> str | None:
    # the readout's own lines lead with bold, so only a heading or the
    # footer ends the block
    return _block(items, "### Closest Hands\n", stops=("\n### ", "\n-# "))


def test_rack_panel_without_assist_has_no_block():
    assert _rack_block(mj_layouts.build_rack_panel(_assist_state(), 0, assist=None)) is None


def test_assist_target_names_lines_and_distance_only():
    state, r = _readout("target")
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=r))
    assert block is not None
    assert r.prospects[0].hand.name in block
    assert "away" in block
    assert "need" not in block
    assert "discard" not in block.lower()


def test_assist_gap_adds_the_needed_tiles():
    state, r = _readout("gap")
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=r))
    assert block is not None and "need" in block
    assert "discard" not in block.lower()


def test_assist_coach_adds_dead_weight_and_suggestion():
    state, r = _readout("coach")
    assert r is not None and r.suggestion is not None
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=r))
    assert block is not None
    assert "Dead weight" in block
    assert "Consider discarding" in block


def test_assist_coach_silence_is_explicit():
    # Rail-silenced suggestion still shows dead weight, with the your-call note.
    state, r = _readout("coach")
    silenced = G.AssistReadout(
        mode="coach", prospects=r.prospects, live_count=r.live_count,
        suggestion=None,
    )
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=silenced))
    assert block is not None
    assert "No clearly safe discard" in block
    assert "Consider discarding" not in block


def test_assist_all_dead_says_play_for_the_wall():
    state = _assist_state()
    empty = G.AssistReadout(mode="gap", prospects=(), live_count=0, suggestion=None)
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=empty))
    assert block is not None and "play for the wall" in block


def test_assist_block_keeps_its_bound():
    for mode in ("target", "gap", "coach"):
        state, r = _readout(mode)
        block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=r))
        assert block is not None and len(block) <= mj_layouts.ASSIST_LIMIT


# ── Stage-4 review findings: honest dead weight + overflow-proof block ───────


def _fake_prospect(hand, distance, needed, dead_weight):
    from bot_modules.games.mahjong.match_logic import Prospect
    return Prospect(hand=hand, distance=distance, needed=tuple(needed),
                    dead_weight=tuple(dead_weight))


def test_dead_weight_is_the_intersection_of_shown_hands():
    # A tile hand #2 still NEEDS must never print as dead weight: the copy
    # promises "tiles none of your closest hands can use".
    h1, h2 = CARD.hands[0], CARD.hands[1]
    p1 = _fake_prospect(h1, 4, [(Tile("8c"), 2)],
                        [(Tile("9d"), 1), (Tile("wn"), 1)])
    p2 = _fake_prospect(h2, 5, [(Tile("wn"), 1)], [(Tile("9d"), 1)])
    r = G.AssistReadout(mode="coach", prospects=(p1, p2), live_count=2,
                        suggestion=None)
    block = _rack_block(mj_layouts.build_rack_panel(_assist_state(), 0, assist=r))
    assert block is not None
    dead_line = next(ln for ln in block.split("\n") if ln.startswith("Dead weight"))
    assert "9D" in dead_line
    assert "N" not in dead_line  # hand #2 needs the wind — not dead


def test_assist_block_survives_the_registered_emoji_map(monkeypatch):
    # Once prod emoji registration runs, every chip becomes ~28 chars of
    # <:mm_xx:snowflake> markup. The block must still fit its bound WITHOUT
    # a blind slice: degrade to text chips rather than cut mid-token and
    # silently drop the coach suggestion (review finding, stage 4).
    from bot_modules.games.mahjong import tile_render

    fake_map = {t.code: 1400000000000000001 for t in Tile}
    fake_map["back"] = 1400000000000000001
    monkeypatch.setattr(tile_render, "_map_cache", fake_map)

    # Adversarial worst case: three far lines with maximal need lists.
    hands = CARD.hands[:3]
    wide = [(t, 2) for t in list(Tile)[:10]]
    prospects = tuple(
        _fake_prospect(h, 14, wide, [(Tile("9d"), 2), (Tile("wn"), 1)])
        for h in hands
    )
    r = G.AssistReadout(mode="coach", prospects=prospects, live_count=22,
                        suggestion=Tile("9d"))
    items = mj_layouts.build_rack_panel(_assist_state(), 0, assist=r)
    block = _rack_block(items)
    assert block is not None
    assert len(block) <= mj_layouts.ASSIST_LIMIT
    # the advice coach exists to deliver is never the part that gets cut
    assert "Consider discarding" in block
    # and no half-sliced emoji token renders as garbage
    assert block.count("<:") == block.count(">") or "<:" not in block
    check_budget(card(items))


def test_card_viewer_shows_notes_and_parity_and_slices_nothing():
    # Grammar-verify G1: the odd/even locks and "these #s only" annotations
    # must be visible in Discord, not just on the web viewer — and long
    # sections split across blocks (and pages) instead of slicing mid-hand.
    from bot_modules.games.mahjong.card_logic import load_card_file
    from bot_modules.games.mahjong.card_logic import FIRST_LIGHT_PATH

    season = load_card_file(FIRST_LIGHT_PATH.parent / "meadow_harvest.json")
    pages = mj_layouts.build_card_viewer(season)
    text = "\n".join(_text(p) for p in pages)
    assert "odd like numbers" in text
    assert "any 2 dragons" in text
    assert text.count("`") // 2 - text.count("`C`") == len(season.hands)   # nothing sliced away
    for page in pages:
        check_budget(card(page))
        assert card(page).content_length() <= MAX_CHARS
    if len(pages) > 1:
        assert _text(pages[1]).startswith(f"## {mj_layouts.ICON} ") and "(cont.)" in _text(pages[1])


def test_how_to_play_covers_the_basics_and_the_live_clocks():
    from bot_modules.games.mahjong.mahjong_service import MahjongSettings

    settings = MahjongSettings(turn_timer=120.0, phase_timer=60.0,
                               claim_window_2=6.0, claim_window_4=8.0)
    items = mj_layouts.build_how_to_play(CARD, settings, "https://dash.example")
    text = _text(items)
    headings = [line for line in text.splitlines() if line.startswith("### ")]
    assert headings == ["### A Hand, Start to Finish", "### Claiming a Discard",
                        "### Jokers", "### Clocks", "### Where to Look"]
    # the clocks are the guild's real dials, not prose constants
    assert "120s" in text and "60s" in text and "6–8s" in text
    # the season's actual card, and a link that matches the served path
    assert CARD.display_name in text and "22 hands" in text
    assert "https://dash.example/static/manual.html#mahjong" in text
    check_budget(card(items))


def test_how_to_play_survives_no_card_and_no_public_dashboard():
    from bot_modules.games.mahjong.mahjong_service import MahjongSettings

    text = _text(mj_layouts.build_how_to_play(None, MahjongSettings(), ""))
    assert "hands" not in text.split("### Where to Look")[-1].split("\n")[1]
    assert "Full guide" not in text          # localhost-only dev: no link
    assert "Card Viewer" in text             # the in-Discord pointers remain


def test_claim_advice_leads_the_block_and_survives_the_fit_loop():
    from tests.games.test_mahjong_game_logic import _claim_state

    # winning tile: the Mahjong call leads
    state = _claim_state("flower*4 2d*4 6b*4 8c", "8c")
    r = G.assist_readout(state, 0, CARD, "coach")
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=r))
    assert block is not None
    assert block.splitlines()[0].startswith("🀄")
    assert "Press Mahjong" in block

    # calling tile: the call advice leads, naming the group and the gain
    state = _claim_state("flower*4 2d*3 6b*4 8c 9b", "2d")
    r = G.assist_readout(state, 0, CARD, "coach")
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=r))
    assert block is not None
    first = block.splitlines()[0]
    assert first.startswith("✋") and "kong" in first and "away" in first

    # a rack with nothing live still shows the advice rather than only
    # "play for the wall"
    empty = G.AssistReadout(
        mode="coach", prospects=(), live_count=0, suggestion=None,
        claim_win=CARD.hands[0],
    )
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=empty))
    assert block is not None and block.splitlines()[0].startswith("🀄")
    assert "play for the wall" in block


def test_claim_advice_is_never_trimmed_by_the_fit_loop(monkeypatch):
    from bot_modules.games.mahjong import tile_render
    from tests.games.test_mahjong_game_logic import _claim_state

    monkeypatch.setattr(
        tile_render, "_map_cache",
        {t.code: 1400000000000000001 for t in Tile} | {"back": 1},
    )
    state = _claim_state("flower*4 2d*3 6b*4 8c 9b", "2d")
    r = G.assist_readout(state, 0, CARD, "coach")
    block = _rack_block(mj_layouts.build_rack_panel(state, 0, assist=r))
    assert block is not None and len(block) <= mj_layouts.ASSIST_LIMIT
    assert block.splitlines()[0].startswith("✋")   # the decision survives


# ── Claim window ticks (mahjong-145) ─────────────────────────────────────────


def _claim_block(state) -> str:
    block = _block(mj_layouts.build_table_panel(state, NAMES, 1, 450), "**Claim Window**\n")
    assert block is not None
    return block


@pytest.mark.parametrize("claims, tick", [
    pytest.param({}, "…", id="undecided"),
    pytest.param({1: (G.AUTO_PASS, [])}, "…", id="auto_pass"),
    pytest.param({1: ("pass", [])}, "✅", id="chose_pass"),
    pytest.param({1: ("call", [Tile("1c")])}, "✅", id="chose_call"),
])
def test_claim_window_ticks_only_what_a_seat_chose(claims, tick):
    # An auto-passed seat is one with no legal route to the tile; ticking it
    # the instant a discard lands told the whole table who could (or could
    # not) call or Mahjong it. Only a seat's own tap earns a tick.
    import copy

    state = copy.deepcopy(STATES["claim_window"])
    state.claims = claims
    assert f"Responses: {tick}" in _claim_block(state)


def test_auto_passed_seat_renders_exactly_like_an_undecided_one():
    import copy

    undecided = copy.deepcopy(STATES["claim_window"])
    undecided.claims = {}
    auto = copy.deepcopy(STATES["claim_window"])
    auto.claims = {1: (G.AUTO_PASS, [])}
    assert _claim_block(undecided) == _claim_block(auto)


# ── Turn nudges (mahjong-144) ────────────────────────────────────────────────


def test_nudge_lines_ping_the_human_on_draw_and_at_the_second_strike():
    from bot_modules.games.mahjong.bot_logic import bot_member_id
    from tests.games.test_mahjong_game_logic import play_state

    state = play_state(2, {0: "9c*13", 1: "8b*13"}, turn=1)
    events = [("strike", {"seat": 0, "strikes": 2}),
              ("tile_drawn", {"seat": 1, "wall_left": 3})]
    lines = mj_layouts.nudge_lines(state, events, turn_changed=True)
    assert [(m, "your draw" in t) for m, t, _ in lines] == [(100, False), (101, True)]
    assert all(t.startswith(f"<@{m}>") for m, t, _ in lines)
    assert "one more" in lines[0].content and "folds" in lines[0].content
    assert [n.warning for n in lines] == [True, False]
    # the warning stands while the seat is one miss from folding, and no longer
    state.seats[0].strikes = 2
    assert mj_layouts.warning_live(state, 100) is True
    state.seats[0].strikes = 0
    assert mj_layouts.warning_live(state, 100) is False
    state.seats[0].strikes = 2
    state.seats[0].fallow = True
    assert mj_layouts.warning_live(state, 100) is False
    assert mj_layouts.warning_live(state, 999) is False
    state.seats[0].fallow = False
    # same turn, nothing new (a joker redeem, say) → no re-ping
    assert mj_layouts.nudge_lines(state, [], turn_changed=False) == []
    # a first strike is not a warning
    assert mj_layouts.nudge_lines(
        state, [("strike", {"seat": 0, "strikes": 1})], turn_changed=False) == []
    # a bot seat is never pinged, and no draw line outside a turn
    state.seats[1].member_id = bot_member_id(7, 1)
    assert mj_layouts.nudge_lines(state, events, turn_changed=True) == [
        lines[0]]
    state.phase = Phase.CLAIM_WINDOW
    assert mj_layouts.nudge_lines(state, [], turn_changed=True) == []


# ── every card leads with the glyph and is bare ──────────────────────────────


@pytest.mark.parametrize("name", ["table", "rack", "reveal", "settlement", "redeem", "help", "stats", "member"])
def test_every_card_leads_with_a_glyph_heading(name):
    from bot_modules.games.mahjong.mahjong_service import MahjongSettings

    out = G.Outcome(kind="mahjong", winner=0, line_id="qp-1", line_name="Quiet Pairs",
                    value=50, won_by="self_pick", jokerless_double=False,
                    discarder=None, point_deltas={0: 150, 1: -150})
    items = {
        "table": mj_layouts.build_table_panel(STATES["mid_play"], NAMES, 1, 450),
        "rack": mj_layouts.build_rack_panel(STATES["mid_play"], 0),
        "reveal": mj_layouts.build_mahjong_reveal(out, "Wren", "tiles"),
        "settlement": mj_layouts.build_settlement(out, {0: "Wren", 1: "Moss"}, 1),
        "redeem": mj_layouts.build_joker_redeemed("Wren", "Moss", Tile("5b")),
        "help": mj_layouts.build_how_to_play(CARD, MahjongSettings(), ""),
        "stats": mj_layouts.build_my_stats([]),
        "member": mj_layouts.build_member_panel(CARD, (1,), 5),
    }[name]
    first = next(i for i in card(items).walk_children() if isinstance(i, discord.ui.TextDisplay))
    assert first.content.startswith(f"## {mj_layouts.ICON} ")
    assert not any(isinstance(i, discord.ui.Container) for i in card(items).walk_children())
