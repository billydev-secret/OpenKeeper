-- Migration 248: Greeting Watch's DM list is retired (2026-09-30).
--
-- Ben: "Can you add a missed hello ping to the mod chat settings. The one
-- that sends me a dm." The unanswered-greeting alert became a staff job ping
-- (Staff · Greetings, docs/staff_pings_spec.md): a role mention in the staff
-- ping channel that each moderator switches on for themselves on the Mod
-- Settings panel. The DM route it replaces is gone, with its dashboard picker
-- and both keys that fed it:
--
--   greeting_watch_notify_user_ids  the CSV of members to DM
--   greeting_watch_notify_user_id   the pre-multi single key (migration 195
--                                   deleted the one prod row; the PUT could
--                                   still write a blank one back)
--
-- Deleted, not blanked: nothing reads either key any more, and the CSV holds
-- member ids, which are not kept past their use.
--
-- Live effect on the next restart: the DMs stop. Unanswered greetings then
-- alert nobody until a moderator presses Staff · Greetings on the Mod
-- Settings panel. One prod row today (one member id, the home guild).
--
-- Config rows only, no schema change.

DELETE FROM config WHERE key IN (
    'greeting_watch_notify_user_ids',
    'greeting_watch_notify_user_id'
);
