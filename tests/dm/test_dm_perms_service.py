"""Tests for the DM-perms service's configurable mode-role support.

Guilds can map each DM status (open/ask/closed) to a pre-existing role
instead of the bot-created "DMs: …" defaults. These tests cover the pure
resolution helpers (``resolve_mode`` / ``is_dm_mode_role``) and the DB
round-trip for the overrides, plus the connection list the settings panel
renders — the panel's own card is covered in ``test_dm_perms_layouts.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.dm.perms.service import (
    ROLE_DM_ASK,
    ROLE_DM_CLOSED,
    ROLE_DM_OPEN,
    get_dm_mode_role_ids,
    get_dms_config,
    init_db,
    is_dm_mode_role,
    load_dm_mode_roles,
    load_member_connections,
    resolve_mode,
    set_dm_mode_role_ids,
)
from bot_modules.no_contact.service import add_pair


def _role(name: str, role_id: int):
    r = MagicMock(spec=discord.Role)
    r.name = name
    r.id = role_id
    return r


def _member(*roles):
    m = MagicMock(spec=discord.Member)
    m.roles = list(roles)
    return m


# ── resolve_mode ─────────────────────────────────────────────────────


def test_resolve_mode_defaults_by_name():
    assert resolve_mode(_member(_role(ROLE_DM_OPEN, 1))) == "open"
    assert resolve_mode(_member(_role(ROLE_DM_CLOSED, 2))) == "closed"
    assert resolve_mode(_member(_role(ROLE_DM_ASK, 3))) == "ask"
    assert resolve_mode(_member()) == "ask"


def test_resolve_mode_uses_configured_role_ids():
    overrides = {"open": 100, "ask": 200, "closed": 300}
    assert resolve_mode(_member(_role("Chatty", 100)), overrides) == "open"
    assert resolve_mode(_member(_role("Do Not Disturb", 300)), overrides) == "closed"
    # A role that matches no configured id and no default name → ask.
    assert resolve_mode(_member(_role("Something Else", 999)), overrides) == "ask"


def test_resolve_mode_open_beats_closed_with_overrides():
    overrides = {"open": 100, "ask": 0, "closed": 300}
    member = _member(_role("Chatty", 100), _role("Do Not Disturb", 300))
    assert resolve_mode(member, overrides) == "open"


def test_resolve_mode_default_names_still_match_when_overrides_set():
    # Migration case: overrides configured, but a member still carries the
    # old bot-created role. Name fallback keeps their status meaningful.
    overrides = {"open": 100, "ask": 200, "closed": 300}
    assert resolve_mode(_member(_role(ROLE_DM_CLOSED, 42)), overrides) == "closed"


# ── is_dm_mode_role ──────────────────────────────────────────────────


def test_is_dm_mode_role_matches_default_names():
    assert is_dm_mode_role(_role(ROLE_DM_OPEN, 1))
    assert is_dm_mode_role(_role(ROLE_DM_ASK, 2))
    assert not is_dm_mode_role(_role("Member", 3))


def test_is_dm_mode_role_matches_configured_ids():
    overrides = {"open": 100, "ask": 0, "closed": 300}
    assert is_dm_mode_role(_role("Chatty", 100), overrides)
    assert is_dm_mode_role(_role("Do Not Disturb", 300), overrides)
    assert not is_dm_mode_role(_role("Member", 999), overrides)
    # Unset (0) entries must not match a hypothetical role id 0.
    assert not is_dm_mode_role(_role("Weird", 0), overrides)


# ── DB round-trip ────────────────────────────────────────────────────


def test_mode_role_ids_roundtrip(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)

    assert get_dm_mode_role_ids(db, 1) == {"open": 0, "ask": 0, "closed": 0}

    set_dm_mode_role_ids(db, 1, open_role_id=100, ask_role_id=200, closed_role_id=300)
    assert get_dm_mode_role_ids(db, 1) == {"open": 100, "ask": 200, "closed": 300}

    # Upsert replaces, including clearing back to 0.
    set_dm_mode_role_ids(db, 1, open_role_id=101, ask_role_id=0, closed_role_id=300)
    assert get_dm_mode_role_ids(db, 1) == {"open": 101, "ask": 0, "closed": 300}

    set_dm_mode_role_ids(db, 2, open_role_id=7, ask_role_id=8, closed_role_id=9)
    loaded = load_dm_mode_roles(db)
    assert loaded[1] == {"open": 101, "ask": 0, "closed": 300}
    assert loaded[2] == {"open": 7, "ask": 8, "closed": 9}


def test_get_dm_mode_role_ids_tolerates_missing_table(tmp_path):
    # A caller outside the DM cog may query before the DM cog ran init_db.
    db = tmp_path / "empty.db"
    assert get_dm_mode_role_ids(db, 1) == {"open": 0, "ask": 0, "closed": 0}


def test_get_dms_config_includes_mode_roles(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    set_dm_mode_role_ids(db, 5, open_role_id=11, ask_role_id=22, closed_role_id=33)
    cfg = get_dms_config(db, 5)
    assert cfg["open_role_id"] == 11
    assert cfg["ask_role_id"] == 22
    assert cfg["closed_role_id"] == 33


# ── the settings panel's connection list ─────────────────────────────
#
# The panel lists who a member is connected to, each row with its own Remove
# button. Nothing answered that question before: the cog's in-memory pair set
# knows whether *these two* are connected, not who anyone's partners are.


def _connect(db, guild_id, a, b, *, rel_type="dm", reason=""):
    from bot_modules.dm.perms.service import add_consent_pair

    add_consent_pair(db, guild_id, a, b, rel_type=rel_type, reason=reason)


def test_load_member_connections_is_empty_for_an_unconnected_member(sync_db_path):
    assert load_member_connections(sync_db_path, 5, 100) == []


def test_load_member_connections_finds_a_pair_from_either_side(sync_db_path):
    _connect(sync_db_path, 5, 100, 200, rel_type="friend", reason="chess")
    for member, other in ((100, 200), (200, 100)):
        rows = load_member_connections(sync_db_path, 5, member)
        assert [r["user_id"] for r in rows] == [other]
        assert rows[0]["type"] == "friend"
        assert rows[0]["reason"] == "chess"
        assert rows[0]["created_at"]


def test_load_member_connections_is_scoped_to_one_guild(sync_db_path):
    _connect(sync_db_path, 5, 100, 200)
    assert load_member_connections(sync_db_path, 6, 100) == []


def test_load_member_connections_orders_newest_first(sync_db_path):
    _connect(sync_db_path, 5, 100, 200)
    _connect(sync_db_path, 5, 100, 300)
    _connect(sync_db_path, 5, 100, 400)
    rows = load_member_connections(sync_db_path, 5, 100)
    assert [r["created_at"] for r in rows] == sorted(
        (r["created_at"] for r in rows), reverse=True
    )
    assert set(r["user_id"] for r in rows) == {200, 300, 400}


def test_load_member_connections_hides_a_no_contact_partner(sync_db_path):
    """A suppressed pair must not be *listed*, on either side.

    The consent row is kept, not deleted — it is the record of when and why
    the two were connected, which is exactly what a mod reviewing the case
    wants. But listing it would hand each of them a card naming the other,
    which is the visibility the no-contact rule exists to remove, and a
    Remove button that would tell one of them the row was still there.
    """
    _connect(sync_db_path, 5, 100, 200)
    _connect(sync_db_path, 5, 100, 300)
    add_pair(sync_db_path, 5, 100, 200, created_by=1, reason="mod action")

    assert [r["user_id"] for r in load_member_connections(sync_db_path, 5, 100)] == [300]
    assert load_member_connections(sync_db_path, 5, 200) == []


def test_load_member_connections_keeps_the_suppressed_row_on_disk(sync_db_path):
    """Suppressed, not deleted: the pair is still there to be reviewed."""
    from bot_modules.dm.perms.service import get_consent_pair_meta

    _connect(sync_db_path, 5, 100, 200, reason="chess")
    add_pair(sync_db_path, 5, 100, 200, created_by=1)
    meta = get_consent_pair_meta(sync_db_path, 5, 100, 200)
    assert meta and meta["reason"] == "chess"


# ── request lifecycle limits (per-guild dashboard dials) ─────────────


def test_request_limits_default_when_nothing_is_stored(sync_db_path):
    from bot_modules.dm.perms.service import get_request_limits

    assert get_request_limits(sync_db_path, 5) == {
        "expiry_hours": 24,
        "max_pending": 5,
    }


@pytest.mark.parametrize(
    ("stored_hours", "expected_hours"),
    [(48, 48), (1, 1), (0, 1), (5000, 720), ("nonsense", 24)],
)
def test_request_expiry_hours_is_clamped(sync_db_path, stored_hours, expected_hours):
    from bot_modules.core.db_utils import open_db, set_config_value
    from bot_modules.dm.perms.service import get_request_limits

    with open_db(sync_db_path) as conn:
        set_config_value(conn, "dm_request_expiry_hours", str(stored_hours), 5)
    assert get_request_limits(sync_db_path, 5)["expiry_hours"] == expected_hours


def test_request_limits_are_per_guild(sync_db_path):
    from bot_modules.core.db_utils import open_db
    from bot_modules.dm.perms.service import (
        get_request_limits,
        set_request_limits,
    )

    with open_db(sync_db_path) as conn:
        set_request_limits(conn, 5, expiry_hours=2, max_pending=1)

    assert get_request_limits(sync_db_path, 5) == {"expiry_hours": 2, "max_pending": 1}
    assert get_request_limits(sync_db_path, 6) == {"expiry_hours": 24, "max_pending": 5}


@pytest.mark.parametrize(
    ("hours", "label"),
    [(1, "1 hour"), (24, "24 hours"), (48, "48 hours")],
)
def test_request_expiry_label(hours, label):
    from bot_modules.dm.perms.service import request_expiry_label

    assert request_expiry_label(hours) == label


def test_expiry_sweep_uses_each_guilds_own_window(sync_db_path):
    """A guild that shortened its window expires sooner; its neighbour, which
    kept the default, keeps waiting."""
    import time

    from bot_modules.core.db_utils import open_db
    from bot_modules.dm.perms.service import (
        expire_stale_pending_requests,
        set_request_limits,
        upsert_request,
    )

    init_db(sync_db_path)
    upsert_request(sync_db_path, 5, 100, 200, "dm", "", 1, None)
    upsert_request(sync_db_path, 6, 101, 201, "dm", "", 2, None)
    six_hours_ago = time.time() - 6 * 3600
    with open_db(sync_db_path) as conn:
        conn.execute("UPDATE dm_requests SET created_at = ?", (six_hours_ago,))
        set_request_limits(conn, 5, expiry_hours=2)

    expired = expire_stale_pending_requests(sync_db_path)

    assert [(r["guild_id"], r["requester_id"]) for r in expired] == [(5, 100)]
    # The window rides along on the row so the cog can word the "expired" DM
    # without reopening SQLite on the bot's event loop.
    assert expired[0]["expiry_hours"] == 2
    with open_db(sync_db_path) as conn:
        statuses = {
            int(r["guild_id"]): r["status"]
            for r in conn.execute("SELECT guild_id, status FROM dm_requests")
        }
    assert statuses == {5: "expired", 6: "pending"}


# ── set_member_dm_mode: role safety (2026-09-14) ─────────────────────────


@pytest.mark.asyncio
async def test_set_mode_leaves_existing_modes_when_the_target_role_is_elevated(monkeypatch):
    """A pinned mode-role id can point at a mod role; refuse it and strip nothing."""
    from unittest.mock import AsyncMock

    from bot_modules.dm.perms import service as dm_perms_service
    from tests.fakes import FakeRole

    ask = FakeRole(id=2)
    open_role = FakeRole(id=1, permissions=discord.Permissions(manage_channels=True))
    member = MagicMock(spec=discord.Member)
    member.roles = [ask]
    member.guild.me = None
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    monkeypatch.setattr(
        dm_perms_service,
        "ensure_dm_roles",
        AsyncMock(return_value={"open": open_role, "ask": ask, "closed": FakeRole(id=3)}),
    )

    await dm_perms_service.set_member_dm_mode(member, "open")

    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
