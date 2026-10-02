"""``bot_modules/casino/cog/layouts.py`` — the Components V2 cards.

These builders are pure, so they are testable exactly the way the embed
builders they replaced were (``tests/test_casino_embeds.py``, deleted with
this conversion): call one, walk the items, assert on the text. Mirrors
``tests/dm/test_dm_perms_layouts.py``'s pattern — a budget sweep, a paradigm
(glyph-led heading, no Container) sweep, and a ``name_fn``/no-raw-mention
sweep over one ``ALL_CARDS`` table, plus targeted tests for the branches a
blanket sweep can't see (win vs. loss text, pools' one-sided warning, the
big-win broadcast's own threshold gate).

The hub panel is **not** here: this module's ``hub_sections``/``TABLES``/
``casino_title`` are relocated-unchanged hub support consumed by
``hub_layout.py``, which owns the hub's own rendering and is covered by
``tests/casino/test_casino_hub_layout.py``.
"""

from __future__ import annotations

import sqlite3

import discord
import pytest

from bot_modules.casino.cog import layouts as L
from bot_modules.core.layout import check_budget
from bot_modules.casino import pools_logic, pools_metrics
from bot_modules.casino.service import CasinoSettings, MinesStep
from bot_modules.economy.service import EconSettings

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731

ECON = EconSettings(currency_emoji="🪙", currency_name="coin", currency_plural="coins")
SETTINGS = CasinoSettings()  # every table enabled, the shipped defaults
POOLS_SPEC = pools_metrics.SPECS["messages"]


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _all_text(items) -> str:
    """Every TextDisplay in the card, however deeply nested, joined."""
    return "\n".join(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _stats_row(**overrides) -> sqlite3.Row:
    fields = dict(
        plays=12, wagered=1000, returned=1200, streak=3,
        biggest_win=500, biggest_win_game="slots",
    )
    fields.update(overrides)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"CREATE TABLE s ({', '.join(f'{k}' for k in fields)})")
    conn.execute(f"INSERT INTO s ({cols}) VALUES ({placeholders})", list(fields.values()))
    row = conn.execute("SELECT * FROM s").fetchone()
    return row


#: Round-open bets: (user_id, description, amount).
_ROUND_BETS = [(11, "on red", 20), (22, "straight 7", 5)]
#: Settled bets: (user_id, description, amount, payout) — 0 payout = lost.
_RESULT_BETS = [(11, "on red", 20, 40), (22, "straight 7", 5, 0)]
_RESULT_BETS_ALL_LOST = [(11, "on red", 20, 0), (22, "straight 7", 5, 0)]
#: Pools payouts: (user_id, stake, payout).
_POOL_PAYOUTS = [(11, 100, 190), (22, 50, 0)]


#: Every builder, with arguments that exercise its optional branches, so the
#: render/name_fn/no-Container/heading sweeps below cover the whole module
#: from one table rather than one test per card.
ALL_CARDS = [
    pytest.param(
        lambda: L.build_comp_layout(ECON, 11, ("7", "7", "🍒"), 10, 50, "Two 7s!", name_fn=NAME_FN),
        id="comp-win",
    ),
    pytest.param(
        lambda: L.build_comp_layout(ECON, 11, ("🍒", "🍋", "🔔"), 10, 0, None, name_fn=NAME_FN),
        id="comp-blank",
    ),
    pytest.param(lambda: L.build_help_layout(ECON, SETTINGS), id="help"),
    pytest.param(
        lambda: L.build_coinflip_layout(ECON, 11, "heads", "heads", 10, 20, name_fn=NAME_FN).items,
        id="coinflip-win",
    ),
    pytest.param(
        lambda: L.build_coinflip_layout(
            ECON, 11, "heads", "tails", 10, 0, pot_after=400, name_fn=NAME_FN
        ).items,
        id="coinflip-loss",
    ),
    pytest.param(
        lambda: L.build_coinflip_spin_layout(ECON, 11, "heads", 10, name_fn=NAME_FN),
        id="coinflip-spin",
    ),
    pytest.param(
        lambda: L.build_slots_layout(ECON, 11, ("7", "7", "7"), 10, 1000, "Triple 7s!", jackpot_won=5000, name_fn=NAME_FN).items,
        id="slots-jackpot",
    ),
    pytest.param(
        lambda: L.build_slots_layout(ECON, 11, ("🍒", "🍋", "🔔"), 10, 0, None, pot_after=300, name_fn=NAME_FN).items,
        id="slots-loss",
    ),
    pytest.param(
        lambda: L.build_slots_spin_layout(ECON, 11, 10, ("7", None, None), name_fn=NAME_FN),
        id="slots-spin",
    ),
    pytest.param(
        lambda: L.build_jackpot_celebration_layout(ECON, 11, 5000, name_fn=NAME_FN),
        id="jackpot-celebration",
    ),
    pytest.param(
        lambda: L.build_blackjack_reveal_layout(ECON, 11, ["A♠", "K♥"], ["9♦", "8♣"], 10, name_fn=NAME_FN),
        id="blackjack-reveal",
    ),
    pytest.param(lambda: L.build_roulette_bounce_layout((14, 7)), id="roulette-bounce"),
    pytest.param(
        lambda: L.build_blackjack_layout(ECON, 11, ["A♠", "K♥"], ["9♦", "?"], 10, name_fn=NAME_FN).items,
        id="blackjack-live",
    ),
    pytest.param(
        lambda: L.build_blackjack_layout(
            ECON, 11, ["A♠", "K♥"], ["9♦", "8♣"], 10,
            outcome="blackjack", payout=25, name_fn=NAME_FN,
        ).items,
        id="blackjack-win",
    ),
    pytest.param(
        lambda: L.build_blackjack_layout(
            ECON, 11, ["9♠", "8♥"], ["9♦", "8♣"], 10,
            outcome="push", payout=0, pot_after=0, name_fn=NAME_FN,
        ).items,
        id="blackjack-push",
    ),
    pytest.param(
        lambda: L.build_blackjack_layout(
            ECON, 11, ["9♠", "8♥"], ["9♦", "Q♣"], 10, doubled=True,
            outcome="lose", payout=0, pot_after=200, name_fn=NAME_FN,
        ).items,
        id="blackjack-loss",
    ),
    pytest.param(
        lambda: L.build_roulette_round_layout(ECON, 1_750_000_000, _ROUND_BETS, name_fn=NAME_FN),
        id="roulette-round",
    ),
    pytest.param(
        lambda: L.build_roulette_round_layout(ECON, 1_750_000_000, [], name_fn=NAME_FN),
        id="roulette-round-empty",
    ),
    pytest.param(
        lambda: L.build_roulette_result_layout(ECON, 7, _RESULT_BETS, pot_after=100, name_fn=NAME_FN).items,
        id="roulette-result-win",
    ),
    pytest.param(
        lambda: L.build_roulette_result_layout(ECON, 0, [], name_fn=NAME_FN).items,
        id="roulette-result-nobody-bet",
    ),
    pytest.param(
        lambda: L.build_derby_round_layout(ECON, 1_750_000_000, _ROUND_BETS, name_fn=NAME_FN),
        id="derby-round",
    ),
    pytest.param(
        lambda: L.build_derby_race_layout([3, 5, 7, 9, 11, 2]),
        id="derby-race",
    ),
    pytest.param(
        lambda: L.build_derby_result_layout(ECON, 0, [3, 5, 7, 9, 11, 2], _RESULT_BETS, name_fn=NAME_FN).items,
        id="derby-result",
    ),
    pytest.param(
        lambda: L.build_baccarat_round_layout(ECON, 1_750_000_000, _ROUND_BETS, name_fn=NAME_FN),
        id="baccarat-round",
    ),
    pytest.param(
        lambda: L.build_baccarat_deal_layout(["A♠", "9♥"], ["K♦", "2♣"]),
        id="baccarat-deal",
    ),
    pytest.param(
        lambda: L.build_baccarat_result_layout(
            ECON, ["A♠", "9♥"], ["K♦", "2♣"], _RESULT_BETS, name_fn=NAME_FN
        ).items,
        id="baccarat-result",
    ),
    pytest.param(
        lambda: L.build_dice_round_layout(ECON, 1_750_000_000, _ROUND_BETS, name_fn=NAME_FN),
        id="dice-round",
    ),
    pytest.param(lambda: L.build_dice_tumble_layout(), id="dice-tumble"),
    pytest.param(
        lambda: L.build_dice_result_layout(ECON, (4, 4, 4), _RESULT_BETS_ALL_LOST, name_fn=NAME_FN).items,
        id="dice-result-triple",
    ),
    pytest.param(
        lambda: L.build_dice_result_layout(ECON, (1, 2, 3), _RESULT_BETS, pot_after=50, name_fn=NAME_FN).items,
        id="dice-result-win",
    ),
    pytest.param(
        lambda: L.build_keno_round_layout(ECON, 1_750_000_000, _ROUND_BETS, name_fn=NAME_FN),
        id="keno-round",
    ),
    pytest.param(lambda: L.build_keno_tumble_layout(), id="keno-tumble"),
    pytest.param(
        lambda: L.build_keno_result_layout(
            ECON, list(range(20)), _RESULT_BETS, name_fn=NAME_FN
        ).items,
        id="keno-result",
    ),
    pytest.param(
        lambda: L.build_war_layout(ECON, 11, "K♠", "9♦", 10, name_fn=NAME_FN).items,
        id="war-live",
    ),
    pytest.param(
        lambda: L.build_war_layout(
            ECON, 11, "K♠", "K♦", 10,
            war_player="Q♣", war_dealer="9♥",
            outcome="war_win", payout=30, streak=2, name_fn=NAME_FN,
        ).items,
        id="war-standoff-win",
    ),
    pytest.param(
        lambda: L.build_war_layout(
            ECON, 11, "K♠", "9♦", 10, outcome="retreat", payout=5, name_fn=NAME_FN
        ).items,
        id="war-retreat",
    ),
    pytest.param(
        lambda: L.build_war_layout(
            ECON, 11, "9♠", "K♦", 10, outcome="lose", pot_after=90, name_fn=NAME_FN
        ).items,
        id="war-loss",
    ),
    pytest.param(
        lambda: L.build_my_stats_layout(ECON, _stats_row(), 100, 500, 1_750_000_000, comp_amount=200),
        id="my-stats-played",
    ),
    pytest.param(
        lambda: L.build_my_stats_layout(ECON, None, 0, 500, 1_750_000_000, comp_amount=200, comp_claimed=True),
        id="my-stats-unplayed",
    ),
    pytest.param(
        lambda: L.build_pools_panel_layout(
            ECON, 1200.0, pools_logic.PoolSplit(400, 250), 1_750_000_000, "2026-08-03",
            spec=POOLS_SPEC,
        ),
        id="pools-panel-open",
    ),
    pytest.param(
        lambda: L.build_pools_panel_layout(
            ECON, 1200.0, pools_logic.PoolSplit(400, 0), 1_750_000_000, "2026-08-03",
            spec=POOLS_SPEC, closing_soon=True,
        ),
        id="pools-panel-one-sided",
    ),
    pytest.param(
        lambda: L.build_pools_panel_layout(
            ECON, 1200.0, pools_logic.PoolSplit(400, 250), 1_750_000_000, "2026-08-03",
            spec=POOLS_SPEC, closed=True,
        ),
        id="pools-panel-closed",
    ),
    pytest.param(
        lambda: L.build_pools_result_layout(
            ECON, "2026-08-03", 1250, 1200.0, "over", _POOL_PAYOUTS, 30,
            spec=POOLS_SPEC, name_fn=NAME_FN,
        ),
        id="pools-result",
    ),
    pytest.param(
        lambda: L.build_pools_result_layout(
            ECON, "2026-08-03", 1250, 1200.0, "over", [], 0,
            spec=POOLS_SPEC, chart=False, name_fn=NAME_FN,
        ),
        id="pools-result-no-bets-no-chart",
    ),
    pytest.param(lambda: L.build_pools_void_layout("2026-08-03", 250), id="pools-void-one-sided"),
    pytest.param(
        lambda: L.build_pools_void_layout("2026-08-03", 250, unmeasurable=True),
        id="pools-void-unmeasurable",
    ),
    pytest.param(
        lambda: L.build_mines_layout(
            ECON, 11,
            MinesStep(hand_id=1, stake=10, bombs=3, revealed=(0, 1), mult=150, next_mult=200),
            name_fn=NAME_FN,
        ).items,
        id="mines-live",
    ),
    pytest.param(
        lambda: L.build_mines_layout(
            ECON, 11,
            MinesStep(
                hand_id=1, stake=10, bombs=3, revealed=(0, 1, 2), mult=200,
                outcome="cashed", payout=20, topped=False, streak=1,
            ),
            name_fn=NAME_FN,
        ).items,
        id="mines-cashed",
    ),
    pytest.param(
        lambda: L.build_mines_layout(
            ECON, 11,
            MinesStep(
                hand_id=1, stake=10, bombs=3, revealed=(0,), mult=0,
                outcome="bombed", payout=0, pot_after=80,
                bomb_tiles=(1, 5, 9),
            ),
            name_fn=NAME_FN,
        ).items,
        id="mines-bombed",
    ),
    pytest.param(
        lambda: L.build_mines_layout(
            ECON, 11,
            MinesStep(hand_id=1, stake=10, bombs=3, outcome="refunded", payout=0),
            name_fn=NAME_FN, footer="Cashed out automatically after 5 minutes idle.",
        ).items,
        id="mines-refunded-with-footer",
    ),
]


# ── Every card renders, fits, and stays bare ────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    """Each builder's items are accepted by a LayoutView and carry text.

    discord.py validates placement on ``add_item`` — a MediaGallery loose
    where it shouldn't be would raise here rather than as a 400.
    """
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


def _lead_heading(items) -> str:
    """The card's first line — its heading, wherever the text display sits."""
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


def _heading_faults(heading: str) -> list[str]:
    """What is wrong with a card's lead line, per guide § Anatomy."""
    if not heading.startswith("## "):
        return [f"a card-level heading is a `## Title` line: {heading!r}"]
    if len(heading) < 4 or heading[3].isascii():
        return [f"lead the title with the feature's glyph: {heading!r}"]
    return []


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    """A card's own heading is one ``## Title`` line, led by a glyph — the
    style guide's Words rule, and every settled result additionally leads
    with one of the three outcome glyphs (✅/❌/➖)."""
    assert not _heading_faults(_lead_heading(build()))


@pytest.mark.parametrize(
    ("heading", "faulty"),
    [
        pytest.param("## ✅ 🎰 Golden Meadow Slots", False, id="glyph-led-title"),
        pytest.param("### Golden Meadow Slots", True, id="section-heading-as-title"),
        pytest.param("## Golden Meadow Slots", True, id="no-glyph"),
        pytest.param("Golden Meadow Slots", True, id="no-heading"),
    ],
)
def test_the_heading_sweep_can_actually_see_a_violation(heading, faulty):
    """The sweep above is born green; this is what proves it can go red."""
    assert bool(_heading_faults(heading)) is faulty


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    """A card is bare (guide § Paradigms): no accent, no coloured box —
    every casino card lost both when the embed pilot converted."""
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_writes_a_raw_mention(build):
    """Members are named through ``name_fn``.

    An embed/layout mention resolves from the *reading* client's cache, so a
    ``<@id>`` renders as a bare number to anyone who hasn't seen that member.
    """
    assert "<@" not in _all_text(build())


#: Cards excluded here either take no ``name_fn`` at all (a stats/help/
#: animation-frame card with no member to name), or the fixture bet list
#: this file feeds them happens to name nobody (an empty board, a bet list
#: with no winner to itemise, a chartless void with no positions).
_NO_MEMBER_NAMED = {
    "help", "my-stats-played", "my-stats-unplayed",
    "derby-race", "dice-tumble", "keno-tumble", "roulette-bounce",
    "pools-panel-open", "pools-panel-one-sided", "pools-panel-closed",
    "pools-void-one-sided", "pools-void-unmeasurable",
    "roulette-round-empty", "roulette-result-nobody-bet",
    "baccarat-deal", "pools-result-no-bets-no-chart", "dice-result-triple",
}


@pytest.mark.parametrize(
    "build",
    [c for c in ALL_CARDS if c.id not in _NO_MEMBER_NAMED],
)
def test_cards_naming_a_member_use_the_resolver(build):
    """Every builder that takes a member id renders it through ``name_fn``
    rather than dropping it silently."""
    assert "N11" in _all_text(build()) or "N22" in _all_text(build())


def test_every_casino_render_site_passes_a_resolver():
    """The other half of the contract above.

    ``name_fn`` is a required keyword-only argument on every builder that
    names a member — a render site that forgets one now fails loudly with a
    ``TypeError`` rather than silently reintroducing todo #90 — but this AST
    walk is kept as defense-in-depth anyway: it turns that crash into a named
    test failure at the exact call site, rather than a stack trace the next
    time the branch runs. This walks the two modules that actually render
    and requires every call to a name-taking builder to hand one over.
    ``round_layout``/``build_show`` are the ``_WindowUI`` indirections onto
    those same builders. ``tests/contracts/test_embed_name_render_sites.py`` (the
    repo-wide sweep) skips casino because this is that guard.
    """
    import ast
    import inspect
    import pathlib

    from bot_modules.casino.cog import cog as casino_cog
    from bot_modules.casino.cog import pools_panel

    needs = {"round_layout", "build_show"} | {
        name
        for name, fn in inspect.getmembers(L, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
    }
    missed = []
    for module in (casino_cog, pools_panel):
        # Explicit utf-8: these sources are full of em-dashes and the CI
        # runner is Windows, where the default encoding is cp1252.
        source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if called in needs and not any(
                kw.arg == "name_fn" for kw in node.keywords
            ):
                missed.append(
                    f"{module.__name__.rsplit('.', 1)[-1]}.py:"
                    f"{node.lineno} {called}()"
                )
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


# ── Budget: the fullest cards the feature can send ──────────────────────


def test_help_panel_fits_with_every_table_open():
    """The explainer names every open table's odds — the fullest it gets."""
    check_budget(_view(L.build_help_layout(ECON, SETTINGS)))


def test_keno_result_fits_with_a_full_page_of_losers():
    bets = [(100 + i, f"pick-{i}", 10, 0) for i in range(20)]
    items = L.build_keno_result_layout(ECON, list(range(20)), bets, name_fn=NAME_FN).items
    check_budget(_view(items))


def test_pools_panel_fits_with_the_one_sided_warning_and_chart():
    items = L.build_pools_panel_layout(
        ECON, 1200.0, pools_logic.PoolSplit(400, 0), 1_750_000_000, "2026-08-03",
        spec=POOLS_SPEC, closing_soon=True,
    )
    check_budget(_view(items))
    assert any(
        isinstance(i, discord.ui.MediaGallery) for i in _view(items).walk_children()
    )


# ── Branch-specific behaviour a blanket sweep can't see ─────────────────


def test_comp_win_names_the_winner_and_the_free_amount():
    items = L.build_comp_layout(ECON, 11, ("7", "7", "7"), 10, 500, "Triple!", name_fn=NAME_FN)
    text = _all_text(items)
    assert "N11" in text and "500" in text


def test_help_panel_omits_a_closed_table():
    closed = CasinoSettings(mines_enabled=False)
    text = _all_text(L.build_help_layout(ECON, closed))
    assert "Mines" not in text


def test_slots_jackpot_renames_the_title():
    win = L.build_slots_layout(
        ECON, 11, ("7", "7", "7"), 10, 5000, "Jackpot!", jackpot_won=5000, name_fn=NAME_FN
    )
    assert "The Jackpot Spills" in win.recap


def test_pools_void_explains_the_reason():
    one_sided = _all_text(L.build_pools_void_layout("2026-08-03", 250))
    unmeasurable = _all_text(
        L.build_pools_void_layout("2026-08-03", 250, unmeasurable=True)
    )
    assert "nothing to play against" in one_sided
    assert "no longer measure" in unmeasurable


def test_pools_result_shows_a_loss_as_a_negative_line():
    items = L.build_pools_result_layout(
        ECON, "2026-08-03", 1250, 1200.0, "over", [(22, 50, 0)], 0,
        spec=POOLS_SPEC, name_fn=NAME_FN,
    )
    assert "N22 −50" in _all_text(items)


def test_mines_live_offers_the_next_rung_only_when_not_topped():
    step = MinesStep(hand_id=1, stake=10, bombs=1, revealed=(0,), mult=110, next_mult=0)
    text = _all_text(L.build_mines_layout(ECON, 11, step, name_fn=NAME_FN).items)
    assert "One more safe tile" not in text


def test_mines_bombed_reveals_the_bomb_tiles():
    step = MinesStep(
        hand_id=1, stake=10, bombs=3, revealed=(0,), outcome="bombed",
        bomb_tiles=(1, 5, 9),
    )
    text = _all_text(L.build_mines_layout(ECON, 11, step, name_fn=NAME_FN).items)
    assert text.count(L._MINES_BOMB) == 3


def test_war_standoff_shows_both_rounds_of_cards():
    text = _all_text(
        L.build_war_layout(
            ECON, 11, "K♠", "K♦", 10,
            war_player="Q♣", war_dealer="9♥", outcome="war_lose",
            payout=0, name_fn=NAME_FN,
        ).items
    )
    assert "K♠" in text and "Q♣" in text and "9♥" in text


def test_baccarat_dragon_seven_pushes_rather_than_loses():
    """A three-card Banker 7 pushes every Banker bet (stake back, no
    profit) — that's a push, not a loss, so the heading takes ➖ the way
    War's retreat/refund do, never ❌ (guide § Words: one glyph per
    outcome)."""
    result = L.build_baccarat_result_layout(
        ECON, ["3♠", "3♦"], ["2♦", "2♣", "3♥"],
        [(11, "🔴 Banker", 20, 20), (22, "🔴 Banker", 5, 5)],
        name_fn=NAME_FN,
    )
    assert result.recap.startswith("## ➖")
    assert "push" in result.recap


def test_baccarat_result_still_shows_a_loss_as_a_cross():
    """A genuine loss — nothing paid at all — keeps ❌."""
    result = L.build_baccarat_result_layout(
        ECON, ["2♦", "2♣"], ["K♠", "9♥"],
        [(11, "🔵 Player", 20, 0)],
        name_fn=NAME_FN,
    )
    assert result.recap.startswith("## ❌")


def test_roulette_result_names_no_winners_when_the_house_sweeps():
    result = L.build_roulette_result_layout(
        ECON, 0, _RESULT_BETS_ALL_LOST, name_fn=NAME_FN
    )
    assert result.recap.startswith("## ❌")


# ── Big-win broadcast: the public card, gated separately from a player's own ──


def test_big_win_broadcast_is_none_below_threshold():
    assert L.build_big_win_broadcast_layout(
        "recap text", payout=50, threshold=1000, stake=10, min_mult=5,
        game_label="Slots",
    ) is None


def test_big_win_broadcast_copies_the_players_recap_text():
    broadcast = L.build_big_win_broadcast_layout(
        "N11 bet 10 and collects 5,000.", payout=5000, threshold=1000,
        stake=10, min_mult=5, game_label="Slots",
    )
    assert broadcast is not None
    text = _all_text(broadcast.items)
    assert "N11 bet 10 and collects 5,000." in text
    assert not any(
        isinstance(i, discord.ui.Container) for i in _view(broadcast.items).walk_children()
    )


def test_big_win_broadcast_with_a_winner_uses_a_section_and_thumbnail():
    broadcast = L.build_big_win_broadcast_layout(
        "recap", payout=5000, threshold=1000, stake=10, min_mult=5,
        game_label="Slots", winner_name="Brynja", winner_icon="https://cdn.example/av.png",
    )
    assert broadcast is not None
    sections = [
        i for i in _view(broadcast.items).walk_children()
        if isinstance(i, discord.ui.Section)
    ]
    assert sections and isinstance(sections[0].accessory, discord.ui.Thumbnail)


# ── The shared "Play Again" row ──────────────────────────────────────────


def test_next_round_row_carries_the_stable_custom_id():
    row = L.next_round_row("coinflip", "🪙")
    button = row.children[0]
    assert isinstance(button, discord.ui.Button)
    assert button.custom_id == "casino:coinflip_next"
    assert button.label == "Play Again"
