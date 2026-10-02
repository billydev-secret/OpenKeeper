-- Migration 223: Auto-Thread (Needle) can DM a thread's poster when someone
-- replies (2026-09-12).
--
-- `needle_channels.notify_op_on_reply` — per channel, ships OFF. With it on,
-- every reply in a thread Needle opened DMs the member whose message started
-- it, naming the replier. No-contact pairs are dropped silently.
--
-- `needle_reply_mutes` — the DM carries a "Stop reply DMs for this thread"
-- button; a row here is that press. PER-USER DATA: purged on erasure
-- (privacy_service.purge_user_data), row in docs/data_register.md. Retention is
-- until the thread is deleted (on_raw_thread_delete) or the channel stops
-- being auto-threaded (the dashboard's remove deletes the channel's rows).
-- `channel_id` is kept for that second path only.
--
-- Ships dark: needle_channels has zero rows in prod, and a new column at 0 on
-- any row that appears later sends nothing.

ALTER TABLE needle_channels
    ADD COLUMN notify_op_on_reply INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS needle_reply_mutes (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, thread_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_needle_reply_mutes_channel
    ON needle_reply_mutes (guild_id, channel_id);
