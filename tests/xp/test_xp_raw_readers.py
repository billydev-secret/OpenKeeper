"""XP readers read ``xp_events`` alone, now that the daily rollup is retired.

The rollup (``xp_daily`` + ``xp_rollup_state``, migrations 186/187) existed so
an opt-in Event Retention setting could delete raw events behind a 90-day
horizon. The setting was never turned on, and the setting, the prune, the
nightly rollup and the readers' union with ``xp_daily`` were removed together.
The tables stay in prod with their last contents until a migration drops them.

So the property under test is that a stale ``xp_daily`` sitting next to a
complete ``xp_events`` is never read: not double-counted where it duplicates
raw history (which in prod it does, day for day), and not surfaced where it
holds a row raw does not. Reverting any reader to the union fails one of these.

The second half pins the 90-day window the XP hour-of-day histograms keep as a
display window, and the report caption that says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.analytics.activity_graphs import (
    XP_HISTOGRAM_WINDOW_DAYS,
    query_xp_activity,
    query_xp_all_time_with_breakdown,
    query_xp_histogram,
    query_xp_histogram_with_breakdown,
)
from bot_modules.analytics.reports_data import get_activity_data
from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import (
    get_oldest_xp_event_timestamp,
    get_time_to_level_details,
    get_user_xp_by_source,
    get_xp_leaderboard,
    has_any_xp_events,
)
from bot_modules.inactive.report_service import channel_activity_map

GUILD = 7001
EMPTY_GUILD = 7002
USER_A = 11
USER_B = 22
GHOST = 33  # present only in the stale rollup
CHAN = 555

NOW = datetime.now(timezone.utc).timestamp()


def _days_ago(n: int, hour: int = 12) -> float:
    base = datetime.fromtimestamp(NOW, timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return (base - timedelta(days=n)).timestamp()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _event(conn, *, days_ago, user=USER_A, source="text", amount=1.0, hour=12):
    conn.execute(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, channel_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, user, source, amount, _days_ago(days_ago, hour), CHAN),
    )


def _stale_bucket(conn, *, guild, user, days_ago, xp, source="text"):
    ts = _days_ago(days_ago, hour=1)
    conn.execute(
        "INSERT INTO xp_daily (guild_id, user_id, source, channel_id, day,"
        " xp, events, first_at, last_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (guild, user, source, CHAN, _day(ts), xp, ts, ts),
    )


def _seed(conn):
    """Raw history, plus the stale rollup prod is left holding.

    The rollup duplicates the old raw days (as prod's does), adds a member raw
    has never seen, and gives an otherwise empty guild a history. The state
    row claims full coverage, which is what made the old readers trust it.
    """
    _event(conn, days_ago=300, amount=100.0)
    _event(conn, days_ago=200, amount=100.0)
    _event(conn, days_ago=150, user=USER_B, amount=60.0, source="voice")
    _event(conn, days_ago=100, amount=100.0)
    _event(conn, days_ago=5, amount=3.0)

    _stale_bucket(conn, guild=GUILD, user=USER_A, days_ago=300, xp=100.0)
    _stale_bucket(conn, guild=GUILD, user=USER_A, days_ago=200, xp=100.0)
    _stale_bucket(conn, guild=GUILD, user=USER_B, days_ago=150, xp=60.0, source="voice")
    _stale_bucket(conn, guild=GUILD, user=USER_A, days_ago=100, xp=100.0)
    _stale_bucket(conn, guild=GUILD, user=GHOST, days_ago=400, xp=9999.0)
    _stale_bucket(conn, guild=EMPTY_GUILD, user=USER_A, days_ago=200, xp=50.0)
    conn.execute(
        "UPDATE xp_rollup_state SET rolled_through_day = ?, first_gap_day = NULL",
        (_day(_days_ago(1)),),
    )


def test_leaderboard_counts_raw_once_and_ignores_the_rollup(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        entries = {e.user_id: e.xp for e in get_xp_leaderboard(conn, GUILD, "text", limit=10)}

    assert entries == {USER_A: 303.0}


def test_by_source_reads_raw_only(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        assert get_user_xp_by_source(conn, GUILD, USER_A) == {"text": 303.0}
        assert get_user_xp_by_source(conn, GUILD, USER_B) == {"voice": 60.0}
        assert get_user_xp_by_source(conn, GUILD, GHOST) == {}


@pytest.mark.parametrize(
    "guild,expected",
    [
        pytest.param(GUILD, True, id="raw-history"),
        pytest.param(EMPTY_GUILD, False, id="rollup-only-is-no-history"),
    ],
)
def test_has_any_xp_events(sync_db_path, guild, expected):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        assert has_any_xp_events(conn, guild) is expected


def test_oldest_event_is_the_oldest_raw_event(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        assert get_oldest_xp_event_timestamp(conn, GUILD) == _days_ago(300)


def test_time_to_level_is_exact_from_raw(sync_db_path):
    """Level 5 needs 250 XP by default: crossed by the third raw event."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        rows = get_time_to_level_details(conn, GUILD, 5)

    assert [(r["user_id"], r["first_at"], r["reached_at"]) for r in rows] == [
        (USER_A, _days_ago(300), _days_ago(100)),
    ]


def test_inactive_report_reads_raw_last_activity(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        act = channel_activity_map(conn, GUILD, [USER_A, USER_B, GHOST], CHAN)

    assert {uid: a.created_at for uid, a in act.items()} == {
        USER_A: _days_ago(5),
        USER_B: _days_ago(150),
    }


def test_month_graph_totals_raw_only(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _, totals, _ = query_xp_activity(conn, GUILD, "month")

    # 12 months back reaches the 300-day event; the 400-day ghost is out of
    # the window anyway, so a double count of the duplicated days is the tell.
    assert sum(totals) == pytest.approx(363.0)


def test_all_time_breakdown_starts_at_the_first_raw_event(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        labels, totals, by_source, _ = query_xp_all_time_with_breakdown(conn, GUILD)

    assert sum(totals) == pytest.approx(363.0)
    assert sum(by_source["text"]) == pytest.approx(303.0)
    # Starting at the ghost's 400-day bucket would add ~14 empty weeks.
    assert len(labels) <= 300 // 7 + 1


# ── the XP histograms keep a 90-day display window ─────────────────────


def test_xp_histogram_is_limited_to_the_window(sync_db_path):
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=XP_HISTOGRAM_WINDOW_DAYS + 10, hour=3, amount=50.0)
        _event(conn, days_ago=5, hour=3, amount=2.0)
        _, totals = query_xp_histogram(conn, GUILD, "hour_of_day")

    assert totals[3] == 2.0
    assert sum(totals) == 2.0


def test_xp_histogram_breakdown_is_limited_to_the_window(sync_db_path):
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=XP_HISTOGRAM_WINDOW_DAYS + 10, hour=3, amount=50.0)
        _event(conn, days_ago=5, hour=3, amount=2.0, source="voice")
        _, totals, by_source = query_xp_histogram_with_breakdown(
            conn, GUILD, "hour_of_day"
        )

    assert set(by_source) == {"voice"}
    assert sum(totals) == 2.0


def test_the_report_says_the_xp_histogram_is_windowed(sync_db_path):
    """A narrowed window that the chart title doesn't mention is the real bug."""
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=5, hour=3, amount=2.0)
        xp = get_activity_data(conn, GUILD, "hour_of_day", 0.0, mode="xp")
        messages = get_activity_data(
            conn, GUILD, "hour_of_day", 0.0, mode="messages"
        )

    assert xp["window_label"] == f"By Hour of Day (last {XP_HISTOGRAM_WINDOW_DAYS} days)"
    assert messages["window_label"] == "By Hour of Day"
