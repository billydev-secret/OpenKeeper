-- Migration 232: drop Pressure Cooker's two dead member-data tables
-- (standards-code-queue, review 2026-09-13 H1). Billy's decision, 2026-09-14:
-- drop rather than add purge statements for tables nothing uses.
--
-- Migration 028 created `pressure_nicks` (a loser's original and imposed
-- nickname) and `pressure_cooldowns` (who played whom). Pressure Cooker has
-- read and written the generic `duel_nicks` / `duel_cooldowns` tables since
-- migration 032, and no code names either original; the 2026-07-22 deep review
-- (S4) flagged them as dead schema. Both still hold member ids, and neither is
-- reached by `purge_user_data`, so an erasure left them behind.
--
-- Not reversible: the rows are gone. Row counts read from prod (read-only) on
-- 2026-09-14: pressure_nicks 1, pressure_cooldowns 4. `pressure_config` (the
-- pre-032 config shape, 0 rows, no member data) is deliberately left for the
-- dashboard-config cleanup that already tracks it.

DROP TABLE IF EXISTS pressure_nicks;
DROP TABLE IF EXISTS pressure_cooldowns;
