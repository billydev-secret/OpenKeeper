-- 221_survivor_hosts.sql
-- Survivor hosts (docs/survivor_spec.md §3.4, 2026-09-12): members an admin
-- names on the Survivor dashboard panel. A host gets that panel (every
-- Survivor route admits admins OR hosts — web_server/deps.py
-- require_survivor_host) and the host DMs: the Saturday pick status, the
-- Monday Reckoning heads-up, and — while at least one host exists — the
-- Tuesday Reckoning itself, which the host posts in place of the bot.
--
-- Guild-scoped, not a key in the season config: a host has to be able to
-- create next season's season while no season is live, and the season config
-- only exists once one is.
--
-- Ships dark: an empty table is exactly today's behaviour — admin-only panel,
-- no host DMs, the bot posts the Reckoning.
--
-- Per-user data: PURGED on erasure (privacy_service.purge_user_data; row in
-- docs/data_register.md). `user_id` and `added_by` are both in
-- SUBJECT_ID_COLUMNS, so the access export sees the table unchanged.

CREATE TABLE IF NOT EXISTS survivor_hosts (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_by INTEGER NOT NULL,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);
