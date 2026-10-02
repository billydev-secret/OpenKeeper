-- Migration 244: Auto-Thread reply DMs become opt-IN (2026-09-28).
--
-- Billy: "i want to make the DMs notifications for threaded posts opt in on
-- the server settings". Until now a channel with `notify_op_on_reply` on DMed
-- every thread starter, and a member left per thread with the DM's "Stop reply
-- DMs for this thread" button (`needle_reply_mutes`). Now a member is DMed only
-- if they switched **Replies to Your Threads** on in Help & Settings → Roles &
-- Pings, and the channel's dial still has to be on too.
--
-- `needle_reply_optins` — one row per member who switched it on. Turning it
-- off deletes the row, so Off stores nothing. PER-USER DATA: purged on erasure
-- (privacy_service.purge_user_data), row in docs/data_register.md.
--
-- `needle_reply_mutes` is dropped. It had 0 rows in prod the day this was
-- written. A mute meant "I'm enrolled, but not for this thread", and every
-- member starts un-enrolled now, so there is nothing a surviving row could
-- mean. No migration of rows.
--
-- Live effect at restart: reply DMs stop for every thread starter until they
-- opt in. Nobody is enrolled by this migration (Billy, 2026-09-28: quiet
-- cutover).

CREATE TABLE IF NOT EXISTS needle_reply_optins (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

DROP INDEX IF EXISTS idx_needle_reply_mutes_channel;
DROP TABLE IF EXISTS needle_reply_mutes;
