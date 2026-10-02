-- Migration 234: drop `quality_score_leaves` (docs/plans/remove-contributors.md).
-- Billy, 2026-09-16: "can we drop the Contributors panel and reports?", and
-- signed off the drop the data register had been waiting on.
--
-- The table held a member's leave of absence (start and end) so the member
-- quality score could skip those weeks. The `/quality_leave` commands that
-- wrote it went 2026-07-28, the quality score that read it went 2026-08-31, and
-- the Contributors report that replaced the score never touched it. Nothing
-- has read or written it since; the register listed it as Orphaned.
--
-- Not reversible, but nothing is lost: prod held 0 rows (read-only count,
-- 2026-09-16). Migration 000 still creates the table on a fresh database; this
-- runs straight after and removes it, as 232 does for the Pressure Cooker
-- tables.

DROP TABLE IF EXISTS quality_score_leaves;
