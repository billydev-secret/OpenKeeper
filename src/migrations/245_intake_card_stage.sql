-- Migration 245: intake cards gain a stage (2026-09-28).
--
-- Intake becomes two passes (docs/plans/intake-two-stage.md). Stage 1 is the
-- join card, as before. Stage 2 opens when a role manager presses Grant on a
-- member's Level 5 promotion card: greeters grant NSFW access from the card
-- and run the NSFW question set.
--
-- Opening Stage 2 closes any still-open Stage 1 card first (resolution
-- 'advanced'), so a member still holds at most one open card and the
-- idx_intake_cards_open dedup index is unchanged. Every existing row is a
-- join card, hence DEFAULT 1.
--
-- opened_by is the Level 5 approver on a Stage 2 card (0 on a join card):
-- the card names who approved on every re-render, since a Components V2
-- card is rebuilt whole and has no untouched content line to keep it in.
-- It is a greeter-side id like resolved_by: purge_user_data deletes the
-- member's own card whole, and an approver's id on someone else's card is
-- kept on the same Art 17(3)(e) ground (docs/data_register.md, intake_cards).
ALTER TABLE intake_cards ADD COLUMN stage INTEGER NOT NULL DEFAULT 1;
ALTER TABLE intake_cards ADD COLUMN opened_by INTEGER NOT NULL DEFAULT 0;
