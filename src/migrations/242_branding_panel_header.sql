-- Per-guild small header image for the game lobbies: a strip the footer's
-- height that tops a lobby card, which then closes with migration 238's
-- footer (Billy, 2026-09-27). NULL means "no header", and a lobby draws the
-- footer only beneath a header, so every lobby renders exactly what it does
-- today until an admin uploads one on the Branding page. Like the banner and
-- footer, the value is the public URL the dashboard's upload route wrote
-- under /static/panel-banners/, never the image itself.
--
-- Numbered 242, not 241: 241 is held by an unmerged branch. The runner keys
-- applied migrations by filename, so the order the two land in is harmless.
CREATE TABLE IF NOT EXISTS branding_config (
    guild_id    INTEGER PRIMARY KEY,
    accent_mode TEXT NOT NULL DEFAULT 'avatar',
    accent_hex  INTEGER NOT NULL DEFAULT -1
);

ALTER TABLE branding_config ADD COLUMN panel_header_url TEXT;
