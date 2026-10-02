"""Tests for the Greeting Watch monitor loop — settings load + the staff ping.

Focus is the behavior the loop adds on top of the service helpers: giving each
due greeting its verdict and posting the Staff · Greetings ping for an
unanswered one. Delivery itself (channel, role, opt-in) is
``tests/moderation/test_staff_ping_service.py``'s.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.onboarding.greeting_watch import loop as greeting_watch_loop
from bot_modules.onboarding.greeting_watch.loop import _load_settings, _process_guild
from bot_modules.onboarding.greeting_watch.service import (
    PendingGreeting,
    acknowledge_greetings,
    record_greeting,
)
from bot_modules.shared import feature_roles as fr
from tests.db_template import migrated_db

GUILD = 1000
CHANNEL = 2000
GREETER = 3000


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "gw.db"
    migrated_db(path)
    return path


@pytest.fixture
def ping(monkeypatch):
    """Stands in for ``post_staff_ping``; True = the ping posted."""
    posted = AsyncMock(return_value=True)
    monkeypatch.setattr(greeting_watch_loop, "post_staff_ping", posted)
    return posted


def _configure(db_path, *, enabled=True, window=10):
    with open_db(db_path) as conn:
        set_config_value(
            conn, "greeting_watch_enabled", "1" if enabled else "0", GUILD
        )
        set_config_value(
            conn, "greeting_watch_window_minutes", str(window), GUILD
        )


# ── _load_settings ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param("15", 15, id="stored"),
        pytest.param("0", 1, id="floored-at-one"),
        pytest.param("soon", greeting_watch_loop.DEFAULT_WINDOW_MINUTES, id="garbage"),
    ],
)
def test_load_settings_reads_the_window(db_path, stored, expected):
    _configure(db_path, window=stored)
    assert _load_settings(db_path, GUILD) == (True, expected)


# ── _process_guild ───────────────────────────────────────────────────


class FakeBot:
    def __init__(self, *, in_guild: bool = True, member: bool = True) -> None:
        self.ctx = SimpleNamespace()
        self._guild = (
            SimpleNamespace(
                id=GUILD,
                get_member=lambda mid: (
                    SimpleNamespace(display_name="Greeter") if member else None
                ),
            )
            if in_guild
            else None
        )

    def get_guild(self, gid: int):
        return self._guild


def _resolved_rows(db_path):
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT outcome FROM greeting_watch WHERE resolved_at IS NOT NULL"
        ).fetchall()


@pytest.mark.parametrize("posted", [True, False], ids=["posted", "nobody-opted-in"])
async def test_unanswered_greeting_pings_staff_once_and_resolves(db_path, ping, posted):
    """The row is resolved whether or not the ping lands: with no moderator
    opted in it would otherwise re-alert every tick."""
    ping.return_value = posted
    _configure(db_path, window=10)
    with open_db(db_path) as conn:
        # Greeting posted at ts=100, window 10 min → due once now is well past.
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)

    bot = FakeBot()
    await _process_guild(bot, db_path, GUILD, now_ts=100 + 10 * 60 + 5)

    ping.assert_awaited_once()
    ctx, guild, entry, text = ping.await_args.args
    assert ctx is bot.ctx and guild.id == GUILD
    assert entry is fr.STAFF_GREETING_PING
    assert f"<#{CHANNEL}>" in text and "about 10 min ago" in text
    assert f"/channels/{GUILD}/{CHANNEL}/1" in text
    assert [r["outcome"] for r in _resolved_rows(db_path)] == ["unanswered"]

    # Resolved means done: the next tick has nothing to say.
    await _process_guild(bot, db_path, GUILD, now_ts=100 + 20 * 60)
    ping.assert_awaited_once()


async def test_acknowledged_greeting_pings_nobody(db_path, ping):
    _configure(db_path, window=10)
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        # Someone answered the greeter inside the window (stamped at ingest).
        acknowledge_greetings(conn, GUILD, 9999, (CHANNEL,), (GREETER,))

    await _process_guild(FakeBot(), db_path, GUILD, now_ts=100 + 10 * 60 + 5)

    ping.assert_not_awaited()
    assert [r["outcome"] for r in _resolved_rows(db_path)] == ["acknowledged"]


async def test_process_guild_rechecks_ack_landed_after_snapshot(db_path, ping, monkeypatch):
    """An answer that lands after `due` was fetched — but before this row's
    ping goes out — must still cancel the notify.

    `list_due_greetings` is one snapshot per tick; the loop then posts (awaited
    network calls) for every due row in turn. Someone can answer mid-tick.
    This forces exactly that: the snapshot handed to `_process_guild` says
    unacknowledged, but the row is stamped acknowledged in the DB before the
    loop gets to it.
    """
    _configure(db_path, window=10)
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        # Answered for real — but the stale snapshot below won't reflect it.
        acknowledge_greetings(conn, GUILD, 9999, (CHANNEL,), (GREETER,))

    stale = PendingGreeting(1, CHANNEL, GREETER, 100, acknowledged=False)
    monkeypatch.setattr(
        greeting_watch_loop, "_due_sync", lambda *a, **k: [stale]
    )

    await _process_guild(FakeBot(), db_path, GUILD, now_ts=100 + 10 * 60 + 5)

    ping.assert_not_awaited()
    assert [r["outcome"] for r in _resolved_rows(db_path)] == ["acknowledged"]


async def test_switched_off_mid_window_retires_the_row_quietly(db_path, ping):
    _configure(db_path, enabled=False, window=10)
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)

    await _process_guild(FakeBot(), db_path, GUILD, now_ts=100 + 10 * 60 + 5)

    ping.assert_not_awaited()
    assert [r["outcome"] for r in _resolved_rows(db_path)] == ["skipped"]


async def test_a_guild_the_bot_left_resolves_without_a_ping(db_path, ping):
    _configure(db_path, window=10)
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)

    await _process_guild(FakeBot(in_guild=False), db_path, GUILD, now_ts=100 + 10 * 60 + 5)

    ping.assert_not_awaited()
    assert [r["outcome"] for r in _resolved_rows(db_path)] == ["unanswered"]


# ── the ping names the greeter ───────────────────────────────────────


@pytest.mark.parametrize(
    ("in_guild", "expected"),
    [
        pytest.param(True, "Greeter said hello", id="cached-member"),
        pytest.param(False, "Gone\\_Greeter said hello", id="left-known-user"),
    ],
)
async def test_notify_names_the_greeter_never_a_mention(
    db_path, ping, in_guild, expected
):
    """A <@id> for a greeter who left would show digits, and one for a greeter
    who can't see the staff channel is noise; known_users still has the name."""
    from bot_modules.shared.message_store import upsert_known_user

    with open_db(db_path) as conn:
        upsert_known_user(conn, GUILD, GREETER, "gone", "Gone_Greeter", 1.0)

    g = PendingGreeting(1, CHANNEL, GREETER, 100, False)
    assert await greeting_watch_loop._notify(
        FakeBot(member=in_guild), db_path, GUILD, g, 10
    )

    text = ping.await_args.args[3]
    assert expected in text
    assert "<@" not in text
