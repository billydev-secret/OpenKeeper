-- Migration 243: batched member notifications (2026-09-28).
--
-- A DM that can wait is queued here instead of sent; rows sharing
-- (kind, guild_id, user_id, group_key) go out as one DM once the oldest has
-- waited ten minutes (dm_batch.logic.WINDOW_SECONDS). First user: Auto-Thread
-- (Needle) reply DMs, kind 'needle_reply', group_key = the thread.
--
-- PER-USER DATA: `user_id` is the recipient, `actor_id` the member whose action
-- the row reports. Kept only until the batch is sent (the window plus any
-- restart downtime); purge_user_data deletes rows naming the member on either
-- side. Row in docs/data_register.md. No message content is stored: `ref_id`
-- is the message id, so deleting that message retracts the row.
--
-- No dial of its own: it changes how an existing switch delivers
-- (needle_channels.notify_op_on_reply), not whether anything is sent.

CREATE TABLE IF NOT EXISTS dm_batch_pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    group_key  INTEGER NOT NULL DEFAULT 0,
    actor_id   INTEGER NOT NULL DEFAULT 0,
    ref_id     INTEGER NOT NULL DEFAULT 0,
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dm_batch_pending_batch
    ON dm_batch_pending (kind, guild_id, user_id, group_key);

CREATE INDEX IF NOT EXISTS idx_dm_batch_pending_ref
    ON dm_batch_pending (guild_id, ref_id);
