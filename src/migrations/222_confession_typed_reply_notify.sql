-- Migration 222: Confessions can DM the author about ordinary replies too, and
-- the old notify dial finally does what its label says (2026-09-12).
--
-- 1. `notify_op_on_typed_reply` — new, ships OFF. With it on, a member typing in
--    a confession's thread (or using Discord's Reply on a bot-posted confession
--    message) DMs the author, naming the replier, exactly when the author's own
--    "Notify me on replies?" answer is yes. Until now only the anonymous-reply
--    buttons could DM anyone.
--
-- 2. `notify_op_on_reply` changes meaning. The confess and reply modals
--    hard-coded "yes" as the notify box's starting answer, and this dial only
--    fed the fallback for rows whose stored answer is the legacy -1 — a row
--    shape no production row has (verified read-only 2026-09-12: 35 rows at 1,
--    4 at 0, none at -1). So the dashboard's "DM the author" checkbox changed
--    nothing. It now sets the modal's starting answer, which is what
--    confessions_spec.md already claimed it did.
--
--    Both prod guilds have it at 0, and a 0 would flip every modal to start at
--    "no" on restart. Every row is set to 1 so members see the same form they
--    see today; an admin who wants "no" unticks it.

ALTER TABLE confession_config
    ADD COLUMN notify_op_on_typed_reply INTEGER NOT NULL DEFAULT 0;

UPDATE confession_config SET notify_op_on_reply = 1;
