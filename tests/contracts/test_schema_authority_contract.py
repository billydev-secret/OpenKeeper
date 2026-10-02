"""Whatever a service module creates, a migration declares too.

Production's schema has two authors. Most tables are written in
``src/migrations/``; a service module then repeats the same
``CREATE TABLE IF NOT EXISTS`` inside its own ``ensure_*_schema()`` so a fresh
install is safe whatever order things boot in. For 58 tables both authors say
the same thing and the second is a harmless no-op.

For twenty-one objects, only the service module ever said it — and those are
invisible to every test in this suite, because the suite's schema comes from
``tests/db_template.migrated_db``, which runs the migration files and nothing
else. Measured against the live database on 2026-09-21: one table
(``dm_mode_roles``) and twenty indexes existed in production and in no
migration, while every column matched. Migration 235 declared all 21 — a
no-op against prod, where they already existed — and this test is what keeps
the two authors in step from here.

**Why it matters beyond tidiness.** Three tests walk the schema and take what
they find as the whole truth: ``test_guild_purge_coverage.py`` (would a
departing guild leave rows behind?), ``test_privacy_register_coverage.py``
(does a table holding a member's id have a register row?), and
``test_guild_index_contract.py``. A table the suite cannot see is a table
those three silently pass. The standards audit's own row 1.12 was wrong for
this reason: it counted 39 guild-scoped tables missing a ``guild_id``-leading
index, when ten of the thirty-nine had had one in production all along.

**Names, not definitions.** The two authors are allowed to disagree about a
table's columns, and do, for documented reasons — ``wellness_service.py``
still creates ``wellness_users.cooldown_until`` and
``wellness_config.crisis_resource_url``, which migration 189 drops, and says
so at the line. Four tables carry a ``source`` column the migrations added and
the service's ``CREATE`` does not list, which the service's own
``ALTER TABLE ... ADD COLUMN`` guards fill in. Checking definitions here would
fail on all seven and teach nobody anything; the question this test asks is
the one with a wrong answer, which is whether the object exists at all.

**Temporary tables are out of scope, and the exclusion is deliberate.** A
``CREATE TEMP TABLE`` lives in the connection, never in ``sqlite_master``, so
no migration can declare it and this rule would be condemning something that
cannot be fixed. ``economy/stats_service.py`` builds
``_stats_active_members`` that way to stage one report query — the only such
site in the tree. The pattern below refuses ``TEMP``/``TEMPORARY`` explicitly
rather than leaving it to chance: the original alternation happened to miss
these anyway, because the interposed keyword broke the match, and an accident
that produces the right answer is one refactor away from producing the wrong
one.

**What this cannot see.** It reads string literals, so DDL assembled at
runtime from a variable is invisible — there is none in ``src`` today, and
every object name found is a literal. It does not look at ``ALTER TABLE``:
columns were measured identical between prod and the migrated schema on
2026-09-21, and the service-side ALTERs are all mirrored by migrations, but
nothing here would catch the day that stops being true. And it says nothing
about production's own orphans — ``incident_events`` and
``message_velocity_baseline`` survive there with 572 and 508 rows after
``3fa9aa86`` deleted the code that made them, and no test can see a table no
source file mentions. ``scripts/privacy_coverage.py`` against prod is the
control for that class.
"""

from __future__ import annotations

import ast
import re
import sqlite3

import pytest

from tests.db_template import migrated_db
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"
MIGRATIONS = SRC / "migrations"

#: ``TEMP``/``TEMPORARY`` is excluded deliberately, not by accident of the
#: alternation — see "Temporary tables are out of scope" in the docstring.
#: Spelling it out keeps the exclusion from silently reversing the day
#: someone widens the pattern.
_CREATE = re.compile(
    r"CREATE\s+(?!TEMP\b|TEMPORARY\b)"
    r"(?:(TABLE)|(?:UNIQUE\s+)?(INDEX))\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"'`\[]?(\w+)",
    re.IGNORECASE,
)

#: Objects a service module creates that no migration declares. Empty, and
#: shrink-only: a new one means adding it to a migration, not to this set.
BASELINE: set[str] = set()


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    db = migrated_db(tmp_path_factory.mktemp("authority") / "schema.db", reap=False)
    conn = sqlite3.connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the string constants that are docstrings, not SQL.

    A module that *describes* a ``CREATE TABLE`` in prose is not creating one,
    and the prose is where this file's own explanations live.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _app_ddl() -> dict[str, list[str]]:
    """``object name -> ["src/path:line", ...]`` for DDL outside the migrations."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if MIGRATIONS in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        skip = _docstring_nodes(tree)
        rel = path.relative_to(SRC.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            for match in _CREATE.finditer(node.value):
                name = match.group(3)
                line = node.lineno + node.value[: match.start()].count("\n")
                found.setdefault(name, []).append(f"{rel}:{line}")
    return found


def test_every_app_created_object_is_declared_in_a_migration(schema):
    declared = {
        row[0]
        for row in schema.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    missing = {
        name: sites
        for name, sites in _app_ddl().items()
        if name not in declared and name not in BASELINE
    }
    assert not missing, (
        "These are created by application code and by no migration, so the "
        "schema every test walks is missing them while production has them:\n  "
        + "\n  ".join(f"{name} — {', '.join(sites)}" for name, sites in sorted(missing.items()))
        + "\nAdd a `CREATE ... IF NOT EXISTS` for each to a new migration in "
        "src/migrations/. Keep the service module's copy; the point is that "
        "both authors say it, not that only one does."
    )


def test_baseline_only_shrinks(schema):
    declared = {
        row[0]
        for row in schema.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    app = _app_ddl()
    stale = sorted(n for n in BASELINE if n in declared or n not in app)
    assert not stale, f"Now declared, or gone — remove from BASELINE: {stale}"


def test_the_scan_finds_the_ddl_it_is_supposed_to_find():
    """A guard on the guard: a silently-empty scan would pass everything.

    The regex reads string literals, so a refactor that moved DDL into a
    generated string, or a stray change to the pattern, would quietly reduce
    this file to a test that asserts nothing. These are load-bearing counts,
    not a golden file — they only ever need touching when DDL genuinely moves.
    """
    found = _app_ddl()
    assert len(found) > 80, f"only {len(found)} objects found — has the scan broken?"
    assert "wellness_caps" in found, "a known service-created table vanished from the scan"
    assert "idx_warnings_user" in found, "a known service-created index vanished from the scan"
