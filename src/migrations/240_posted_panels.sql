-- Where each posted-once channel panel went, so it can be edited in place when
-- the code or the panel art changes (docs/plans/panel-refresh.md). Sticky
-- panels keep their own ids and are not listed here.
--
-- One row per (guild, panel, channel): posting a panel again into a channel
-- that already holds it moves it (a new copy, then the old one deleted)
-- rather than leaving two. A panel
-- key names a panel_registry spec; options_json is what the poster chose
-- (empty object for panels without options).
--
-- It absorbs ticket_panels (migration 023), which the ticket panel wrote and
-- nothing ever read. Its rows carry over, so a restart refreshes the ticket
-- panels already posted; the guild-0 rows are strays from before per-guild
-- config and are dropped.
CREATE TABLE IF NOT EXISTS posted_panels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    panel_key    TEXT    NOT NULL,
    channel_id   INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    options_json TEXT    NOT NULL DEFAULT '{}',
    posted_at    REAL    NOT NULL,
    UNIQUE (guild_id, panel_key, channel_id)
);

INSERT OR IGNORE INTO posted_panels (guild_id, panel_key, channel_id, message_id, posted_at)
SELECT guild_id, 'ticket-panel', channel_id, message_id, created_at
FROM ticket_panels
WHERE guild_id != 0;

DROP TABLE IF EXISTS ticket_panels;
