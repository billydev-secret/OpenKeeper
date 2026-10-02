"""Survivor season/config/roster service — stage 1 of docs/plans/survivor.md.

The season's rules all live in one JSON config column (spec §5), read through
:func:`get_config` which merges over :data:`DEFAULT_CONFIG` — so a season
created today keeps its rules even if the defaults move next year, and every
key is guaranteed to have a reader (the staged-config lesson: a config key
whose reader never ships is a silent no-op).

Pure DB logic over a caller-owned connection, no Discord imports — the cog,
routes, and tasks are glue on top of this. Game logic (locks, settle, the
gauntlet) arrives in later stages as ``survivor_logic.py``; this module is the
part the dashboard panel needs to exist.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

# Every rule in spec §1 is a setting, not code (spec §3/§5). Values here are
# the season-one decisions of 2026-08-17: free entry, a 10,000 house seed with
# 20% carved off for the Ghost Streak side-pot.
DEFAULT_CONFIG: dict[str, object] = {
    "buyin_coins": 0,
    "pot_seed": 10000,
    "ghost_pot_pct": 20,
    "gauntlet_fee_per_week": 50,
    # A small weekly faucet (added 2026-08-18): every player whose picks all
    # won that week collects this at the Reckoning. 0 = off. ~15-20 winners
    # × 25 ≈ 400/week ≈ 60/day — about 1% of current net supply growth.
    "weekly_win_coins": 25,
    "strikes": 1,
    "tie_rule": "loss",             # loss | survive
    "late_entry": "gauntlet",       # gauntlet | closed | ghost_only
    "missed_pick": "auto_assign",   # auto_assign | eliminate
    "max_auto_assigns": 3,
    "double_pick_start_week": 14,   # 0 = never
    # Wipeout/annul (spec §1.6, stage 6b, 2026-09-11): a week that kills every
    # living player is annulled through this week — nobody dies, burned teams
    # stay burned — and from the week after, it ends the season in an equal
    # split. 0 = never annul (always split); there is no "off", because a
    # season with nobody alive has to resolve somehow. Read by
    # survivor/settle.py's wipeout sweep and survivor/payout.py — the rule and
    # the dial shipped together, which is why this key exists again.
    "wipeout_annul_through_week": 13,
    # No double_pick_min_alive / accord_max_alive here on purpose: the Tier 2
    # rules that would enforce them (§1.5 escalation, §1.6's Accord) are
    # unbuilt, and a stored dial nothing reads is a silent no-op — they come
    # back with the code that honours them, exactly as the wipeout dial has.
    "ghost_streak": True,
    "slate_hour": 9,                # Wed, guild-local
    "lastcall_hour": 18,            # Sat
    "reckoning_hour": 9,            # Tue
    # Wiring, not rules: where the season lives and which roles the bot
    # manages. 0 = unset; role/channel steps degrade to skipped-and-logged
    # rather than failing the game (spec §3.3).
    "channel_id": 0,
    "role_survivor_id": 0,
    "role_ghost_id": 0,
    "role_sole_survivor_id": 0,
    # The pinned §2.2 announcement — id AND channel, so the entrant counter
    # can be refreshed and a repost can retire the previous pin even if the
    # Survivor channel has since been re-pointed. Set by the post route.
    "announcement_message_id": 0,
    "announcement_channel_id": 0,
    # Weekly-task state, not rules: which week each Tuesday/Wednesday/
    # Saturday task last ran for, so a restart never double-posts and a
    # missed hour catches up. Stored here because the season's config JSON
    # is the one per-season store and every key must have a reader.
    "last_reckoned_week": 0,
    "last_slate_week": 0,
    "last_lastcall_week": 0,
    # The Monday host heads-up (spec §3.4) — same once-per-week shape.
    "last_host_preview_week": 0,
    "last_reckoned_at": 0,
    # Which weeks a wipeout annulled (spec §1.6, stage 6b). State, not a rule:
    # the annul is a *decision* taken once, at the sweep that found the week
    # had killed everyone, and recorded here so recompute_player can exclude
    # it forever after. Deriving it instead would let a later correction
    # dissolve an annul the Reckoning had already announced — and retroactively
    # bury the whole roster the bot had just told everyone survived.
    "annulled_weeks": [],
}

_ENUM_KEYS: dict[str, tuple[str, ...]] = {
    "tie_rule": ("loss", "survive"),
    "late_entry": ("gauntlet", "closed", "ghost_only"),
    "missed_pick": ("auto_assign", "eliminate"),
}

# Inclusive ranges for the integer dials. Snowflake keys are bounded only
# below — Discord ids don't have a meaningful ceiling worth encoding.
_INT_RANGES: dict[str, tuple[int, int]] = {
    "buyin_coins": (0, 1_000_000),
    "pot_seed": (0, 1_000_000),
    "ghost_pot_pct": (0, 100),
    "gauntlet_fee_per_week": (0, 10_000),
    "weekly_win_coins": (0, 10_000),
    "strikes": (0, 2),
    "max_auto_assigns": (0, 18),
    "double_pick_start_week": (0, 18),
    "wipeout_annul_through_week": (0, 18),
    "slate_hour": (0, 23),
    "lastcall_hour": (0, 23),
    "reckoning_hour": (0, 23),
    "channel_id": (0, 1 << 63),
    "role_survivor_id": (0, 1 << 63),
    "role_ghost_id": (0, 1 << 63),
    "role_sole_survivor_id": (0, 1 << 63),
    "announcement_message_id": (0, 1 << 63),
    "announcement_channel_id": (0, 1 << 63),
    "last_reckoned_week": (0, 30),
    "last_slate_week": (0, 30),
    "last_lastcall_week": (0, 30),
    "last_host_preview_week": (0, 30),
    "last_reckoned_at": (0, 1 << 63),
}

_BOOL_KEYS = frozenset({"ghost_streak"})

# Keys holding a sorted list of week numbers. Stored as a JSON array; the
# validator normalises (dedupes and sorts) so the order can never carry
# meaning that a re-write would lose.
_WEEK_LIST_KEYS = frozenset({"annulled_weeks"})

SEASON_STATUSES = ("enrolling", "active", "complete")


class SeasonError(ValueError):
    """A season lifecycle rule was violated (message is user-presentable)."""


# ── seasons ────────────────────────────────────────────────────────────


def create_season(
    conn: sqlite3.Connection,
    guild_id: int,
    name: str,
    season_year: int,
    overrides: dict | None = None,
) -> int:
    """Create a season in ``enrolling`` state and return its id.

    One live (non-complete) season per guild (spec §6.12) — a second create
    raises :class:`SeasonError`. ``overrides`` are validated config keys
    stored sparsely; everything else reads through the defaults.
    """
    name = name.strip()
    if not name:
        raise SeasonError("Season name can't be empty.")
    if not 2020 <= season_year <= 2100:
        raise SeasonError("Season year looks wrong.")
    if get_active_season(conn, guild_id) is not None:
        raise SeasonError("This server already has a live season — end it first.")
    config = validate_config(overrides or {})
    try:
        cur = conn.execute(
            "INSERT INTO survivor_seasons (guild_id, name, season_year, status, config) "
            "VALUES (?, ?, ?, 'enrolling', ?)",
            (guild_id, name, season_year, json.dumps(config)),
        )
    except sqlite3.IntegrityError as exc:
        # The schema backstop (partial unique index on live seasons): two
        # concurrent creates both pass the check above; the loser lands here.
        raise SeasonError(
            "This server already has a live season — end it first."
        ) from exc
    return int(cur.lastrowid or 0)


def get_season(conn: sqlite3.Connection, season_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, guild_id, name, season_year, status, config "
        "FROM survivor_seasons WHERE id = ?",
        (season_id,),
    ).fetchone()
    return _season_dict(row) if row else None


def get_active_season(conn: sqlite3.Connection, guild_id: int) -> dict | None:
    """The guild's live season — ``enrolling`` or ``active`` — or None."""
    row = conn.execute(
        "SELECT id, guild_id, name, season_year, status, config "
        "FROM survivor_seasons WHERE guild_id = ? AND status != 'complete' "
        "ORDER BY id DESC LIMIT 1",
        (guild_id,),
    ).fetchone()
    return _season_dict(row) if row else None



def panel_ids(conn: sqlite3.Connection, guild_id: int) -> tuple[int, int]:
    """The live channel panel's (channel_id, message_id) for the sticky
    machinery — (0, 0) when there is no live season or no posted panel."""
    season = get_active_season(conn, guild_id)
    if season is None:
        return 0, 0
    cfg = season["config"]
    return (
        int(cfg.get("announcement_channel_id") or 0),
        int(cfg.get("announcement_message_id") or 0),
    )


def set_panel_ids(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, message_id: int
) -> bool:
    """Persist the panel's location after a sticky repost (or clear it with
    (0, 0) when its channel is deleted). No live season → no-op False, so a
    late restick can't resurrect ids onto an archived season."""
    season = get_active_season(conn, guild_id)
    if season is None:
        return False
    update_config(conn, season["id"], {
        "announcement_channel_id": channel_id,
        "announcement_message_id": message_id,
    })
    return True


def list_seasons(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, guild_id, name, season_year, status, config "
        "FROM survivor_seasons WHERE guild_id = ? ORDER BY id DESC",
        (guild_id,),
    ).fetchall()
    return [_season_dict(r) for r in rows]


def set_season_status(conn: sqlite3.Connection, season_id: int, status: str) -> None:
    """Move a season along enrolling → active → complete (no resurrection).

    Completing is one-shot: ``complete`` is the season-end payout's "already
    paid" flag, so a second claim on it is refused rather than a no-op.
    """
    if status not in SEASON_STATUSES:
        raise SeasonError(f"Unknown season status {status!r}.")
    season = get_season(conn, season_id)
    if season is None:
        raise SeasonError("No such season.")
    current = season["status"]
    if status == "complete" and current == "complete":
        raise SeasonError("This season is already complete.")
    order = {s: i for i, s in enumerate(SEASON_STATUSES)}
    if order[status] < order[current]:
        raise SeasonError(f"A {current} season can't go back to {status}.")
    # Compare-and-set on the status just read: the settle sweep and the
    # dashboard's correction run on separate connections and can both read
    # 'active' before either commits. Only one of them may end the season.
    cur = conn.execute(
        "UPDATE survivor_seasons SET status = ? WHERE id = ? AND status = ?",
        (status, season_id, current),
    )
    if cur.rowcount == 0:
        raise SeasonError("The season changed while this was saving. Try again.")


def end_season(conn: sqlite3.Connection, season_id: int) -> None:
    """Archive the season. History stays queryable (spec §6.12)."""
    set_season_status(conn, season_id, "complete")


REFUND_KIND = "survivor_refund"
# Pot payouts (logic.KIND_PAYOUT / KIND_GHOST_PAYOUT; literals here because
# logic imports this module). A season with any of these rows has paid out.
_PAYOUT_KINDS = ("survivor_payout", "survivor_ghost_payout")
_REFUNDABLE_KINDS = ("survivor_buyin", "survivor_gauntlet_fee")


def cancel_season(conn: sqlite3.Connection, season_id: int) -> dict[str, int]:
    """End a season with no winner and refund every stake (survivor-181).

    Credits each of the season's buy-in and Gauntlet-fee debits back as a
    ``survivor_refund`` carrying ``{"refund_of": <ledger id>}``, exactly the
    amount taken (no booster), then archives the season. Idempotent: a debit
    that already has a refund row is skipped, so a second call refunds
    nothing. Prizes already paid (weekly wins) are not clawed back. A season
    that is already complete, or whose pots have paid out, is refused with a
    :class:`SeasonError`: refunding stakes the winners already collected on
    would mint coins. Returns
    ``{"refunds": n, "coins": total}``. Season-scoped via ``json_extract``,
    never LIKE (``"season_id": 1`` is a substring of ``12``).
    """
    from bot_modules.economy.service import apply_credit  # noqa: PLC0415

    season = get_season(conn, season_id)
    if season is None:
        raise SeasonError("No such season.")
    if season["status"] == "complete":
        raise SeasonError("This season is already complete; nothing to refund.")
    if conn.execute(
        "SELECT 1 FROM econ_ledger WHERE guild_id = ? AND kind IN (?, ?) "
        "AND json_extract(CASE WHEN json_valid(meta) THEN meta ELSE '{}' END, "
        "'$.season_id') = ? LIMIT 1",
        (season["guild_id"], *_PAYOUT_KINDS, season_id),
    ).fetchone():
        raise SeasonError("This season has already paid out; it can't be refunded.")
    meta = "CASE WHEN json_valid(meta) THEN meta ELSE '{}' END"
    debits = conn.execute(
        f"SELECT id, guild_id, user_id, -amount AS amount FROM econ_ledger "
        f"WHERE guild_id = ? AND amount < 0 AND kind IN (?, ?) "
        f"AND COALESCE(json_extract({meta}, '$.season_id'), 0) = ? "
        f"AND id NOT IN (SELECT json_extract({meta}, '$.refund_of') "
        f"FROM econ_ledger WHERE kind = ? "
        f"AND json_extract({meta}, '$.refund_of') IS NOT NULL) "
        f"ORDER BY id",
        (season["guild_id"], *_REFUNDABLE_KINDS, season_id, REFUND_KIND),
    ).fetchall()
    coins = 0
    for d in debits:
        apply_credit(
            conn, int(d["guild_id"]), int(d["user_id"]), int(d["amount"]),
            REFUND_KIND,
            meta={"season_id": season_id, "refund_of": int(d["id"])},
        )
        coins += int(d["amount"])
    set_season_status(conn, season_id, "complete")
    return {"refunds": len(debits), "coins": coins}


def _season_dict(row: sqlite3.Row) -> dict:
    try:
        stored = json.loads(row["config"] or "{}")
    except json.JSONDecodeError:
        stored = {}
    return {
        "id": int(row["id"]),
        "guild_id": int(row["guild_id"]),
        "name": row["name"],
        "season_year": int(row["season_year"]),
        "status": row["status"],
        "config": get_config(stored),
    }


# ── config ─────────────────────────────────────────────────────────────


def get_config(stored: dict) -> dict:
    """Merge a season's sparse stored config over the defaults."""
    merged = {
        # List defaults are copied, not shared: a shallow dict() would hand
        # every season the same ``annulled_weeks`` object, and one in-place
        # append would annul a week across every live season at once.
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_CONFIG.items()
    }
    for key, value in stored.items():
        if key in merged:
            merged[key] = value
    return merged


def validate_config(updates: dict) -> dict:
    """Validate config ``updates``; returns the cleaned dict or raises.

    Unknown keys are rejected outright — every stored key must have a reader,
    or it becomes a silent no-op that outlives its branch.
    """
    cleaned: dict[str, object] = {}
    for key, value in updates.items():
        if key not in DEFAULT_CONFIG:
            raise SeasonError(f"Unknown setting {key!r}.")
        if key in _ENUM_KEYS:
            if value not in _ENUM_KEYS[key]:
                allowed = ", ".join(_ENUM_KEYS[key])
                raise SeasonError(f"{key} must be one of: {allowed}.")
            cleaned[key] = value
        elif key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise SeasonError(f"{key} must be true or false.")
            cleaned[key] = value
        elif key in _WEEK_LIST_KEYS:
            if not isinstance(value, (list, tuple)):
                raise SeasonError(f"{key} must be a list of week numbers.")
            weeks = set()
            for week in value:
                if isinstance(week, bool) or not isinstance(week, int):
                    raise SeasonError(f"{key} must hold whole week numbers.")
                if not 1 <= week <= 30:
                    raise SeasonError(f"{key} weeks must be between 1 and 30.")
                weeks.add(week)
            cleaned[key] = sorted(weeks)
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SeasonError(f"{key} must be a whole number.")
            lo, hi = _INT_RANGES[key]
            if not lo <= value <= hi:
                raise SeasonError(f"{key} must be between {lo} and {hi}.")
            cleaned[key] = value
    return cleaned


def update_config(conn: sqlite3.Connection, season_id: int, updates: dict) -> dict:
    """Apply validated ``updates`` to a season's stored config; returns the
    full merged config after the write."""
    season = get_season(conn, season_id)
    if season is None:
        raise SeasonError("No such season.")
    cleaned = validate_config(updates)
    row = conn.execute(
        "SELECT config FROM survivor_seasons WHERE id = ?", (season_id,)
    ).fetchone()
    try:
        stored = json.loads(row["config"] or "{}")
    except json.JSONDecodeError:
        stored = {}
    stored.update(cleaned)
    conn.execute(
        "UPDATE survivor_seasons SET config = ? WHERE id = ?",
        (json.dumps(stored), season_id),
    )
    return get_config(stored)


# ── roster ─────────────────────────────────────────────────────────────


def list_players(conn: sqlite3.Connection, season_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT user_id, status, strikes_used, eliminated_week, joined_at "
        "FROM survivor_players WHERE season_id = ? "
        "ORDER BY status, joined_at",
        (season_id,),
    ).fetchall()
    return [
        {
            "user_id": int(r["user_id"]),
            "status": r["status"],
            "strikes_used": int(r["strikes_used"]),
            "eliminated_week": r["eliminated_week"],
            "joined_at": r["joined_at"],
        }
        for r in rows
    ]


def add_player(
    conn: sqlite3.Connection, season: dict, user_id: int, *, joined_at: float
) -> bool:
    """Enroll a member; False if they already have an entry (one per person,
    spec §1.1). Takes the season dict — guild_id is denormalized onto the
    row so the access export's standard guild scoping covers it."""
    iso = datetime.fromtimestamp(joined_at, timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT OR IGNORE INTO survivor_players "
        "(season_id, guild_id, user_id, joined_at) VALUES (?, ?, ?, ?)",
        (season["id"], season["guild_id"], user_id, iso),
    )
    return (cur.rowcount or 0) > 0


def eliminate_player(
    conn: sqlite3.Connection,
    season_id: int,
    user_id: int,
    week: int,
    *,
    source: str = "admin",
) -> bool:
    """Elimination: alive → ghost at ``week``. False if not alive. ``source``
    records why ('admin' | 'cap' | 'left'); non-'picks' sources survive the
    settle engine's recomputation."""
    cur = conn.execute(
        "UPDATE survivor_players SET status = 'ghost', eliminated_week = ?, "
        "elimination_source = ? "
        "WHERE season_id = ? AND user_id = ? AND status = 'alive'",
        (week, source, season_id, user_id),
    )
    return (cur.rowcount or 0) > 0


def revive_player(conn: sqlite3.Connection, season_id: int, user_id: int) -> bool:
    """Admin revival: ghost → alive, elimination week + source cleared.
    Strike counts are left as they stand — a wrong *result* is corrected by
    re-settling the game, which recomputes state properly; a revive over an
    uncorrected picks-death stands only until the next settle touches that
    player's picks (the record still says they died)."""
    cur = conn.execute(
        "UPDATE survivor_players SET status = 'alive', eliminated_week = NULL, "
        "elimination_source = NULL "
        "WHERE season_id = ? AND user_id = ? AND status = 'ghost'",
        (season_id, user_id),
    )
    return (cur.rowcount or 0) > 0


# ── hosts (spec §3.4, 2026-09-12) ─────────────────────────────────────
#
# Members an admin names on the dashboard. Guild-scoped rather than a season
# config key: a host must be able to create the next season while none is
# live. Being a host grants the Survivor panel and the host DMs, and while
# any host exists the Tuesday Reckoning goes to them instead of the channel
# (survivor/tasks.py). Hosts may also play — a deliberate call (§1.2).


def list_host_ids(conn: sqlite3.Connection, guild_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT user_id FROM survivor_hosts WHERE guild_id = ? "
        "ORDER BY added_at, user_id",
        (guild_id,),
    ).fetchall()
    return [int(r["user_id"]) for r in rows]


def is_host(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM survivor_hosts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone() is not None


def add_host(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, added_by: int
) -> bool:
    """False if the member is already a host. Does not commit."""
    if int(user_id) <= 0:
        raise SeasonError("Pick a member to add as host.")
    cur = conn.execute(
        "INSERT OR IGNORE INTO survivor_hosts (guild_id, user_id, added_by) "
        "VALUES (?, ?, ?)",
        (guild_id, user_id, added_by),
    )
    return (cur.rowcount or 0) > 0


def remove_host(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    """False if the member wasn't a host. Does not commit."""
    cur = conn.execute(
        "DELETE FROM survivor_hosts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return (cur.rowcount or 0) > 0
