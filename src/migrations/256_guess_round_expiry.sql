-- Migration 256: Guess Who rounds can time out (2026-10-01).
--
-- `guess_rounds.expired_at` -- stamped when a round is resolved unsolved
-- because it outlived the guild's `guess_round_lifetime_days` dial. It is NOT
-- `solved_at`: the top-posters query counts any row with `solved_at` set as a
-- solve, so reusing that column would have shown every timed-out round as one
-- on the public leaderboard (commit 6edc1b55).
--
-- `guess_rounds.expiry_cleared_at` -- stamped once the expired round's card is
-- deleted from the channel (or confirmed gone). An expired round reveals
-- nothing: members consented to a reveal only when a round is SOLVED, so the
-- card and the cached original are deleted. A delete that fails stays NULL and
-- the next daily sweep retries it.
--
-- Not per-user data beyond what the round row already is: it is a timestamp on
-- a row `forget_member` already deletes whole.
--
-- Ships dark: NULL on every existing round, and the dial defaults to 0 (never
-- expire), so nothing changes at restart until an admin sets a number of days.

ALTER TABLE guess_rounds ADD COLUMN expired_at REAL;
ALTER TABLE guess_rounds ADD COLUMN expiry_cleared_at REAL;
