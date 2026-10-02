"""``bot_modules/duels/pressure_cooker/layouts.py`` — the Components V2 cards.

Two builders, two paradigms:

* :func:`build_pump_layout` — **Paradigm 9, game turn card**: the live
  gauge, the last five pumps, whose turn it is. ``GameView`` (``views.py``)
  supplies the single Pump button below it, and reuses the same builder,
  unmodified, to paint the frozen card a bust leaves behind (``state`` flips
  to ``"RESOLVED"``, which just drops the Turn line).
* :func:`build_boom_layout` — **Paradigm 5, decision card**: the facts half
  of the result card. ``duels.base_game.BaseGame._result_layout`` is what a
  member actually sees — the ping line, then these items, then the Name the
  Loser / Run It Back row — so the budget checks below build that fuller
  shape rather than the bare item list, the gap wave 3a's samples repeatedly
  left uncaught.

Both builders are pure: primitives and a ``name_fn`` in, ``list[discord.ui.
Item]`` out. Since the ping line on the result card already names both
players, ``build_boom_layout`` never repeats either by resolved name (style
guide § Mentions: "name a person once ... never both") — there is nothing
for a ``name_fn`` sweep to check there, unlike the turn card.
"""
from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.duels.pressure_cooker.game import PumpEntry
from bot_modules.duels.pressure_cooker.layouts import build_boom_layout, build_pump_layout
from bot_modules.duels.pressure_cooker.views import GameView
from bot_modules.duels.views import ResultLayoutView

CHALLENGER = 111
TARGET = 222

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that fell back to the default
#: ``mention`` arg or wrote a raw mention itself shows up immediately.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _all_text(items) -> str:
    return "\n".join(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _pumps(count: int, gauge_start: int = 10) -> list[PumpEntry]:
    gauge = gauge_start
    entries: list[PumpEntry] = []
    for i in range(count):
        player = CHALLENGER if i % 2 == 0 else TARGET
        roll = 7
        entries.append(
            PumpEntry(player_id=player, roll=roll, gauge_before=gauge, ts=1_750_000_000.0 + i)
        )
        gauge += roll
    return entries


#: Every builder, with arguments exercising its optional branches — turn
#: card at every state, result card in each of its four mutually exclusive
#: nickname states.
ALL_CARDS = [
    pytest.param(
        lambda: build_pump_layout(
            CHALLENGER, TARGET, 10, [], TARGET, "ACTIVE", name_fn=NAME_FN
        ),
        id="pump-fresh-no-history",
    ),
    pytest.param(
        lambda: build_pump_layout(
            CHALLENGER, TARGET, 92, _pumps(5), CHALLENGER, "ACTIVE", name_fn=NAME_FN
        ),
        id="pump-with-history-and-turn",
    ),
    pytest.param(
        lambda: build_pump_layout(
            CHALLENGER, TARGET, 100, _pumps(3, gauge_start=80), None, "RESOLVED",
            name_fn=NAME_FN,
        ),
        id="pump-busted-no-turn-line",
    ),
    pytest.param(
        lambda: build_boom_layout(100, 7, None, "The stake is forfeited."),
        id="boom-forfeit-only",
    ),
    pytest.param(
        lambda: build_boom_layout(
            100, 4, "loser buys the next round", "The stake is forfeited."
        ),
        id="boom-custom-stakes",
    ),
    pytest.param(
        lambda: build_boom_layout(
            100, 9, None, "The stake is forfeited.",
            imposed_copy="Now going by **Doofus**, for 24 hours.",
        ),
        id="boom-imposed",
    ),
    pytest.param(
        lambda: build_boom_layout(
            100, 2, None, "The stake is forfeited.",
            self_apply_copy=(
                "Discord won't let me rename the server owner, so the "
                "nickname has to be set by hand: **Doofus**, for 24 hours."
            ),
        ),
        id="boom-self-apply",
    ),
    pytest.param(
        lambda: build_boom_layout(
            100, 1, None, "The stake is forfeited.",
            awaiting_copy=(
                "Press **Name the Loser** within 30 minutes. The nickname "
                "lasts 24 hours."
            ),
        ),
        id="boom-awaiting",
    ),
]


# ── Every card renders, fits, and stays bare ────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


def _lead_heading(items) -> str:
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    """Guide § Anatomy: a card's own heading is one ``## Title`` line, led
    by a glyph — ``###`` (the pump card's "Recent Pumps", the result card's
    "Stakes"/"Nickname …") is a section heading *within* the card, never
    its lead line."""
    heading = _lead_heading(build())
    assert heading.startswith("## "), f"lead line must be a `## Title`: {heading!r}"
    assert not heading[3].isascii(), f"lead the title with a glyph: {heading!r}"


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    """Pressure reads through text — a gauge bar, a meter line — never a
    colour (style guide § No container; CLAUDE.md: "pressure now reads
    through text ... never color")."""
    assert not any(
        isinstance(item, discord.ui.Container) for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_writes_a_raw_mention(build):
    """A ``<@id>`` resolves from the *reading* client's own cache — the
    result card's ping line is the one deliberate exception, and it lives
    in ``BaseGame._result_layout``, outside this module entirely."""
    assert "<@" not in _all_text(build())


# ── name_fn is actually used, not silently defaulted ────────────────────


def test_pump_card_uses_name_fn_for_every_slot_not_the_default_mention():
    """Both players and the active turn all come from the caller's
    ``name_fn`` — a card that fell back to the default ``mention`` arg for
    even one slot would print a raw ``<@id>`` there instead."""
    items = build_pump_layout(
        CHALLENGER, TARGET, 50, _pumps(2), TARGET, "ACTIVE", name_fn=NAME_FN
    )
    text = _all_text(items)
    assert f"N{CHALLENGER}" in text
    assert f"N{TARGET}" in text
    assert "<@" not in text


def test_pump_log_names_come_from_name_fn_too():
    items = build_pump_layout(
        CHALLENGER, TARGET, 92, _pumps(5), None, "ACTIVE", name_fn=NAME_FN
    )
    log_text = "\n".join(
        i.content for i in items if isinstance(i, discord.ui.TextDisplay)
        if "Recent Pumps" in i.content
    )
    assert f"N{CHALLENGER}" in log_text and f"N{TARGET}" in log_text


def test_pump_card_omits_default_mention_when_name_fn_given():
    """A regression guard for the ``name_fn`` default itself: calling the
    builder with **no** ``name_fn`` falls back to ``services.name_resolver.
    mention``, which *does* write ``<@id>`` — proving the assertions above
    would actually catch a builder that quietly stopped forwarding the
    argument."""
    from bot_modules.shared.name_resolver import mention

    items = build_pump_layout(CHALLENGER, TARGET, 10, [], TARGET, "ACTIVE")
    assert mention(CHALLENGER) in _all_text(items)


# ── Paradigm shape: who owns the buttons ─────────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_neither_builder_places_its_own_buttons(build):
    """Paradigm 9's Pump button belongs to ``GameView``; Paradigm 5's
    Name the Loser / Run It Back row belongs to
    ``duels.base_game.BaseGame._result_layout``. Either builder adding a
    control of its own would duplicate — or fight — the base's row."""
    assert not any(
        isinstance(item, (discord.ui.Button, discord.ui.ActionRow, discord.ui.Select))
        for item in _view(build()).walk_children()
    )


def test_pump_card_turn_line_is_gated_by_state_not_by_active_player_alone():
    """A resolved (busted) game still has an ``active_player`` row in the
    DB until the next write; the Turn line must gate on ``state ==
    "ACTIVE"`` too, or the frozen busted card would still claim a turn."""
    items = build_pump_layout(
        CHALLENGER, TARGET, 100, [], CHALLENGER, "RESOLVED", name_fn=NAME_FN
    )
    assert "Turn:" not in _all_text(items)


def test_pump_card_only_shows_the_last_five_pumps():
    """8 pumps step the gauge 10 -> 66 by 7s; the earliest entry's own
    "before" reading (10) is outside the last-five window and must not
    appear, while the most recent one (59) must."""
    items = build_pump_layout(
        CHALLENGER, TARGET, 92, _pumps(8), TARGET, "ACTIVE", name_fn=NAME_FN
    )
    log_text = _all_text(items)
    assert "10 →" not in log_text
    assert "→ 59/100" in log_text


def test_pump_card_history_window_is_exactly_five_entries():
    items = build_pump_layout(
        CHALLENGER, TARGET, 92, _pumps(8), TARGET, "ACTIVE", name_fn=NAME_FN
    )
    log = next(
        i.content for i in items
        if isinstance(i, discord.ui.TextDisplay) and "Recent Pumps" in i.content
    )
    lines = [ln for ln in log.splitlines() if ln.startswith("**N")]
    assert len(lines) == 5


# ── Boom card never re-names either player ───────────────────────────────


def test_boom_card_never_names_either_player_by_id_or_resolved_name():
    """The ping line ahead of these items (``BaseGame._result_layout``)
    already names both players once each; ``build_boom_layout`` takes no
    ``name_fn`` at all and cannot restate either (style guide § Mentions:
    "name a person once ... never both")."""
    import inspect

    assert "name_fn" not in inspect.signature(build_boom_layout).parameters


def test_boom_card_renders_pre_escaped_stakes_text_without_double_escaping():
    """``stakes_text`` arrives already markdown-escaped by
    ``duels.filters.validate_stakes`` at creation time — that's the one
    place that can tell the member-typed half from bot-composed lines
    like the wager line spliced on by ``resolve_stakes_text``. Escaping
    again here would double-escape the member half (visible literal
    backslashes) and break the bot's own bold markup on those lines, so
    this asserts the builder renders the pre-escaped value verbatim."""
    raw = "loser buys *drinks* for everyone_else"
    pre_escaped = discord.utils.escape_markdown(raw)
    wager_line = "💰 **10** gems each — winner takes the pot."
    stakes_text = f"{pre_escaped}\n{wager_line}"

    items = build_boom_layout(100, 3, stakes_text, "The stake is forfeited.")
    text = _all_text(items)

    assert stakes_text in text
    assert discord.utils.escape_markdown(stakes_text) not in text
    assert "\\\\" not in text
    assert wager_line in text


def test_boom_card_falls_back_to_forfeit_copy_with_no_stakes_text():
    items = build_boom_layout(100, 3, None, "A distinctive forfeit sentence.")
    assert "A distinctive forfeit sentence." in _all_text(items)


@pytest.mark.parametrize(
    ("kwargs", "heading"),
    [
        pytest.param({"imposed_copy": "x"}, "### 🏷️ Nickname Applied", id="imposed"),
        pytest.param({"self_apply_copy": "x"}, "### 🏷️ Nickname — Over To You", id="self-apply"),
        pytest.param({"awaiting_copy": "x"}, "### ⏳ Awaiting Nickname", id="awaiting"),
    ],
)
def test_boom_card_nickname_state_headings_are_mutually_exclusive(kwargs, heading):
    items = build_boom_layout(100, 1, None, "forfeit", **kwargs)
    text = _all_text(items)
    assert heading in text
    other_headings = {
        "### 🏷️ Nickname Applied", "### 🏷️ Nickname — Over To You", "### ⏳ Awaiting Nickname",
    } - {heading}
    assert not any(h in text for h in other_headings)


def test_boom_card_with_no_nickname_state_shows_only_stakes():
    items = build_boom_layout(100, 1, None, "forfeit")
    text = _all_text(items)
    assert "🏷️" not in text and "⏳" not in text


# ── Budget: the fullest ACTUAL combined card, not a bare fragment ───────


def test_pump_card_fits_with_a_full_pump_history_inside_the_real_game_view():
    """The turn card a member actually sees is ``GameView`` — the bare
    items *plus* the persistent Pump button's ``ActionRow`` — not the item
    list alone."""
    view = GameView(game_id=1, on_pump=None)  # type: ignore[arg-type]
    items = build_pump_layout(
        CHALLENGER, TARGET, 92, _pumps(20), CHALLENGER, "ACTIVE", name_fn=NAME_FN
    )
    view.render(items)
    check_budget(view)


def test_boom_result_card_fits_as_the_full_composed_result_layout():
    """The card a member actually sees on a bust is
    ``BaseGame._result_layout``'s composition: the ping line, then this
    builder's items, then the Name the Loser / Run It Back row — built here
    directly through :class:`ResultLayoutView` (the same class that method
    uses) with its longest-copy branch, since that is the fullest state the
    result card can reach."""
    ping_line = discord.ui.TextDisplay(f"<@{111}> <@{222}>")
    items = build_boom_layout(
        100, 40, "loser buys the next round for the whole voice channel",
        "The stake is forfeited.",
        self_apply_copy=(
            "Discord won't let me rename the server owner, so the "
            "nickname has to be set by hand: **A Very Long Nickname Indeed**, "
            "for 48 hours."
        ),
    )
    view = ResultLayoutView(
        game_id=1,
        winner_id=111,
        loser_id=222,
        on_set_nick=lambda *a, **k: None,  # signature only; never awaited here
        on_rematch=lambda *a, **k: None,
        rematch_deadline=1_800_000_000.0,
        content=[ping_line, *items],
    )
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS
