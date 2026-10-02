"""Own-table features are visible to the setup-gap scan (standards audit 4.7).

The settings registry described only config-key features, so Starboard, Pen
Pals and every other feature configured in a table of its own could never
appear on the Suggested Setup card or in the assistant's "what am I missing?"
— an absent key says nothing about them. A ``TableSignal`` reads presence from
the feature's own row instead.

The coverage half: every ``*_config`` / ``*_settings`` table in the migrated
schema is either a feature's signal or in ``EXEMPT`` with the reason it isn't
one. A new feature table fails here until someone decides which. ``EXEMPT``
only shrinks.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.advisor import gaps as ag
from bot_modules.core import settings_registry as sr
from db_template import migrated_db

_GAME_TUNING = "game tuning — the game runs on defaults with no row"
_JOIN = "wiring lives in a second table; needs a join signal before it can be one"

EXEMPT: dict[str, str] = {
    "anon_audit_config": "a retention dial for the anon audit log, not a feature",
    "auto_react_config": "per-channel rules; an empty table is an ordinary server",
    "branding_config": "cosmetic; every default applies with no row",
    "chicken_config": _GAME_TUNING,
    "dm_panel_settings": "where the DM panel is posted — placement, not setup",
    "duel_config": _GAME_TUNING,
    "feature_rotation_config": _JOIN,
    "games_game_config": _GAME_TUNING,
    "hot_potato_config": _GAME_TUNING,
    "hp_group_config": _GAME_TUNING,
    "legitlibs_channel_config": "per-channel tier cap for one game",
    "mc_config": _GAME_TUNING,
    "no_contact_settings": "optional alert routing; the list works without it",
    "pressure_config": _GAME_TUNING,
    "quickdraw_config": _GAME_TUNING,
    "revive_channel_config": _JOIN,
    "revive_guild_config": _JOIN,
    "voice_transcription_config": (
        "records members' voice; sensitive access is opt-in, so the assistant "
        "never nudges an admin toward it"
    ),
    "wellness_config": "turned on through its own Activate card, not a row",
}

SIGNALLED = [f for f in sr.FEATURES if f.table_signal is not None]
SIGNAL_TABLES = {f.table_signal.table for f in sr.FEATURES if f.table_signal is not None}


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    path = migrated_db(tmp_path_factory.mktemp("signals") / "schema.db")
    conn = sqlite3.connect(path)
    yield conn
    conn.close()


def _feature_tables(conn) -> set[str]:
    return {
        name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if name.endswith(("_config", "_settings"))
    }


def test_every_feature_table_is_signalled_or_exempt(schema):
    undecided = sorted(_feature_tables(schema) - SIGNAL_TABLES - EXEMPT.keys())
    assert not undecided, (
        f"{undecided}: give the feature a TableSignal in settings_registry so "
        "the setup scan can see it, or add the table to EXEMPT with the reason "
        "it isn't a feature an admin should be pointed at."
    )


def test_exempt_only_shrinks(schema):
    tables = _feature_tables(schema)
    assert not EXEMPT.keys() - tables, "table gone — remove it from EXEMPT"
    assert not EXEMPT.keys() & SIGNAL_TABLES, "now signalled — remove it from EXEMPT"


@pytest.mark.parametrize("feature", SIGNALLED, ids=lambda f: f.slug)
def test_signal_predicates_run_against_the_real_schema(schema, feature):
    """A typo in a predicate would read every server as unwired and log an
    error on each scan; run each one against the migrated table."""
    sig = feature.table_signal
    assert sig is not None
    schema.execute(
        f"SELECT ({sig.wired}), ({sig.on or 'NULL'}) FROM {sig.table} WHERE guild_id = ?",
        (1,),
    )


def _state_db(schema_sql: str, row: dict | None):
    conn = sqlite3.connect(":memory:")
    conn.execute(schema_sql)
    if row is not None:
        cols = ", ".join(row)
        conn.execute(
            f"INSERT INTO t ({cols}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
    return conn


_SWITCHED = sr.Feature(
    "demo", "Demo", "Config → Demo", "A demo.", (),
    table_signal=sr.TableSignal("t", "channel_id != 0", on="enabled = 1"),
)
_SWITCHLESS = sr.Feature(
    "demo2", "Demo 2", "Config → Demo 2", "Another.", (),
    table_signal=sr.TableSignal("t", "channel_id != 0"),
)
_T = "CREATE TABLE t (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, enabled INTEGER)"


@pytest.mark.parametrize(
    ("feature", "row", "status", "switch_on"),
    [
        pytest.param(_SWITCHED, None, "unconfigured", False, id="no-row"),
        pytest.param(_SWITCHED, {"guild_id": 1, "channel_id": 0, "enabled": 1},
                     "unconfigured", True, id="switched-on-but-unwired"),
        pytest.param(_SWITCHED, {"guild_id": 1, "channel_id": 5, "enabled": 0},
                     "ready_but_off", False, id="wired-but-off"),
        pytest.param(_SWITCHED, {"guild_id": 1, "channel_id": 5, "enabled": 1},
                     "configured", True, id="wired-and-on"),
        pytest.param(_SWITCHED, {"guild_id": 2, "channel_id": 5, "enabled": 1},
                     "unconfigured", False, id="another-guilds-row"),
        pytest.param(_SWITCHLESS, None, "unconfigured", None, id="switchless-no-row"),
        pytest.param(_SWITCHLESS, {"guild_id": 1, "channel_id": 5, "enabled": 0},
                     "configured", None, id="switchless-wired"),
    ],
)
def test_table_state_classifies_like_a_kv_feature(feature, row, status, switch_on):
    conn = _state_db(_T, row)
    gap = ag._classify(conn, 1, feature, {})
    assert (gap.status, gap.switch_on) == (status, switch_on)


def test_a_missing_table_reads_as_unwired_not_a_crash():
    conn = sqlite3.connect(":memory:")
    gap = ag._classify(conn, 1, _SWITCHED, {})
    assert gap.status == "unconfigured"


def test_an_unwired_table_feature_costs_one_step():
    """Zero would sort it ahead of a KV feature one field from working."""
    gap = ag.classify_table_feature(_SWITCHED, False, False)
    assert gap.effort == 1
    assert ag.classify_table_feature(_SWITCHED, True, True).effort == 0


def test_the_report_says_the_assistant_cant_set_it_up():
    """A signalled feature lists no settings, so nothing is proposable — the
    report has to say so or the model offers an Apply it can't build."""
    text = ag.format_gap_report([ag.classify_table_feature(_SWITCHED, False, False)])
    assert "Set up from the panel only" in text
    assert "Still needs" not in text


def test_the_registry_refuses_settings_on_a_table_feature(monkeypatch):
    bad = sr.Feature(
        "bad", "Bad", "Config → Bad", "Bad.",
        (sr._flag("bad_enabled", "Bad on"),),
        table_signal=sr.TableSignal("t", "1"),
    )
    monkeypatch.setattr(sr, "FEATURES", (*sr.FEATURES, bad))
    with pytest.raises(ValueError, match="lists no config keys"):
        sr._check_registry()


def test_the_scan_sees_a_configured_starboard(schema):
    """End to end on the real schema: the row the Starboard panel writes."""
    schema.execute(
        "INSERT OR REPLACE INTO starboard_config (guild_id, channel_id, enabled) "
        "VALUES (77, 123, 1)"
    )
    schema.row_factory = sqlite3.Row
    by_slug = {g.feature.slug: g for g in ag.scan_guild(schema, 77)}
    assert by_slug["starboard"].status == "configured"
    assert by_slug["pen_pals"].status == "unconfigured"
