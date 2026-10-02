"""break_holds round trip: the row is the break, one per member."""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.breaks import store
from bot_modules.core.db_utils import open_db, set_config_value
from tests.db_template import migrated_db

G = 100


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "breaks.db"
    migrated_db(path)
    return path


def test_create_read_list_and_delete(db):
    with open_db(db) as conn:
        store.create_break(conn, guild_id=G, user_id=5, stored_roles=[11, 12], swapped_spicy=True)
        row = store.get_break(conn, G, 5)
        assert row is not None
        assert row.stored_roles == [11, 12]
        assert row.swapped_spicy is True
        assert store.is_on_break_conn(conn, G, 5)
        assert not store.is_on_break_conn(conn, G + 1, 5)
        assert store.on_break_user_ids(conn, G) == {5}
        assert [r.user_id for r in store.list_breaks(conn, G)] == [5]
        assert store.delete_break(conn, G, 5) is True
        assert store.delete_break(conn, G, 5) is False
        assert store.get_break(conn, G, 5) is None
    assert store.is_on_break(db, G, 5) is False


def test_a_second_break_never_overwrites_the_first_snapshot(db):
    with open_db(db) as conn:
        store.create_break(conn, guild_id=G, user_id=5, stored_roles=[11], swapped_spicy=False)
    with pytest.raises(sqlite3.IntegrityError), open_db(db) as conn:
        store.create_break(conn, guild_id=G, user_id=5, stored_roles=[900], swapped_spicy=False)
    with open_db(db) as conn:
        assert store.get_break(conn, G, 5).stored_roles == [11]


def test_role_dials_read_zero_until_set_and_ignore_the_legacy_row(db):
    with open_db(db) as conn:
        set_config_value(conn, store.BREAK_ROLE_KEY, "77", 0)
        assert store.read_break_role_ids(conn, G) == (0, 0)
        set_config_value(conn, store.BREAK_ROLE_KEY, "900", G)
        set_config_value(conn, store.SPICY_LITE_ROLE_KEY, "901", G)
        set_config_value(conn, store.BREAK_CHANNEL_KEY, "42", G)
        assert store.read_break_role_ids(conn, G) == (900, 901)
        assert store.read_break_channel_id(conn, G) == 42


def test_a_break_remembers_the_hold_roles_it_granted(db):
    with open_db(db) as conn:
        store.create_break(
            conn, guild_id=G, user_id=5, stored_roles=[11], swapped_spicy=True,
            break_role_id=900, lite_role_id=901,
        )
        assert store.get_break(conn, G, 5).granted_role_ids == {900, 901}


@pytest.mark.parametrize(
    ("old", "new", "retired"),
    [
        pytest.param(901, 902, {901}, id="moved"),
        pytest.param(901, 0, {901}, id="switched-off"),
        pytest.param(0, 902, set(), id="first-pick"),
        pytest.param(901, 901, set(), id="unchanged"),
    ],
)
def test_retiring_a_hold_role(db, old, new, retired):
    with open_db(db) as conn:
        store.retire_hold_role(conn, G, old, new)
        assert store.read_retired_role_ids(conn, G) == retired


def test_a_dial_repicking_a_retired_role_unretires_it(db):
    with open_db(db) as conn:
        store.retire_hold_role(conn, G, 901, 902)
        store.retire_hold_role(conn, G, 902, 901)
        assert store.read_retired_role_ids(conn, G) == {902}


@pytest.mark.parametrize(
    ("key", "retired"),
    [
        pytest.param(store.BREAK_ROLE_KEY, {900}, id="break-dial"),
        pytest.param(store.SPICY_LITE_ROLE_KEY, {901}, id="lite-dial"),
        pytest.param("jailed_role_id", set(), id="not-a-break-dial"),
    ],
)
def test_the_generic_dial_hook_retires_only_break_dials(db, key, retired):
    with open_db(db) as conn:
        set_config_value(conn, store.BREAK_ROLE_KEY, "900", G)
        set_config_value(conn, store.SPICY_LITE_ROLE_KEY, "901", G)
        set_config_value(conn, "jailed_role_id", "555", G)
        store.retire_on_dial_change(conn, G, key, 0)
        assert store.read_retired_role_ids(conn, G) == retired


def test_all_hold_roles_are_the_dials_plus_what_they_named_before(db):
    with open_db(db) as conn:
        set_config_value(conn, store.BREAK_ROLE_KEY, "900", G)
        set_config_value(conn, store.SPICY_LITE_ROLE_KEY, "902", G)
        store.retire_hold_role(conn, G, 901, 902)
        assert store.all_hold_role_ids(conn, G) == {900, 901, 902}


def test_absorb_folds_a_break_in_and_closes_it(db):
    with open_db(db) as conn:
        set_config_value(conn, store.BREAK_ROLE_KEY, "900", G)
        store.create_break(
            conn, guild_id=G, user_id=5, stored_roles=[11, 12], swapped_spicy=True,
            break_role_id=900, lite_role_id=801,  # a Lite the dial no longer names
        )
        assert store.absorb_break(conn, G, 5, [900, 801, 77]) == [11, 12, 77]
        assert store.get_break(conn, G, 5) is None


@pytest.mark.parametrize(
    "snapshot, expected",
    [
        pytest.param([900, 11], [11], id="break-role"),
        pytest.param([901, 11], [11], id="retired-lite"),
        pytest.param([11, 12], [11, 12], id="nothing-to-drop"),
    ],
)
def test_absorb_drops_hold_roles_even_with_no_row(db, snapshot, expected):
    """An erased row leaves the roles on: a jail must still never save one."""
    with open_db(db) as conn:
        set_config_value(conn, store.BREAK_ROLE_KEY, "900", G)
        store.retire_hold_role(conn, G, 901, 0)
        assert store.absorb_break(conn, G, 5, snapshot) == expected
