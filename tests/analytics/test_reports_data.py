from __future__ import annotations

import sqlite3
import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.shared.message_store import init_member_events_table, init_message_tables, record_member_event, store_message
from bot_modules.core.channel_rollup import build_resolver
from bot_modules.invites.tracker import record_invite
from bot_modules.analytics.reports_data import get_channel_comparison_data, get_greeter_log_sessions, get_greeter_response_data, get_invite_effectiveness_data, get_xp_leaderboard_data
from tests.db_template import migrated_db
from web_server.schemas import InviteEffectivenessResponse


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_message_tables(c)
    init_member_events_table(c)
    yield c
    c.close()


def _join(conn, user_id: int, ts: float) -> None:
    record_member_event(conn, 1, user_id, "join", ts)


def _leave(conn, user_id: int, ts: float) -> None:
    record_member_event(conn, 1, user_id, "leave", ts)


def _store(conn, *, message_id: int, channel_id: int, author_id: int, ts: int) -> None:
    store_message(
        conn,
        message_id=message_id,
        guild_id=1,
        channel_id=channel_id,
        author_id=author_id,
        reply_to_id=None,
        ts=ts,
        mention_ids=[],
    )


def test_greeter_response_tracks_greeted_left_and_waiting_sessions(conn):
    greeter_channel_id = 20
    greeter_id = 900

    _join(conn, user_id=100, ts=100)
    _store(conn, message_id=2, channel_id=greeter_channel_id, author_id=greeter_id, ts=220)
    _join(conn, user_id=101, ts=300)
    _leave(conn, user_id=101, ts=450)
    _join(conn, user_id=102, ts=500)

    sessions = get_greeter_log_sessions(conn, guild_id=1)
    data = get_greeter_response_data(
        conn,
        guild_id=1,
        greeter_channel_id=greeter_channel_id,
        greeter_ids={greeter_id},
        sessions=sessions,
        now_ts=900,
    )

    assert len(sessions) == 3
    assert data["total_joins"] == 3
    assert data["count"] == 1
    assert data["left_before_greeting_count"] == 1
    assert data["awaiting_greeting_count"] == 1
    assert data["response_times_seconds"] == [120.0]

    assert data["entries"][0]["user_id"] == "102"
    assert data["entries"][0]["status"] == "awaiting_greeting"
    assert data["entries"][0]["wait_seconds"] == 400

    assert data["entries"][1]["user_id"] == "101"
    assert data["entries"][1]["status"] == "left_before_greeting"
    assert data["entries"][1]["left_at"] == 450
    assert data["entries"][1]["wait_seconds"] == 150

    assert data["entries"][2]["user_id"] == "100"
    assert data["entries"][2]["status"] == "greeted"
    assert data["entries"][2]["greeted_at"] == 220
    assert data["entries"][2]["response_seconds"] == 120
    assert data["entries"][2]["greeter_id"] == str(greeter_id)


def test_since_ts_filters_old_joins(conn):
    _join(conn, user_id=1, ts=100)
    _join(conn, user_id=2, ts=500)

    sessions = get_greeter_log_sessions(conn, guild_id=1, since_ts=400)
    assert len(sessions) == 1
    assert sessions[0]["user_id"] == 2


def test_rejoin_pairs_correctly(conn):
    _join(conn, user_id=1, ts=100)
    _leave(conn, user_id=1, ts=200)
    _join(conn, user_id=1, ts=300)

    sessions = get_greeter_log_sessions(conn, guild_id=1)
    assert len(sessions) == 2
    assert sessions[0]["left_at"] == 200.0
    assert sessions[1]["left_at"] is None


# ── Channel comparison: thread attribution ───────────────────────────
#
# The panel's rows are meant to be channels. A message posted in a thread
# carries the thread's own id, so without folding, every thread became a row
# of its own — see services/channel_rollup and todo #91.

CH = 100
THREAD = 101


@pytest.fixture
def cc_conn(tmp_path):
    """Migrated DB for the channel-comparison report."""
    path = tmp_path / "cc.db"
    migrated_db(path)
    with open_db(path) as c:
        yield c


def _cc_message(conn, *, mid: int, cid: int, aid: int, ts: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(message_id, guild_id, channel_id, author_id, reply_to_id, ts)"
        " VALUES (?, 1, ?, ?, NULL, ?)",
        (mid, cid, aid, ts),
    )


def _cc_thread(conn, *, thread_id: int, parent_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO known_channels "
        "(guild_id, channel_id, channel_name, updated_at, parent_id, is_thread)"
        " VALUES (1, ?, '', 0, ?, 1)",
        (thread_id, parent_id),
    )


def _compare(conn, live_ids):
    return get_channel_comparison_data(
        conn,
        1,
        days=30,
        resolver=build_resolver(conn, 1, live_channel_ids=live_ids),
    )


def test_comparison_attributes_thread_messages_to_the_parent(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    _cc_message(cc_conn, mid=2, cid=THREAD, aid=2, ts=now - 120)
    _cc_message(cc_conn, mid=3, cid=THREAD, aid=3, ts=now - 180)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    rows = _compare(cc_conn, [CH])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH)]
    assert rows[0]["message_count"] == 3
    assert rows[0]["unique_authors"] == 3


def test_comparison_counts_a_shared_author_once(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    _cc_message(cc_conn, mid=2, cid=THREAD, aid=1, ts=now - 120)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    rows = _compare(cc_conn, [CH])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH)]
    assert rows[0]["message_count"] == 2
    assert rows[0]["unique_authors"] == 1
    # One author holding every message is maximal concentration, and the Gini
    # must see the merged distribution rather than two single-author channels.
    assert rows[0]["gini"] == 0.0


def test_comparison_weights_sentiment_by_volume_not_by_channel(cc_conn):
    # A three-word thread must not drag the parent's sentiment as hard as the
    # parent's own hundred messages. Averaging two averages would let it.
    now = int(time.time())
    for mid in range(1, 10):
        _cc_message(cc_conn, mid=mid, cid=CH, aid=1, ts=now - 60)
        cc_conn.execute(
            "INSERT INTO message_sentiment "
            "(message_id, guild_id, channel_id, sentiment, emotion, computed_at)"
            " VALUES (?, 1, ?, 1.0, '', ?)",
            (mid, CH, now),
        )
    _cc_message(cc_conn, mid=99, cid=THREAD, aid=2, ts=now - 60)
    cc_conn.execute(
        "INSERT INTO message_sentiment "
        "(message_id, guild_id, channel_id, sentiment, emotion, computed_at)"
        " VALUES (99, 1, ?, -1.0, '', ?)",
        (THREAD, now),
    )
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    rows = _compare(cc_conn, [CH])["channels"]

    # Nine at +1 and one at -1 → 0.8, not the 0.0 of averaged averages.
    assert rows[0]["avg_sentiment"] == 0.8


def test_comparison_drops_a_thread_whose_parent_is_gone(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=THREAD, aid=1, ts=now - 60)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    # The guild still has channels — just not this thread's parent, so there is
    # nothing left to attribute the thread's messages to.
    rows = _compare(cc_conn, [999])["channels"]

    assert rows == []


def test_comparison_drops_a_pen_pals_channel(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    _cc_message(cc_conn, mid=2, cid=555, aid=2, ts=now - 60)
    cc_conn.execute(
        "INSERT INTO pen_pals_sessions "
        "(session_id, guild_id, channel_id, user1_id, user2_id, started_at,"
        " expiry_at, next_question_at) VALUES ('s1', 1, 555, 1, 2, 0, 0, 0)"
    )

    rows = _compare(cc_conn, [CH, 555])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH)]


def test_comparison_ranks_by_the_folded_total(cc_conn):
    # #100 loses on its own messages but wins once its thread is counted, so
    # the ordering cannot be the SQL's.
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    for mid in (2, 3):
        _cc_message(cc_conn, mid=mid, cid=THREAD, aid=1, ts=now - 60)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)
    for mid in (10, 11):
        _cc_message(cc_conn, mid=mid, cid=200, aid=2, ts=now - 60)

    rows = _compare(cc_conn, [CH, 200])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH), "200"]


@pytest.fixture
def xp_conn(tmp_path):
    """Migrated DB with XP tables — for the xp-leaderboard report."""
    path = tmp_path / "xp.db"
    migrated_db(path)
    with open_db(path) as c:
        yield c


def _award_xp(conn, guild_id, user_id, *, level, amount, ts, source="text"):
    conn.execute(
        "INSERT INTO member_xp (guild_id, user_id, total_xp, level) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, amount, level),
    )
    conn.execute(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, source, amount, ts),
    )


def test_xp_level_distribution_excludes_members_inactive_past_the_window(xp_conn):
    """Billy: 'We have this level distribution here, and it includes everyone.
    Maybe it's better for people active over the last thirty days, not
    everyone.' A member whose only XP event is 90 days old drops out of the
    distribution even though their member_xp row (and level) still exists."""
    now = time.time()
    _award_xp(xp_conn, 1, 100, level=5, amount=500, ts=now - 1 * 86400)
    _award_xp(xp_conn, 1, 200, level=9, amount=900, ts=now - 90 * 86400)
    xp_conn.commit()

    data = get_xp_leaderboard_data(xp_conn, guild_id=1)

    levels = {b["level"]: b["count"] for b in data["level_distribution"]}
    assert levels == {5: 1}
    assert data["level_distribution_active_days"] == 30


def test_xp_level_distribution_window_is_independent_of_the_days_filter(xp_conn):
    """The report's own time-period selector (`days`) scopes the leaderboard
    table and source totals; the level distribution's 30-day active window is
    fixed and does not shrink or grow with it."""
    now = time.time()
    _award_xp(xp_conn, 1, 300, level=3, amount=300, ts=now - 20 * 86400)
    xp_conn.commit()

    data = get_xp_leaderboard_data(xp_conn, guild_id=1, days=7)

    levels = {b["level"]: b["count"] for b in data["level_distribution"]}
    assert levels == {3: 1}


def test_xp_level_distribution_empty_when_nobody_is_active(xp_conn):
    now = time.time()
    _award_xp(xp_conn, 1, 400, level=2, amount=200, ts=now - 60 * 86400)
    xp_conn.commit()

    data = get_xp_leaderboard_data(xp_conn, guild_id=1)

    assert data["level_distribution"] == []
    assert data["level_distribution_active_days"] == 30


@pytest.fixture
def ie_conn(tmp_path):
    """Migrated DB with invite_edges + member_activity — for the
    invite-effectiveness report."""
    path = tmp_path / "ie.db"
    migrated_db(path)
    with open_db(path) as c:
        yield c


def _mark_active(conn, guild_id, user_id, ts):
    conn.execute(
        "INSERT INTO member_activity (guild_id, user_id, last_channel_id, last_message_id, last_message_at)"
        " VALUES (?, ?, 0, 0, ?)",
        (guild_id, user_id, ts),
    )


def test_invite_effectiveness_service_returns_invitees_per_inviter(ie_conn):
    """The service layer already carries the per-invitee detail the
    dashboard's expand row needs — this is the fixture the response schema
    test below re-uses to prove the layer *above* it drops that detail."""
    now = time.time()
    record_invite(ie_conn, guild_id=1, inviter_id=900, invitee_id=1, joined_at=now)
    record_invite(ie_conn, guild_id=1, inviter_id=900, invitee_id=2, joined_at=now)
    _mark_active(ie_conn, 1, 1, now)
    ie_conn.commit()

    data = get_invite_effectiveness_data(ie_conn, guild_id=1)

    assert data["total_invites"] == 2
    assert data["total_active"] == 1
    row = data["inviters"][0]
    assert row["invite_count"] == 2
    invitee_ids = {i["invitee_id"] for i in row["invitees"]}
    assert invitee_ids == {"1", "2"}


def test_invite_effectiveness_response_schema_preserves_invitees(ie_conn):
    """Billy: 'it says this board has twenty eight invites, seven still
    active, and I hit the drop down. It says no joins recorded.'

    The route returns ``get_invite_effectiveness_data``'s dict straight
    through FastAPI's ``response_model=InviteEffectivenessResponse``, which
    silently drops any dict key its schema doesn't declare. ``InviterRowSchema``
    never declared ``invitees``, so every expand row rendered the empty state
    no matter how many joins the inviter actually had — this reproduces that
    at the schema layer the route depends on, without touching HTTP or the
    panel.
    """
    now = time.time()
    record_invite(ie_conn, guild_id=1, inviter_id=900, invitee_id=1, joined_at=now)
    record_invite(ie_conn, guild_id=1, inviter_id=900, invitee_id=2, joined_at=now)
    _mark_active(ie_conn, 1, 1, now)
    ie_conn.commit()

    data = get_invite_effectiveness_data(ie_conn, guild_id=1)
    assert data["inviters"][0]["invitees"], "fixture setup: service must produce invitees"

    served = InviteEffectivenessResponse.model_validate(data).model_dump()

    # The summary totals (what Billy read as "28 invites, 7 still active")
    # survive because they're top-level fields on the response schema.
    assert served["total_invites"] == data["total_invites"]
    assert served["total_active"] == data["total_active"]

    # The per-inviter invitee list (what the expand arrow renders) must
    # survive the same round trip — before the fix this comes back empty.
    served_invitees = served["inviters"][0]["invitees"]
    assert {i["invitee_id"] for i in served_invitees} == {"1", "2"}
