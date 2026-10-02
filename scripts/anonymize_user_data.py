#!/usr/bin/env python3
"""Anonymise a departed member — rewrite their id, keep the statistics.

The third privacy path beside ``export_user_data.py`` (access) and the erasure
in ``docs/gdpr_runbook.md``. It replaces one member's Discord id with a small
surrogate integer everywhere it appears and destroys the names, so the server's
activity history stays *numerically identical* while ceasing to identify
anyone. Use it for a member who has left; use the purge when someone has
actually asked to be erased.

    # What would change. Read-only, safe against the live bot.
    python scripts/anonymize_user_data.py --user 203899999999999916 --dry-run

    # Rehearse on a snapshot, and prove the metrics did not move.
    python scripts/anonymize_user_data.py --user <uid> \\
        --db ~/dk-gdpr-rehearsal/anon_rehearsal.db --apply --verify-metrics

    # The real thing.
    python scripts/anonymize_user_data.py --user <uid> --db dungeonkeeper.db --apply

``--dry-run`` is the default and opens the database ``mode=ro``; you have to
ask for ``--apply`` explicitly and name the database. **The rewrite is one-way
by design** — no surrogate↔id mapping is stored anywhere, because storing one
would make the result pseudonymous rather than anonymous. Take a backup first
if you want the option of going back.

Runs across **every guild at once**. Anonymising one guild while the member's
roster row survives in another leaves their real name beside timestamps that
match the surrogate's activity exactly, which anyone with the database can
undo.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.privacy.service import (  # noqa: E402
    AnonymizeReport,
    anonymize_missed_text,
    anonymize_user_data,
    next_surrogate_id,
    plan_anonymization,
)

DEFAULT_DB = PROJECT_ROOT / "dungeonkeeper.db"

#: The claim this whole feature makes, expressed as SQL. Each is an aggregate
#: that must be bit-identical before and after — if a rewrite ever moves one of
#: these, the premise ("every count, sum, rate and distribution is unchanged")
#: is false and the operator needs to know before committing, not after.
METRIC_PROBES: tuple[tuple[str, str], ...] = (
    ("messages, total", "SELECT COUNT(*) FROM messages"),
    (
        "messages, distinct authors",
        "SELECT COUNT(DISTINCT author_id) FROM messages",
    ),
    (
        "messages, per-channel histogram",
        "SELECT channel_id, COUNT(*) FROM messages GROUP BY channel_id "
        "ORDER BY channel_id",
    ),
    ("xp_events, total rows", "SELECT COUNT(*) FROM xp_events"),
    ("xp_events, summed amount", "SELECT ROUND(SUM(amount), 4) FROM xp_events"),
    # NOTE these two select the aggregate WITHOUT its key. That is deliberate
    # and is the only honest way to state the claim: the subject's key is
    # *supposed* to change (that is the feature), while the distribution of
    # values across members must not move at all. A probe that selected
    # `user_id, SUM(...)` would report a false failure on every single run —
    # it did, on the first rehearsal, showing the same 23,325.85 sitting in the
    # same rank under a new key.
    (
        "xp_events, per-member total distribution",
        "SELECT ROUND(SUM(amount), 4) FROM xp_events GROUP BY user_id "
        "ORDER BY 1 DESC, 1",
    ),
    (
        "xp_events, distinct members",
        "SELECT COUNT(DISTINCT user_id) FROM xp_events",
    ),
    ("xp_daily, summed", "SELECT ROUND(SUM(xp), 4) FROM xp_daily"),
    ("member_xp, summed total_xp", "SELECT ROUND(SUM(total_xp), 4) FROM member_xp"),
    ("econ_ledger, net float", "SELECT SUM(amount) FROM econ_ledger"),
    ("econ_ledger, rows", "SELECT COUNT(*) FROM econ_ledger"),
    (
        "econ_ledger, per-member balance distribution",
        "SELECT SUM(amount) FROM econ_ledger GROUP BY user_id ORDER BY 1 DESC, 1",
    ),
    (
        "econ_ledger, distinct members",
        "SELECT COUNT(DISTINCT user_id) FROM econ_ledger",
    ),
    ("message_mentions, rows", "SELECT COUNT(*) FROM message_mentions"),
    ("voice_sessions, rows", "SELECT COUNT(*) FROM voice_sessions"),
    ("known_users, roster size", "SELECT COUNT(*) FROM known_users"),
)


#: Sentinel for a probe that could not run. It has to be distinguishable from
#: a result, because an unavailable probe compares equal to itself before and
#: after and would otherwise pass vacuously — which one did: `xp_daily` was
#: probed as `SUM(amount)` when the column is `xp`, so that check silently
#: measured nothing while reporting "identical".
_UNAVAILABLE = "unavailable"


def _snapshot_metrics(conn: sqlite3.Connection) -> dict[str, object]:
    out: dict[str, object] = {}
    for label, sql in METRIC_PROBES:
        try:
            out[label] = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            out[label] = (_UNAVAILABLE, str(exc))
    return out


def _print_report(report: AnonymizeReport, *, applied: bool) -> None:
    verb = "Rewrote" if applied else "Would rewrite"
    print(f"\n  user {report.user_id}  →  surrogate {report.surrogate_id}")
    if report.username or report.display_name:
        print(
            f"  username {report.username!r}  display name "
            f"{report.display_name!r}"
        )
        print(
            "  the username is swept from free text; the display name is NOT "
            "(see anonymize_user_data's docstring)"
        )

    for bucket, title in (
        ("id_rewrites", f"{verb} the id in these columns"),
        ("text_rewrites", f"{verb} the id/username inside text"),
        ("text_cleared", "Free text cleared (rows kept, text gone)"),
    ):
        rows: dict[str, int] = getattr(report, bucket)
        if not rows:
            continue
        total = sum(rows.values())
        print(f"\n  {title} — {len(rows)} columns, {total:,} rows")
        for label, n in sorted(rows.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"      {n:>8,}  {label}")

    print(f"\n  TOTAL: {report.total_rows:,} rows")
    print(
        f"\n  Keeping the real id, by decision, in: "
        f"{', '.join(report.kept)}"
    )
    if report.skipped:
        print(
            f"\n  Not present in this schema ({len(report.skipped)}): "
            f"{', '.join(report.skipped[:12])}"
            + (" …" if len(report.skipped) > 12 else "")
        )


def _diff_metrics(before: dict, after: dict) -> int:
    moved = [k for k in before if before[k] != after.get(k)]
    blind = [
        k
        for k in before
        if isinstance(before[k], tuple) and before[k][0] == _UNAVAILABLE
    ]
    print("\n── Metric verification " + "─" * 46)
    print(f"  {len(before) - len(moved)}/{len(before)} aggregates identical")
    if blind:
        # Not a failure — a deployment legitimately may not have every table —
        # but it must never be reported as a passing check.
        print(f"  ⚠️  {len(blind)} probe(s) could not run and measured NOTHING:")
        for k in blind:
            print(f"      {k}: {before[k][1]}")
    if not moved:
        print("  ✅ every probe unchanged — the statistics survived the rewrite")
        return 0
    print("  ❌ THESE MOVED — the anonymisation was not metric-neutral:")
    for k in moved:
        print(f"      {k}")
        print(f"        before: {str(before[k])[:160]}")
        print(f"        after:  {str(after.get(k))[:160]}")
    return 1


def _remediate(args) -> int:
    """The text-only second pass over an already-anonymised surrogate."""
    if not args.apply:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        try:
            report = anonymize_missed_text(conn, args.surrogate, dry_run=True)
        finally:
            conn.close()
        print(f"\n── REMEDIATION DRY RUN (read-only) — {args.db} " + "─" * 12)
    else:
        conn = sqlite3.connect(args.db, timeout=60, isolation_level="DEFERRED")
        conn.execute("PRAGMA busy_timeout = 60000")
        try:
            before = _snapshot_metrics(conn) if args.verify_metrics else {}
            with conn:
                report = anonymize_missed_text(conn, args.surrogate)
            print(f"\n── REMEDIATION APPLIED — {args.db} " + "─" * 20)
            _print_remediation(report, applied=True)
            rc = 0
            if args.verify_metrics:
                rc = _diff_metrics(before, _snapshot_metrics(conn))
            print()
            return rc
        finally:
            conn.close()

    _print_remediation(report, applied=False)
    print(
        "\n  Nothing was written. Re-run with --apply to do it for real.\n"
    )
    return 0


def _print_remediation(report: AnonymizeReport, *, applied: bool) -> None:
    verb = "Cleared" if applied else "Would clear"
    print(f"\n  surrogate {report.surrogate_id} — text only, no id is rewritten")
    if not report.text_cleared:
        print("\n  Nothing to clear: this surrogate is already up to date.")
        return
    total = sum(report.text_cleared.values())
    print(f"\n  {verb} — {len(report.text_cleared)} columns, {total:,} rows")
    for label, n in sorted(
        report.text_cleared.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        print(f"      {n:>8,}  {label}")
    print(f"\n  TOTAL: {report.total_rows:,} rows")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument(
        "--user", type=int, help="the member's Discord id (not with --remediate)"
    )
    ap.add_argument(
        "--remediate",
        action="store_true",
        help="re-run only the TEXT clears against an already-anonymised "
        "surrogate (--surrogate N). For when the clear lists have grown since "
        "that member was anonymised: no id is rewritten and no metric moves.",
    )
    ap.add_argument(
        "--surrogate",
        type=int,
        help="surrogate id to use (default: the next free one)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="report what would change and write nothing (the default)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually do it — one-way, and there is no mapping kept",
    )
    ap.add_argument(
        "--verify-metrics",
        action="store_true",
        help="with --apply: snapshot the aggregates before and after and diff "
        "them, failing if any moved. Use this on a rehearsal snapshot.",
    )
    args = ap.parse_args()

    if not args.db.exists():
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2

    if args.remediate:
        if args.surrogate is None:
            print("--remediate needs --surrogate N", file=sys.stderr)
            return 2
        if args.user is not None:
            print(
                "--remediate takes --surrogate, not --user: it clears text "
                "beside an id that has already been rewritten.",
                file=sys.stderr,
            )
            return 2
        return _remediate(args)

    if args.user is None:
        print("--user is required (or --remediate --surrogate N)", file=sys.stderr)
        return 2

    if not args.apply:
        uri = f"file:{args.db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            # `is not None`, not `or` — an explicit `--surrogate 0` must be
            # rejected by plan_anonymization/_validate_surrogate, not silently
            # swapped for a different, auto-picked surrogate.
            surrogate = (
                args.surrogate if args.surrogate is not None
                else next_surrogate_id(conn)
            )
            report = plan_anonymization(
                conn, args.user, surrogate_id=surrogate
            )
        finally:
            conn.close()
        print(f"\n── DRY RUN (read-only) — {args.db} " + "─" * 20)
        _print_report(report, applied=False)
        print(
            "\n  Nothing was written. Re-run with --apply --db <path> to do it "
            "for real.\n  It cannot be undone: no surrogate↔id mapping is "
            "stored anywhere, by design.\n"
        )
        return 0

    conn = sqlite3.connect(args.db, timeout=60, isolation_level="DEFERRED")
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        before = _snapshot_metrics(conn) if args.verify_metrics else {}
        # `is not None`, not `or` — an explicit `--surrogate 0` must reach
        # anonymize_user_data's validation and be refused, not be silently
        # swapped for a different, auto-picked surrogate.
        surrogate = (
            args.surrogate if args.surrogate is not None
            else next_surrogate_id(conn)
        )
        # One transaction: a raise anywhere rolls the whole rewrite back
        # rather than leaving the member half-anonymised.
        with conn:
            report = anonymize_user_data(
                conn, args.user, surrogate_id=surrogate
            )
        print(f"\n── APPLIED — {args.db} " + "─" * 30)
        _print_report(report, applied=True)
        rc = 0
        if args.verify_metrics:
            rc = _diff_metrics(before, _snapshot_metrics(conn))
        print()
        return rc
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
