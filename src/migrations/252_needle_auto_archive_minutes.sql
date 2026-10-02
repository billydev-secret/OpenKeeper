-- Migration 252: Auto-Thread (Needle) threads archive after a per-channel
-- duration (2026-10-01).
--
-- Ben: "Can we add a config dial for auto_archive_duration when needle
-- creates threads". Every thread Needle opened was hard-coded to archive
-- after one day of quiet.
--
-- `needle_channels.auto_archive_minutes` -- per channel, one of the four
-- durations Discord accepts: 60 (1 hour), 1440 (1 day), 4320 (3 days),
-- 10080 (1 week). Anything else stored here is read as 1440 by
-- needle.logic.auto_archive_minutes, so a bad value never costs a thread.
--
-- Not per-user data: the column is a channel setting.
--
-- Ships dark: DEFAULT 1440 on every existing row, which is exactly the value
-- the cog used to send, so a restart changes nothing until an admin picks a
-- different duration on the Auto-Thread panel.

ALTER TABLE needle_channels
    ADD COLUMN auto_archive_minutes INTEGER NOT NULL DEFAULT 1440;
