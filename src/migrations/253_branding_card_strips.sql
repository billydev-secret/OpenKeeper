-- Per-guild thin header and footer strips for the public cards that are
-- neither hubs nor lobbies: economy and casino posts, the staff-channel
-- review and audit cards (Ben, 2026-09-30: "a third
-- thinner header with no title for use in the embeds"). Nothing is drawn
-- onto either. NULL means "not set", and a card draws the footer only
-- beneath a header, so every card renders exactly what it does today until
-- an admin uploads a card header on the Branding page. Like the other panel
-- art, each value is the public URL the dashboard's upload route wrote under
-- /static/panel-banners/, never the image itself.
--
-- Numbered 253: 251 is chat revive's and 252 is held by an unmerged branch. The runner keys
-- applied migrations by filename, so the order they land in is harmless.
CREATE TABLE IF NOT EXISTS branding_config (
    guild_id    INTEGER PRIMARY KEY,
    accent_mode TEXT NOT NULL DEFAULT 'avatar',
    accent_hex  INTEGER NOT NULL DEFAULT -1
);

ALTER TABLE branding_config ADD COLUMN panel_card_header_url TEXT;
ALTER TABLE branding_config ADD COLUMN panel_card_footer_url TEXT;
