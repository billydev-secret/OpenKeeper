-- Migration 241: screenshots on bug reports (2026-09-26, bug list row 244).
--
-- A member filing a bug can attach up to three images, and they are posted on
-- the bug board's next change card beside the bug's text. The bot keeps no copy
-- of an image: the card message in Discord is the only place one lives, and
-- closing the bug edits that card to take its images back off.
--
-- todos.attachment_urls
--   JSON list of the Discord upload links the filer attached, held ONLY until
--   the next change-card tick posts them (or gives up). The tick clears it, and
--   the bug loop clears any left older than an hour, so a switch turned off
--   mid-window cannot strand one. NULL on every row that has none.
--
-- todos.card_channel_id, todos.card_message_id
--   The change card a bug's images landed on, so closing the bug can find the
--   card and edit them off it. Cleared once that edit is done.
--
-- NOTHING CHANGES ON RESTART. Every existing row gets NULLs, and screenshots are
-- only offered where the bug board's change-card switch is on.
ALTER TABLE todos ADD COLUMN attachment_urls TEXT;
ALTER TABLE todos ADD COLUMN card_channel_id INTEGER;
ALTER TABLE todos ADD COLUMN card_message_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_todos_bug_card
    ON todos (card_message_id) WHERE card_message_id IS NOT NULL;
