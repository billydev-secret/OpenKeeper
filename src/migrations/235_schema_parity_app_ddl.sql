-- Migration 235: declare the schema objects only application code was creating.
--
-- Production's schema has two authors. Most tables are created here, in
-- `src/migrations/`; a service module then repeats the same `CREATE TABLE IF
-- NOT EXISTS` in its own `ensure_*_schema()` so a fresh install is safe
-- whatever order things boot in. For 58 tables both authors say the same
-- thing and the second is a harmless no-op.
--
-- For twenty-one objects only the service module ever said it. They exist in
-- prod because the bot created them at boot, and they exist in no migration
-- at all — so the schema the test suite builds (`tests/db_template.migrated_db`,
-- which runs exactly these files) has never had them. Measured 2026-09-21
-- against the live database, read-only: 1 table and 20 indexes present in
-- prod and absent from the migrated schema, and zero column differences.
--
-- That gap is not cosmetic. Three guards walk the schema and take what they
-- find as the whole truth — `test_guild_purge_coverage.py`,
-- `test_privacy_register_coverage.py`, and now the guild-index contract — so
-- each has been reasoning about a schema no bot has ever run on. It also made
-- the standards audit's own count wrong: row 1.12 of
-- `docs/reviews/2026-09-14-standards-audits-status.md` reported 39 guild-scoped
-- tables missing a `guild_id`-leading index, but ten of those thirty-nine have
-- had one in production all along, created by `moderation.py`,
-- `health_service.py`, `dm_perms_service.py` and `wellness_service.py`.
--
-- **This migration changes none of the 21 objects it names.** Each was
-- verified present in the live database before it was written, and every
-- statement carries IF NOT EXISTS, so every one of them is a no-op against
-- prod: no table is created, no index is built, no row is touched.
--
-- That is not the same as doing nothing, and the difference is worth being
-- exact about. Applying any pending migration makes the runner open a write
-- transaction, take the WAL writer lock for its duration, and INSERT one row
-- into `schema_version` — this file included. It is the cost every migration
-- pays and it is measured in milliseconds, but a reader who was promised a
-- perfect no-op and then found a write would be right to distrust the rest of
-- this comment.
--
-- What it actually changes is the *test* schema, which from here on is a
-- superset of what a running bot has rather than a subset of it.
--
-- The definitions below are copied from the live database, which stores the
-- statement each service module issued. The service modules keep their copies:
-- removing them is a separate question from making migrations complete, and
-- 58 other tables already live with the duplication. Nor does keeping them
-- change who wins — `__main__.py:168` runs the migrations before the bot is
-- even constructed, so on a fresh database these twenty-one now come from
-- here and each service's own CREATE is the same no-op it already is for
-- every other table it declares.
--
-- `tests/test_schema_authority_contract.py` holds the line from here — a new
-- CREATE TABLE or CREATE INDEX in a service module now has to be declared in a
-- migration too, or the suite fails.
--
-- Note for whoever reads this next: prod also holds `incident_events` and
-- `message_velocity_baseline`, which are *not* declared here. Their creator was
-- deleted by `3fa9aa86` "Remove incident detection system", so no code makes
-- them any more and a fresh database should not have them. They are orphans in
-- prod holding 572 and 508 rows; `incident_events` is listed in
-- `docs/data_register.md` as preserved under Art 17(3)(e), so dropping it is a
-- data decision and not this migration's business.

CREATE TABLE IF NOT EXISTS dm_mode_roles (
    guild_id       INTEGER PRIMARY KEY,
    open_role_id   INTEGER NOT NULL DEFAULT 0,
    ask_role_id    INTEGER NOT NULL DEFAULT 0,
    closed_role_id INTEGER NOT NULL DEFAULT 0
);

-- services/moderation.py
CREATE INDEX IF NOT EXISTS idx_jails_active ON jails (guild_id, status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_tickets_active ON tickets (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_guild ON audit_log (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transcripts_record ON transcripts (record_type, record_id);
CREATE INDEX IF NOT EXISTS idx_policy_tickets_guild ON policy_tickets (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_policies_guild ON policies (guild_id);

-- services/message_store.py
CREATE INDEX IF NOT EXISTS idx_messages_guild_ts ON messages (guild_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages (guild_id, author_id);
CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages (guild_id, channel_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_sentiment ON messages (guild_id, sentiment);
CREATE INDEX IF NOT EXISTS idx_messages_emotion ON messages (guild_id, emotion);
CREATE INDEX IF NOT EXISTS idx_mentions_user ON message_mentions (user_id);

-- services/health_service.py
CREATE INDEX IF NOT EXISTS idx_sentiment_guild_ts ON message_sentiment (guild_id, computed_at);
CREATE INDEX IF NOT EXISTS idx_sentiment_guild_channel ON message_sentiment (guild_id, channel_id);

-- services/dm_perms_service.py
CREATE INDEX IF NOT EXISTS idx_dm_audit_log_guild ON dm_audit_log(guild_id);

-- services/wellness_service.py
CREATE INDEX IF NOT EXISTS idx_wellness_caps_user ON wellness_caps (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_wellness_blackouts_user ON wellness_blackouts (guild_id, user_id);

-- services/auto_delete_service.py
CREATE INDEX IF NOT EXISTS idx_auto_delete_messages_due ON auto_delete_messages (guild_id, channel_id, created_at);

-- services/confessions_service.py
CREATE INDEX IF NOT EXISTS idx_confession_threads_created_at ON confession_threads(created_at);
