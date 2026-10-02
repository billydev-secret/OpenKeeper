"""Migration 254: bio_questions.weight is gone and the question CRUD still works."""

from __future__ import annotations

import pytest

import migrations
from bot_modules.bios import db as bios_db
from bot_modules.core.db_utils import open_db


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrations.apply_migrations_sync(path)
    return path


def test_weight_column_is_gone_and_the_rest_survive(db):
    with open_db(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bio_questions)")}
    assert "weight" not in cols
    assert {"id", "guild_id", "prompt", "active"} <= cols


def test_question_crud_round_trips_without_a_weight(db):
    with open_db(db) as conn:
        qid = bios_db.create_question(conn, 1, prompt="Fave snack?")
        bios_db.update_question(conn, qid, prompt="Best snack?", active=True)
        assert bios_db.get_question(conn, qid).prompt == "Best snack?"
        assert [q["prompt"] for q in bios_db.list_questions_admin(conn, 1)] == [
            "Best snack?"
        ]
        assert [q.id for q in bios_db.list_active_questions(conn, 1)] == [qid]
        bios_db.soft_retire_question(conn, qid)
        assert bios_db.list_active_questions(conn, 1) == []
