"""Contract: ``no_contact_pairs`` is queried only inside ``no_contact_service``.

Every surface that puts two members in contact consults the list through
``is_no_contact_conn`` / ``no_contact_partners_conn`` / ``no_contact_pairs_among``
(CLAUDE.md, docs/specs/no_contact_spec.md). A hand-written query elsewhere drifts from
the service's pair normalisation (low id first) and its protected-party column,
and is invisible to whoever changes either.

Matches SQL only: the bare table name in ``privacy_service``'s erasure and
retention frozensets is a registry entry, not a query, and stays allowed.

Born green: zero offenders on 2026-09-14.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"

SERVICE = "bot_modules/no_contact/service.py"

_TABLE = re.compile(r"\bno_contact_pairs\b")
_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN|INTO)\b", re.IGNORECASE)


def _sql_mentions(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        else:
            continue
        if _TABLE.search(text) and _SQL.search(text):
            hits.append(node.lineno)
    return hits


def test_no_contact_pairs_is_queried_only_by_its_service():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == SERVICE:
            continue
        offenders += [f"src/{rel}:{line}" for line in sorted(set(_sql_mentions(path)))]
    assert not offenders, (
        "Query the no-contact list through bot_modules.no_contact.service "
        "(is_no_contact_conn / no_contact_partners_conn / no_contact_pairs_among), "
        f"not raw SQL. Offenders: {offenders}"
    )


def test_the_service_is_still_where_the_queries_live():
    """Guards the sweep itself: if the service moves, the exemption above goes stale."""
    assert _sql_mentions(SRC / SERVICE)
