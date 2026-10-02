"""Tests for the word cloud's one database read: ranking channels by traffic.

Words are always fetched live from Discord; the database only says which
channels an "everywhere" cloud should read first.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.shared.message_store import init_message_tables
from bot_modules.word_cloud.corpus import recent_channel_ids

GUILD = 1000
CHANNEL = 10
OTHER_CHANNEL = 11
HUMAN = 100


@pytest.fixture
def conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_message_tables(conn)
    return conn


def _add(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    channel_id: int = CHANNEL,
    ts: int = 5_000,
) -> None:
    conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (message_id, GUILD, channel_id, HUMAN, ts),
    )


def test_recent_channel_ids_ranks_by_last_message(conn):
    _add(conn, 1, channel_id=CHANNEL, ts=10)
    _add(conn, 2, channel_id=OTHER_CHANNEL, ts=99)
    assert recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL, OTHER_CHANNEL], limit=2
    ) == [OTHER_CHANNEL, CHANNEL]


def test_recent_channel_ids_respects_the_limit(conn):
    _add(conn, 1, channel_id=CHANNEL, ts=10)
    _add(conn, 2, channel_id=OTHER_CHANNEL, ts=99)
    assert recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL, OTHER_CHANNEL], limit=1
    ) == [OTHER_CHANNEL]


def test_recent_channel_ids_keeps_channels_it_never_saw(conn):
    """A brand-new channel must not become unreachable."""
    _add(conn, 1, channel_id=CHANNEL, ts=10)
    ranked = recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL, OTHER_CHANNEL], limit=5
    )
    assert set(ranked) == {CHANNEL, OTHER_CHANNEL}
    assert ranked[0] == CHANNEL


def test_recent_channel_ids_on_nothing_is_empty(conn):
    assert recent_channel_ids(conn, guild_id=GUILD, channel_ids=[], limit=5) == []
    assert recent_channel_ids(
        conn, guild_id=GUILD, channel_ids=[CHANNEL], limit=0
    ) == []
