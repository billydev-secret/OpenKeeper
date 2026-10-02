-- Migration 246: Auto-Thread (Needle) can require an upload on every post
-- (2026-09-29).
--
-- Ben: "i'm having users send normal messages in threaded channels. is there a
-- way to prevent that and still allow anyone to post images and texts
-- together?" Discord has no permission for it: removing Send Messages blocks a
-- captioned image too.
--
-- `needle_channels.require_attachment` -- per channel, ships OFF. With it on,
-- a member's post with no Discord attachment is deleted before any thread is
-- opened, and the poster gets a short note in the channel that the bot removes
-- after 15 seconds. A post with an upload keeps its caption and is threaded as
-- before. Members with Manage Messages and every bot are exempt.
--
-- Not per-user data: the column is a channel setting, and nothing about the
-- removed post is stored.
--
-- Ships dark: DEFAULT 0 on all five prod rows, so a restart changes nothing
-- until an admin ticks the box on the Auto-Thread panel.
--
-- Numbered 246 because 245 is taken by the unmerged intake-redesign branch.

ALTER TABLE needle_channels
    ADD COLUMN require_attachment INTEGER NOT NULL DEFAULT 0;
