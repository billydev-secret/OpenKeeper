-- Migration 254: the Bios icebreaker "How Often It Comes Up" weight is removed.
--
-- Owner decision, 2026-10-01. The dial was saved and nothing read it: members
-- pick their own icebreakers from a list (the weighted random draw that once
-- used it was never wired into the wizard, and is deleted with the column).
-- The dashboard field, the route fields and the draw helper go in the same
-- commit. No index references weight, so DROP COLUMN is legal.
--
-- Live effect on the next restart: none a member sees. Every question keeps
-- its text, active flag and answers.

ALTER TABLE bio_questions DROP COLUMN weight;
