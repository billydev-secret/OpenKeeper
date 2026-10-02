-- Migration 251: Chat Revive's on/off becomes a three-state mode (2026-09-30).
--
-- Ben: "I want to be able to turn off the automated chat revive posting and
-- ping mods for it anyway." Lull detection is unchanged; what a confirmed lull
-- does is now a dial on revive_guild_config.mode:
--
--   off         nothing runs
--   ping_staff  a lull mentions @Staff · Chat Revive in the staff ping
--               channel and nothing posts in the channel
--   post        a lull posts a question card (the old enabled = 1)
--
-- One dial, not a second boolean beside `enabled`: the old column is copied
-- into `mode` and dropped, so no row can say "off" and "ping" at once.
--
-- A lull handed to the moderators is written to revive_events as
-- trigger_kind 'staff', so the rest period, gap and daily budget pace those
-- pings as they pace posts (the scoreboard and follow-up measuring skip it).
-- The column's CHECK only allowed 'auto' and 'manual', and SQLite can't alter
-- a CHECK, so the table is rebuilt with its rows and ids copied as they are:
-- an id is the quest occurrence key for the follow-up payout.
--
-- Live effect on the next restart: none. enabled = 1 becomes 'post' and
-- enabled = 0 becomes 'off', which is exactly what each row did before, and
-- every revive_events row survives unchanged.

ALTER TABLE revive_guild_config
    ADD COLUMN mode TEXT NOT NULL DEFAULT 'off';

UPDATE revive_guild_config
    SET mode = CASE WHEN enabled = 1 THEN 'post' ELSE 'off' END;

ALTER TABLE revive_guild_config DROP COLUMN enabled;

CREATE TABLE revive_events_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    question_id    INTEGER,
    message_id     INTEGER,
    trigger_kind   TEXT    NOT NULL DEFAULT 'auto'
                   CHECK (trigger_kind IN ('auto', 'manual', 'staff')),
    pinged         INTEGER NOT NULL DEFAULT 0,
    local_day      TEXT    NOT NULL,
    created_at     REAL    NOT NULL,
    measured_at    REAL,
    follow_msgs    INTEGER,
    follow_authors INTEGER,
    success        INTEGER
);

INSERT INTO revive_events_new
    (id, guild_id, channel_id, question_id, message_id, trigger_kind, pinged,
     local_day, created_at, measured_at, follow_msgs, follow_authors, success)
SELECT id, guild_id, channel_id, question_id, message_id, trigger_kind, pinged,
       local_day, created_at, measured_at, follow_msgs, follow_authors, success
  FROM revive_events;

DROP TABLE revive_events;

ALTER TABLE revive_events_new RENAME TO revive_events;

CREATE INDEX IF NOT EXISTS idx_revive_events_guild_ts
    ON revive_events (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_revive_events_channel_ts
    ON revive_events (guild_id, channel_id, created_at);
