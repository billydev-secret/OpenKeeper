-- Per-guild switch: draw each channel hub's title into the hub banner instead
-- of the panel's text heading (Billy, 2026-09-26). 0 = off, so every guild
-- renders exactly what it does today until an admin ticks it on the Branding
-- page — and the art has to leave a clear band at its foot for the title, so
-- it can't default on. The drawn banners are files beside the upload, not rows.
CREATE TABLE IF NOT EXISTS branding_config (
    guild_id    INTEGER PRIMARY KEY,
    accent_mode TEXT NOT NULL DEFAULT 'avatar',
    accent_hex  INTEGER NOT NULL DEFAULT -1
);

ALTER TABLE branding_config ADD COLUMN panel_banner_title INTEGER NOT NULL DEFAULT 0;
