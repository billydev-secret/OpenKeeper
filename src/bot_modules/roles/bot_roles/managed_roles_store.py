"""Where every bot-managed role's id is stored, read the way its feature reads it.

Lifted out of the Bot-Managed Roles route (``web_server/routes/bot_roles.py``)
on 2026-09-26, when the Server Features panel's Roles & Pings card became the
second thing that needs to know which roles the bot owns: it must never offer
one of them to a member (``server_features.logic.EXCLUDED``), and a copy of
this read would drift the first time a family moved its id somewhere new.

The roles live in four families of storage — the ``config`` KV, the
DM-mode table, the active Survivor season, and Wellness config — and each is
read here exactly as the feature that owns it reads it. Anything else and a
surface would report a value no feature ever sees.
"""

from __future__ import annotations

import sqlite3

from bot_modules.core.db_utils import get_config_value
from bot_modules.core.role_provision import role_dial_opted_out
from bot_modules.shared import feature_roles as fr


def read_stored_ids(
    conn: sqlite3.Connection, guild_id: int
) -> dict[str, tuple[int, bool, bool]]:
    """``key -> (stored id, the row is this guild's own, opted out)`` for every
    entry in ``feature_roles.MANAGED_ROLES``. One database pass for all of them.

    ``opted_out`` is ``honours_none``, not ``none_means_off``: a create-on-offer
    dial's panel writes "0" on every unrelated save, and offering the role in
    onboarding is what makes it — so a 0 there is not a decision. Every surface
    reading these dials has to agree on that, which is why they share this.
    """
    from bot_modules.dm.perms.service import get_dm_mode_role_ids_with_conn  # noqa: PLC0415
    from bot_modules.survivor.service import get_active_season  # noqa: PLC0415
    from bot_modules.wellness.service import get_wellness_config  # noqa: PLC0415

    out: dict[str, tuple[int, bool, bool]] = {}
    for entry in fr.MANAGED_ROLES:
        if entry.source != fr.SOURCE_CONFIG:
            continue
        raw = get_config_value(
            conn, entry.key, "0", guild_id,
            allow_legacy_fallback=entry.legacy_fallback,
        )
        try:
            stored = int(raw or "0")
        except ValueError:
            stored = 0
        own = conn.execute(
            "SELECT 1 FROM config WHERE guild_id = ? AND key = ?",
            (guild_id, entry.key),
        ).fetchone() is not None
        opted_out = entry.honours_none and role_dial_opted_out(
            conn, entry.key, guild_id,
            allow_legacy_fallback=entry.legacy_fallback,
        )
        out[entry.key] = (stored, own, opted_out)

    dm_ids = get_dm_mode_role_ids_with_conn(conn, guild_id)
    for mode, rid in dm_ids.items():
        out[f"dm_mode_{mode}_role_id"] = (int(rid or 0), True, False)

    season = get_active_season(conn, guild_id)
    season_cfg = (season or {}).get("config") or {}
    for entry in fr.MANAGED_ROLES:
        if entry.source == fr.SOURCE_SURVIVOR:
            out[entry.key] = (int(season_cfg.get(entry.key) or 0), True, False)

    cfg = get_wellness_config(conn, guild_id)
    out[fr.WELLNESS_ROLE.key] = (int(getattr(cfg, "role_id", 0) or 0), True, False)
    return out
