-- Migration 250: the sponsored question of the day is retired with /qotd.
--
-- Ben, 2026-09-30: "meant to retire the /qotd command". A mod tagging the
-- QOTD role in an ordinary message is how nearly every question arrives (the
-- command ran 7 times ever), and that path stays. The paid queue goes with
-- the command because /qotd post was the only thing that drained it: an
-- approved question had no other way out. Ben chose to retire it rather than
-- give it a new exit.
--
-- What this removes, all of it read-only-verified empty in prod first:
--
--   econ_qotd_submissions       the queue (090). Zero rows, ever.
--   econ_qotd.sponsor_user_id   who paid for a posted question (090). Zero on
--                               every row: no sponsored question ever ran.
--   econ_price_qotd_sponsor     the price dial, set to 40 on the home guild.
--   econ_qotd_sponsor_expire_days  its review window, set to 14 there.
--
-- The config delete enumerates its two keys: econ_emoji_sponsor_expire_days
-- shares the "sponsor_expire_days" tail and is a live Sponsored Emoji dial.
-- The ledger kinds qotd_sponsor / qotd_sponsor_refund have no rows either,
-- and econ_ledger is kept whole on principle, so nothing there is touched.
--
-- No index references sponsor_user_id, so DROP COLUMN is legal.
--
-- Live effect on the next restart: none that a member sees. The Sponsor row
-- has already left the shop in the same commit; this only removes the
-- storage it wrote to.

DROP INDEX IF EXISTS idx_econ_qotd_sub_open;
DROP INDEX IF EXISTS idx_econ_qotd_sub_state;
DROP TABLE IF EXISTS econ_qotd_submissions;

ALTER TABLE econ_qotd DROP COLUMN sponsor_user_id;

DELETE FROM config WHERE key IN (
    'econ_price_qotd_sponsor',
    'econ_qotd_sponsor_expire_days'
);
