#!/usr/bin/env python3
"""Record the Guess Who consent gap for members who never saw the disclosure.

Since 2026-08-06 joining the pool writes a ``guess_consents`` row. Members who
held the Guess role before that were grandfathered in and have none, so for
most of the people whose photos and guesses the game stores there is no record
either way (games review, rotation-rooms-171: 4 consent rows against 57
submitters). This writes one **version-0** row per such member: "legacy role
holder, disclosure not shown". It records that the gap exists; it is NOT
evidence of consent, and nothing in the bot treats it as such. The row's
``consented_at`` is the member's earliest round or guess, the nearest thing the
database has to when they began.

Who gets a row: every member with a round (as submitter or answer) or a guess in
a guild and no ``guess_consents`` row in it. The database cannot say who holds
the role today, so a member who left or dropped the role and has no row would be
included; pass ``--holders-file`` (one Discord user id per line, an export of the
Guess role's members) to restrict it to current holders.

    python scripts/backfill_guess_legacy_consents.py --db var/dungeonkeeper.db
    python scripts/backfill_guess_legacy_consents.py --db var/dungeonkeeper.db \\
        --holders-file holders.txt --apply

Dry run by default (the database is opened read-only and nothing is written).
``--apply`` first copies the database with SQLite's backup API to
``<db>.pre-guess-legacy-consents-<timestamp>``, then writes the rows in one
transaction. Idempotent: a second run finds nobody left and writes nothing.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bot_modules.games.guess.repo import (  # noqa: E402
    LEGACY_DISCLOSURE_VERSION,
    list_unevidenced_participants,
    record_legacy_consents,
)


def select_members(
    conn: sqlite3.Connection, holders: set[int] | None
) -> list[tuple[int, int, float]]:
    members = list_unevidenced_participants(conn)
    if holders is not None:
        members = [m for m in members if m[1] in holders]
    return members


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--db", required=True, help="path to dungeonkeeper.db")
    ap.add_argument("--holders-file", help="file of current Guess role holder ids, one per line")
    ap.add_argument("--apply", action="store_true", help="write the rows (default is a dry run)")
    args = ap.parse_args(argv)

    holders: set[int] | None = None
    if args.holders_file:
        holders = {
            int(line.split()[0])
            for line in Path(args.holders_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    # as_uri() percent-encodes the path; a bare f-string would misread a db path
    # holding "?", "#" or "%" as URI syntax and open the wrong file.
    uri = Path(args.db).resolve().as_uri() + "?mode=" + ("rw" if args.apply else "ro")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    members = select_members(conn, holders)
    if not members:
        print("Nobody needs a legacy consent row. Nothing to do.")
        return 0

    print(f"{len(members)} member(s) with Guess Who data and no consent row:")
    for guild_id, user_id, first_seen in members:
        when = time.strftime("%Y-%m-%d", time.gmtime(first_seen))
        print(f"  guild {guild_id}  user {user_id}  first seen {when}")
    if holders is None:
        print("\nNo --holders-file given: members who have since left or dropped the "
              "role are included.")

    if not args.apply:
        print(f"\nDry run - nothing written. --apply backs the database up, then writes "
              f"{len(members)} version-{LEGACY_DISCLOSURE_VERSION} row(s) to guess_consents.")
        return 0

    backup_path = f"{args.db}.pre-guess-legacy-consents-{int(time.time())}"
    dest = sqlite3.connect(backup_path)
    with dest:
        conn.backup(dest)
    dest.close()
    print(f"\nBackup written to {backup_path}")
    with conn:
        written = record_legacy_consents(conn, members)
    print(f"Wrote {written} legacy consent row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
