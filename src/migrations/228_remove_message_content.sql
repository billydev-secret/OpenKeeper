-- Remove stored message text.
--
-- On 2026-09-13 the owner decided the bot keeps no copy of what members
-- write: stop storing it, and destroy what is stored
-- (docs/plans/remove-message-storage.md). The earlier commits on that branch
-- removed every reader (Message Search, AI Query, the /ai commands, Flagged
-- Messages) and the writer (store_message no longer takes text). This is the
-- stage that erases.
--
-- Production at removal time (backup-API snapshot, 2026-09-13):
--   messages.content        600,799 of 765,040 rows held text, 61.8 MB,
--                           all but 6 in the primary guild
--   message_attachments      45,433 attachment URLs
--   message_embeds           84,276 embed titles, descriptions and fields
--   health_metrics_cache          3 rows quoting message text (the Flagged
--                                   Messages feeds and the outlier list)
--
-- Kept: every message row and what was derived from it at ingest (author,
-- channel, timestamps, reply target, media_kind, sentiment, emotion, deletion
-- marks), message_mentions, message_reactions and message_sentiment.
-- media_kind replaces message_attachments for "did this message carry a file".
--
-- secure_delete makes SQLite overwrite the cells and pages this migration
-- frees with zeros instead of leaving the old bytes in the file's free space.
-- It applies to this connection only, and it cannot reach space that was
-- already free before it ran: old copies of rows rewritten or deleted earlier
-- (sentiment updates, retention sweeps, erasure requests). A dry run on a
-- production snapshot found 2 of 200 sampled texts still readable in that
-- older free space afterwards. VACUUM rebuilds the file and removes them (11.6 s
-- on the snapshot, 1117 MB down to 816 MB), but it cannot run inside the
-- migration's transaction, so it is a separate step run by hand after this
-- migration has applied (docs/plans/remove-message-storage.md, stage 4).
--
-- Backups taken before this ran still hold the text until they are deleted.
PRAGMA secure_delete = ON;

DROP TABLE IF EXISTS message_attachments;
DROP TABLE IF EXISTS message_embeds;

-- Rewrites every row of messages. No index, view or trigger names the column.
ALTER TABLE messages DROP COLUMN content;

-- Config rows only the removed features read: the storage dial and the
-- content-retention dial (both DEAD_KEYS since stage 3), and the prompts of
-- the four removed /ai commands.
DELETE FROM config WHERE key IN (
    'message_storage_level',
    'message_retention_enabled',
    'ai_prompt_review',
    'ai_prompt_scan',
    'ai_prompt_query_user',
    'ai_prompt_query_channel'
);

-- Cached dashboard payloads that quote message text. Nothing regenerates
-- them: the endpoints that built them were removed in stage 2. The plain
-- 'sentiment' aggregate holds scores only and stays.
DELETE FROM health_metrics_cache
 WHERE metric_key LIKE 'sentiment_feed%'
    OR metric_key LIKE 'deep:sentiment_feed%'
    OR metric_key LIKE 'sentiment_outliers%'
