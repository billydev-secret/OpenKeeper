-- Per-guild footer image for the channel-resident hub panels: the strip that
-- closes the card the way migration 236's banner opens it (Billy, 2026-09-26).
-- NULL means "no footer", so every guild renders exactly what it does today —
-- the column ships dark until an admin uploads one on the Branding page. Like
-- the banner, the value is the public URL the dashboard's upload route wrote
-- under /static/panel-banners/, never the image itself.
--
-- Numbered 238, not 237: 237 is held by an unmerged branch. The runner keys
-- applied migrations by filename, so the order the two land in is harmless.
CREATE TABLE IF NOT EXISTS branding_config (
    guild_id    INTEGER PRIMARY KEY,
    accent_mode TEXT NOT NULL DEFAULT 'avatar',
    accent_hex  INTEGER NOT NULL DEFAULT -1
);

ALTER TABLE branding_config ADD COLUMN panel_footer_url TEXT;
