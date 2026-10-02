"""Reading a transcript leaves a receipt (standards audit row 4.32).

A transcript is members' words from a private channel; the dashboard's reader
used to leave no trace of who opened one. ``read_transcripts`` writes a
``transcript_view`` audit row, and only when there was something to read.
"""

from __future__ import annotations

import json
import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.moderation.service import IS_MODERATION_ACTION, read_transcripts
from tests.db_template import migrated_db

GUILD = 10
MOD = 555


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "receipt.db"
    migrated_db(db_path)
    with open_db(db_path) as c:
        yield c


def _store(conn, record_type="ticket", record_id=42, guild_id=GUILD):
    conn.execute(
        "INSERT INTO transcripts (guild_id, record_type, record_id, content, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (guild_id, record_type, record_id, json.dumps({"messages": []}), time.time()),
    )


def _receipts(conn):
    return conn.execute(
        "SELECT guild_id, actor_id, extra FROM audit_log WHERE action = 'transcript_view'"
    ).fetchall()


def test_reading_a_transcript_names_the_reader_and_the_record(conn):
    _store(conn)
    assert len(read_transcripts(conn, "ticket", 42, GUILD, reader_id=MOD)) == 1

    [row] = _receipts(conn)
    assert (row["guild_id"], row["actor_id"]) == (GUILD, MOD)
    assert json.loads(row["extra"]) == {"record_type": "ticket", "record_id": 42}


@pytest.mark.parametrize(
    ("stored_guild", "asked_id"),
    [
        pytest.param(GUILD, 99, id="no-such-record"),
        pytest.param(20, 42, id="another-guilds-record"),
    ],
)
def test_nothing_read_writes_nothing(conn, stored_guild, asked_id):
    _store(conn, guild_id=stored_guild)
    assert read_transcripts(conn, "ticket", asked_id, GUILD, reader_id=MOD) == []
    assert _receipts(conn) == []


def test_the_receipt_stays_out_of_the_moderation_feed(conn):
    """Reading isn't done *to* a member; the feed and Home tile count actions."""
    _store(conn)
    read_transcripts(conn, "ticket", 42, GUILD, reader_id=MOD)
    shown = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE 1=1" + IS_MODERATION_ACTION
    ).fetchone()[0]
    assert shown == 0
