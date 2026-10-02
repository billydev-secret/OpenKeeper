-- Migration 237: self-service break holds (docs/plans/take-a-break.md).
--
-- A member presses "Take a break": their roles are snapshotted here, stripped,
-- and they are given the break role (one channel, holding the "I'm back"
-- panel) plus Spicy Lite if they had Spicy. "I'm back" restores the snapshot
-- and deletes the row. A row existing IS the break: there is no history here
-- (the audit log records start and end), so nothing about a finished break is
-- kept.
--
-- Kept apart from `inactive_members` on purpose: that table's moderator/reason
-- columns are mod-shaped, and every reader of it (the inactive report, the
-- promotion sleeper watch, the inactive sweep's exclude set) would otherwise
-- have to learn to filter breaks out — each one that forgot would misfire
-- silently.
--
-- The row is written BEFORE the roles come off, so a crash mid-strip never
-- loses a member's roles. The primary key makes a double press a constraint
-- error rather than a second snapshot overwriting the first.
--
-- swapped_spicy: 1 when Spicy was taken and Spicy Lite given, so the end of
-- the break knows to take Spicy Lite back off.
--
-- break_role_id / lite_role_id: the hold roles this break actually put on
-- (0 = none). The end of the break takes these off as well as whatever the
-- dials name now, so an admin re-pointing either dial mid-break can't strand
-- the old role on the member for good.

CREATE TABLE IF NOT EXISTS break_holds (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    stored_roles   TEXT    NOT NULL DEFAULT '[]',
    swapped_spicy  INTEGER NOT NULL DEFAULT 0,
    break_role_id  INTEGER NOT NULL DEFAULT 0,
    lite_role_id   INTEGER NOT NULL DEFAULT 0,
    started_at     REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
