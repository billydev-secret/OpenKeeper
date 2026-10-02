#!/usr/bin/env python3
"""Close Guess rounds that were created but never posted (bug #227).

ARCHIVED. Applied to prod 2026-09-17 (rounds 21-23).

Before the rollback landed, a round whose channel post raised left the row
behind with ``message_id = 0``: unsolved, undeleted, and so still counted as
an open round by everything that asks "which rounds are live?". Prod rounds
21-23 (2026-05-10) are the survivors.

This soft-deletes them — the same ``deleted_at`` stamp ``/guess delete`` sets,
and the same one the new rollback writes — and records a ``post_failed`` audit
event for each, so the rows say what happened to them rather than nothing.

It also **clears ``original_path`` and deletes the cached file**, exactly as
``guess_cleanup_service.rollback_unposted_round`` does. Closing the row without
that would strand the member's full-size photo forever: the age-out only looks
at rounds with ``deleted_at IS NULL``, and the orphan sweep skips any file a
row still records a path to — so a soft-deleted round holding a path is a
photo no sweep can ever reach. Clearing the path is the part that matters; the
unlink here is a best-effort shortcut for when the script runs with the bot's
working directory (recorded paths are relative), and the orphan sweep picks up
whatever it misses on the next pass.

A round only qualifies if it never posted (``message_id`` 0 or NULL) **and**
is not already solved or deleted. A solved round obviously posted; a round
posted a moment ago has its message id by then. Running it twice is a no-op.

Dry run (prints what it would do, changes nothing)::

    python scripts/archive/close_unposted_guess_rounds.py --db /path/to/dungeonkeeper.db

Apply::

    python scripts/archive/close_unposted_guess_rounds.py --db /path/to/dungeonkeeper.db --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# The actor recorded on the audit event. Not a member: nobody pressed
# anything, this is the cleanup itself speaking.
SCRIPT_ACTOR_ID = 0

SELECT_SQL = """
    SELECT id, guild_id, submitter_id, created_at, original_path
      FROM guess_rounds
     WHERE (message_id IS NULL OR message_id = 0)
       AND solved_at IS NULL
       AND deleted_at IS NULL
     ORDER BY id
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to dungeonkeeper.db")
    ap.add_argument(
        "--apply", action="store_true",
        help="write the changes (without this it is a dry run)",
    )
    args = ap.parse_args()

    uri = args.db if args.db.startswith("file:") else f"file:{args.db}"
    if not args.apply:
        uri += ("&" if "?" in uri else "?") + "mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(SELECT_SQL).fetchall()
    if not rows:
        print("No never-posted rounds to close. Nothing to do.")
        return 0

    print(f"{len(rows)} never-posted round(s):")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_at"]))
        orig = r["original_path"] or "(no cached original)"
        print(
            f"  round {r['id']}  guild {r['guild_id']}  submitter {r['submitter_id']}"
            f"  created {when}  {orig}"
        )

    if not args.apply:
        print(
            "\nDry run — nothing written. Re-run with --apply to close them "
            "and delete the cached originals listed above."
        )
        return 0

    now = time.time()
    unlinked = 0
    with conn:
        for r in rows:
            original_path = r["original_path"] or ""
            # Clear the path in the same statement that closes the round: a
            # soft-deleted row still holding a path is invisible to both
            # sweeps, and that is precisely the photo bug #227 leaves behind.
            conn.execute(
                "UPDATE guess_rounds SET deleted_at = ?, original_path = '' WHERE id = ?",
                (now, r["id"]),
            )
            deleted = False
            if original_path:
                try:
                    path = Path(original_path)
                    if path.is_file():
                        path.unlink()
                        deleted = True
                        unlinked += 1
                except OSError as exc:
                    print(f"  ! could not delete {original_path}: {exc}")
            conn.execute(
                """
                INSERT INTO guess_audit_log
                    (guild_id, ts, actor_id, action, round_id, details)
                VALUES (?, ?, ?, 'post_failed', ?, ?)
                """,
                (
                    r["guild_id"], now, SCRIPT_ACTOR_ID, r["id"],
                    json.dumps({
                        "closed_by": "close_unposted_guess_rounds.py",
                        "had_original": bool(original_path),
                        "original_deleted": deleted,
                    }),
                ),
            )
    print(f"\nClosed {len(rows)} round(s); deleted {unlinked} cached original(s).")
    print(
        "Any original this script could not reach on disk is now unreferenced, "
        "so the bot's orphan sweep deletes it on its next pass."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
