"""Tests for survivor/reckoning.py + tasks.py decisions + ghost streaks.

Stages 5 and 6a of docs/plans/survivor.md: the three-act data assembly,
leaver elimination (§6.14), the weekly-task
due logic over guild-local clocks, and the streak rules as decided
2026-08-17 (miss breaks, void neither, unsettled pending).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.survivor.service import (
    SeasonError,
    add_host,
    create_season,
    eliminate_player,
    get_season,
    update_config,
)
from bot_modules.survivor import tasks
from bot_modules.survivor.logic import ghost_streaks, join_season, place_pick
from bot_modules.survivor.reckoning import (
    build_reckoning_data,
    build_reckoning_embed,
    eulogy_for,
    eliminate_leavers,
    named_ids,
    next_reckoning_week,
    reckoning_copy_text,
    wipeout_for,
)
from bot_modules.survivor.settle import run_settle
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR
THU = NOW + DAY
MON = NOW + 5 * DAY
AFTER_W1 = MON + 5 * HOUR

WEEK1 = [
    ("g-thu", "SEA", "NE", THU),
    ("g-mon", "KC", "LV", MON),
]
WEEK2 = [("g2", "SF", "ARI", NOW + 12 * DAY)]
WEEK3 = [("g3", "BUF", "MIA", NOW + 19 * DAY)]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        for week, games in ((1, WEEK1), (2, WEEK2), (3, WEEK3)):
            for gid, home, away, ts in games:
                conn.execute(
                    "INSERT INTO nfl_games (season_year, week, game_id, home, away, kickoff_utc)"
                    " VALUES (?,?,?,?,?,?)",
                    (YEAR, week, gid, home, away, _iso(ts)),
                )
    return db_path


def _season(conn, **overrides):
    return get_season(conn, create_season(conn, GID, "S", YEAR,
                                          overrides=overrides or None))


def _finalize(conn, game_id, winner):
    conn.execute(
        "UPDATE nfl_games SET status = 'final', winner = ? WHERE game_id = ?",
        (winner, game_id),
    )


def _settled_week1(conn, season):
    """Two players: 1 picks SEA (wins), 2 picks NE (dies, sudden death)."""
    join_season(conn, season, 1, NOW)
    join_season(conn, season, 2, NOW)
    place_pick(conn, season, 1, 1, "SEA", NOW)
    place_pick(conn, season, 2, 1, "NE", NOW)
    _finalize(conn, "g-thu", "SEA")
    _finalize(conn, "g-mon", "KC")
    run_settle(conn, season, AFTER_W1)


# ── reckoning week + rotation ──────────────────────────────────────────


def test_next_reckoning_week_gates_on_elapsed(db):
    with open_db(db) as conn:
        season = _season(conn)
        assert next_reckoning_week(conn, season, NOW) is None  # nothing kicked
        assert next_reckoning_week(conn, season, AFTER_W1) == 1
        update_config(conn, season["id"], {"last_reckoned_week": 1})
        season = get_season(conn, season["id"])
        assert next_reckoning_week(conn, season, AFTER_W1) is None  # wk2 open


# ── the three acts ─────────────────────────────────────────────────────


def test_reckoning_data_toll_ledger_deaths(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        _settled_week1(conn, season)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert (data["before"], data["after"]) == (2, 1)
        # Deaths sorted first, states honest.
        assert [e["user_id"] for e in data["deaths"]] == [2]
        assert data["deaths"][0]["fatal_team"] == "NE"
        assert [e["user_id"] for e in data["ledger"]] == [1]
        assert data["ledger"][0]["teams"].startswith("SEA")
        assert data["stragglers"] == 0


def test_reckoning_no_deaths_is_just_the_numbers(db):
    # Just-the-facts (2026-08-18): a clean week carries no special line —
    # the survivors delta in the embed says it.
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        _finalize(conn, "g-thu", "SEA")
        _finalize(conn, "g-mon", "KC")
        run_settle(conn, season, AFTER_W1)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert data["deaths"] == []
        assert "toll_line" not in data


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        pytest.param("cap", "out of auto-assigns", id="cap-fixed-line"),
        pytest.param("missed", "never picked", id="missed-fixed-line"),
    ],
)
def test_eulogy_fixed_lines_by_source(source, needle):
    entry = {"source": source, "fatal_team": None, "user_id": 1}
    line = eulogy_for(entry, {"week": 3}, "Loaf", 0)
    assert needle in line and "Loaf" in line


def test_no_leaver_eulogy_because_leavers_are_dropped():
    """Billy, 2026-09-11 (todo #203): a member who left the server is
    dropped from every Survivor surface, so there is nobody in the post to
    eulogise — the roster simply stops listing them. This test fails if the
    'left the server mid-season' line is reintroduced without also undoing
    the drop, which would print a eulogy for a name the post never showed.
    """
    import bot_modules.survivor.reckoning as reck

    assert not hasattr(reck, "LEAVER_LINE")
    entry = {"source": "left", "fatal_team": None, "user_id": 1}
    assert "left the server" not in eulogy_for(entry, {"week": 3}, "Loaf", 0)


def test_eulogy_football_death_states_the_facts():
    # Just-the-facts (2026-08-18): team, result, week — no corpus.
    entry = {"source": "picks", "fatal_team": "NE", "user_id": 1}
    line = eulogy_for(entry, {"week": 3}, "Loaf", 0)
    assert line == "**Loaf** — NE lost. Eliminated in Week 3."
    assert "{" not in line
    # A brace in a nickname must not crash or leak a slot (stage-1 contract).
    braced = eulogy_for(entry, {"week": 3}, "{Loaf}", 0)
    assert "{Loaf}" in braced


def test_leavers_die_at_the_reckoning(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)  # player 2's loss is fatal
        _settled_week1(conn, season)
        # Player 1 (alive) has left the guild; player 2 is already a ghost.
        gone = eliminate_leavers(conn, season, 1, present_ids={2, 999})
        assert gone == [1]
        row = conn.execute(
            "SELECT status, elimination_source FROM survivor_players "
            "WHERE season_id = ? AND user_id = 1",
            (season["id"],),
        ).fetchone()
        assert (row["status"], row["elimination_source"]) == ("ghost", "left")
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert {e["user_id"] for e in data["deaths"]} == {1, 2}


def test_arrivals_gate_counts_gauntlet_walkers_only(db):
    with open_db(db) as conn:
        season = _season(conn)
        # Enrolled before the first kickoff: never walked anything.
        join_season(conn, season, 6, NOW)
        # Mid-week join after Thursday kicked: a real gate arrival.
        join_season(conn, season, 7, THU + HOUR)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert [a["user_id"] for a in data["arrivals"]] == [7]


# ── ghost streaks (§1.7 as decided) ────────────────────────────────────


def test_ghost_streaks_miss_breaks_void_neither(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        eliminate_player(conn, season["id"], 1, week=1)
        # wk2: ghost picks SF, wins. wk3: no pick (miss) → streak resets;
        # best stays 1.
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot,"
            " team, game_id, result) VALUES (?, ?, 1, 2, 1, 'SF', 'g2', 'win')",
            (season["id"], GID),
        )
        now = NOW + 21 * DAY  # weeks 1-3 all elapsed
        streaks = ghost_streaks(conn, season, now)
        assert streaks[1] == {"current": 0, "best": 1}
        # A void in wk3 instead of a miss leaves the streak untouched.
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot,"
            " team, game_id, result) VALUES (?, ?, 1, 3, 1, 'BUF', 'g3', 'void')",
            (season["id"], GID),
        )
        streaks = ghost_streaks(conn, season, now)
        assert streaks[1] == {"current": 1, "best": 1}


def test_reckoning_counts_the_ghosts_picks(db):
    """survivor-182: 'Ghosts: N picked, N correct' under the streak strip.

    Ghost 1 wins, ghost 2 splits (win + loss: not correct), ghost 3 picked
    nothing, ghost 4 died THIS week (a death, not a ghost) and so is left out.
    """
    with open_db(db) as conn:
        season = _season(conn)
        for uid in (1, 2, 3, 4, 5):
            join_season(conn, season, uid, NOW)
        for uid in (1, 2, 3):
            eliminate_player(conn, season["id"], uid, week=0)
        eliminate_player(conn, season["id"], 4, week=2)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week,"
            " slot, team, game_id, result) VALUES"
            " (?, ?, 1, 2, 1, 'SF', 'g2', 'win'),"
            " (?, ?, 2, 2, 1, 'SF', 'g2', 'win'),"
            " (?, ?, 2, 2, 2, 'ARI', 'g2', 'loss'),"
            " (?, ?, 4, 2, 1, 'ARI', 'g2', 'loss'),"
            " (?, ?, 5, 2, 1, 'SF', 'g2', 'win')",
            (season["id"], GID) * 5,
        )
        data = build_reckoning_data(conn, season, 2, NOW + 15 * DAY)
        assert data["ghost_picks"] == {"picked": 2, "correct": 1}
        embed = build_reckoning_embed(data, lambda uid: f"P{uid}", season_name="S")
        field = next(f for f in embed.fields if f.name == "Ghost Streak")
        assert "👻 Ghosts: 2 picked, 1 correct" in field.value
        # Hidden (departed) ghosts drop out of the count with everyone else.
        hidden = build_reckoning_data(conn, season, 2, NOW + 15 * DAY, hidden={1})
        assert hidden["ghost_picks"] == {"picked": 1, "correct": 0}


def test_ghost_pick_counts_ignore_pending_picks():
    """Match pay_weekly_wins: an ungraded pick is neither a miss nor a pick."""
    from bot_modules.survivor.reckoning import ghost_pick_counts

    def pl(uid):
        return {"user_id": uid, "status": "ghost", "eliminated_week": 0}

    by_user = {
        1: [{"result": "win"}, {"result": None}],  # one graded win, one pending
        2: [{"result": None}],                      # only pending: not counted
        3: [{"result": "win"}, {"result": "loss"}],
    }
    assert ghost_pick_counts([pl(1), pl(2), pl(3)], by_user, 2) == {
        "picked": 2, "correct": 1,
    }


def test_reckoning_without_ghost_picks_has_no_ghost_line(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        eliminate_player(conn, season["id"], 1, week=0)
        data = build_reckoning_data(conn, season, 2, NOW + 15 * DAY)
        assert data["ghost_picks"] == {"picked": 0, "correct": 0}


def test_ghost_streaks_ignore_pre_death_wins(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)  # player 2's loss is fatal
        _settled_week1(conn, season)  # player 1 WON week 1, alive
        # Player 2 died wk1; their wk1 loss must not seed a streak, and the
        # living player has no streak entry at all.
        streaks = ghost_streaks(conn, season, AFTER_W1)
        assert streaks[2] == {"current": 0, "best": 0}
        assert 1 not in streaks


# ── weekly-task due logic ──────────────────────────────────────────────


def _cfg_season(conn, **overrides):
    season = _season(conn, **overrides)
    update_config(conn, season["id"], {"channel_id": 555})
    return get_season(conn, season["id"])


@pytest.mark.parametrize(
    ("dow_offset_days", "hour", "due"),
    [
        # NOW is Wednesday 12:00 UTC; slate_hour default 9, offset 0.
        pytest.param(0, 12, True, id="wednesday-after-hour"),
        pytest.param(-1, 12, False, id="tuesday-not-yet"),
        pytest.param(2, 12, True, id="friday-catchup"),
        # survivor-172: a season created weeks before Week 1 must not post
        # the Week 1 slate on the first Wednesday it sees — pick_week is 1
        # from the moment the schedule ingests, so the guard is the frame:
        # the Tue–Mon week the first kickoff falls in has to have opened.
        pytest.param(-21, 12, False, id="three-weeks-early-not-imminent"),
        pytest.param(-7, 12, False, id="eight-days-out-not-imminent"),
        pytest.param(-14, 12, False, id="two-weeks-early-not-imminent"),
        # The week BEFORE the opener: past_weekly_moment reads every day
        # after Wednesday as "missed, catch up", so a day-count window
        # opening mid-week posted the slate a week early (review 2026-09-04).
        pytest.param(-6, 12, False, id="thursday-before-not-imminent"),
        pytest.param(-3, 12, False, id="sunday-before-not-imminent"),
        pytest.param(-1, 0, False, id="tuesday-of-the-week-not-yet"),
    ],
)
def test_slate_due_frame(db, dow_offset_days, hour, due):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        now = NOW + dow_offset_days * DAY
        got = tasks.slate_due(conn, season, now, 0.0)
        assert (got == 1) is due


def test_slate_proximity_guard_holds_under_force_and_explains(db):
    # The proximity guard is state, not clock: the dashboard's run-now
    # button (force=True) two weeks early must not recreate the prod
    # defect, and idle_reason says why nothing was due.
    with open_db(db) as conn:
        season = _cfg_season(conn)
        early = NOW - 14 * DAY
        assert tasks.slate_due(conn, season, early, 0.0, force=True) is None
        assert tasks.lastcall_due(conn, season, early, 0.0, force=True) is None
        assert "first kickoff" in tasks.idle_reason(conn, season, early)
        # The Wednesday before the opener is the real week-open.
        assert tasks.slate_due(conn, season, NOW, 0.0) == 1


def test_slate_due_once_per_week(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        assert tasks.slate_due(conn, season, NOW, 0.0) == 1
        update_config(conn, season["id"], {"last_slate_week": 1})
        season = get_season(conn, season["id"])
        assert tasks.slate_due(conn, season, NOW, 0.0) is None
        # Next week's slate becomes due after MNF rolls pick_week forward.
        assert tasks.slate_due(conn, season, NOW + 7 * DAY, 0.0) == 2


def test_reckoning_due_tuesday_after_week_elapsed(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        # Week 1 fully kicked Monday night; Tuesday 9am+ is the moment.
        tue_early = MON + 8 * HOUR    # Tue 01:00 UTC
        tue_nine = MON + 21 * HOUR    # Tue 14:00 UTC — past 9 local (UTC)
        assert tasks.reckoning_due(conn, season, tue_early, 0.0) is None
        assert tasks.reckoning_due(conn, season, tue_nine, 0.0) == 1


def test_lastcall_due_saturday(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        sat_evening = NOW + 3 * DAY + 7 * HOUR  # Sat 19:00 UTC, hour 18 due
        assert tasks.lastcall_due(conn, season, NOW, 0.0) is None  # Wednesday
        assert tasks.lastcall_due(conn, season, sat_evening, 0.0) == 1
        # survivor-172: the Saturday two weeks before the opener is not a
        # last call — nobody can be late for a week that hasn't opened.
        assert tasks.lastcall_due(conn, season, sat_evening - 14 * DAY, 0.0) is None
        # The Saturday before the opener's week is not this week's Saturday.
        assert tasks.lastcall_due(conn, season, sat_evening - 7 * DAY, 0.0) is None


# ── weekly clock (the dashboard's operator view) ───────────────────────

# A Thursday 10:00 in a UTC-7 guild.
_THU_10 = datetime(2026, 9, 10, 17, 0, tzinfo=timezone.utc).timestamp()
_PT = -7.0


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=_PT)))


@pytest.mark.parametrize(
    ("dow", "hour", "expected"),
    [
        # Next Wednesday 9am: six days on.
        pytest.param(2, 9, (2026, 9, 16, 9), id="wed-next-week"),
        # Saturday 6pm: still ahead this week.
        pytest.param(5, 18, (2026, 9, 12, 18), id="sat-this-week"),
        # Thursday 9am already passed today → next Thursday.
        pytest.param(3, 9, (2026, 9, 17, 9), id="today-hour-passed"),
        # Thursday 11am still ahead today.
        pytest.param(3, 11, (2026, 9, 10, 11), id="today-hour-ahead"),
    ],
)
def test_next_weekly_moment_is_guild_local_and_strictly_ahead(dow, hour, expected):
    got = _local(tasks.next_weekly_moment(_THU_10, _PT, dow, hour))
    assert (got.year, got.month, got.day, got.hour) == expected
    assert got.minute == 0 and got.weekday() == dow


def test_weekly_tasks_use_the_loops_own_days():
    # The dashboard's clock and the poll loop's gates are the same constants.
    assert [t[1] for t in tasks.WEEKLY_TASKS.values()] == [
        tasks.WEDNESDAY, tasks.SATURDAY, tasks.MONDAY, tasks.TUESDAY,
    ]


def test_weekly_clock_rows_flag_spent_and_due_and_resettable():
    config = {
        "slate_hour": 9, "lastcall_hour": 18, "reckoning_hour": 9,
        "last_slate_week": 1, "last_lastcall_week": 1, "last_reckoned_week": 0,
    }
    rows = tasks.weekly_clock_rows(
        config, _THU_10, _PT, week=1,
        due={"slate": None, "lastcall": None, "reckoning": 1},
    )
    by = {r["task"]: r for r in rows}
    assert [r["task"] for r in rows] == ["slate", "lastcall", "reckoning"]
    # The Week 1 shape: both posts already spent for the current pick week.
    assert by["slate"]["spent"] and by["lastcall"]["spent"]
    assert by["slate"]["fired_week"] == 1
    assert not by["reckoning"]["spent"]
    assert by["reckoning"]["due_week"] == 1
    # Only the two idempotent posts can be re-armed; the Reckoning pays coins.
    assert by["slate"]["resettable"] and by["lastcall"]["resettable"]
    assert not by["reckoning"]["resettable"]
    assert _local(by["slate"]["next_ts"]).weekday() == 2
    assert _local(by["lastcall"]["next_ts"]).hour == 18


def test_weekly_clock_rows_without_a_pick_week_is_never_spent():
    rows = tasks.weekly_clock_rows(
        {"last_slate_week": 3}, _THU_10, _PT, week=None, due={},
    )
    assert not any(r["spent"] for r in rows)
    assert all(r["due_week"] is None for r in rows)


def test_weekly_clock_reads_the_due_functions_and_the_pick_week(db):
    # NOW is the Wednesday of Week 1's frame at 12:00 UTC (slate_hour 9):
    # the slate is due on the next tick, the other two are not.
    with open_db(db) as conn:
        season = _cfg_season(conn)
        rows, week = tasks.weekly_clock(conn, season, NOW, 0.0)
    assert week == 1
    by = {r["task"]: r for r in rows}
    assert by["slate"]["due_week"] == 1 and not by["slate"]["spent"]
    assert by["lastcall"]["due_week"] is None
    assert by["reckoning"]["due_week"] is None


def test_rearm_weekly_task_resets_to_the_week_before(db):
    with open_db(db) as conn:
        season = _cfg_season(conn, last_slate_week=1)
        assert tasks.slate_due(conn, season, NOW, 0.0) is None
        assert tasks.rearm_weekly_task(conn, season, "slate", NOW) == (1, 1, 0)
        season = get_season(conn, season["id"])
        assert season["config"]["last_slate_week"] == 0
        # Re-armed: the loop would post it again on its next tick.
        assert tasks.slate_due(conn, season, NOW, 0.0) == 1


@pytest.mark.parametrize(
    ("task", "overrides", "needle"),
    [
        # The Reckoning pays coins in the transaction that marks the week.
        pytest.param("reckoning", {"last_reckoned_week": 1}, "Only the slate",
                     id="reckoning-refused"),
        pytest.param("bogus", {}, "Only the slate", id="unknown-refused"),
        pytest.param("lastcall", {}, "nothing to reset", id="not-fired-yet"),
    ],
)
def test_rearm_weekly_task_refuses(db, task, overrides, needle):
    with open_db(db) as conn:
        season = _cfg_season(conn, **overrides)
        before = dict(season["config"])
        with pytest.raises(SeasonError, match=needle):
            tasks.rearm_weekly_task(conn, season, task, NOW)
        assert get_season(conn, season["id"])["config"] == before


# ── the slate as weekly mini-announcement (2026-08-18) ─────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"buyin": 0, "late_entry": "gauntlet", "gauntlet_mode": False},
            "free entry", id="pre-kickoff-free",
        ),
        pytest.param(
            {"buyin": 100, "late_entry": "gauntlet", "gauntlet_mode": True},
            "auto-replay as the Gauntlet", id="gauntlet-era",
        ),
        pytest.param(
            {"buyin": 0, "late_entry": "ghost_only", "gauntlet_mode": True},
            "Ghost Streak side game", id="ghost-only",
        ),
    ],
)
def test_slate_join_line_variants(kwargs, expected):
    from bot_modules.survivor.reckoning import slate_join_line

    line = slate_join_line(**kwargs)
    assert line is not None and expected in line


def test_slate_join_line_closed_is_none():
    from bot_modules.survivor.reckoning import slate_join_line

    assert slate_join_line(
        buyin=0, late_entry="closed", gauntlet_mode=True
    ) is None


def test_panel_embed_carries_the_door_unless_closed():
    from bot_modules.survivor.embeds import build_panel_embed

    games = [{"home": "SEA", "away": "NE", "kickoff_ts": 1_700_000_000.0}]
    kwargs = dict(
        season_name="S", entrants=9, buyin=0, gauntlet_mode=True,
        week=3, games=games, alive=7, eliminated=2, picked=5,
        pot=8150, ghost_pot=2000,
    )
    open_embed = build_panel_embed(**{**kwargs, "late_entry": "gauntlet"})
    assert any(f.name == "New Here?" for f in open_embed.fields)
    assert "Alive **7**" in open_embed.description
    assert "Pot 🪙 **8,150** Coins" in open_embed.description
    games_field = next(
        f for f in open_embed.fields if f.name == "This Week's Games"
    )
    assert "**NE** @ **SEA**" in games_field.value

    closed_embed = build_panel_embed(**{**kwargs, "late_entry": "closed"})
    assert all(f.name != "New Here?" for f in closed_embed.fields)


def test_panel_embed_enrolling_face():
    # No pick week yet (schedule empty/finished): the pre-kickoff pitch,
    # no games section, entrant count front and center.
    from bot_modules.survivor.embeds import build_panel_embed

    embed = build_panel_embed(
        season_name="S", entrants=4, buyin=0, gauntlet_mode=False, pot=8000,
    )
    assert "**4** players in" in embed.description
    assert all(f.name != "This Week's Games" for f in embed.fields)
    assert any(f.name == "New Here?" for f in embed.fields)


# ── history (panel button + public command share one builder) ──────────


def test_history_embed_public_hides_unrevealed_own_shows_tagged():
    from bot_modules.survivor.embeds import build_history_embed

    rows = [
        {"week": 1, "team": "SEA", "result": "win", "auto_assigned": False,
         "opponent": "NE", "is_home": True, "winner": "SEA"},
        {"week": 2, "team": "KC", "result": "loss", "auto_assigned": True,
         "opponent": "LV", "is_home": False, "winner": "LV"},
        {"week": 3, "team": "SF", "result": None, "auto_assigned": False,
         "opponent": "ARI", "is_home": True, "winner": None},
    ]
    public = build_history_embed(
        rows, display_name="Loaf", revealed_week=2, own=False
    )
    assert "SF" not in public.description          # secrecy holds
    assert "Week 1: **SEA** vs NE · ✅ won" in public.description
    assert "Week 2: **KC** at LV · 💀 LV won 📎" in public.description
    assert "Revealed picks only" in public.footer.text

    own = build_history_embed(
        rows, display_name="Loaf", revealed_week=2, own=True
    )
    assert "Week 3: **SF** vs ARI" in own.description  # your own eyes only
    assert "⏳ awaiting result" in own.description
    assert "hidden from others" in own.description
    assert "Only you can see this" in own.footer.text


def test_panel_view_button_roster():
    from bot_modules.survivor.views import panel_view

    ids = [c.custom_id for c in panel_view(7, join_open=True).children]
    assert ids == [
        "survivor_slate:7", "survivor_join:7", "survivor_history:7"
    ]
    ids = [c.custom_id for c in panel_view(7, join_open=False).children]
    assert ids == ["survivor_slate:7", "survivor_history:7"]


# ── the weekly prize (2026-08-18) ──────────────────────────────────────


def test_weekly_prize_pays_all_win_players_only(db):
    from bot_modules.economy import service as economy_service
    from bot_modules.survivor.reckoning import pay_weekly_wins

    with open_db(db) as conn:
        season = _season(conn)  # weekly_win_coins default 25
        _settled_week1(conn, season)  # 1 won (SEA), 2 lost (NE)
        paid = pay_weekly_wins(conn, season, 1)
        assert paid == [(1, 25)]
        assert economy_service.get_balance(conn, GID, 1) == 25
        assert economy_service.get_balance(conn, GID, 2) == 0
        row = conn.execute(
            "SELECT kind, amount, meta FROM econ_ledger WHERE user_id = 1"
        ).fetchone()
        assert row["kind"] == "survivor_weekly_win" and row["amount"] == 25
        import json as _json

        meta = _json.loads(row["meta"])
        assert (meta["season_id"], meta["week"]) == (season["id"], 1)


def test_weekly_prize_ghosts_collect_and_double_pick_split_does_not(db):
    from bot_modules.survivor.reckoning import pay_weekly_wins

    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)   # ghost, wins
        join_season(conn, season, 2, NOW)   # split week: win + loss
        eliminate_player(conn, season["id"], 1, week=0)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week,"
            " slot, team, game_id, result) VALUES"
            " (?, ?, 1, 1, 1, 'SEA', 'g-thu', 'win'),"
            " (?, ?, 2, 1, 1, 'SEA', 'g-thu', 'win'),"
            " (?, ?, 2, 1, 2, 'LV', 'g-mon', 'loss')",
            (season["id"], GID, season["id"], GID, season["id"], GID),
        )
        paid = pay_weekly_wins(conn, season, 1)
        assert paid == [(1, 25)]  # the ghost collects; the split week doesn't


def test_weekly_prize_dial_zero_is_off(db):
    from bot_modules.survivor.reckoning import pay_weekly_wins

    with open_db(db) as conn:
        season = _season(conn, weekly_win_coins=0)
        _settled_week1(conn, season)
        assert pay_weekly_wins(conn, season, 1) == []


def test_reckoning_embed_shows_the_prize_line():
    from bot_modules.survivor.reckoning import build_reckoning_embed

    data = {
        "week": 1, "before": 3, "after": 3,
        "pots": {"main": 8000, "ghost": 2000},
        "stragglers": 0, "arrivals": [],
        "deaths": [], "ledger": [],
        "streak_strip": [], "streak_record": 0,
        "weekly_win": {"count": 14, "amount": 25},
    }
    embed = build_reckoning_embed(data, lambda u: f"P{u}", season_name="S")
    assert (
        "💰 14 correct pick(s) collect 🪙 **25** Coins each"
        in embed.description
    )


# ── why nothing was due (2026-08-18) ──────────────────────────────────
#
# Every gate routes through pick_week, so a season on a year with no
# ingested schedule looks exactly like a quiet week: all three return None
# and the forced run posts nothing. idle_reason is what the dashboard says
# instead of reporting success — the silence is what cost a debugging round
# when the first live season was created on year 2035.

def test_idle_reason_names_a_missing_schedule(db):
    with open_db(db) as conn:
        season = get_season(
            conn, create_season(conn, GID + 1, "No Schedule", 2035)
        )
        assert tasks.idle_reason(conn, season, NOW) == (
            "no schedule ingested for 2035 — generate or ingest one "
            "before any weekly task can be due"
        )


def test_idle_reason_names_the_once_per_week_state(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        update_config(conn, season["id"], {"last_slate_week": 1})
        season = get_season(conn, season["id"])
        assert "already run for this week" in tasks.idle_reason(conn, season, NOW)


def test_idle_reason_names_the_gap_between_weeks(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        after_everything = NOW + 60 * DAY
        assert "between weeks" in tasks.idle_reason(
            conn, season, after_everything
        )


# ── last call carries the pick button (2026-08-18) ────────────────────
#
# The Saturday nudge used to point at /survivor pick; now the DM (and the
# closed-DM channel fallback) carries the same persistent SlatePickButton
# as the channel panel — the nudge is the door, not directions to one.
# This is deliberate wiring, so it gets the one wiring assertion.

class _FakeMember:
    def __init__(self, uid, fail=False):
        self.id = uid
        self.display_name = f"P{uid}"
        self._fail = fail
        self.sent = []

    async def send(self, content=None, **kwargs):
        import discord as _d

        if self._fail:
            resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
            raise _d.Forbidden(resp, "closed DMs")
        self.sent.append((content, kwargs))


class _FakeChannel:
    def __init__(self, guild):
        self.guild = guild
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class _FakeGuild:
    def __init__(self, members, channel_id):
        self._members = {m.id: m for m in members}
        self._channel_id = channel_id
        self.channel = _FakeChannel(self)

    def get_member(self, uid):
        return self._members.get(uid)

    def get_channel(self, cid):
        return self.channel if cid == self._channel_id else None


class _FakeBot:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, gid):
        return self._guild


def _pick_button_ids(view):
    return [
        c.custom_id for c in view.children
        if getattr(c, "custom_id", "").startswith("survivor_slate:")
    ]


@pytest.mark.asyncio
async def test_last_call_dm_carries_the_pick_button(db, monkeypatch):
    import discord  # noqa: F401 — fakes raise discord errors

    with open_db(db) as conn:
        season = _cfg_season(conn)
        join_season(conn, season, 1, NOW)
        join_season(conn, season, 2, NOW)
        season = get_season(conn, season["id"])
    member_ok = _FakeMember(1)
    member_closed = _FakeMember(2, fail=True)
    guild = _FakeGuild(
        [member_ok, member_closed], int(season["config"]["channel_id"] or 0)
    )
    bot = _FakeBot(guild)
    # _channel() isinstance-checks discord.TextChannel, which a test fake
    # can't be — route it to the fake so the closed-DM fallback is reachable.
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)

    handled = await tasks.send_last_call(bot, db, season, 1, NOW)

    assert handled is True
    # The open-DM member got the nudge with the button attached.
    (content, kwargs), = member_ok.sent
    assert "Week 1" in content and "/survivor pick" not in content
    assert _pick_button_ids(kwargs["view"]) == [f"survivor_slate:{season['id']}"]
    # The closed-DM member fell back to the channel — with the button too.
    (fb_content, fb_kwargs), = guild.channel.sent
    assert "<@2>" in fb_content
    assert _pick_button_ids(fb_kwargs["view"]) == [f"survivor_slate:{season['id']}"]



@pytest.mark.parametrize("closed", [False, True], ids=["open-dms", "closed-dms"])
@pytest.mark.asyncio
async def test_last_call_skips_a_member_on_a_break(db, monkeypatch, closed):
    """Take a Break: no DM, and never the public mention a closed DM falls
    back to — they can't see the channel it would ping them in."""
    from bot_modules.breaks.store import create_break

    with open_db(db) as conn:
        season = _cfg_season(conn)
        join_season(conn, season, 1, NOW)
        create_break(conn, guild_id=GID, user_id=1, stored_roles=[], swapped_spicy=False)
        season = get_season(conn, season["id"])
    resting = _FakeMember(1, fail=closed)
    guild = _FakeGuild([resting], int(season["config"]["channel_id"] or 0))
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)

    assert await tasks.send_last_call(_FakeBot(guild), db, season, 1, NOW) is True
    assert resting.sent == []
    assert guild.channel.sent == []

# ── role reconcile (2026-08-18, Billy's #10) ──────────────────────────
#
# Every decision pass repairs life-state roles: alive holds Survivor,
# ghost holds Ghost. The point is drift — the a41e70e2 join crash left a
# member charged but roleless, and nothing would ever have granted it.
# swap_member_roles checks the cached roles first, so the no-drift case
# must make zero API calls.

class _FakeRole:
    def __init__(self, rid, permissions=None):
        from discord import Permissions

        self.id = rid
        self.name = f"role-{rid}"
        # Read by core.role_safety before the swap grants anything.
        self.managed = False
        self.permissions = permissions or Permissions.none()

    def is_default(self):
        return False


class _RoleMember:
    def __init__(self, uid, roles):
        self.id = uid
        self.roles = list(roles)
        self.calls = []

    async def add_roles(self, role, reason=None):
        self.calls.append(("add", role.id))
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.calls.append(("remove", role.id))
        self.roles.remove(role)


class _RoleGuild:
    def __init__(self, members, roles):
        self._members = {m.id: m for m in members}
        self._roles = {r.id: r for r in roles}

    def get_member(self, uid):
        return self._members.get(uid)

    def get_role(self, rid):
        return self._roles.get(rid)


@pytest.mark.asyncio
async def test_swap_refuses_an_elevated_role_without_stripping_the_old_one():
    # Removing Survivor with nothing safe to put on would leave no life state.
    from discord import Permissions

    from bot_modules.survivor.views import swap_member_roles

    survivor = _FakeRole(11)
    ghost = _FakeRole(22, permissions=Permissions(manage_roles=True))
    member = _RoleMember(1, [survivor])
    bot = _FakeBot(_RoleGuild([member], [survivor, ghost]))

    note = await swap_member_roles(
        bot, 1, {"role_survivor_id": 11, "role_ghost_id": 22}, 1, to_ghost=True
    )

    assert note == "role swap skipped — that role can't be granted"
    assert member.calls == []


@pytest.mark.asyncio
async def test_reconcile_roles_heals_drift_and_skips_the_settled(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        update_config(conn, season["id"], {
            "role_survivor_id": 11, "role_ghost_id": 22,
        })
        season = get_season(conn, season["id"])
        join_season(conn, season, 1, NOW)   # alive, roleless (crashed join)
        join_season(conn, season, 2, NOW)   # alive, already correct
        join_season(conn, season, 3, NOW)
        eliminate_player(conn, season["id"], 3, week=1)  # ghost, still 🏈

    survivor, ghost = _FakeRole(11), _FakeRole(22)
    roleless = _RoleMember(1, [])
    settled = _RoleMember(2, [survivor])
    stale_ghost = _RoleMember(3, [survivor])
    guild = _RoleGuild([roleless, settled, stale_ghost], [survivor, ghost])
    bot = _FakeBot(guild)

    await tasks.reconcile_roles(bot, db, season)

    assert roleless.calls == [("add", 11)]          # the crashed-join heal
    assert settled.calls == []                       # no drift → zero API calls
    assert stale_ghost.calls == [("remove", 11), ("add", 22)]


# ── currency vocabulary (2026-08-19, Billy's #12 follow-up) ───────────
#
# The style guide's rule: amounts render with the guild-configured
# emoji/name, singular at 1, never a hard-coded "coins". The second live
# guild runs its own denomination, so this is behavior, not pedantry.

def test_money_renders_in_the_guilds_own_denomination():
    from bot_modules.economy.service import EconSettings
    from bot_modules.survivor.embeds import build_panel_embed
    from bot_modules.survivor.reckoning import slate_join_line

    nuts = EconSettings(
        currency_name="Nut", currency_plural="Nuts", currency_emoji="🥜"
    )
    embed = build_panel_embed(
        season_name="S", entrants=3, buyin=100, gauntlet_mode=False,
        pot=10300, settings=nuts,
    )
    assert "Pot 🥜 **10,300** Nuts" in embed.description
    assert "coins" not in embed.description.lower()
    line = slate_join_line(
        buyin=1, late_entry="gauntlet", gauntlet_mode=False, settings=nuts
    )
    assert "🥜 **1** Nut to enter" in line  # singular at 1


# ── last call names the games still to come (survivor-180) ────────────
#
# The early-games list used to LIMIT 3 in SQL and only then drop games that
# had already kicked, so a kicked-but-unsettled Thursday game (an ESPN
# outage, a late-season Saturday game in progress) stole a slot from the
# Sunday-morning game the DM exists to name. The filter now runs first.


def test_early_game_lines_filter_future_kickoffs_before_truncating(db):
    # Saturday 19:00 UTC; Week 1 has three kicked-but-still-'scheduled'
    # games ahead of one Sunday 09:30 game (the international trap).
    sat = NOW + 3 * DAY + 7 * HOUR
    with open_db(db) as conn:
        season = _cfg_season(conn)
        for gid, ts in (("g-fri", sat - DAY), ("g-sat-am", sat - 8 * HOUR)):
            conn.execute(
                "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
                " kickoff_utc) VALUES (?, 1, ?, 'DAL', 'NYG', ?)",
                (YEAR, gid, _iso(ts)),
            )
        conn.execute(
            "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
            " kickoff_utc) VALUES (?, 1, 'g-sun-am', 'JAX', 'TEN', ?)",
            (YEAR, _iso(sat + 14 * HOUR + 30 * 60)),  # Sun 09:30 UTC
        )
        lines = tasks.early_game_lines(conn, season, 1, sat, 0.0)
    assert len(lines) == 1 and lines[0].startswith("TEN @ JAX kicks <t:")


# ── the Reckoning posts before it pays (survivor-173, -174) ───────────
#
# Marking the week reckoned and paying the weekly prize used to happen in
# the transaction BEFORE the channel send, so one Forbidden lost the week's
# post forever — resetting the mark to retry would double-pay. The send
# now comes first; leavers, prize and mark commit in a second transaction
# only after Discord accepted the post. Leaver elimination also needs a
# warm member cache and an API confirmation: a partial cache after a
# re-IDENTIFY must never bury the roster as source='left'.


class _ReckoningChannel(_FakeChannel):
    def __init__(self, guild, *, fail_sends=0):
        super().__init__(guild)
        self.fail_sends = fail_sends

    async def send(self, content=None, **kwargs):
        if self.fail_sends:
            import discord as _d

            self.fail_sends -= 1
            resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
            raise _d.Forbidden(resp, "no send permission")
        self.sent.append((content, kwargs))


class _ReckoningGuild:
    def __init__(self, members, *, chunked=True, missing=()):
        self.id = GID
        self.members = list(members)
        self._members = {m.id: m for m in members}
        self.chunked = chunked
        self._missing = set(missing)
        self.channel = _ReckoningChannel(self)
        self.fetched: list[int] = []

    def get_member(self, uid):
        return self._members.get(uid)

    def get_role(self, rid):
        return None

    async def fetch_member(self, uid):
        import discord as _d

        self.fetched.append(uid)
        resp = type("R", (), {"status": 404, "reason": "Not Found"})()
        if uid in self._missing:
            raise _d.NotFound(resp, "unknown member")
        return self._members.get(uid) or _FakeMember(uid)


class _ReadyBot(_FakeBot):
    def __init__(self, guild, *, ready=True):
        super().__init__(guild)
        self._ready = ready

    def is_ready(self):
        return self._ready


def _quiet_post_side_effects(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tasks, "swap_member_roles", _noop)
    monkeypatch.setattr(tasks, "send_branded_dm", _noop)
    monkeypatch.setattr(tasks, "safe_resolve_accent", _noop)
    import bot_modules.survivor.views as views

    monkeypatch.setattr(views, "refresh_panel", _noop)


@pytest.mark.asyncio
async def test_reckoning_failed_send_pays_and_marks_nothing(db, monkeypatch):
    from bot_modules.economy import service as economy_service

    with open_db(db) as conn:
        season = _cfg_season(conn, strikes=0)
        _settled_week1(conn, season)  # 1 won (prize), 2 lost (dies)
        season = get_season(conn, season["id"])
    guild = _ReckoningGuild([_FakeMember(1), _FakeMember(2)])
    guild.channel.fail_sends = 1
    bot = _ReadyBot(guild)
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)
    _quiet_post_side_effects(monkeypatch)

    ok = await tasks.post_reckoning(bot, db, season, 1, AFTER_W1)

    assert ok == "" and guild.channel.sent == []
    with open_db(db) as conn:
        season = get_season(conn, season["id"])
        assert int(season["config"].get("last_reckoned_week") or 0) == 0
        assert economy_service.get_balance(conn, GID, 1) == 0
        # Still due — the next pass retries the post.
        assert next_reckoning_week(conn, season, AFTER_W1) == 1

    # The retry posts, pays once, marks once.
    ok = await tasks.post_reckoning(bot, db, season, 1, AFTER_W1)
    assert ok == "reckoning" and len(guild.channel.sent) == 1
    with open_db(db) as conn:
        season = get_season(conn, season["id"])
        assert int(season["config"]["last_reckoned_week"]) == 1
        assert economy_service.get_balance(conn, GID, 1) == 25
        assert next_reckoning_week(conn, season, AFTER_W1) is None
    # The prize line the post carried matches what was actually paid.
    (_, kwargs), = guild.channel.sent
    assert "collect" in kwargs["embed"].description


@pytest.mark.parametrize(
    ("ready", "chunked"),
    [
        pytest.param(False, True, id="bot-not-ready"),
        pytest.param(True, False, id="guild-not-chunked"),
    ],
)
@pytest.mark.asyncio
async def test_reckoning_partial_cache_eliminates_nobody(
    db, monkeypatch, ready, chunked
):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        _settled_week1(conn, season)  # both alive (default strikes)
        season = get_season(conn, season["id"])
    # An EMPTY member cache: every player looks gone.
    guild = _ReckoningGuild([], chunked=chunked)
    bot = _ReadyBot(guild, ready=ready)
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)
    _quiet_post_side_effects(monkeypatch)

    assert await tasks.post_reckoning(bot, db, season, 1, AFTER_W1) == "reckoning"
    assert guild.fetched == []
    with open_db(db) as conn:
        alive = conn.execute(
            "SELECT COUNT(*) FROM survivor_players WHERE season_id = ? "
            "AND status = 'alive'", (season["id"],),
        ).fetchone()[0]
        assert alive == 2


@pytest.mark.asyncio
async def test_reckoning_confirms_leavers_with_the_api(db, monkeypatch):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        _settled_week1(conn, season)
        season = get_season(conn, season["id"])
    # Neither player is in the cache; only player 2 is really gone.
    guild = _ReckoningGuild([], chunked=True, missing={2})
    bot = _ReadyBot(guild)
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)
    _quiet_post_side_effects(monkeypatch)

    assert await tasks.post_reckoning(bot, db, season, 1, AFTER_W1) == "reckoning"
    assert sorted(guild.fetched) == [1, 2]
    with open_db(db) as conn:
        rows = {
            int(r["user_id"]): (r["status"], r["elimination_source"])
            for r in conn.execute(
                "SELECT user_id, status, elimination_source FROM "
                "survivor_players WHERE season_id = ?", (season["id"],),
            ).fetchall()
        }
    assert rows[1] == ("alive", None)          # cache miss, API says present
    assert rows[2] == ("ghost", "left")        # API says gone


# ── role reconcile runs hourly, and backs off failing members (survivor-185)


@pytest.mark.asyncio
async def test_reconcile_roles_hourly_cadence_and_per_member_backoff(
    db, monkeypatch
):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        update_config(conn, season["id"], {
            "role_survivor_id": 11, "role_ghost_id": 22,
        })
        season = get_season(conn, season["id"])
        join_season(conn, season, 1, NOW)
        join_season(conn, season, 2, NOW)
    monkeypatch.setattr(tasks, "_reconciled_at", {})
    monkeypatch.setattr(tasks, "_role_failures", {})
    calls: list[int] = []

    async def _swap(bot, guild_id, config, user_id, *, to_ghost):
        calls.append(user_id)
        return "role swap failed — check Manage Roles" if user_id == 2 else None

    monkeypatch.setattr(tasks, "swap_member_roles", _swap)
    bot = _FakeBot(None)

    await tasks.reconcile_roles(bot, db, season, NOW, force=False)
    assert sorted(calls) == [1, 2]              # first pass runs
    await tasks.reconcile_roles(bot, db, season, NOW + 60, force=False)
    assert sorted(calls) == [1, 2]              # a minute later: skipped
    # A decision fired (force): the pass runs, but the member whose swap
    # failed a minute ago is left alone until an hour has passed.
    await tasks.reconcile_roles(bot, db, season, NOW + 120, force=True)
    assert sorted(calls) == [1, 1, 2]
    # An hour after the last pass the cadence rolls and the backoff has
    # expired: both members are tried again.
    await tasks.reconcile_roles(bot, db, season, NOW + 120 + HOUR, force=False)
    assert sorted(calls) == [1, 1, 1, 2, 2]


@pytest.mark.parametrize(
    ("kickoff", "offset", "opens"),
    [
        # A Wednesday-evening local opener (prod Week 1, 00:20Z Thursday at
        # UTC-7) belongs to the frame that opened the Tuesday before it.
        pytest.param(
            datetime(2026, 9, 10, 0, 20, tzinfo=timezone.utc).timestamp(), -7.0,
            datetime(2026, 9, 8, 7, 0, tzinfo=timezone.utc).timestamp(),
            id="wednesday-evening-local",
        ),
        # A Tuesday kickoff opens its own frame at that Tuesday's midnight.
        pytest.param(
            datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc).timestamp(), 0.0,
            datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc).timestamp(),
            id="tuesday-kickoff",
        ),
        # A Monday-night game is the tail of the frame that opened six days earlier.
        pytest.param(
            datetime(2026, 9, 14, 23, 0, tzinfo=timezone.utc).timestamp(), 0.0,
            datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc).timestamp(),
            id="monday-night",
        ),
    ],
)
def test_week_frame_opens_on_the_tuesday_before_the_kickoff(kickoff, offset, opens):
    assert tasks.week_frame_opens(kickoff, offset) == opens


@pytest.mark.asyncio
async def test_reckoning_commit_yields_to_a_parallel_pass(db, monkeypatch):
    """The loop's tick and the dashboard's run-now share no lock, and the
    send sits between the due-check and the commit. The commit re-reads the
    mark under the write lock: a week another pass already reckoned is not
    paid a second time."""
    from bot_modules.economy import service as economy_service

    with open_db(db) as conn:
        season = _cfg_season(conn, strikes=0)
        _settled_week1(conn, season)
        season = get_season(conn, season["id"])
    guild = _ReckoningGuild([_FakeMember(1), _FakeMember(2)])
    bot = _ReadyBot(guild)
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)
    _quiet_post_side_effects(monkeypatch)

    real_send = guild.channel.send

    async def _send_while_another_pass_lands(content=None, **kwargs):
        # The other pass reckons (and pays) the week while this one is
        # waiting on Discord.
        with open_db(db) as conn:
            economy_service.apply_credit(conn, GID, 1, 25, kind="survivor_weekly_win")
            update_config(conn, season["id"], {"last_reckoned_week": 1})
        await real_send(content, **kwargs)

    guild.channel.send = _send_while_another_pass_lands
    assert await tasks.post_reckoning(bot, db, season, 1, AFTER_W1) == "reckoning"
    with open_db(db) as conn:
        # Paid exactly once — by the pass that got there first.
        assert economy_service.get_balance(conn, GID, 1) == 25
        assert int(get_season(conn, season["id"])["config"]["last_reckoned_week"]) == 1


# ── §1.6 in the post (stage 6b) ───────────────────────────────────────
#
# The settle sweep decides the wipeout; the Reckoning only reports it. So
# these check the reporting reads the recorded decision and nothing else.


def _wipe_week1(conn, season):
    """Both players pick the loser of the Thursday game."""
    for user_id in (1, 2):
        join_season(conn, season, user_id, NOW)
        place_pick(conn, season, user_id, 1, "NE", NOW)
    _finalize(conn, "g-thu", "SEA")
    _finalize(conn, "g-mon", "KC")
    return run_settle(conn, season, AFTER_W1)


def test_a_clean_week_carries_no_wipeout(db):
    with open_db(db) as conn:
        season = _season(conn)
        _settled_week1(conn, season)
        assert build_reckoning_data(conn, season, 1, AFTER_W1)["wipeout"] is None


def test_an_annulled_week_leads_the_post(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        assert _wipe_week1(conn, season).annulled == [1]
        season = get_season(conn, season["id"])

        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert data["wipeout"] == {"kind": "annul", "week": 1}
        # Nobody died, so the toll is flat and there are no eulogies.
        assert (data["before"], data["after"]) == (2, 2)
        assert data["deaths"] == []

        embed = build_reckoning_embed(
            data, lambda uid: f"P{uid}", season_name="S"
        )
        assert embed.description.startswith("☠️ **Wipeout.**")
        assert "stricken from the record" in embed.description
        assert not [f for f in embed.fields if f.name.startswith("⚖️")]


def test_a_split_week_prints_the_shares(db):
    with open_db(db) as conn:
        season = _season(
            conn, strikes=0, wipeout_annul_through_week=0,
            pot_seed=1000, ghost_pot_pct=0, buyin_coins=0,
        )
        assert _wipe_week1(conn, season).season_ended is not None
        season = get_season(conn, season["id"])

        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert data["wipeout"]["kind"] == "split"
        assert [s["amount"] for s in data["wipeout"]["shares"]] == [500, 500]
        # Every id the post will name is prefetched, the split included —
        # a missed one renders as a bare <@id> for exactly that member.
        assert named_ids(data) == [1, 2]

        embed = build_reckoning_embed(
            data, lambda uid: f"P{uid}", season_name="S"
        )
        assert "The season ends here" in embed.description
        split = next(f for f in embed.fields if f.name.startswith("⚖️"))
        assert "P1" in split.value and "P2" in split.value


def test_a_departed_split_recipient_is_dropped_from_the_shares(db):
    """Regression, 2026-09-11 review: every other list build_reckoning_data
    assembles is filtered through ``hidden`` (todo #203), but the wipeout
    split's shares came straight from payout_receipt unfiltered — a departed
    member who shared in the split still printed in the 'The Split' field,
    the exact thing todo #203 exists to stop."""
    with open_db(db) as conn:
        season = _season(
            conn, strikes=0, wipeout_annul_through_week=0,
            pot_seed=1000, ghost_pot_pct=0, buyin_coins=0,
        )
        assert _wipe_week1(conn, season).season_ended is not None
        season = get_season(conn, season["id"])

        full = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert [s["user_id"] for s in full["wipeout"]["shares"]] == [1, 2]

        data = build_reckoning_data(conn, season, 1, AFTER_W1, hidden={2})
        assert [s["user_id"] for s in data["wipeout"]["shares"]] == [1]
        assert named_ids(data) == [1]

        embed = build_reckoning_embed(
            data, lambda uid: f"P{uid}", season_name="S"
        )
        split = next(f for f in embed.fields if f.name.startswith("⚖️"))
        assert "P1" in split.value and "P2" not in split.value


def test_wipeout_for_reports_only_what_was_recorded(db):
    """A season that ended some other way, or a week nobody recorded, is
    not this week's wipeout — the ceremony never re-decides §1.6."""
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        _wipe_week1(conn, season)
        season = get_season(conn, season["id"])
        assert wipeout_for(conn, season, 1) == {"kind": "annul", "week": 1}
        assert wipeout_for(conn, season, 2) is None


# ── departed members are dropped (todo #203) ──────────────────────────


def test_departed_members_leave_the_post_entirely(db):
    """Billy, 2026-09-11: a member who left the server is dropped, not
    printed as a bare id. The toll counts come from the filtered roster, so
    before → after still reconciles with the names underneath it."""
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        join_season(conn, season, 1, NOW)
        join_season(conn, season, 2, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        place_pick(conn, season, 2, 1, "NE", NOW)
        join_season(conn, season, 3, THU + HOUR)  # a gauntlet arrival
        _finalize(conn, "g-thu", "SEA")
        _finalize(conn, "g-mon", "KC")
        run_settle(conn, season, AFTER_W1)  # 1 survives, 2 dies

        full = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert (full["before"], full["after"]) == (3, 2)
        assert [a["user_id"] for a in full["arrivals"]] == [3]

        data = build_reckoning_data(conn, season, 1, AFTER_W1, hidden={2, 3})
        assert data["deaths"] == []               # the dead leaver is gone
        assert [e["user_id"] for e in data["ledger"]] == [1]
        assert data["arrivals"] == []             # so is the arrival
        # 1 survivor listed, 1 counted, 0 deaths: the numbers add up.
        assert (data["before"], data["after"]) == (1, 1)
        assert named_ids(data) == [1]


def test_a_departed_ghost_leaves_the_streak_strip(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        _settled_week1(conn, season)  # player 2 is a ghost from week 1
        season = get_season(conn, season["id"])
        place_pick(conn, season, 2, 2, "SF", AFTER_W1)
        _finalize(conn, "g2", "SF")
        run_settle(conn, season, NOW + 13 * DAY)

        full = build_reckoning_data(conn, season, 2, NOW + 13 * DAY)
        assert [uid for uid, _ in full["streak_strip"]] == [2]
        assert full["streak_record"] == 1

        data = build_reckoning_data(
            conn, season, 2, NOW + 13 * DAY, hidden={2}
        )
        assert data["streak_strip"] == []
        # The record goes with them: it was their streak.
        assert data["streak_record"] == 0


# ── Survivor hosts (spec §3.4, 2026-09-12) ─────────────────────────────
#
# While a guild has hosts: a pick-status DM rides Saturday's last call, a
# heads-up goes out Monday evening, and Tuesday's Reckoning is DM'd to the
# hosts to post INSTEAD of the channel post — unless no host can be reached,
# in which case the bot posts it itself. With no hosts, nothing changes.

HOST = 9
MON_18 = MON + 6 * HOUR   # MON is Monday 12:00 UTC; the tests run at offset 0


def _host_dms(monkeypatch):
    """send_branded_dm that really reaches the fake member: a closed DM
    (Forbidden) comes back as None, as the real helper returns it."""
    import discord as _d

    async def _dm(member, *, db_path, guild, content=None, embed=None, **kw):
        try:
            await member.send(content, embed=embed, **kw)
        except _d.Forbidden:
            return None
        return object()

    monkeypatch.setattr(tasks, "send_branded_dm", _dm)


@pytest.mark.parametrize(
    ("now", "overrides", "force", "due"),
    [
        pytest.param(MON_18, {}, False, 1, id="monday-at-the-hour"),
        pytest.param(MON_18 - HOUR, {}, False, None, id="monday-before-the-hour"),
        pytest.param(MON - DAY, {}, False, None, id="sunday"),
        # No catch-up: the Reckoning itself supersedes a missed heads-up.
        pytest.param(MON_18 + 15 * HOUR, {}, False, None, id="tuesday-no-catch-up"),
        pytest.param(MON - DAY, {}, True, 1, id="forced-any-day"),
        pytest.param(
            MON_18, {"last_host_preview_week": 1}, False, None, id="once-per-week",
        ),
        # Week 1 hasn't kicked off: nothing to preview, forced or not.
        pytest.param(NOW, {}, True, None, id="before-first-kickoff"),
        # Week 1 reckoned; week 2's first game is a week away.
        pytest.param(
            MON_18, {"last_reckoned_week": 1}, True, None, id="next-week-not-started",
        ),
    ],
)
def test_host_preview_due(db, now, overrides, force, due):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        if overrides:
            update_config(conn, season["id"], overrides)
            season = get_season(conn, season["id"])
        assert tasks.host_preview_due(conn, season, now, 0.0, force=force) == due


@pytest.mark.parametrize(
    ("pickless", "expect"),
    [
        pytest.param(["Ann", "Bo"], ["1 of 3 alive have picked", "Ann, Bo"], id="some"),
        pytest.param([], ["3 of 3 alive", "Everyone still alive has picked"], id="all"),
    ],
)
def test_host_pick_status_text(pickless, expect):
    text = tasks.host_pick_status_text(4, 3, pickless)
    assert "Week 4" in text
    for part in expect:
        assert part in text
    assert "<@" not in text  # names, never pings


def test_reckoning_copy_text_is_the_card_without_spacers():
    import discord as _d

    from bot_modules.core.branding import apply_section_spacing

    embed = _d.Embed(title="🏈 Week 2 — The Reckoning", description="👥 Survivors 3 → 2")
    embed.add_field(name="📜 The Ledger", value="✅ Ann · SEA", inline=False)
    embed.add_field(name="🪦 Eliminations", value="Bo — NE lost.", inline=False)
    embed.set_footer(text="S • results post every Tuesday")
    apply_section_spacing(embed)

    text = reckoning_copy_text(embed)
    assert "\u200b" not in text
    assert text.startswith("**🏈 Week 2 — The Reckoning**\n\n👥 Survivors 3 → 2")
    assert "**📜 The Ledger**\n✅ Ann · SEA\n\n**🪦 Eliminations**\nBo — NE lost." in text
    assert "results post every Tuesday" not in text


def test_host_reckoning_messages_chunk_under_the_dm_cap():
    import discord as _d

    embed = _d.Embed(title="🏈 Week 9 — The Reckoning", description="toll")
    for i in range(6):
        embed.add_field(name=f"Section {i}", value="\n".join(["x" * 60] * 16), inline=False)
    messages = tasks.host_reckoning_messages(9, embed)
    assert messages[0]["embed"] is embed
    assert "Week 9" in messages[0]["content"]
    assert len(messages) > 2
    assert all(len(m["content"]) <= 2000 for m in messages)
    assert all("embed" not in m for m in messages[1:])


def test_weekly_clock_rows_show_the_host_heads_up_only_with_hosts():
    config = {"reckoning_hour": 9, "last_host_preview_week": 0}
    kwargs = dict(week=1, due={"host_preview": 1})
    without = tasks.weekly_clock_rows(config, _THU_10, _PT, **kwargs)
    assert "host_preview" not in [r["task"] for r in without]
    rows = tasks.weekly_clock_rows(config, _THU_10, _PT, with_hosts=True, **kwargs)
    assert [r["task"] for r in rows] == ["slate", "lastcall", "host_preview", "reckoning"]
    row = rows[2]
    assert row["hour"] == tasks.HOST_PREVIEW_HOUR and row["weekday"] == tasks.MONDAY
    assert row["due_week"] == 1 and not row["resettable"]


@pytest.mark.asyncio
async def test_reckoning_goes_to_the_host_instead_of_the_channel(db, monkeypatch):
    from bot_modules.economy import service as economy_service

    with open_db(db) as conn:
        season = _cfg_season(conn, strikes=0)
        _settled_week1(conn, season)  # 1 won (prize), 2 lost (dies)
        add_host(conn, GID, HOST, added_by=1)
        season = get_season(conn, season["id"])
    host = _FakeMember(HOST)
    guild = _ReckoningGuild([_FakeMember(1), _FakeMember(2), host])
    bot = _ReadyBot(guild)
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)
    _quiet_post_side_effects(monkeypatch)
    _host_dms(monkeypatch)

    label = await tasks.post_reckoning(bot, db, season, 1, AFTER_W1)

    assert label == "reckoning (sent to host)"
    assert guild.channel.sent == []
    (intro, intro_kw), *copies = host.sent
    assert "yours to post" in intro
    assert intro_kw["embed"].title == "🏈 Week 1 — The Reckoning"
    assert copies and copies[0][0].startswith("**🏈 Week 1 — The Reckoning**")
    # The week's writes happen exactly as for a channel post.
    with open_db(db) as conn:
        season = get_season(conn, season["id"])
        assert int(season["config"]["last_reckoned_week"]) == 1
        assert economy_service.get_balance(conn, GID, 1) == 25


@pytest.mark.parametrize(
    "host_state",
    [pytest.param("closed-dms", id="closed-dms"), pytest.param("left", id="left-server")],
)
@pytest.mark.asyncio
async def test_reckoning_falls_back_to_the_channel_when_no_host_is_reachable(
    db, monkeypatch, host_state
):
    with open_db(db) as conn:
        season = _cfg_season(conn, strikes=0)
        _settled_week1(conn, season)
        add_host(conn, GID, HOST, added_by=1)
        season = get_season(conn, season["id"])
    members = [_FakeMember(1), _FakeMember(2)]
    if host_state == "closed-dms":
        members.append(_FakeMember(HOST, fail=True))
    guild = _ReckoningGuild(members)
    bot = _ReadyBot(guild)
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)
    _quiet_post_side_effects(monkeypatch)
    _host_dms(monkeypatch)

    label = await tasks.post_reckoning(bot, db, season, 1, AFTER_W1)

    assert label == "reckoning"
    assert len(guild.channel.sent) == 1
    with open_db(db) as conn:
        season = get_season(conn, season["id"])
        assert int(season["config"]["last_reckoned_week"]) == 1


@pytest.mark.asyncio
async def test_a_host_holding_the_card_is_reached_even_if_the_copy_text_fails(
    db, monkeypatch
):
    """The card arrived, so the host can post from it — the bot must not
    post the same Reckoning publicly on top of that."""
    import discord as _d

    class _CardOnlyHost(_FakeMember):
        async def send(self, content=None, **kwargs):
            if "embed" not in kwargs or kwargs["embed"] is None:
                resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
                raise _d.Forbidden(resp, "closed mid-way")
            self.sent.append((content, kwargs))

    with open_db(db) as conn:
        season = _cfg_season(conn, strikes=0)
        _settled_week1(conn, season)
        add_host(conn, GID, HOST, added_by=1)
        season = get_season(conn, season["id"])
    host = _CardOnlyHost(HOST)
    guild = _ReckoningGuild([_FakeMember(1), _FakeMember(2), host])
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)
    _quiet_post_side_effects(monkeypatch)
    _host_dms(monkeypatch)

    label = await tasks.post_reckoning(_ReadyBot(guild), db, season, 1, AFTER_W1)

    assert label == "reckoning (sent to host)"
    assert len(host.sent) == 1
    assert guild.channel.sent == []


@pytest.mark.parametrize(
    ("picks", "expect"),
    [
        pytest.param([1], "1 of 2 alive have picked", id="one-pickless"),
        pytest.param([1, 2], "Everyone still alive has picked", id="all-picked"),
    ],
)
@pytest.mark.asyncio
async def test_last_call_sends_the_hosts_pick_status(db, monkeypatch, picks, expect):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        join_season(conn, season, 1, NOW)
        join_season(conn, season, 2, NOW)
        for uid in picks:
            place_pick(conn, season, uid, 1, "SEA", NOW)
        add_host(conn, GID, HOST, added_by=1)
        season = get_season(conn, season["id"])
    host = _FakeMember(HOST)
    guild = _FakeGuild(
        [_FakeMember(1), _FakeMember(2), host],
        int(season["config"]["channel_id"] or 0),
    )
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)

    assert await tasks.send_last_call(_FakeBot(guild), db, season, 1, NOW) is True

    (content, _), = host.sent
    assert expect in content
    if len(picks) == 1:
        assert "P2" in content and "P1" not in content


@pytest.mark.parametrize(
    ("closed", "mod_log", "label"),
    [
        pytest.param(False, True, "host heads-up", id="delivered"),
        # No host reachable: the card goes to the admins-only mod-log —
        # never the Survivor channel, since it shows live picks.
        pytest.param(
            True, True, "host heads-up (to mod-log: no host could be DM'd)",
            id="closed-dms-to-mod-log",
        ),
        pytest.param(True, False, "", id="closed-dms-no-mod-log-blocked"),
    ],
)
@pytest.mark.asyncio
async def test_host_preview_dms_the_card_and_marks_the_week(
    db, monkeypatch, closed, mod_log, label
):
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    with open_db(db) as conn:
        season = _cfg_season(conn)
        _settled_week1(conn, season)
        add_host(conn, GID, HOST, added_by=1)
        season = get_season(conn, season["id"])
    host = _FakeMember(HOST, fail=closed)
    guild = _ReckoningGuild([_FakeMember(1), _FakeMember(2), host])
    mod_channel = _FakeChannel(guild)
    _quiet_post_side_effects(monkeypatch)
    _host_dms(monkeypatch)
    monkeypatch.setattr(
        tasks, "_mod_log_channel", lambda b, g: mod_channel if mod_log else None
    )
    monkeypatch.setattr(tasks, "_channel", lambda b, se: guild.channel)

    got = await tasks.send_host_preview(_ReadyBot(guild), db, season, 1, MON_18)

    assert got == label
    assert guild.channel.sent == []
    if not closed:
        (content, kwargs), = host.sent
        assert mod_channel.sent == []
    elif mod_log:
        (content, kwargs), = mod_channel.sent
        assert "No Survivor host could be DM'd" in content
    if not closed or mod_log:
        assert "Week 1 heads-up" in content and "<t:" in content
        assert kwargs["embed"].title == "🏈 Week 1 — The Reckoning"
    # Marked either way — closed DMs must not be retried every tick.
    with open_db(db) as conn:
        season = get_season(conn, season["id"])
        assert int(season["config"]["last_host_preview_week"]) == 1
        assert tasks.host_preview_due(conn, season, MON_18, 0.0) is None
        # A preview, not a Reckoning: nothing reckoned, nobody paid.
        assert int(season["config"]["last_reckoned_week"]) == 0


@pytest.mark.parametrize(
    ("hosts", "now", "force", "fires"),
    [
        pytest.param(True, MON_18, False, True, id="host-monday"),
        pytest.param(False, MON_18, False, False, id="no-host-no-heads-up"),
        # Forced while week 1 is reckonable: the Reckoning reaches the host
        # in the same pass, so the preview of that same week is skipped.
        pytest.param(True, AFTER_W1, True, False, id="forced-reckoning-same-week"),
    ],
)
@pytest.mark.asyncio
async def test_run_weekly_tasks_fires_the_heads_up_only_for_hosts(
    db, monkeypatch, hosts, now, force, fires
):
    with open_db(db) as conn:
        _cfg_season(conn)
        if hosts:
            add_host(conn, GID, HOST, added_by=1)
    calls: list[str] = []

    def _stub(name, result):
        async def _run(*args, **kwargs):
            calls.append(name)
            return result
        return _run

    monkeypatch.setattr(tasks, "send_host_preview", _stub("preview", "host heads-up"))
    monkeypatch.setattr(tasks, "post_reckoning", _stub("reckoning", "reckoning"))
    monkeypatch.setattr(tasks, "post_slate", _stub("slate", True))
    monkeypatch.setattr(tasks, "send_last_call", _stub("lastcall", True))
    monkeypatch.setattr(tasks, "reconcile_roles", _stub("roles", None))

    report = await tasks.run_weekly_tasks(object(), db, now, force=force)

    assert ("preview" in calls) is fires
    (row,) = report
    assert ("host heads-up" in row["fired"]) is fires


@pytest.mark.asyncio
async def test_a_blocked_heads_up_names_its_own_reason(db, monkeypatch):
    with open_db(db) as conn:
        _cfg_season(conn)
        add_host(conn, GID, HOST, added_by=1)

    async def _blocked(*args, **kwargs):
        return ""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tasks, "send_host_preview", _blocked)
    monkeypatch.setattr(tasks, "reconcile_roles", _noop)

    (row,) = await tasks.run_weekly_tasks(object(), db, MON_18)

    assert row["blocked"] == ["host heads-up"]
    assert "mod-log" in row["reason"]
    assert "Survivor channel" not in row["reason"]
