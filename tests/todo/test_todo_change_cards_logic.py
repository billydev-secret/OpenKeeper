"""Tests for the todo board's change cards (todo/change_logic.py + its queries).

The behaviour worth pinning down here is mostly about *restraint* — what the
cards refuse to say, and when they refuse to say anything at all. Those are the
rules a later cleanup pass is most likely to "fix" by accident, so each one has
a test that names why it exists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.confessions.service import pending_confession_count
from bot_modules.todo.service import (
    BUG_CHANGE_CARDS_KEY,
    CHANGE_CARDS_KEY,
    LIST_BUG,
    LIST_TODO,
    change_cards_enabled,
    chore_changes_since,
    complete_todo,
    create_todo,
    get_board,
    guilds_with_change_cards,
    pending_todos,
    save_board,
    save_change_cursors,
)
from bot_modules.todo.change_logic import (
    SEND_GIVE_UP,
    SEND_OK,
    SEND_RETRY,
    CardPlan,
    cursor_after,
    plan_cards,
    subject_id,
    subject_ids,
    CONFESSION_MARKER,
    MAX_CARD_LINES,
    approval_line,
    bug_line,
    plan_bug_cards,
    confession_line,
    local_noon_before,
    plan_arrival_card,
    plan_digest_card,
    signoff_line,
    task_line,
)
from tests.db_template import migrated_db

GUILD = 123
USER = 9001
NOW = 1_757_700_000.0  # 2025-09-12 ~19:20 UTC


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


# ── the dark-launch guards ────────────────────────────────────────────
#
# These two are the whole "ship it dark" promise. If either regresses, ticking
# the dial on in a guild with a queue makes the feature's first act a wall of
# cards about items that had been waiting for days.


def test_null_cursor_posts_nothing_even_with_arrivals():
    plan = plan_arrival_card(
        now=NOW, cursor_at=None, task_lines=["➕ **Billy** added Thing"]
    )
    assert plan.posts is False
    assert plan.body is None


def test_null_cursor_banks_the_clock_so_the_next_tick_has_a_window():
    plan = plan_arrival_card(now=NOW, cursor_at=None)
    assert plan.cursor_at == NOW


def test_null_digest_at_posts_nothing_even_with_chores():
    plan = plan_digest_card(
        now=NOW, digest_at=None, tz_offset_hours=0.0, spawned=2, missed=1
    )
    assert plan.posts is False
    assert plan.cursor_at == NOW


# ── arrivals ──────────────────────────────────────────────────────────


def test_quiet_window_posts_nothing_but_still_advances():
    plan = plan_arrival_card(now=NOW, cursor_at=NOW - 60)
    assert plan.posts is False
    # Left behind, every later tick re-scans an ever-widening window for nothing.
    assert plan.cursor_at == NOW


def test_one_arrival_reads_singular():
    plan = plan_arrival_card(now=NOW, cursor_at=NOW - 60, task_lines=["a"])
    assert plan.title is not None
    assert "1 new item needs" in plan.title


def test_a_burst_is_one_card_not_one_each():
    """Six sign-offs in one window cost one card — the chattiness guard."""
    plan = plan_arrival_card(
        now=NOW, cursor_at=NOW - 60, signoff_lines=[f"s{i}" for i in range(6)]
    )
    assert plan.title is not None
    assert "6 new items" in plan.title
    assert plan.body is not None
    assert plan.body.count("\n") == 5


def test_every_section_lands_in_one_card():
    plan = plan_arrival_card(
        now=NOW,
        cursor_at=NOW - 60,
        task_lines=["t"],
        signoff_lines=["s"],
        approval_lines=["a"],
        confession_count=1,
    )
    assert plan.body is not None
    for expected in ("t", "s", "a", CONFESSION_MARKER):
        assert expected in plan.body


def test_the_title_counts_items_not_lines():
    """A confession is one line but may be several items."""
    plan = plan_arrival_card(now=NOW, cursor_at=NOW - 60, confession_count=4)
    assert plan.title is not None
    assert "4 new items" in plan.title
    assert plan.body is not None
    assert plan.body.count("\n") == 0


def test_overflow_defers_to_the_board_below():
    """The pointer has to say *below*. A card is posted and the board is then
    re-stuck beneath it, so the live view is the channel's last message and the
    cards sit above it — sending a mod upwards would send them into the history
    they just read."""
    lines = [f"line{i}" for i in range(MAX_CARD_LINES + 5)]
    plan = plan_arrival_card(now=NOW, cursor_at=NOW - 60, task_lines=lines)
    assert plan.body is not None
    assert "…and **5** more on the board below." in plan.body
    assert "board above" not in plan.body
    assert "line19" not in plan.body


def test_cursor_is_the_tick_clock_not_the_newest_row():
    """Rows sharing a timestamp, or beyond the fetch ceiling, would fall through
    a max-row-timestamp cursor. ``now`` can neither skip nor repeat one."""
    plan = plan_arrival_card(now=NOW, cursor_at=NOW - 60, task_lines=["a"])
    assert plan.cursor_at == NOW


# ── line shapes ───────────────────────────────────────────────────────


def test_a_mod_added_task_names_the_adder():
    line = task_line({"task": "Check backups"}, name="Billy")
    assert "**Billy** added Check backups" in line


def test_a_paid_order_says_ordered_not_added():
    """A shop purchase is not something the mod team chose to take on, and the
    board already marks it separately because coins are in escrow behind it."""
    line = task_line({"task": "Custom role colour", "purchase_id": 7}, name="Ben")
    assert "ordered" in line
    assert "added" not in line


def test_an_unknown_member_reads_as_someone_never_a_bare_id():
    """An embed cannot carry a <@id> — it renders as a number to anyone who has
    not seen that user. An erased buyer must degrade to a word, not an id."""
    assert "someone" in task_line({"task": "x"}, name="")
    assert "someone" in signoff_line({"quest_title": "q"}, name="")
    assert "someone" in approval_line({"summary": "s"}, name="", label="🎨 Theme")


def test_a_long_task_is_clipped_not_left_to_own_the_screen():
    line = task_line({"task": "x" * 200}, name="Billy")
    assert "…" in line
    assert len(line) < 120


def test_a_task_lines_markdown_is_escaped():
    """task is member/mod-typed free text landing in a card's live
    description markdown, same as bug_line — a task of ``**bold**`` must not
    bend the channel's formatting."""
    line = task_line({"task": "**bold** and `code` and __underline__"}, name="Billy")
    assert r"\*\*bold\*\* and \`code\` and \_\_underline\_\_" in line
    assert "**bold**" not in line


def test_a_signoff_lines_title_markdown_is_escaped():
    """quest_title is dashboard-authored free text, same column as
    board_logic.render_signoff_rows' title."""
    line = signoff_line({"quest_title": "**FAKE MOD NOTICE**"}, name="Billy")
    assert r"\*\*FAKE MOD NOTICE\*\*" in line
    assert "**FAKE MOD NOTICE**" not in line


def test_an_approval_lines_summary_markdown_is_escaped():
    """summary is member-paid free text, same column as
    board_logic.render_approval_rows' what."""
    line = approval_line(
        {"summary": "**FAKE MOD NOTICE**"}, name="Billy", label="🎨 Theme"
    )
    assert r"\*\*FAKE MOD NOTICE\*\*" in line
    assert "**FAKE MOD NOTICE**" not in line


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 new confession waiting"), (3, "3 new confessions waiting")],
)
def test_confession_line_pluralises(count, expected):
    assert expected in confession_line(count)


def test_confession_card_never_quotes_the_confession_or_names_anyone():
    """DELIBERATE ANOMALY — do not "fix" this for consistency.

    ``pending_confessions`` returns no author id at all, so that approving a
    confession cannot become a second way to de-anonymise one. The *content* is
    withheld on top of that, which is stricter than the board section above
    (which does quote it): a board section clears when a mod resolves the
    confession, but a change card is permanent, so quoting one here would leave
    a denied-and-deleted confession printed in the channel forever.

    This test is what a stranger's cleanup pass will actually run.
    """
    secret = "I once ate the last biscuit"
    line = confession_line(2)
    assert secret not in line
    assert "2" in line
    # And the planner must not be able to smuggle it in through any other path.
    plan = plan_arrival_card(now=NOW, cursor_at=NOW - 60, confession_count=2)
    assert plan.body is not None
    assert secret not in plan.body


# ── the noon digest ───────────────────────────────────────────────────


def test_noon_is_guild_local_not_utc():
    """Offset -7 means local noon is 19:00 UTC."""
    noon = local_noon_before(NOW, -7.0)
    assert (NOW - noon) < 86400
    # 19:00 UTC on the same day.
    assert noon % 86400 == 19 * 3600


def test_morning_resolves_to_yesterdays_noon():
    """So the digest fires exactly once per local day rather than twice."""
    nine_am_utc = (NOW // 86400) * 86400 + 9 * 3600
    noon = local_noon_before(nine_am_utc, 0.0)
    assert noon < nine_am_utc
    assert nine_am_utc - noon > 20 * 3600


def test_digest_does_not_repeat_once_it_has_gone_out_today():
    noon = local_noon_before(NOW, 0.0)
    plan = plan_digest_card(
        now=NOW, digest_at=noon + 10, tz_offset_hours=0.0, spawned=2
    )
    assert plan.posts is False
    # And it must not move the cursor either, or the window would be lost.
    assert plan.cursor_at is None


def test_digest_fires_once_noon_has_passed():
    noon = local_noon_before(NOW, 0.0)
    plan = plan_digest_card(
        now=NOW, digest_at=noon - 3600, tz_offset_hours=0.0, spawned=2, missed=1
    )
    assert plan.posts is True
    assert plan.body is not None
    assert "2 chores posted" in plan.body
    assert "1 chore went unticked" in plan.body


def test_a_guild_with_no_chores_gets_no_daily_nothing_happened_card():
    noon = local_noon_before(NOW, 0.0)
    plan = plan_digest_card(now=NOW, digest_at=noon - 3600, tz_offset_hours=0.0)
    assert plan.posts is False
    # Banked anyway, so the window does not widen forever.
    assert plan.cursor_at == NOW


def test_completions_are_never_announced_anywhere():
    """Billy's call: ticking a task off posts nothing. 41 ticks landed in one
    day in prod, and the board showing the row gone is the notification."""
    arrival = plan_arrival_card(now=NOW, cursor_at=NOW - 60)
    digest = plan_digest_card(
        now=NOW,
        digest_at=local_noon_before(NOW, 0.0) - 3600,
        tz_offset_hours=0.0,
        spawned=0,
        missed=0,
    )
    assert arrival.posts is False
    assert digest.posts is False


# ── the dial ──────────────────────────────────────────────────────────


def test_dial_is_off_when_no_row_exists(db):
    with open_db(db) as conn:
        assert change_cards_enabled(conn, GUILD) is False
        assert guilds_with_change_cards(conn) == []


def test_dial_reads_on_when_set(db):
    with open_db(db) as conn:
        set_config_value(conn, CHANGE_CARDS_KEY, "1", GUILD)
        assert change_cards_enabled(conn, GUILD) is True
        assert guilds_with_change_cards(conn) == [GUILD]


@pytest.mark.parametrize(
    "stored", ["0", "", "false", "False", "off", "no", "OFF", "FALSE"]
)
def test_off_values_do_not_enable_the_poll(db, stored):
    """Truthiness is ``parse_bool``'s allow-list, so anything that is not one of
    1/true/yes/on is off. A deny-list would read "off" and "no" as **on**, and
    these dials do get set by hand in the prod config table — the failure mode
    is the bot starting to post into a mod channel on the strength of a word
    that plainly says not to."""
    with open_db(db) as conn:
        set_config_value(conn, CHANGE_CARDS_KEY, stored, GUILD)
        assert change_cards_enabled(conn, GUILD) is False
        assert guilds_with_change_cards(conn) == []


@pytest.mark.parametrize("stored", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_the_panel_and_the_poll_never_disagree_about_a_stored_value(db, stored):
    """Two readers of one key is the repo's most common config bug. Both go
    through ``parse_bool``, so the loop cannot poll a guild whose own panel
    shows the box unticked, or skip one where it shows ticked."""
    with open_db(db) as conn:
        set_config_value(conn, CHANGE_CARDS_KEY, stored, GUILD)
        assert change_cards_enabled(conn, GUILD) is True
        assert guilds_with_change_cards(conn) == [GUILD]


def test_dial_is_not_inherited_from_the_legacy_guild_zero_row(db):
    """This dial makes the bot post. Inheriting it would switch the feature on
    in guilds nobody configured."""
    with open_db(db) as conn:
        set_config_value(conn, CHANGE_CARDS_KEY, "1", 0)
        assert change_cards_enabled(conn, GUILD) is False


# ── the cursors ───────────────────────────────────────────────────────


def test_a_fresh_board_row_ships_both_cursors_null(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 666)
        board = get_board(conn, GUILD)
    assert board.change_cursor_at is None
    assert board.digest_at is None


def test_saving_one_cursor_leaves_the_other_alone(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 666)
        save_change_cursors(conn, GUILD, change_cursor_at=NOW)
        save_change_cursors(conn, GUILD, digest_at=NOW + 5)
        board = get_board(conn, GUILD)
    assert board.change_cursor_at == NOW
    assert board.digest_at == NOW + 5


def test_a_repost_does_not_move_the_announcement_watermark(db):
    """``save_board`` is called by the sticky machinery on every repost. If it
    touched the cursor, a routine reposition would skip items silently."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 666)
        save_change_cursors(conn, GUILD, change_cursor_at=NOW)
        save_board(conn, GUILD, 555, 777)
        board = get_board(conn, GUILD)
    assert board.change_cursor_at == NOW


def test_cursor_write_on_a_guild_with_no_board_creates_nothing(db):
    with open_db(db) as conn:
        save_change_cursors(conn, GUILD, change_cursor_at=NOW)
        assert get_board(conn, GUILD).posted is False


# ── the arrival queries ───────────────────────────────────────────────


def test_since_ts_finds_only_what_arrived_after_it(db):
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "old", now_ts=NOW - 600)
        create_todo(conn, GUILD, USER, "new", now_ts=NOW - 10)
        rows = pending_todos(conn, GUILD, since_ts=NOW - 60)
    assert [r["task"] for r in rows] == ["new"]


def test_an_item_handled_inside_the_window_is_never_announced(db):
    """A card saying a task needs doing, posted after somebody did it, is worse
    than no card — so the age filter stacks with the outstanding predicate."""
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "quick", now_ts=NOW - 10)
        complete_todo(conn, todo_id, GUILD, USER)
        rows = pending_todos(conn, GUILD, since_ts=NOW - 60)
    assert rows == []


def test_arrival_query_carries_the_adder_so_the_card_can_name_them(db):
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "thing", now_ts=NOW - 10)
        rows = pending_todos(conn, GUILD, since_ts=NOW - 60)
    assert rows[0]["added_by"] == USER


def test_confession_count_narrows_by_age(db):
    with open_db(db) as conn:
        for ts, text in ((NOW - 600, "old"), (NOW - 10, "new")):
            conn.execute(
                "INSERT INTO confession_pending (guild_id, author_id, content,"
                " created_at) VALUES (?, ?, ?, ?)",
                (GUILD, USER, text, ts),
            )
        assert pending_confession_count(conn, GUILD) == 2
        assert pending_confession_count(conn, GUILD, since_ts=NOW - 60) == 1


def test_chore_changes_counts_spawned_and_missed_separately(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO todo_recurring (guild_id, task, recurrence, time_of_day,"
            " status, next_run_at, created_at, created_by)"
            " VALUES (?, 'Do a QOTD', 'daily', 720, 'active', ?, ?, ?)",
            (GUILD, NOW, NOW, USER),
        )
        rec_id = conn.execute("SELECT id FROM todo_recurring").fetchone()["id"]
        fresh = create_todo(conn, GUILD, USER, "chore new", now_ts=NOW - 10)
        conn.execute(
            "UPDATE todos SET recurring_id = ? WHERE id = ?", (rec_id, fresh)
        )
        stale = create_todo(conn, GUILD, USER, "chore old", now_ts=NOW - 90_000)
        conn.execute(
            "UPDATE todos SET recurring_id = ?, missed_at = ? WHERE id = ?",
            (rec_id, NOW - 20, stale),
        )
        spawned, missed = chore_changes_since(conn, GUILD, NOW - 60)
    assert (spawned, missed) == (1, 1)


def test_chore_changes_ignores_ordinary_tasks(db):
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "not a chore", now_ts=NOW - 10)
        assert chore_changes_since(conn, GUILD, NOW - 60) == (0, 0)


# ── the whole pass: who gets named, and when the watermark moves ──────────────
#
# These three rules used to live in the cog, where the only way to prove them
# was a Discord mock. They are decisions, so they belong here.


def _pass_data(**over):
    data = {
        "now": NOW,
        "channel_id": 555,
        "cursor_at": NOW - 60,
        "digest_at": NOW,
        "tz_offset_hours": 0.0,
        "tasks": [],
        "signoffs": [],
        "approvals": [],
        "confessions": 0,
        "spawned": 0,
        "missed": 0,
    }
    data.update(over)
    return data


def _names(mapping):
    return lambda uid: mapping.get(uid, "")


def test_an_order_is_credited_to_the_buyer_not_whoever_added_it():
    """A shop order is entered *for* the buyer by the purchase machinery. Naming
    the adder would credit a mod — or the bot's own path — for a member's coins."""
    assert subject_id({"buyer_id": 7, "added_by": 99}) == 7
    assert subject_id({"buyer_id": None, "added_by": 99}) == 99
    assert subject_id({}) == 0

    plans = plan_cards(
        data=_pass_data(
            tasks=[
                {
                    "id": 1,
                    "task": "Blue name",
                    "buyer_id": 7,
                    "added_by": 99,
                    "purchase_id": 4,
                }
            ]
        ),
        name_fn=_names({7: "Ada", 99: "ModMallory"}),
    )
    body = plans[0][0].body or ""
    assert "Ada" in body
    assert "ModMallory" not in body


def test_the_boot_pass_banks_only_the_arrivals_cursor():
    """Re-banking the digest window too would mean any restart before noon shrank
    that day's window to "since the deploy" — and prod's chores spawn in the
    morning, so a routine deploy would silently cancel the day's card."""
    plans = plan_cards(
        data=_pass_data(tasks=[{"id": 1, "task": "Arrived while down", "added_by": 5}]),
        name_fn=_names({}),
        announce=False,
    )
    assert [column for _, column in plans] == ["change_cursor_at"]
    plan, _ = plans[0]
    assert plan.posts is False
    assert plan.cursor_at == NOW


def test_the_boot_pass_looks_nobody_up():
    """No card is going out, so the batched name lookup has nothing to resolve."""
    data = _pass_data(
        tasks=[{"id": 1, "task": "x", "added_by": 5}],
        signoffs=[{"user_id": 6}],
        approvals=[{"user_id": 7}],
    )
    assert subject_ids(data) == [5, 6, 7]
    assert subject_ids(data, announce=False) == []


def test_a_transient_failure_leaves_the_watermark_alone():
    """A banked cursor after a failed send silently drops a history entry, which
    is the one thing a log must not do. It has to be rebuilt next tick."""
    card = CardPlan(title="t", body="a line", cursor_at=NOW)
    assert cursor_after(card, outcome=SEND_RETRY) is None
    assert cursor_after(card, outcome=SEND_OK) == NOW


def test_a_permanent_failure_banks_the_cursor_instead_of_retrying_forever():
    """The opposite call, and on purpose. A channel the bot cannot post in will
    still refuse it in a minute, so holding the cursor there widens the arrival
    window every tick — a weekend of a locked channel would come back as a wall
    of everything since Friday, from a query reaching further back each time. The
    board is still the complete live view, so one lost log entry is survivable."""
    card = CardPlan(title="t", body="a line", cursor_at=NOW)
    assert cursor_after(card, outcome=SEND_GIVE_UP) == NOW


def test_a_silent_tick_banks_its_cursor_regardless():
    """Nothing was going to post, so there is no history entry to lose — and the
    window must not widen forever."""
    assert cursor_after(CardPlan(cursor_at=NOW), outcome=SEND_OK) == NOW
    # A plan with no cursor at all (today's digest already went out) stores nothing.
    assert cursor_after(CardPlan(), outcome=SEND_OK) is None


def test_arrivals_come_before_the_digest_in_one_tick():
    """A burst and the noon digest on the same tick should read chronologically."""
    plans = plan_cards(data=_pass_data(), name_fn=_names({}))
    assert [column for _, column in plans] == ["change_cursor_at", "digest_at"]


# ── the bug board's cards ─────────────────────────────────────────────


def _bug_data(bugs, *, cursor_at=NOW - 60):
    return {"now": NOW, "channel_id": 555, "cursor_at": cursor_at, "bugs": bugs}


def test_a_bug_card_quotes_the_bug_and_never_names_the_filer():
    """DELIBERATE ANOMALY — do not "fix" this to match the task line.

    A task card names its adder because the adder is a moderator. A bug can be
    filed by any member, and a change card is a Discord message the erasure
    scrub can never reach: printing the filer would leave their name in the
    channel for good, breaking the anonymise-on-erasure promise. The text is
    kept on erasure anyway, so quoting it widens nothing.
    """
    row = {"task": "The shop button does nothing", "added_by": USER}
    [(plan, column)] = plan_bug_cards(data=_bug_data([row]))
    assert column == "change_cursor_at"
    assert plan.title == "🐛 1 new bug"
    assert plan.body == "🐛 The shop button does nothing"
    assert bug_line(row) == plan.body
    assert "**" not in plan.body
    assert str(USER) not in plan.body


def test_a_bug_lines_markdown_is_escaped():
    """The bug text is member-filed free text landing in a card's live
    description markdown, so a member writing ``**bold**`` must not bend the
    channel's formatting."""
    row = {"task": "**bold** and `code` and __underline__"}
    assert bug_line(row) == r"🐛 \*\*bold\*\* and \`code\` and \_\_underline\_\_"


def test_a_burst_of_bugs_is_one_card_counting_bugs():
    rows = [{"task": f"bug {i}"} for i in range(3)]
    [(plan, _)] = plan_bug_cards(data=_bug_data(rows))
    assert plan.title == "🐛 3 new bugs"
    assert plan.body is not None and plan.body.count("\n") == 2


@pytest.mark.parametrize(
    ("data", "announce"),
    [
        pytest.param(_bug_data([{"task": "x"}], cursor_at=None), True, id="null-cursor"),
        pytest.param(_bug_data([{"task": "x"}]), False, id="boot-pass"),
    ],
)
def test_the_bug_card_keeps_both_silences(data, announce):
    """The dark-launch guard and the boot pass, reused rather than copied."""
    [(plan, column)] = plan_bug_cards(data=data, announce=announce)
    assert plan.posts is False
    assert (plan.cursor_at, column) == (NOW, "change_cursor_at")


def test_the_bug_board_has_no_digest():
    """The noon card is about recurring chores, and a bug list has none."""
    plans = plan_bug_cards(data=_bug_data([]))
    assert [column for _, column in plans] == ["change_cursor_at"]


@pytest.mark.parametrize(
    ("key", "on", "off"),
    [
        pytest.param(CHANGE_CARDS_KEY, LIST_TODO, LIST_BUG, id="todo-switch"),
        pytest.param(BUG_CHANGE_CARDS_KEY, LIST_BUG, LIST_TODO, id="bug-switch"),
    ],
)
def test_each_board_has_its_own_switch(db, key, on, off):
    """Turning cards on above one board must never start them above the other."""
    with open_db(db) as conn:
        set_config_value(conn, key, "1", GUILD)
        assert change_cards_enabled(conn, GUILD, list_key=on) is True
        assert change_cards_enabled(conn, GUILD, list_key=off) is False
        assert guilds_with_change_cards(conn, list_key=on) == [GUILD]
        assert guilds_with_change_cards(conn, list_key=off) == []


def test_the_bug_switch_is_not_inherited_from_the_legacy_guild_zero_row(db):
    with open_db(db) as conn:
        set_config_value(conn, BUG_CHANGE_CARDS_KEY, "1", 0)
        assert change_cards_enabled(conn, GUILD, list_key=LIST_BUG) is False


# ── bug screenshots ─────────────────────────────────────────────────


def _bug(bug_id, *urls, raw=None):
    import json

    stored = raw if raw is not None else (
        json.dumps([{"url": u, "ext": "png"} for u in urls]) if urls else None
    )
    return {"id": bug_id, "task": f"bug {bug_id}", "attachment_urls": stored}


def test_a_posting_bug_card_carries_its_bugs_screenshots_by_bug_name():
    from bot_modules.todo.change_logic import CardImage, plan_bug_cards

    [(plan, _)] = plan_bug_cards(
        data={"now": 5.0, "cursor_at": 1.0, "bugs": [_bug(7, "u1", "u2"), _bug(8)]}
    )
    assert plan.images == (
        CardImage(7, "u1", "bug-7-1.png"),
        CardImage(7, "u2", "bug-7-2.png"),
    )


@pytest.mark.parametrize(
    "data, announce",
    [
        pytest.param({"now": 5.0, "cursor_at": None}, True, id="dial-just-on"),
        pytest.param({"now": 5.0, "cursor_at": 1.0}, False, id="boot-pass"),
    ],
)
def test_a_silent_bug_tick_carries_no_screenshots(data, announce):
    from bot_modules.todo.change_logic import plan_bug_cards

    [(plan, _)] = plan_bug_cards(
        data={**data, "bugs": [_bug(7, "u1")]}, announce=announce
    )
    assert not plan.posts and plan.images == ()


def test_a_bug_folded_into_the_overflow_line_brings_no_screenshot():
    """A picture posts only beside a bug whose line the card actually shows."""
    from bot_modules.todo.change_logic import MAX_CARD_LINES, plan_bug_cards

    rows = [_bug(i) for i in range(1, MAX_CARD_LINES + 1)] + [_bug(99, "u1")]
    [(plan, _)] = plan_bug_cards(data={"now": 5.0, "cursor_at": 1.0, "bugs": rows})
    assert plan.body is not None and "1** more" in plan.body
    assert plan.images == ()


def test_bug_images_cap_at_discords_file_limit_and_skip_bad_rows():
    from bot_modules.todo.change_logic import MAX_CARD_IMAGES, bug_images

    rows = [_bug(1, raw="not json"), _bug(2, raw='{"url": "x"}')] + [
        _bug(i, "u", "v") for i in range(3, 10)
    ]
    images = bug_images(rows)
    assert len(images) == MAX_CARD_IMAGES
    assert images[0].bug_id == 3


@pytest.mark.parametrize(
    "filename, bug_id, mine",
    [
        pytest.param("bug-12-1.png", 12, True, id="own"),
        pytest.param("bug-123-1.png", 12, False, id="longer-id"),
        pytest.param("bug-1-1.png", 12, False, id="shorter-id"),
        pytest.param("photo.png", 12, False, id="foreign"),
    ],
)
def test_is_bug_image_matches_only_its_own_bug(filename, bug_id, mine):
    from bot_modules.todo.change_logic import is_bug_image

    assert is_bug_image(filename, bug_id) is mine


@pytest.mark.parametrize(
    ("url", "name"),
    [
        pytest.param(
            "https://cdn.discordapp.com/attachments/5/6/bug-12-1.png?ex=a&is=b&hm=c",
            "bug-12-1.png", id="signed-cdn-link",
        ),
        pytest.param("attachment://bug-12-2.jpg", "bug-12-2.jpg", id="attachment-ref"),
        pytest.param("https://cdn.example/a/b/", "", id="no-filename"),
    ],
)
def test_media_filename_reads_the_name_off_a_gallery_link(url, name):
    """A fetched V2 card reports no attachments, so the gallery item's link is
    where a screenshot's ``bug-<id>-<n>`` name is read back from."""
    from bot_modules.todo.change_logic import media_filename

    assert media_filename(url) == name


@pytest.mark.parametrize(
    ("filename", "bug_ids", "stays"),
    [
        pytest.param("bug-12-1.png", [12], False, id="its-bug-closed"),
        pytest.param("bug-12-1.png", [7, 12], False, id="one-of-several"),
        pytest.param("bug-123-1.png", [12], True, id="longer-id-stays"),
        pytest.param("bug-12-1.png", [], True, id="nothing-closed"),
    ],
)
def test_survives_strip_keeps_every_other_bugs_image(filename, bug_ids, stays):
    from bot_modules.todo.change_logic import survives_strip

    assert survives_strip(filename, bug_ids) is stays


def test_strips_group_by_card_so_one_card_costs_one_edit():
    from bot_modules.todo.change_logic import strips_by_card

    rows = [
        {"id": 1, "guild_id": 9, "card_channel_id": 5, "card_message_id": 6},
        {"id": 2, "guild_id": 9, "card_channel_id": 5, "card_message_id": 6},
        {"id": 3, "guild_id": 9, "card_channel_id": 5, "card_message_id": 7},
    ]
    assert strips_by_card(rows) == {(9, 5, 6): [1, 2], (9, 5, 7): [3]}


@pytest.mark.parametrize(
    "outcomes, expected, calls",
    [
        pytest.param([b"img"], b"img", 1, id="first-try"),
        pytest.param([OSError("reset"), b"img"], b"img", 2, id="retried-once"),
        pytest.param([None, b"img"], b"img", 2, id="empty-then-ok"),
        pytest.param([OSError("a"), OSError("b")], None, 2, id="gives-up-after-two"),
    ],
)
async def test_a_kept_screenshot_gets_one_retry_before_it_is_lost(monkeypatch, outcomes, expected, calls):
    """A kept image that won't download is lost with the strip's edit, so a
    transient failure gets one more go (ship review, 2026-10-01)."""
    import asyncio

    from bot_modules.todo import change_cards

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    seen = []

    async def fetch(url):
        seen.append(url)
        out = outcomes[len(seen) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    assert await change_cards._fetch_with_retry(fetch, "u") == expected
    assert len(seen) == calls
    assert await change_cards._fetch_with_retry(None, "u") is None
