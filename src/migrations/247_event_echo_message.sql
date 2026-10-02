-- Migration 247: Event Echo remembers the card it posted (bug #273,
-- 2026-09-29).
--
-- Ben: "the chat echo feature can leave cards up if the game was deleted. We
-- should go back and delete card if that happens." Nothing could: the log row
-- recorded that an echo went out, never which message it was.
--
-- `echo_channel_id` / `echo_message_id` -- where the card landed, written
-- after a successful send. NULL on a suppressed row, on a row whose send
-- failed, on every row older than this migration, and again once the card has
-- been retracted (clearing the id is the exactly-once claim on the delete).
--
-- Not per-user data: a channel id and the id of the bot's own message.
--
-- Nothing changes at restart for cards already posted: their rows carry no
-- id, so they cannot be retracted. From the restart on, force-closing a party
-- game (/games end, a rotation close, the expiry sweep) deletes its echo.

ALTER TABLE event_echo_log ADD COLUMN echo_channel_id INTEGER;
ALTER TABLE event_echo_log ADD COLUMN echo_message_id INTEGER;
