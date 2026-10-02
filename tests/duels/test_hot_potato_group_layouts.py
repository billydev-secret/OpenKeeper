"""Contract + behavior tests for the Hot Potato (Group) Components V2 cards
(``bot_modules/duels/hot_potato_group/layouts.py`` — wave 3b). Mirrors
``tests/games/test_mfk_layouts.py``'s checklist: paradigm shape, budget, no
``Container``, a glyph-led title, and every member named through ``name_fn``
rather than a raw mention.
"""

from __future__ import annotations

import ast
from pathlib import Path

import discord
import pytest

from bot_modules.duels.hot_potato_group.layouts import build_holding_layout, build_result_layout
from bot_modules.core.layout import check_budget


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


def _named(uid: int) -> str:
    return f"Member{uid}"


def _holding_no_out():
    return build_holding_layout(
        [1, 2, 3], [], 1, "🥔", "Loser surrenders their nickname.", name_fn=_named
    )


def _holding_with_out():
    return build_holding_layout([2, 3], [1], 2, "🥔💣", "💰 **10** gems to join", name_fn=_named)


def _holding_max_dread():
    return build_holding_layout([3], [1, 2], 3, "🥔💣💥💥", None, name_fn=_named)


def _holding_no_holder():
    return build_holding_layout([1, 2], [], None, "🥔", None, name_fn=_named)


def _result_minimal():
    return build_result_layout(2, "Loser surrenders their nickname.")


def _result_full():
    return build_result_layout(
        4,
        "💰 **10** gems to join",
        bravest_id=5,
        bravest_seconds=42.0,
        nick_line="🏷️ **Alice** is now stuck as *Spud*.",
        name_fn=_named,
    )


ALL_CARDS = [
    pytest.param(_holding_no_out, id="holding-no-out"),
    pytest.param(_holding_with_out, id="holding-with-out"),
    pytest.param(_holding_max_dread, id="holding-max-dread"),
    pytest.param(_holding_no_holder, id="holding-no-holder"),
    pytest.param(_result_minimal, id="result-minimal"),
    pytest.param(_result_full, id="result-full"),
]

# The result card is composed by BaseGame._result_layout with its own ping
# line first (<@winner> <@loser>) — this builder's own items must never
# name or ping either player (see its docstring), so it is included in the
# "no raw mention" sweep below same as every holding-card state.
NO_PING_CARDS = ALL_CARDS


# ── Contract sweep ───────────────────────────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


@pytest.mark.parametrize("build", [ALL_CARDS[0], ALL_CARDS[1], ALL_CARDS[2], ALL_CARDS[3]])
def test_every_holding_card_leads_with_a_glyph_led_title(build):
    heading = next(
        item.content
        for item in _view(build()).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]
    assert heading.startswith("## ")
    assert len(heading) > 3 and not heading[3].isascii()


def test_result_card_has_no_title_of_its_own():
    """No ``## Title`` here: the composed message's ping line is the whole
    card's lead line for this paradigm (see the builder's own docstring) —
    a second heading here would duplicate it."""
    for build in (_result_minimal, _result_full):
        first_line = _all_text(build()).splitlines()[0]
        assert not first_line.startswith("## ")


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container) for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", NO_PING_CARDS)
def test_no_card_writes_a_raw_at_mention(build):
    assert "<@" not in _all_text(build())


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_carries_any_controls(build):
    """Both builders are content-only: the holding card's Pass button is
    the view's own frozen control row (``PassGroupLayoutView``), never part
    of what these functions return, and the result card's buttons are
    added by ``BaseGame._result_layout`` around this content."""
    assert not any(
        isinstance(item, (discord.ui.ActionRow, discord.ui.Button, discord.ui.Select))
        for item in _view(build()).walk_children()
    )


# ── Paradigm / content shape ─────────────────────────────────────────


def test_holding_card_lists_still_in_and_out():
    text = _all_text(_holding_with_out())
    assert "Still in:" in text
    assert "Out:" in text
    assert "Member2" in text and "Member3" in text  # still in
    assert "Member1" in text  # out


def test_holding_card_omits_out_line_when_nobody_eliminated_yet():
    text = _all_text(_holding_no_out())
    assert "Out:" not in text


def test_holding_card_names_the_current_holder():
    text = _all_text(_holding_with_out())
    assert "Holding:" in text
    assert "Member2" in text


def test_holding_card_defaults_to_a_mention_for_an_unwired_caller():
    """``name_fn`` defaults to ``mention`` — every real call site
    (``cog.render_game_state``) passes ``resolve_name_from`` instead; the
    default only fires for a caller that doesn't wire one up."""
    text = _all_text(build_holding_layout([1, 2], [], 1, "🥔", None))
    assert "<@1>" in text and "<@2>" in text


def test_holding_card_unknown_holder_renders_a_question_mark():
    text = _all_text(_holding_no_holder())
    assert "Holding:** ?" in text


def test_holding_card_dash_when_nobody_alive():
    text = _all_text(build_holding_layout([], [1, 2], None, "🥔", None))
    assert "Still in:** —" in text


def test_holding_card_shows_the_escalating_emoji_in_the_title():
    assert _all_text(_holding_no_out()).splitlines()[0] == "## 🥔 Hot Potato (Group)"
    assert _all_text(_holding_max_dread()).splitlines()[0] == "## 🥔💣💥💥 Hot Potato (Group)"


def test_holding_card_stakes_line_is_not_re_escaped():
    """``stakes_text`` arrives pre-escaped (``duels.filters.validate_stakes``)
    — the builder must pass it through verbatim, not run
    ``escape_markdown`` on it a second time."""
    text = _all_text(build_holding_layout([1, 2], [], 1, "🥔", "gems \\*10\\* on the line"))
    assert "gems \\*10\\* on the line" in text
    assert "\\\\*" not in text


def test_holding_card_falls_back_to_the_default_stakes_line():
    text = _all_text(build_holding_layout([1, 2], [], 1, "🥔", None))
    assert "Loser surrenders their nickname." in text
    text = _all_text(build_holding_layout([1, 2], [], 1, "🥔", ""))
    assert "Loser surrenders their nickname." in text


def test_result_card_announces_the_blowup():
    text = _all_text(_result_minimal())
    assert "bomb went off" in text.lower()


def test_result_card_reports_the_player_count():
    text = _all_text(build_result_layout(7, "stakes"))
    assert "**Players:** 7" in text


def test_result_card_omits_bravest_hands_when_none():
    assert "Bravest Hands" not in _all_text(_result_minimal())


def test_result_card_names_bravest_hands_when_present():
    text = _all_text(_result_full())
    assert "Bravest Hands" in text
    assert "Member5" in text
    assert "42s total" in text


def test_result_card_omits_nick_line_when_none():
    assert "🏷️" not in _all_text(_result_minimal())


def test_result_card_includes_the_nick_line_verbatim_when_given():
    text = _all_text(_result_full())
    assert "🏷️ **Alice** is now stuck as *Spud*." in text


def test_result_card_stakes_and_nick_lines_are_not_re_escaped():
    """Same pre-escaped contract as the holding card: ``stakes_line`` and
    ``nick_line`` are the caller's already-safe strings."""
    text = _all_text(
        build_result_layout(2, "gems \\*10\\*", nick_line="stuck as \\*Spud\\*")
    )
    assert "gems \\*10\\*" in text
    assert "stuck as \\*Spud\\*" in text
    assert "\\\\*" not in text


# ── name_fn render-site guard ─────────────────────────────────────────
#
# Self-contained instead of a new row in tests/contracts/test_embed_name_render_sites.py
# (that file's own docstring: "Features whose guard already lives beside
# their logic tests ... are not repeated here"). Every place this module
# resolves a bare user id into display text must go through the ``name_fn``
# parameter, never ``str(uid)``/an f-string splice of the raw id, or a
# viewer with no cache hit for that id sees a bare number instead of a name.

def test_every_id_interpolation_in_layouts_goes_through_name_fn():
    src = Path("src/bot_modules/duels/hot_potato_group/layouts.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls_to_name_fn_param = set()

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            has_name_fn = any(a.arg == "name_fn" for a in node.args.kwonlyargs)
            if has_name_fn:
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "name_fn"
                    ):
                        calls_to_name_fn_param.add(node.name)
            self.generic_visit(node)

    Visitor().visit(tree)
    # Both builders take an id (holder_id/alive_ids/eliminated_ids on one,
    # bravest_id on the other) and must resolve it through name_fn.
    assert "build_holding_layout" in calls_to_name_fn_param
    assert "build_result_layout" in calls_to_name_fn_param


# ── Budget ────────────────────────────────────────────────────────────


def test_fullest_holding_card_fits_the_budget():
    """10 players — the feature's own default ``max_players``
    (``hot_potato_group/db.py``'s config default; nothing in this cog or
    ``game.py`` caps the roster below the dashboard's config ceiling) —
    with long display names and a long custom stakes line, comfortably
    under the dashboard's 2000-char ``max_stakes_length`` allowance. See
    this module's own ``guide_gaps`` note: neither the pre-conversion embed
    nor this builder truncates an oversized roster or stakes line, so an
    admin who pushes ``max_players``/``max_stakes_length`` toward the
    dashboard's outer ceiling (up to 50 / 2000) can still produce a card
    that overflows Discord's budget — a pre-existing gap this conversion
    inherits rather than introduces (the old embed had the same unbounded
    ``add_field`` with no length guard), tightened by Components V2's
    single 4,000-char pool replacing embeds' more forgiving multi-field
    budget."""
    alive = list(range(8))
    out = [8, 9]

    def _long_name(uid: int) -> str:
        return f"{'A' * 28}#{uid:02d}"

    long_stakes = "S" * 400
    view = _view(
        build_holding_layout(alive, out, alive[0], "🥔💣💥💥", long_stakes, name_fn=_long_name)
    )
    check_budget(view)


def test_fullest_result_card_fits_the_budget():
    """10 players (the same default ceiling as the holding-card test above),
    a long stakes line and nick line, and a long bravest-hands name."""
    long_stakes = "S" * 400
    long_nick_line = "N" * 300

    def _long_name(uid: int) -> str:
        return f"{'B' * 28}#{uid:02d}"

    view = _view(
        build_result_layout(
            10,
            long_stakes,
            bravest_id=1,
            bravest_seconds=9999.0,
            nick_line=long_nick_line,
            name_fn=_long_name,
        )
    )
    check_budget(view)
