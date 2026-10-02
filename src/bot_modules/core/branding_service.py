"""Per-guild branding config — the embed accent color and product names.

Bot identity (per-guild nickname + avatar) is handled directly against
Discord by the web route ``POST /config/bot-identity`` and lives on the
Discord member, not here. This table stores the accent-color preference
used by the shared resolver in ``bot_modules.core.branding``, plus the
guild-facing names of the two named products — the casino and the AI
assistant — and the optional banner and footer images that frame the
guild's channel-resident hub panels (``panel_banner_url`` /
``panel_footer_url``; see ``core.layout.hub_banner``) and the small header
that tops a game lobby (``panel_header_url``; ``core.layout.lobby_art``),
the thin untitled strips that frame the other public cards
(``panel_card_header_url`` / ``panel_card_footer_url``;
``core.layout.card_strips``), with the switch that
writes each hub's title into the banner (``panel_banner_title``; see
``panel_title_service``). Those names default to the values the home server has always
used; every other guild can rename them from the Branding panel.

This module is the single home for those defaults: call sites that have no
guild handy import ``DEFAULT_CASINO_NAME`` / ``DEFAULT_ASSISTANT_NAME``
from here rather than repeating the literal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from bot_modules.core.db_utils import open_db

# Discord blurple — the safe default accent when nothing else resolves.
DEFAULT_ACCENT = 0x5865F2

ACCENT_MODE_AVATAR = "avatar"
ACCENT_MODE_CUSTOM = "custom"
_VALID_MODES = frozenset({ACCENT_MODE_AVATAR, ACCENT_MODE_CUSTOM})

# accent_hex sentinel meaning "no custom color set".
ACCENT_HEX_UNSET = -1

# Where the dashboard's upload route writes the hub art, and the URL path it
# is served under — shared with ``panel_title_service``, which draws the
# titled banners beside the uploads.
PANEL_ART_DIR = Path(__file__).resolve().parents[2] / "web_server" / "static" / "panel-banners"
PANEL_ART_URL_PATH = "/static/panel-banners/"

# Built-in product names. A guild that never sets its own reads these.
DEFAULT_CASINO_NAME = "Golden Meadow"
DEFAULT_ASSISTANT_NAME = "Billy-bot"

# Names are rendered into embed titles and slash-command replies; keep them
# short enough that a title can never blow Discord's 256-char limit.
MAX_NAME_LEN = 60


@dataclass
class BrandingConfig:
    guild_id: int
    accent_mode: str = ACCENT_MODE_AVATAR
    accent_hex: int = ACCENT_HEX_UNSET  # -1 = unset; else 0x000000..0xFFFFFF
    # "" (stored NULL) = fall back to the module default above.
    casino_name: str = ""
    assistant_name: str = ""
    # "" (stored NULL) = no banner — the hub panels render exactly as before.
    # A public https URL the dashboard's upload route wrote (never admin-typed).
    panel_banner_url: str = ""
    # The same for the strip that closes the hub panels (Billy, 2026-09-26).
    panel_footer_url: str = ""
    # The small strip that tops a game lobby, the footer's height, whose
    # presence also brings the footer onto the lobby (Billy, 2026-09-27).
    panel_header_url: str = ""
    # The thin, untitled strips that top and close the public cards that are
    # neither hubs nor lobbies; the footer draws only beneath a header
    # (Ben, 2026-09-30).
    panel_card_header_url: str = ""
    panel_card_footer_url: str = ""
    # Draw each hub's title into the banner instead of its text heading
    # (Billy, 2026-09-26). Off by default: the art has to leave room for it.
    panel_banner_title: bool = False

    def normalized_mode(self) -> str:
        return self.accent_mode if self.accent_mode in _VALID_MODES else ACCENT_MODE_AVATAR

    def has_custom_color(self) -> bool:
        return 0 <= self.accent_hex <= 0xFFFFFF

    def resolved_casino_name(self) -> str:
        return (self.casino_name or "").strip() or DEFAULT_CASINO_NAME

    def resolved_assistant_name(self) -> str:
        return (self.assistant_name or "").strip() or DEFAULT_ASSISTANT_NAME


def init_db(db_path: Path) -> None:
    with open_db(db_path) as conn:
        _create_tables(conn)


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create the table on a fresh DB — kept in sync with migrations 115, 236, 238, 239, 242 and 253.

    The name columns are added with ALTER so a database whose table predates
    migration 115 (created lazily by an older build of this module) still ends
    up with the same shape as a migrated one.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS branding_config (
            guild_id       INTEGER PRIMARY KEY,
            accent_mode    TEXT NOT NULL DEFAULT 'avatar',
            accent_hex     INTEGER NOT NULL DEFAULT -1,
            casino_name    TEXT,
            assistant_name TEXT,
            panel_banner_url TEXT,
            panel_footer_url TEXT,
            panel_header_url TEXT,
            panel_card_header_url TEXT,
            panel_card_footer_url TEXT,
            panel_banner_title INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    for column in (
        "casino_name",
        "assistant_name",
        "panel_banner_url",
        "panel_footer_url",
        "panel_header_url",
        "panel_card_header_url",
        "panel_card_footer_url",
    ):
        try:
            conn.execute(f"ALTER TABLE branding_config ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass  # already present
    try:
        conn.execute(
            "ALTER TABLE branding_config ADD COLUMN "
            "panel_banner_title INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # already present


def _row_to_cfg(row) -> BrandingConfig:
    return BrandingConfig(
        guild_id=row["guild_id"],
        accent_mode=row["accent_mode"],
        accent_hex=row["accent_hex"],
        casino_name=row["casino_name"] or "",
        assistant_name=row["assistant_name"] or "",
        panel_banner_url=row["panel_banner_url"] or "",
        panel_footer_url=row["panel_footer_url"] or "",
        panel_header_url=row["panel_header_url"] or "",
        panel_card_header_url=row["panel_card_header_url"] or "",
        panel_card_footer_url=row["panel_card_footer_url"] or "",
        panel_banner_title=bool(row["panel_banner_title"]),
    )


def get_branding(db_path: Path, guild_id: int) -> BrandingConfig:
    with open_db(db_path) as conn:
        return get_branding_conn(conn, guild_id)


def get_branding_conn(conn, guild_id: int) -> BrandingConfig:
    """Read branding config on an open connection.

    Defensive against a missing table (returns defaults) so a bot that
    posts an embed before the table has been created never crashes.
    """
    try:
        row = conn.execute(
            "SELECT guild_id, accent_mode, accent_hex, casino_name, assistant_name, "
            "panel_banner_url, panel_footer_url, panel_header_url, "
            "panel_card_header_url, panel_card_footer_url, panel_banner_title "
            "FROM branding_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return BrandingConfig(guild_id=guild_id)
    if row is None:
        return BrandingConfig(guild_id=guild_id)
    return _row_to_cfg(row)


def upsert_branding(db_path: Path, cfg: BrandingConfig) -> None:
    with open_db(db_path) as conn:
        _create_tables(conn)
        conn.execute(
            """
            INSERT INTO branding_config
                (guild_id, accent_mode, accent_hex, casino_name, assistant_name,
                 panel_banner_url, panel_footer_url, panel_header_url,
                 panel_card_header_url, panel_card_footer_url,
                 panel_banner_title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                accent_mode      = excluded.accent_mode,
                accent_hex       = excluded.accent_hex,
                casino_name      = excluded.casino_name,
                assistant_name   = excluded.assistant_name,
                panel_banner_url = excluded.panel_banner_url,
                panel_footer_url = excluded.panel_footer_url,
                panel_header_url = excluded.panel_header_url,
                panel_card_header_url = excluded.panel_card_header_url,
                panel_card_footer_url = excluded.panel_card_footer_url,
                panel_banner_title = excluded.panel_banner_title
            """,
            (
                cfg.guild_id,
                cfg.normalized_mode(),
                cfg.accent_hex,
                _stored_name(cfg.casino_name),
                _stored_name(cfg.assistant_name),
                (cfg.panel_banner_url or "").strip() or None,
                (cfg.panel_footer_url or "").strip() or None,
                (cfg.panel_header_url or "").strip() or None,
                (cfg.panel_card_header_url or "").strip() or None,
                (cfg.panel_card_footer_url or "").strip() or None,
                1 if cfg.panel_banner_title else 0,
            ),
        )


def _stored_name(raw: str | None) -> str | None:
    """Trim a name for storage; blank (or default-equal) becomes NULL."""
    name = (raw or "").strip()[:MAX_NAME_LEN]
    return name or None


# ── name resolvers ─────────────────────────────────────────────────────
#
# Thin wrappers so a call site can ask for "this guild's casino name" without
# knowing about the config row or repeating the default literal.


def resolve_casino_name(db_path: Path, guild_id: int) -> str:
    return get_branding(db_path, guild_id).resolved_casino_name()


def resolve_casino_name_conn(conn, guild_id: int) -> str:
    return get_branding_conn(conn, guild_id).resolved_casino_name()


def resolve_assistant_name(db_path: Path, guild_id: int) -> str:
    return get_branding(db_path, guild_id).resolved_assistant_name()


def resolve_assistant_name_conn(conn, guild_id: int) -> str:
    return get_branding_conn(conn, guild_id).resolved_assistant_name()


class PanelArt(NamedTuple):
    """The images that frame a guild's hub panels — each ``None`` when unset.

    ``title_in_banner`` asks for each hub's title drawn into the banner;
    ``panel_title_service.with_title`` swaps in the drawn banner and clears
    the flag whenever it can't, so a builder that sees it set can drop its
    text heading. ``header`` is the game lobbies' small strip, not a hub's;
    ``panel_title_service.with_lobby_title`` sets ``title_in_header`` once it
    has written the game's name into it. ``card_header`` and ``card_footer``
    are the thin untitled strips for the public cards that are neither
    (``core.layout.card_strips``); nothing is ever drawn onto them.
    """

    banner: str | None
    footer: str | None
    title_in_banner: bool = False
    header: str | None = None
    title_in_header: bool = False
    card_header: str | None = None
    card_footer: str | None = None


def resolve_panel_art_conn(conn, guild_id: int) -> PanelArt:
    """The guild's hub-panel banner and footer URLs, ``None`` where unset.

    Only the channel-resident hub panels read this (``core.layout.hub_banner``
    and ``hub_footer`` are the one place each becomes a component); ephemeral
    cards never do. One read for both, since every hub draws both. The game
    lobbies read it too, for ``header`` and the footer beneath it
    (``core.layout.lobby_art``), and the other public cards for the card
    strips (``core.layout.card_strips``).
    """
    cfg = get_branding_conn(conn, guild_id)
    return PanelArt(
        banner=cfg.panel_banner_url.strip() or None,
        footer=cfg.panel_footer_url.strip() or None,
        title_in_banner=cfg.panel_banner_title,
        header=cfg.panel_header_url.strip() or None,
        card_header=cfg.panel_card_header_url.strip() or None,
        card_footer=cfg.panel_card_footer_url.strip() or None,
    )
