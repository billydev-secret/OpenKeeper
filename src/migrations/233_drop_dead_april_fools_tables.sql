-- Migration 233: drop the April Fools prank's two tables
-- (standards-code-queue, review 2026-09-13 M7). Billy's decision, 2026-09-14:
-- delete the dead service and drop both tables rather than register them.
--
-- `foolsday_service` created `foolsday_names` (each member's saved original
-- nickname) and `foolsday_exclusions` (members who opted out of the prank) with
-- CREATE TABLE IF NOT EXISTS at first use, so no migration ever made them and
-- neither exists in a freshly-migrated schema. Nothing has imported the service
-- since the April Fools cog was deleted (c4f8758a, 2026-05-29). The exclusions
-- had a purge statement; the saved nicknames had none.
--
-- Not reversible: the rows are gone. Row counts read from prod (read-only) on
-- 2026-09-14: foolsday_names 0, foolsday_exclusions 10. Those ten opt-outs are
-- a preference for a feature that no longer exists; if the prank ever returns,
-- members opt out again. IF EXISTS because only a database that ran the prank
-- before 2026-05-29 has either table.

DROP TABLE IF EXISTS foolsday_names;
DROP TABLE IF EXISTS foolsday_exclusions;
