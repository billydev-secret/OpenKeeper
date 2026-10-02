"""Version-0 "legacy role holder" consent rows and the one-shot script that
writes them (rotation-rooms-171).

The row records a gap, not consent — the tests pin who is picked up, that a
member with any consent row (including a withdrawn one) is left alone, that the
script's dry run writes nothing, and that ``--apply`` backs up first and is
idempotent.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.games.guess.repo import (
    LEGACY_DISCLOSURE_VERSION,
    get_guess_consent,
    insert_guess,
    insert_round,
    list_unevidenced_participants,
    record_guess_consent,
    record_legacy_consents,
    withdraw_guess_consent,
)
from tests.paths import REPO_ROOT

GUILD = 9001
U = 10**16  # real member ids sit above the snowflake floor; below it is a surrogate


def _seed(conn) -> None:
    """10 submits+answers; 20 only guesses; 30 has a real consent; 40 a withdrawn
    one; 50 plays in another guild; 1 is a departed-member surrogate."""
    r1 = insert_round(conn, guild_id=GUILD, submitter_id=U + 10, answer_id=U + 10)
    conn.execute("UPDATE guess_rounds SET created_at = 100 WHERE id = ?", (r1,))
    r2 = insert_round(conn, guild_id=GUILD, submitter_id=U + 30, answer_id=U + 30)
    insert_round(conn, guild_id=GUILD, submitter_id=U + 40, answer_id=U + 40)
    insert_round(conn, guild_id=GUILD + 1, submitter_id=U + 50, answer_id=U + 50)
    insert_round(conn, guild_id=GUILD, submitter_id=1, answer_id=1)
    insert_guess(conn, round_id=r1, guesser_id=U + 20, guessed_user_id=U + 10, correct=True)
    conn.execute("UPDATE guess_guesses SET created_at = 200 WHERE guesser_id = ?", (U + 20,))
    insert_guess(conn, round_id=r2, guesser_id=U + 10, guessed_user_id=U + 30, correct=True)
    record_guess_consent(conn, guild_id=GUILD, user_id=U + 30, consented_at=5.0)
    record_guess_consent(conn, guild_id=GUILD, user_id=U + 40, consented_at=5.0)
    withdraw_guess_consent(conn, guild_id=GUILD, user_id=U + 40, withdrawn_at=9.0)


def test_only_members_without_any_consent_row_are_listed(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        found = list_unevidenced_participants(conn)
    assert [(g, u) for g, u, _ in found] == [(GUILD, U + 10), (GUILD, U + 20), (GUILD + 1, U + 50)]
    first_seen = {u: t for _, u, t in found}
    assert first_seen[U + 10] == 100 and first_seen[U + 20] == 200


def test_record_writes_version_zero_and_is_idempotent(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        members = list_unevidenced_participants(conn)
        assert record_legacy_consents(conn, members) == 3
        row = get_guess_consent(conn, guild_id=GUILD, user_id=U + 10)
        assert row["disclosure_version"] == LEGACY_DISCLOSURE_VERSION == 0
        assert row["consented_at"] == 100 and row["withdrawn_at"] is None
        # a second pass, even with the stale list, writes nothing
        assert record_legacy_consents(conn, members) == 0
        assert list_unevidenced_participants(conn) == []
        # the proper consents were not touched
        assert get_guess_consent(conn, guild_id=GUILD, user_id=U + 30)["disclosure_version"] != 0


def _script():
    spec = importlib.util.spec_from_file_location(
        "backfill_guess_legacy_consents",
        REPO_ROOT / "scripts" / "backfill_guess_legacy_consents.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _count(db: Path) -> int:
    c = sqlite3.connect(db)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM guess_consents WHERE disclosure_version = 0"
        ).fetchone()[0]
    finally:
        c.close()


def test_script_dry_run_writes_nothing_and_apply_backs_up(sync_db_path: Path, capsys):
    with open_db(sync_db_path) as conn:
        _seed(conn)
    script = _script()

    assert script.main(["--db", str(sync_db_path)]) == 0
    assert _count(sync_db_path) == 0
    assert "Dry run" in capsys.readouterr().out
    assert not list(sync_db_path.parent.glob(f"{sync_db_path.name}.pre-guess-legacy*"))

    assert script.main(["--db", str(sync_db_path), "--apply"]) == 0
    assert _count(sync_db_path) == 3
    backups = list(sync_db_path.parent.glob(f"{sync_db_path.name}.pre-guess-legacy-consents-*"))
    assert len(backups) == 1 and _count(backups[0]) == 0  # the backup predates the rows

    capsys.readouterr()
    assert script.main(["--db", str(sync_db_path), "--apply"]) == 0
    assert "Nothing to do" in capsys.readouterr().out
    assert _count(sync_db_path) == 3


@pytest.mark.parametrize(
    ("holders", "expected"),
    [(f"{U + 10}\n# a comment\n\n{U + 20} trailing words\n", 2), ("999\n", 0)],
)
def test_script_holders_file_restricts_the_set(sync_db_path: Path, tmp_path, holders, expected):
    with open_db(sync_db_path) as conn:
        _seed(conn)
    f = tmp_path / "holders.txt"
    f.write_text(holders, encoding="utf-8")
    _script().main(["--db", str(sync_db_path), "--holders-file", str(f), "--apply"])
    assert _count(sync_db_path) == expected
