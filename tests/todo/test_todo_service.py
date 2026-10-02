"""Tests for todo/service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.todo.service import (
    BUG_FILING_KEY,
    FILING_EVERYONE,
    FILING_MODS,
    LIST_BUG,
    bug_filing_mode,
    bug_filing_refusal,
    clear_board,
    complete_todo,
    create_todo,
    get_board,
    guilds_with_board,
    last_bug_filed_at,
    list_todos,
    mark_missed,
    pending_count,
    pending_todos,
    reset_change_cursors,
    save_board,
    save_change_cursors,
    save_cursor_column,
)
from tests.db_template import migrated_db

GUILD = 123
USER = 9001


def _read(conn, todo_id):
    """Read one row back. `get_todo` used to exist for this but had no
    production caller, so the test owns its own read-back."""
    return next(r for r in list_todos(conn, GUILD) if r["id"] == todo_id)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


# ── create_todo ───────────────────────────────────────────────────────


def test_create_returns_id(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Fix the bug")
    assert isinstance(todo_id, int)
    assert todo_id > 0


def test_create_increments_ids(db):
    with open_db(db) as conn:
        id1 = create_todo(conn, GUILD, USER, "Task 1")
        id2 = create_todo(conn, GUILD, USER, "Task 2")
    assert id2 > id1


def test_create_stores_correct_values(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do the thing")
        row = conn.execute(
            "SELECT guild_id, added_by, task, completed_at FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["guild_id"] == GUILD
    assert row["added_by"] == USER
    assert row["task"] == "Do the thing"
    assert row["completed_at"] is None


def test_create_isolated_by_guild(db):
    other_guild = 999
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Guild A task")
        create_todo(conn, other_guild, USER, "Guild B task")
        a_count = conn.execute(
            "SELECT COUNT(*) FROM todos WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
        b_count = conn.execute(
            "SELECT COUNT(*) FROM todos WHERE guild_id = ?", (other_guild,)
        ).fetchone()[0]
    assert a_count == 1
    assert b_count == 1


# ── create_todo with new optional fields ─────────────────────────────


def test_create_with_description_and_source_url(db):
    with open_db(db) as conn:
        todo_id = create_todo(
            conn,
            GUILD,
            USER,
            "Message from @alice in #general",
            description="hello world\n\nfollow up next week",
            source_message_url="https://discord.com/channels/1/2/3",
        )
        row = conn.execute(
            "SELECT description, source_message_url FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["description"] == "hello world\n\nfollow up next week"
    assert row["source_message_url"] == "https://discord.com/channels/1/2/3"


def test_create_without_new_fields_leaves_them_null(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Plain task")
        row = conn.execute(
            "SELECT description, source_message_url FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["description"] is None
    assert row["source_message_url"] is None


# ── list / get / complete ─────────────────────────────────────────────


def test_list_returns_newest_first(db):
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Older", now_ts=100.0)
        create_todo(conn, GUILD, USER, "Newer", now_ts=200.0)
        rows = list_todos(conn, GUILD)
    assert [r["task"] for r in rows] == ["Newer", "Older"]


def test_pending_returns_oldest_first(db):
    """The board reads oldest-first so the longest-waiting task nags at the top."""
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Older", now_ts=100.0)
        create_todo(conn, GUILD, USER, "Newer", now_ts=200.0)
        rows = pending_todos(conn, GUILD)
    assert [r["task"] for r in rows] == ["Older", "Newer"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [("pending", ["Open"]), ("completed", ["Done"]), (None, ["Done", "Open"])],
)
def test_list_status_filter(db, status, expected):
    with open_db(db) as conn:
        open_id = create_todo(conn, GUILD, USER, "Open", now_ts=100.0)
        done_id = create_todo(conn, GUILD, USER, "Done", now_ts=200.0)
        complete_todo(conn, done_id, GUILD, USER, now_ts=300.0)
        rows = list_todos(conn, GUILD, status=status)
    assert [r["task"] for r in rows] == expected
    assert open_id != done_id


def test_list_is_scoped_by_guild(db):
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Mine")
        create_todo(conn, 999, USER, "Theirs")
        assert [r["task"] for r in list_todos(conn, GUILD)] == ["Mine"]


def test_complete_records_who_and_when(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do it")
        assert complete_todo(conn, todo_id, GUILD, 4242, now_ts=555.0)
        row = _read(conn, todo_id)
    assert row["completed_at"] == 555.0
    assert row["completed_by"] == 4242


def test_complete_is_idempotent(db):
    """The board button and the dashboard can race; the second call is a no-op."""
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do it")
        assert complete_todo(conn, todo_id, GUILD, USER, now_ts=100.0)
        assert not complete_todo(conn, todo_id, GUILD, 999, now_ts=200.0)
        row = _read(conn, todo_id)
    assert row["completed_at"] == 100.0
    assert row["completed_by"] == USER


def test_complete_rejects_other_guilds(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Mine")
        assert not complete_todo(conn, todo_id, 999, USER)
        assert _read(conn, todo_id)["completed_at"] is None


def test_complete_missing_returns_false(db):
    with open_db(db) as conn:
        assert not complete_todo(conn, 4242, GUILD, USER)


def test_pending_count_is_scoped_and_uncapped_by_the_list_limit(db):
    """The board footer counts every outstanding task, not just the page it
    renders, so the count query is separate from the row query."""
    with open_db(db) as conn:
        for i in range(25):
            create_todo(conn, GUILD, USER, f"Task {i}")
        create_todo(conn, 999, USER, "Theirs")
        assert pending_count(conn, GUILD) == 25
        assert len(pending_todos(conn, GUILD, limit=16)) == 16
        assert pending_count(conn, 999) == 1


def test_completed_rows_leave_the_pending_list(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Do it")
        complete_todo(conn, todo_id, GUILD, USER)
        assert pending_todos(conn, GUILD) == []


# ── board placement ───────────────────────────────────────────────────


def test_board_defaults_to_unposted(db):
    with open_db(db) as conn:
        board = get_board(conn, GUILD)
    assert board.channel_id == 0
    assert board.message_id == 0
    assert not board.posted


def test_save_and_read_board(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 666, now_ts=42.0)
        board = get_board(conn, GUILD)
    assert (board.channel_id, board.message_id) == (555, 666)
    assert board.posted
    assert board.updated_at == 42.0


def test_save_board_upserts_in_place(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 1, 2)
        save_board(conn, GUILD, 3, 4)
        board = get_board(conn, GUILD)
        count = conn.execute(
            "SELECT COUNT(*) FROM todo_board WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
    assert (board.channel_id, board.message_id) == (3, 4)
    assert count == 1


def test_clear_board_marks_unposted(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 555, 666)
        clear_board(conn, GUILD)
        board = get_board(conn, GUILD)
    assert not board.posted


def test_board_is_scoped_by_guild(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 1, 2)
        assert not get_board(conn, 999).posted


def test_guilds_with_board_lists_only_posted(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 1, 2)
        save_board(conn, 999, 0, 0)
        assert guilds_with_board(conn) == [GUILD]


# ── One board per guild ─────────────────────────────────────────────────


def test_save_and_read_the_board(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        board = get_board(conn, GUILD)
        assert (board.channel_id, board.message_id) == (111, 222)
        assert board.posted


def test_an_unplaced_guild_reads_as_unposted(db):
    with open_db(db) as conn:
        assert not get_board(conn, GUILD).posted


def test_saving_again_moves_the_board_rather_than_adding_one(db):
    """One row per guild — migration 180 made guild_id the whole key."""
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        save_board(conn, GUILD, 333, 444)
        assert get_board(conn, GUILD).channel_id == 333
        rows = conn.execute(
            "SELECT COUNT(*) FROM todo_board WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
        assert rows == 1


def test_clearing_forgets_the_placement(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        clear_board(conn, GUILD)
        assert not get_board(conn, GUILD).posted


def test_guilds_with_board_lists_only_the_posted_ones(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        save_board(conn, 999, 0, 0)
        assert guilds_with_board(conn) == [GUILD]


# ── Two lists, one table (migration 225) ────────────────────────────────


def test_a_bug_is_invisible_to_every_todo_list_reader(db):
    """Every pre-existing caller defaults to the todo list; a bug leaking into
    it would put member-filed reports on the mod worklist board."""
    with open_db(db) as conn:
        todo = create_todo(conn, GUILD, USER, "todo", now_ts=1.0)
        bug = create_todo(conn, GUILD, USER, "bug", now_ts=2.0, list_key=LIST_BUG)
        assert [r["id"] for r in list_todos(conn, GUILD)] == [todo]
        assert [r["id"] for r in pending_todos(conn, GUILD)] == [todo]
        assert pending_count(conn, GUILD) == 1
        assert [r["id"] for r in list_todos(conn, GUILD, list_key=LIST_BUG)] == [bug]
        assert [r["id"] for r in pending_todos(conn, GUILD, list_key=LIST_BUG)] == [bug]
        assert pending_count(conn, GUILD, list_key=LIST_BUG) == 1
        assert _read(conn, todo)["list"] == "todo"


def test_complete_refuses_an_id_from_the_other_list(db):
    """Ids share one sequence, so a stale picker must not close the wrong list."""
    with open_db(db) as conn:
        bug = create_todo(conn, GUILD, USER, "bug", list_key=LIST_BUG)
        assert complete_todo(conn, bug, GUILD, USER) is False
        assert complete_todo(conn, bug, GUILD, USER, list_key=LIST_BUG) is True


@pytest.mark.parametrize("field", ["recurring_id", "purchase_id"])
def test_a_bug_cannot_carry_a_chore_or_an_order(db, field):
    with open_db(db) as conn, pytest.raises(ValueError):
        create_todo(conn, GUILD, USER, "x", list_key=LIST_BUG, **{field: 1})


def test_an_unknown_list_is_refused_not_read_as_empty(db):
    with open_db(db) as conn, pytest.raises(ValueError):
        list_todos(conn, GUILD, list_key="bugs")


def test_each_list_has_its_own_board_and_cursors(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        save_board(conn, GUILD, 333, 444, list_key=LIST_BUG)
        save_change_cursors(conn, GUILD, change_cursor_at=5.0, digest_at=6.0)
        save_change_cursors(conn, GUILD, change_cursor_at=7.0, list_key=LIST_BUG)
        reset_change_cursors(conn, GUILD, list_key=LIST_BUG)
        todo_board = get_board(conn, GUILD)
        bug_board = get_board(conn, GUILD, list_key=LIST_BUG)
        assert (todo_board.channel_id, todo_board.change_cursor_at) == (111, 5.0)
        assert (bug_board.channel_id, bug_board.change_cursor_at) == (333, None)
        clear_board(conn, GUILD, list_key=LIST_BUG)
        assert get_board(conn, GUILD).posted
        assert guilds_with_board(conn) == [GUILD]
        assert guilds_with_board(conn, list_key=LIST_BUG) == []


@pytest.mark.parametrize(
    "column, expect_change, expect_digest",
    [("change_cursor_at", 8.0, None), ("digest_at", None, 8.0)],
)
def test_a_named_cursor_writes_only_its_own_column(
    db, column, expect_change, expect_digest
):
    """The plan carries a column name; only that watermark moves.

    ``deliver_cards`` holds the column as data, so the translation back into a
    keyword is worth pinning: a name landing on the wrong parameter — ``list_key``
    was the one within reach — would write nothing and look like a quiet success.
    """
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        save_cursor_column(conn, GUILD, column, 8.0)
        board = get_board(conn, GUILD)
        assert (board.change_cursor_at, board.digest_at) == (expect_change, expect_digest)


def test_a_named_cursor_stays_on_its_own_list(db):
    with open_db(db) as conn:
        save_board(conn, GUILD, 111, 222)
        save_board(conn, GUILD, 333, 444, list_key=LIST_BUG)
        save_cursor_column(conn, GUILD, "change_cursor_at", 9.0, list_key=LIST_BUG)
        assert get_board(conn, GUILD).change_cursor_at is None
        assert get_board(conn, GUILD, list_key=LIST_BUG).change_cursor_at == 9.0


# ── Who may file a bug ──────────────────────────────────────────────────


def test_filing_is_moderators_only_until_an_admin_opens_it(db):
    """The dark launch: no row means /bug is exactly as closed as /todo."""
    with open_db(db) as conn:
        assert bug_filing_mode(conn, GUILD) == FILING_MODS
        set_config_value(conn, BUG_FILING_KEY, "everyone", GUILD)
        assert bug_filing_mode(conn, GUILD) == FILING_EVERYONE
        assert bug_filing_mode(conn, 999) == FILING_MODS


@pytest.mark.parametrize("stored", ["Everyone ", "1", "true", "on", "all", ""])
def test_only_the_exact_word_opens_filing(db, stored):
    """Case and whitespace are forgiven; a truthy-looking value is not."""
    with open_db(db) as conn:
        set_config_value(conn, BUG_FILING_KEY, stored, GUILD)
        expected = FILING_EVERYONE if stored.strip().lower() == "everyone" else FILING_MODS
        assert bug_filing_mode(conn, GUILD) == expected


def test_the_legacy_global_row_does_not_open_filing(db):
    with open_db(db) as conn:
        set_config_value(conn, BUG_FILING_KEY, "everyone", 0)
        assert bug_filing_mode(conn, GUILD) == FILING_MODS


def test_last_filed_reads_only_this_members_bugs_in_this_guild(db):
    with open_db(db) as conn:
        assert last_bug_filed_at(conn, GUILD, USER) is None
        create_todo(conn, GUILD, USER, "a todo", now_ts=900.0)
        create_todo(conn, 999, USER, "elsewhere", now_ts=800.0, list_key=LIST_BUG)
        create_todo(conn, GUILD, 1, "someone else", now_ts=700.0, list_key=LIST_BUG)
        assert last_bug_filed_at(conn, GUILD, USER) is None
        create_todo(conn, GUILD, USER, "old", now_ts=100.0, list_key=LIST_BUG)
        create_todo(conn, GUILD, USER, "new", now_ts=200.0, list_key=LIST_BUG)
        assert last_bug_filed_at(conn, GUILD, USER) == 200.0


@pytest.mark.parametrize(
    ("is_mod", "mode", "last", "now", "refused"),
    [
        pytest.param(True, FILING_MODS, None, 0.0, None, id="mod-closed"),
        pytest.param(True, FILING_EVERYONE, 999.0, 1000.0, None, id="mod-no-cooldown"),
        pytest.param(False, FILING_MODS, None, 0.0, "moderators", id="member-closed"),
        pytest.param(False, FILING_EVERYONE, None, 0.0, None, id="member-first"),
        pytest.param(False, FILING_EVERYONE, 1000.0, 1001.0, "5 minutes", id="cooling"),
        pytest.param(False, FILING_EVERYONE, 1000.0, 1250.0, "1 minute", id="last-minute"),
        pytest.param(False, FILING_EVERYONE, 1000.0, 1300.0, None, id="boundary"),
    ],
)
def test_bug_filing_refusal(is_mod, mode, last, now, refused):
    msg = bug_filing_refusal(is_mod=is_mod, mode=mode, last_filed_at=last, now=now)
    if refused is None:
        assert msg is None
    else:
        assert msg is not None and refused in msg


# ── The third state ─────────────────────────────────────────────────────


def test_mark_missed_closes_a_row_without_crediting_it(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        assert mark_missed(conn, todo_id, now_ts=1000.0) is True
        row = _read(conn, todo_id)
    assert row["missed_at"] == 1000.0
    assert row["completed_at"] is None
    assert not row["completed_by"]


def test_mark_missed_is_idempotent(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        assert mark_missed(conn, todo_id, now_ts=1000.0) is True
        assert mark_missed(conn, todo_id, now_ts=2000.0) is False
        assert _read(conn, todo_id)["missed_at"] == 1000.0


def test_mark_missed_refuses_a_completed_row(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        complete_todo(conn, todo_id, GUILD, USER, now_ts=500.0)
        assert mark_missed(conn, todo_id, now_ts=1000.0) is False
        assert _read(conn, todo_id)["missed_at"] is None


def test_complete_refuses_a_missed_row(db):
    """Yesterday's box is not tickable today."""
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Post QOTD")
        mark_missed(conn, todo_id, now_ts=1000.0)
        assert complete_todo(conn, todo_id, GUILD, USER, now_ts=2000.0) is False
        assert _read(conn, todo_id)["completed_at"] is None


def test_missed_rows_leave_the_pending_list_and_count(db):
    with open_db(db) as conn:
        keep = create_todo(conn, GUILD, USER, "Still open")
        drop = create_todo(conn, GUILD, USER, "Written off")
        mark_missed(conn, drop, now_ts=1000.0)

        assert [r["id"] for r in pending_todos(conn, GUILD)] == [keep]
        assert pending_count(conn, GUILD) == 1


def test_missed_rows_are_not_pending_in_the_dashboard_filter(db):
    """The dashboard's "pending" tab has to agree with the board."""
    with open_db(db) as conn:
        create_todo(conn, GUILD, USER, "Still open")
        drop = create_todo(conn, GUILD, USER, "Written off")
        mark_missed(conn, drop, now_ts=1000.0)

        pending = list_todos(conn, GUILD, status="pending")
        assert [r["task"] for r in pending] == ["Still open"]


def test_missed_rows_are_not_completed_either(db):
    """A written-off chore is closed, but nobody did it — it is not a completion."""
    with open_db(db) as conn:
        drop = create_todo(conn, GUILD, USER, "Written off")
        mark_missed(conn, drop, now_ts=1000.0)
        assert list_todos(conn, GUILD, status="completed") == []
        assert len(list_todos(conn, GUILD)) == 1


# ── bug screenshots ─────────────────────────────────────────────────


_SHOT = [{"url": "https://cdn.example/a.png", "ext": "png"}]


def _urls(conn, todo_id):
    return conn.execute(
        "SELECT attachment_urls, card_channel_id, card_message_id FROM todos"
        " WHERE id = ?",
        (todo_id,),
    ).fetchone()


@pytest.mark.parametrize(
    "uploads, refused",
    [
        pytest.param([("image/png", 1000)], None, id="an-image"),
        pytest.param([("image/png", 1)] * 4, "at most 3", id="too-many"),
        pytest.param([("video/mp4", 1000)], "must be images", id="not-an-image"),
        pytest.param([(None, 1000)], "must be images", id="no-type"),
        pytest.param([("image/png", 9 * 1024 * 1024)], "under 8 MB", id="too-big"),
    ],
)
def test_screenshot_refusal(uploads, refused):
    from bot_modules.todo.service import screenshot_refusal

    result = screenshot_refusal(uploads)
    assert result is None if refused is None else refused in result


@pytest.mark.parametrize(
    "filename, ext",
    [
        pytest.param("Screen Shot.PNG", "png", id="lowercased"),
        pytest.param("my private name.jpeg", "jpeg", id="name-dropped"),
        pytest.param("noextension", "png", id="no-ext"),
        pytest.param("weird.p/ng", "png", id="junk-ext"),
    ],
)
def test_screenshot_entries_keep_only_the_extension(filename, ext):
    from bot_modules.todo.service import screenshot_entries

    assert screenshot_entries([("https://cdn/x", filename)]) == [
        {"url": "https://cdn/x", "ext": ext}
    ]


def test_screenshots_belong_on_the_bug_list(db):
    with open_db(db) as conn, pytest.raises(ValueError):
        create_todo(conn, GUILD, USER, "a todo", screenshots=_SHOT)


def test_screenshots_are_offered_only_with_a_bug_board_and_its_cards_on(db):
    from bot_modules.todo.service import (
        BUG_CHANGE_CARDS_KEY,
        bug_screenshots_open,
    )

    with open_db(db) as conn:
        assert bug_screenshots_open(conn, GUILD) is False
        set_config_value(conn, BUG_CHANGE_CARDS_KEY, "1", GUILD)
        assert bug_screenshots_open(conn, GUILD) is False  # no board to card on
        save_board(conn, GUILD, 5, 6, list_key=LIST_BUG)
        assert bug_screenshots_open(conn, GUILD) is True
        set_config_value(conn, BUG_CHANGE_CARDS_KEY, "0", GUILD)
        assert bug_screenshots_open(conn, GUILD) is False
        # The todo board's switch is not the bug board's.
        from bot_modules.todo.service import CHANGE_CARDS_KEY

        set_config_value(conn, CHANGE_CARDS_KEY, "1", GUILD)
        assert bug_screenshots_open(conn, GUILD) is False


def test_settle_clears_the_window_and_records_the_posted_card(db):
    from bot_modules.todo.service import settle_bug_screenshots

    with open_db(db) as conn:
        posted = create_todo(
            conn, GUILD, USER, "a", screenshots=_SHOT, now_ts=10, list_key=LIST_BUG
        )
        dropped = create_todo(
            conn, GUILD, USER, "b", screenshots=_SHOT, now_ts=11, list_key=LIST_BUG
        )
        later = create_todo(
            conn, GUILD, USER, "c", screenshots=_SHOT, now_ts=30, list_key=LIST_BUG
        )
        other_guild = create_todo(
            conn, 999, USER, "d", screenshots=_SHOT, now_ts=10, list_key=LIST_BUG
        )
        settle_bug_screenshots(
            conn, GUILD, upto_ts=20, posted_bug_ids=[posted],
            channel_id=5, message_id=6,
        )
        assert tuple(_urls(conn, posted)) == (None, 5, 6)
        # Its window closed with its images unposted: dropped, not held.
        assert tuple(_urls(conn, dropped)) == (None, None, None)
        assert _urls(conn, later)["attachment_urls"] is not None
        assert _urls(conn, other_guild)["attachment_urls"] is not None


def test_expire_is_the_backstop_for_links_no_card_took(db):
    from bot_modules.todo.service import (
        SCREENSHOT_HOLD_SECONDS,
        expire_bug_screenshots,
    )

    with open_db(db) as conn:
        old = create_todo(
            conn, GUILD, USER, "a", screenshots=_SHOT, now_ts=100, list_key=LIST_BUG
        )
        fresh = create_todo(
            conn, GUILD, USER, "b", screenshots=_SHOT, now_ts=200, list_key=LIST_BUG
        )
        assert expire_bug_screenshots(conn, now=150 + SCREENSHOT_HOLD_SECONDS) == 1
        assert _urls(conn, old)["attachment_urls"] is None
        assert _urls(conn, fresh)["attachment_urls"] is not None


def test_closed_bugs_are_stripped_once_and_open_ones_never(db):
    from bot_modules.todo.service import (
        bug_cards_to_strip,
        forget_bug_cards,
        settle_bug_screenshots,
    )

    with open_db(db) as conn:
        fixed = create_todo(conn, GUILD, USER, "a", list_key=LIST_BUG)
        still_open = create_todo(conn, GUILD, USER, "b", list_key=LIST_BUG)
        settle_bug_screenshots(
            conn, GUILD, upto_ts=0, posted_bug_ids=[fixed, still_open],
            channel_id=5, message_id=6,
        )
        assert bug_cards_to_strip(conn) == []
        complete_todo(conn, fixed, GUILD, USER, list_key=LIST_BUG)
        [row] = bug_cards_to_strip(conn)
        assert (row["id"], row["guild_id"], row["card_channel_id"], row["card_message_id"]) == (
            fixed, GUILD, 5, 6,
        )
        forget_bug_cards(conn, [fixed])
        assert bug_cards_to_strip(conn) == []
