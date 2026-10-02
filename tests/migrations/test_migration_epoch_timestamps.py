"""A migration stamps time as epoch seconds, never as SQL's text clock.

Every timestamp this bot writes from Python is ``time.time()`` — a REAL
count of seconds — and every reader compares, subtracts and formats it as
one. ``CURRENT_TIMESTAMP`` (and ``CURRENT_DATE``, ``CURRENT_TIME``,
``datetime('now')``) stores ``'2026-09-27 14:03:11'`` instead: UTC text that
sorts correctly against itself and against nothing else. A column defaulted
that way and then written from Python holds both shapes at once, and a
``WHERE added_at > ?`` bound to a float compares text to a number — which
SQLite answers without complaint, and wrongly.

The epoch spellings are ``(unixepoch())`` and ``strftime('%s', 'now')``.
``date('now', …)`` is not flagged: it yields a *day*, and day columns such
as ``local_day`` are ``'YYYY-MM-DD'`` text by design, so comparing one with
it (``125_econ_monthly_community.sql`` does) is the right shape.

Ten shipped migrations use the text clock. Migrations are immutable (see
``test_migration_manifest.py``), so those ten stay as they are — a later
migration that converts a column is the fix, and it would not need an entry
here. The set exists to stop the eleventh.

Standards audit row 4.8.
"""

from __future__ import annotations

import re

from migrations import _strip_line_comments
from tests.paths import REPO_ROOT

MIGRATIONS = REPO_ROOT / "src" / "migrations"

TEXT_CLOCK = re.compile(
    r"\bCURRENT_(?:TIMESTAMP|DATE|TIME)\b|\bdatetime\s*\(\s*'now'",
    re.IGNORECASE,
)

SHIPPED_WITH_TEXT_CLOCK = frozenset({
    "019_games.sql",
    "022_games_config.sql",
    "029_games_editor_role.sql",
    "031_bios.sql",
    "056_external_game_tracking.sql",
    "097_external_watch_multi.sql",
    "099_external_game_payouts.sql",
    "149_bios_archived_at.sql",
    "220_dead_clapback_player_limit_options.sql",
    "221_survivor_hosts.sql",
})


def _text_clock_files() -> set[str]:
    return {
        p.name
        for p in MIGRATIONS.glob("*.sql")
        if TEXT_CLOCK.search(_strip_line_comments(p.read_text(encoding="utf-8")))
    }


def test_no_new_migration_uses_the_text_clock():
    new = sorted(_text_clock_files() - SHIPPED_WITH_TEXT_CLOCK)
    assert not new, (
        f"{new} stamp time with SQL's text clock. Use REAL with DEFAULT "
        "(unixepoch()) — or write time.time() from Python — so the column "
        "compares with every other timestamp in the schema."
    )


def test_the_shipped_set_still_matches():
    """A frozen entry the scan no longer finds means the scan broke, since
    a shipped migration can't legitimately change."""
    assert SHIPPED_WITH_TEXT_CLOCK <= _text_clock_files()


def test_the_scan_catches_every_spelling_and_skips_comments():
    for sql in (
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP",
        "d TEXT DEFAULT current_date",
        "SET t = datetime('now')",
        "SET t = datetime( 'now', 'utc')",
    ):
        assert TEXT_CLOCK.search(sql), sql
    for sql in (
        "created_at REAL NOT NULL DEFAULT (unixepoch())",
        "t INTEGER DEFAULT (strftime('%s', 'now'))",
        "a.local_day >= date('now', '-28 day')",
    ):
        assert not TEXT_CLOCK.search(sql), sql
    assert not TEXT_CLOCK.search(
        _strip_line_comments("-- never CURRENT_TIMESTAMP\nx REAL")
    )
