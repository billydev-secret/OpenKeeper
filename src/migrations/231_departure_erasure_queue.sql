-- Migration 231: the departure auto-anonymise queue
-- (docs/plans/departure-auto-anonymise.md, stage 2). Billy, 2026-09-14: "when
-- someone leaves I want to automatically run delete_me and anonymize data for
-- them".
--
-- One row per member who left a guild whose `departure_auto_anonymise` dial is
-- on, written by the privacy cog's on_member_remove listener. The listener
-- cannot do the work itself: nine other leave listeners still look the member
-- up by real id, and a full message scan takes minutes. The worker picks a row
-- once `next_at` has passed, deletes the member's Discord messages, then
-- anonymises them and deletes this row in the same transaction.
--
-- The row holds the REAL member id, so it must never outlive the anonymisation
-- — a row left behind would be the surrogate-to-member mapping the rewrite
-- exists never to write. A rejoin, or a jail or ticket found at run time,
-- deletes it with nothing done.
--
-- state: 'pending' (waiting for next_at), or 'failed' after the worker's last
-- permitted attempt — parked for an admin to retry or drop, never retried by
-- itself. attempts/last_error make a failing run visible on the dashboard.
--
-- Ships dark: with the dial off everywhere, nothing writes this table.

CREATE TABLE IF NOT EXISTS departure_erasure_queue (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    left_at     REAL NOT NULL,
    next_at     REAL NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_departure_erasure_queue_due
    ON departure_erasure_queue (state, next_at);
