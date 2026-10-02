-- Migration 225: "Game Night" becomes "Game Lobby" in the stored names too
-- (2026-09-12, todo 190 — "drop games night branding across the bot. They're
-- just games").
--
-- The ping role's config key, its bot_managed_roles provenance row and the
-- once-only payload flag the start-ping sweep claims were all spelled
-- game_night_*. The code now reads game_lobby_*, so each stored name has to be
-- carried across or it silently stops meaning anything.
--
-- The config row is the one that matters. Read back read-only on 2026-09-12:
--
--   config            | guild 1469...666 | game_night_ping_role_id = '0'
--   bot_managed_roles | guild 1469...666 | game_night_ping_role_id → role 1546...829 (created)
--   games_active_games  | 0 rows carry game_night_pinged
--   games_game_history  | 8 rows carry game_night_pinged
--
-- That '0' is an admin's "(none)", set 2026-09-11: the dial is none_means_off,
-- so the sweep resolves it to (True, None) and pings nobody. Left behind under
-- the old key, the new key would read as never set and the next lobby would
-- create a role and start pinging it again. OR REPLACE keeps the carried value
-- if a row under the new key somehow already exists — the stored decision wins
-- over anything written before this ran.
--
-- The payload flag is renamed rather than dropped: a lobby open across the
-- restart already had its line, and an unset flag would ping it a second time.
-- json_type keeps true as JSON true (json_extract would hand back 1). History
-- rows are renamed only so nothing stored still carries the old spelling.

UPDATE OR REPLACE config
   SET key = 'game_lobby_ping_role_id'
 WHERE key = 'game_night_ping_role_id';

UPDATE OR REPLACE bot_managed_roles
   SET role_key = 'game_lobby_ping_role_id'
 WHERE role_key = 'game_night_ping_role_id';

UPDATE games_active_games
   SET payload = json_set(
         json_remove(payload, '$.game_night_pinged'),
         '$.game_lobby_pinged',
         CASE json_type(payload, '$.game_night_pinged')
           WHEN 'true' THEN json('true')
           WHEN 'false' THEN json('false')
           ELSE json_extract(payload, '$.game_night_pinged')
         END)
 WHERE json_valid(payload)
   AND json_type(payload, '$.game_night_pinged') IS NOT NULL;

UPDATE games_game_history
   SET payload = json_set(
         json_remove(payload, '$.game_night_pinged'),
         '$.game_lobby_pinged',
         CASE json_type(payload, '$.game_night_pinged')
           WHEN 'true' THEN json('true')
           WHEN 'false' THEN json('false')
           ELSE json_extract(payload, '$.game_night_pinged')
         END)
 WHERE json_valid(payload)
   AND json_type(payload, '$.game_night_pinged') IS NOT NULL;
