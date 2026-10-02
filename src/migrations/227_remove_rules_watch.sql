-- Remove Rules Watch.
--
-- Rules Watch was the passive AI-assisted rule-violation monitor: the guard
-- model screened public messages, flagged the ones it judged rule-breaking
-- into a moderator queue, and kept the moderators' verdicts as labels. On
-- 2026-09-13 the owner decided to drop it completely ("it's too close for not
-- enough utility"). The package, its cog (including the Report Rule Violation
-- context menu), its web routes, both dashboard panels, its scripts and its
-- docs are removed in the same commit as this migration.
--
-- Production rows at removal time, all in the primary guild:
--   rules_events   3104  one per flagged message, with a content excerpt and
--                        a verbatim channel-transcript window
--   rules_labels    277  moderator verdicts on those events
--   rules_ledger      2  ledger entries with the matched phrase and excerpt
--
-- Deliberately not archived or exported first. The whole store for the
-- retired processing activity goes, so nothing is left for an access or
-- erasure request to reach; moving it to a side table would defeat the
-- decision.
--
-- rules_labels.event_id references rules_events(id), so it is dropped first.
-- The tables came from 030_rules_watch.sql and 095_rules_ledger.sql, which
-- stay as they are.
DROP TABLE IF EXISTS rules_labels;
DROP TABLE IF EXISTS rules_events;
DROP TABLE IF EXISTS rules_ledger;

-- The config keys only the removed feature read: its on/off dial, its
-- immediate-alert channel, and the guard model's editable prompt.
DELETE FROM config WHERE key IN (
    'rules_watch_enabled',
    'rules_watch_channel_id',
    'ai_prompt_rules_watch'
);
