# Archived one-shot scripts

Backfills, refunds and migrations that have already run against prod. Each
docstring opens with the date it was applied. They are kept as a record of
what changed the data and how, not to be run again. Most are idempotent, but
they were written against the schema of their day.

Migration 163 still names `scripts/backfill_thread_parents.py` at its old path;
migrations are hash-pinned, so the comment stays as written.
