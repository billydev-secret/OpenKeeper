-- Per-guild banner image for the channel-resident hub panels (the Perk Shop
-- and DM Requests posters, and every later V2 hub that opts in). NULL means
-- "no banner", so every existing guild renders exactly what it does today —
-- the column ships dark until an admin uploads an image on the Branding page.
-- The value is the public URL the dashboard's upload route wrote under
-- /static/panel-banners/, not the image itself: a sticky panel reposts on
-- channel activity, and a URL is fetched by Discord's media proxy once
-- rather than re-uploaded on every repost.
--
-- Numbered 236, not 235: 235 is held by an unmerged branch. The runner keys
-- applied migrations by filename, so the order the two land in is harmless.
CREATE TABLE IF NOT EXISTS branding_config (
    guild_id    INTEGER PRIMARY KEY,
    accent_mode TEXT NOT NULL DEFAULT 'avatar',
    accent_hex  INTEGER NOT NULL DEFAULT -1
);

ALTER TABLE branding_config ADD COLUMN panel_banner_url TEXT;
