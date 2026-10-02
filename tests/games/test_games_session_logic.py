"""Tests for the extracted Session Recap pure-logic modules.

Covers ``bot_modules/games/session/logic.py`` (duration formatting,
per-game highlight rules) and ``bot_modules/games/session/layouts.py``
(recap card shape). Mirrors the traditional pattern: the cog file
stays thin; this module proves the extracted pieces work without
spinning up Discord or a guild.
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from datetime import datetime, timezone

import discord
import pytest

from bot_modules.games.constants import GAME_NAMES
from bot_modules.games.utils import game_manager
from bot_modules.games.session.layouts import build_session_recap_layout
from bot_modules.games.session.logic import (
    build_game_highlight,
    build_highlights,
    format_duration,
)


def _text_of(items) -> str:
    return "\n".join(
        item.content for item in items if isinstance(item, discord.ui.TextDisplay)
    )


# ── format_duration ──────────────────────────────────────────────────


def test_format_duration_basic_minutes_only():
    assert format_duration("2026-01-01T12:00:00", "2026-01-01T12:15:00") == "15m"


def test_format_duration_with_hours_includes_both_components():
    assert format_duration("2026-01-01T12:00:00", "2026-01-01T14:15:00") == "2h 15m"


def test_format_duration_zero_when_same_timestamp():
    assert format_duration("2026-01-01T12:00:00", "2026-01-01T12:00:00") == "0m"


def test_format_duration_drops_hours_when_under_an_hour():
    """A 12m session shouldn't render as ``0h 12m``."""
    result = format_duration("2026-01-01T00:00:00", "2026-01-01T00:12:00")
    assert "h" not in result


def test_format_duration_unknown_on_invalid_iso():
    assert format_duration("not a date", "2026-01-01T12:00:00") == "unknown"


def test_format_duration_unknown_on_none_inputs():
    # type-ignore: deliberately wrong input to exercise the fallback
    assert format_duration(None, None) == "unknown"  # type: ignore[arg-type]


def test_format_duration_unknown_on_negative_span():
    """An end-before-start row is malformed — render ``"unknown"`` instead
    of a confusing negative duration."""
    assert format_duration("2026-01-01T12:30:00", "2026-01-01T12:00:00") == "unknown"


def test_format_duration_handles_multi_hour_session():
    assert format_duration("2026-01-01T10:00:00", "2026-01-01T14:30:00") == "4h 30m"


# ── build_game_highlight ─────────────────────────────────────────────


def test_build_game_highlight_unknown_type_uses_raw_key():
    out = build_game_highlight("unknown_game", {})
    # No icon configured for unknown_game — but the key still surfaces.
    assert "unknown_game" in out


def test_build_game_highlight_known_game_uses_friendly_name():
    out = build_game_highlight("wyr", {})
    assert "Would You Rather" in out


def test_build_game_highlight_wyr_picks_most_divisive():
    payload = {
        "rounds": {
            "1": {"q": "Coffee or tea?", "a": [1, 2, 3], "b": [4]},
            "2": {"q": "Cats or dogs?", "a": [1, 2], "b": [3, 4]},
            "3": {"q": "Pizza or burger?", "a": [1, 2, 3, 4], "b": []},
        }
    }
    out = build_game_highlight("wyr", payload)
    # Round 2 is the most divisive — equal split of 2/2
    assert "Cats or dogs?" in out
    assert "Most divisive" in out


def test_build_game_highlight_wyr_truncates_long_question():
    long_q = "x" * 100
    payload = {"rounds": {"1": {"q": long_q, "a": [1], "b": [2]}}}
    out = build_game_highlight("wyr", payload)
    # 50-char truncation
    assert "x" * 50 in out
    assert "x" * 51 not in out


def test_build_game_highlight_wyr_no_rounds_returns_bare_header():
    out = build_game_highlight("wyr", {})
    assert "Would You Rather" in out
    assert "Most divisive" not in out


def test_build_game_highlight_nhie_picks_guiltiest():
    payload = {"guilt_scores": {"100": 2, "200": 7, "300": 5}}
    name_lookup = {"100": "Alice", "200": "Bob", "300": "Carol"}
    out = build_game_highlight("nhie", payload, name_lookup)
    assert "Bob" in out
    assert "7 guilty" in out


def test_build_game_highlight_nhie_falls_back_to_raw_id_without_lookup():
    """No name_lookup means the raw id renders — never crashes."""
    payload = {"guilt_scores": {"200": 7}}
    out = build_game_highlight("nhie", payload)
    assert "200" in out


def test_build_game_highlight_nhie_empty_scores_returns_bare_header():
    out = build_game_highlight("nhie", {"guilt_scores": {}})
    assert "Never Have I Ever" in out
    assert "Guiltiest" not in out


def test_build_game_highlight_ttl_picks_best_liar():
    payload = {
        "scores": {
            "100": {"fooled": 2},
            "200": {"fooled": 5},
            "300": {"fooled": 1},
        }
    }
    out = build_game_highlight("ttl", payload, {"200": "Bob"})
    assert "Bob" in out
    assert "Best Liar" in out


def test_build_game_highlight_ttl_falls_back_to_raw_id_without_lookup():
    payload = {"scores": {"42": {"fooled": 5}}}
    out = build_game_highlight("ttl", payload)
    assert "42" in out
    assert "Best Liar" in out


def test_build_game_highlight_ttl_empty_scores_returns_bare_header():
    out = build_game_highlight("ttl", {"scores": {}})
    assert "Best Liar" not in out


def test_build_game_highlight_hottakes_picks_highest_avg():
    payload = {
        "results": [
            {"text": "lukewarm take", "avg": 2.1},
            {"text": "very hot take", "avg": 3.8},
            {"text": "cold take", "avg": 1.0},
        ]
    }
    out = build_game_highlight("hottakes", payload)
    assert "very hot take" in out
    assert "3.8/4" in out


def test_build_game_highlight_hottakes_truncates_long_text():
    payload = {"results": [{"text": "y" * 100, "avg": 4.0}]}
    out = build_game_highlight("hottakes", payload)
    assert "y" * 40 in out
    assert "y" * 41 not in out


def test_build_game_highlight_hottakes_empty_returns_bare_header():
    out = build_game_highlight("hottakes", {"results": []})
    assert "Hot Takes" in out
    assert "Hottest" not in out


def test_build_game_highlight_other_game_returns_bare_header():
    """A game type with no special highlight rule (e.g. ffa) still
    renders cleanly, using its friendly name from GAME_NAMES."""
    out = build_game_highlight("ffa", {"question": "Q?"})
    assert GAME_NAMES["ffa"] in out


# ── markdown escaping (misfix sweep: games-logic) ──────────────────────
#
# The recap card is a public embed field built by re-deriving the same
# member-controlled text every per-game embeds.py already escapes (a
# WYR question, a guilty/liar display name, a hot take) — sibling
# asymmetry the original sweep never reached.


def test_build_game_highlight_wyr_escapes_question_markdown():
    payload = {"rounds": {"1": {"q": "**Moderator** says hi", "a": [1], "b": [2]}}}
    out = build_game_highlight("wyr", payload)
    assert "**Moderator**" not in out
    assert "\\*\\*Moderator\\*\\*" in out


def test_build_game_highlight_wyr_escape_then_cut_never_overflows_the_raw_width():
    """Escaping happens before the 50-char cut, not after — an all-markdown
    question must not double past the intended escaped width."""
    payload = {"rounds": {"1": {"q": "*" * 100, "a": [1], "b": [2]}}}
    out = build_game_highlight("wyr", payload)
    # Each "*" escapes to the 2-char "\*"; cutting the *escaped* string to
    # 50 chars keeps only 25 pairs, not 50 (which raw-slice-then-escape
    # would have produced — a 100-char snippet against a 50-char budget).
    assert "\\*" * 25 in out
    assert "\\*" * 26 not in out


def test_build_game_highlight_nhie_escapes_guiltiest_display_name():
    payload = {"guilt_scores": {"100": 3}}
    out = build_game_highlight("nhie", payload, {"100": "__Admin__"})
    assert "__Admin__" not in out
    assert "\\_\\_Admin\\_\\_" in out


def test_build_game_highlight_ttl_escapes_best_liar_display_name():
    payload = {"scores": {"100": {"fooled": 4}}}
    out = build_game_highlight("ttl", payload, {"100": "~~Sly~~"})
    assert "~~Sly~~" not in out
    assert "\\~\\~Sly\\~\\~" in out


def test_build_game_highlight_hottakes_escapes_and_bounds_take_text():
    payload = {"results": [{"text": "`" * 80, "avg": 3.0}]}
    out = build_game_highlight("hottakes", payload)
    assert "`" * 80 not in out
    # Each "`" escapes to the 2-char "\`"; the 40-char trim of the escaped
    # text keeps only 20 pairs.
    assert "\\`" * 20 in out
    assert "\\`" * 21 not in out


# ── build_highlights ─────────────────────────────────────────────────


def test_build_highlights_runs_per_game():
    histories = [
        {"game_type": "wyr", "payload": {"rounds": {"1": {"q": "Q?", "a": [1], "b": [2]}}}},
        {"game_type": "ffa", "payload": {}},
    ]
    out = build_highlights(histories)
    assert len(out) == 2
    assert GAME_NAMES["wyr"] in out[0]
    assert GAME_NAMES["ffa"] in out[1]


def test_build_highlights_passes_name_lookup_through():
    histories = [{"game_type": "ttl", "payload": {"scores": {"7": {"fooled": 3}}}}]
    out = build_highlights(histories, {"7": "Zara"})
    assert "Zara" in out[0]


def test_build_highlights_empty_returns_empty_list():
    assert build_highlights([]) == []


# ── build_session_recap_layout ───────────────────────────────────────


def test_build_session_recap_layout_shows_core_stats():
    text = _text_of(
        build_session_recap_layout(
            game_count=3,
            player_ids=[1, 2, 3, 4],
            duration_str="1h 5m",
            highlights=[],
        )
    )
    assert "**🎮 Games Played:** 3" in text
    assert "**👥 Unique Players:** 4" in text
    assert "**⏱️ Total Duration:** 1h 5m" in text


def test_build_session_recap_layout_omits_players_line_when_empty():
    text = _text_of(build_session_recap_layout(0, [], "0m", []))
    assert "🏆 Players" not in text


def _name_fn(uid: int) -> str:
    return f"User{uid}"


def test_build_session_recap_layout_renders_names_for_players():
    """Players are named through the resolver, never as ``<@id>``: a
    mention renders as digits to anyone whose client hasn't cached that
    member, and the recap is the card members screenshot."""
    text = _text_of(build_session_recap_layout(1, [42, 99], "5m", [], name_fn=_name_fn))
    assert "User42" in text
    assert "User99" in text
    assert "<@" not in text


def test_build_session_recap_layout_default_resolver_keeps_a_mention():
    """An un-wired caller still renders rather than crashing; the AST test
    below is what forces the cog to wire a resolver."""
    text = _text_of(build_session_recap_layout(1, [42], "5m", []))
    assert "<@42>" in text


def test_build_session_recap_layout_truncates_player_list_to_ten():
    ids = list(range(1, 20))  # 19 players
    text = _text_of(build_session_recap_layout(5, ids, "30m", [], name_fn=_name_fn))
    assert "User10" in text
    # The 11th and beyond shouldn't appear
    assert "User11" not in text
    # But unique count still reflects the full list
    assert "**👥 Unique Players:** 19" in text


def test_build_session_recap_layout_renders_highlights_block():
    text = _text_of(
        build_session_recap_layout(1, [1], "5m", ["**🤔 Would You Rather**: round 1"])
    )
    assert "Game Highlights" in text
    assert "round 1" in text
    assert "• **🤔 Would You Rather**: round 1" in text


def test_build_session_recap_layout_omits_highlights_line_when_empty():
    text = _text_of(build_session_recap_layout(0, [1], "0m", []))
    assert "Game Highlights" not in text


def test_build_session_recap_layout_truncates_highlights_to_eight():
    highlights = [f"**game {i}**" for i in range(15)]
    text = _text_of(build_session_recap_layout(15, [1], "1h", highlights))
    # Eighth highlight should be present, ninth should not
    assert "game 7" in text
    assert "game 8" not in text


def test_build_session_recap_layout_has_title():
    text = _text_of(build_session_recap_layout(0, [], "0m", []))
    assert text.startswith("## 📋 Games Session Recap")


def test_build_session_recap_layout_stays_under_the_components_v2_budget():
    from bot_modules.core.layout import check_budget

    view = discord.ui.LayoutView(timeout=None)
    for item in build_session_recap_layout(
        15, list(range(1, 20)), "1h", [f"**game {i}**" for i in range(15)]
    ):
        view.add_item(item)
    check_budget(view)


def test_every_session_render_site_passes_a_resolver():
    """``name_fn`` defaults to ``mention``, so a render site that forgets to
    pass one silently reintroduces digits in the recap. This walks the cog and
    requires every call to a name-taking builder to hand a resolver over."""
    import ast
    import inspect
    import pathlib

    from bot_modules.games.session import cog as games_session_cog
    from bot_modules.games.session import layouts as session_layouts

    needs = {
        name
        for name, fn in inspect.getmembers(session_layouts, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
    }
    assert "build_session_recap_layout" in needs
    # Explicit utf-8: the CI runner is Windows, where the default is cp1252.
    source = pathlib.Path(inspect.getfile(games_session_cog)).read_text(
        encoding="utf-8"
    )
    missed = [
        f"games_session_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in needs
        and not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


# ── session timestamps stay naive UTC ────────────────────────────────
#
# ``update_session`` used to call the deprecated ``datetime.utcnow()``. Its
# replacement has to stay *naive* rather than becoming tz-aware, because
# ``games_session_tracker`` mixes two writers: Python writes ``last_game_at``
# as an ISO string, while ``started_at`` defaults to SQLite's
# ``CURRENT_TIMESTAMP`` (migration 019), which is naive. The two are compared
# as strings in SQL and subtracted from each other in ``format_duration``, so
# an aware value would both break the ``last_game_at >= ?`` window and raise
# TypeError against every pre-existing row.


class _FakeDB:
    """Minimal async DB shim over an in-memory SQLite connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    async def fetchone(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchone()

    async def execute(self, sql: str, params: tuple = ()):
        self.conn.execute(sql, params)
        self.conn.commit()


@pytest.fixture
def session_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE games_session_tracker (
               session_id   TEXT PRIMARY KEY,
               channel_id   INTEGER NOT NULL,
               started_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               last_game_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               game_ids     TEXT NOT NULL DEFAULT '[]',
               player_ids   TEXT NOT NULL DEFAULT '[]'
           )"""
    )
    yield _FakeDB(conn)
    conn.close()


async def test_update_session_writes_a_naive_utc_timestamp(session_db):
    await game_manager.update_session(session_db, channel_id=5, game_id="wyr",
                                      player_ids=[1, 2])
    row = session_db.conn.execute(
        "SELECT started_at, last_game_at FROM games_session_tracker"
    ).fetchone()

    parsed = datetime.fromisoformat(row["last_game_at"])
    assert parsed.tzinfo is None, "an aware value breaks the SQL string compare"
    assert "+" not in row["last_game_at"] and "Z" not in row["last_game_at"]
    # It is UTC, not local time: within a minute of "now" in UTC.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now_utc - parsed).total_seconds()) < 60

    # And it is still subtractable from SQLite's own naive default.
    assert format_duration(row["started_at"], row["last_game_at"]) != "unknown"


async def test_update_session_finds_the_row_it_just_wrote(session_db):
    """The ``last_game_at >= ?`` window is a *string* comparison.

    A "+00:00" suffix on either side would sort wrong and silently start a new
    session for every game instead of appending to the open one.
    """
    first = await game_manager.update_session(session_db, channel_id=5,
                                              game_id="wyr", player_ids=[1])
    second = await game_manager.update_session(session_db, channel_id=5,
                                               game_id="nhie", player_ids=[2])
    assert first == second
    row = session_db.conn.execute(
        "SELECT game_ids, player_ids FROM games_session_tracker"
    ).fetchone()
    assert json.loads(row["game_ids"]) == ["wyr", "nhie"]
    assert sorted(json.loads(row["player_ids"])) == [1, 2]


async def test_update_session_emits_no_datetime_deprecation_warning(session_db):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await game_manager.update_session(session_db, channel_id=5, game_id="wyr",
                                          player_ids=[1])
    offenders = [
        w
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "utcnow" in str(w.message).lower()
    ]
    assert not offenders, [str(w.message) for w in offenders]
