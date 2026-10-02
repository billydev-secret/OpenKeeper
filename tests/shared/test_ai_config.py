"""AI prompt registry: what is editable, and where the rows live.

One defect lives here.

*The prompts are bot-wide but used to be stored per guild.* The dashboard panel
is primary-guild-only, so a second guild had no surface at all — it silently ran
whatever stale ``guild_id=0`` row happened to exist. Prompts are now written at
``guild_id=0``, which every guild's reader resolves through
``get_config_value``'s legacy fallback, so what the panel shows is what every
guild runs. That also makes "Restore Original" work: it used to delete only the
active guild's row, leaving a legacy guild-0 override to fall back onto, so the
button put the *old override* back rather than the shipped default.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.shared import ai_config
from tests.db_template import migrated_db

PRIMARY = 1469491362444480666
SECOND = 1476999999999999998


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ai_config.db"
    migrated_db(path)
    return path


# ── registry ───────────────────────────────────────────────────────────


def test_the_registry_carries_no_model_dial():
    """One model is loaded at a time, so a per-command model would be a lie."""
    assert not hasattr(ai_config.PromptInfo, "model_key")
    for name in ("get_mod_model", "get_wellness_model", "get_command_model",
                 "set_mod_model", "set_wellness_model", "set_command_model",
                 "KNOWN_MODELS"):
        assert not hasattr(ai_config, name), f"{name} still pretends to be read"


# ── storage scope ──────────────────────────────────────────────────────


@pytest.mark.parametrize("key", [p.key for p in ai_config.list_prompts()])
def test_a_global_override_reaches_every_guild(db_path, key):
    with open_db(db_path) as conn:
        ai_config.set_prompt(conn, key, "custom instructions", 0)
    with open_db(db_path) as conn:
        for guild_id in (0, PRIMARY, SECOND):
            assert ai_config.get_prompt(conn, key, guild_id) == "custom instructions"


def test_resetting_the_global_row_restores_the_shipped_default(db_path):
    key = "ai_prompt_watch_check"
    with open_db(db_path) as conn:
        ai_config.set_prompt(conn, key, "These are bulk messages.", 0)
    with open_db(db_path) as conn:
        ai_config.reset_prompt(conn, key, 0)
    with open_db(db_path) as conn:
        text, is_override = ai_config.get_prompt_with_source(conn, key, SECOND)
    assert is_override is False
    assert text == ai_config._PROMPTS_BY_KEY[key].default_factory()


def test_an_unknown_prompt_key_is_refused(db_path):
    with open_db(db_path) as conn:
        with pytest.raises(KeyError):
            ai_config.set_prompt(conn, "ai_prompt_nope", "x", 0)
        with pytest.raises(KeyError):
            ai_config.reset_prompt(conn, "ai_prompt_nope", 0)
        with pytest.raises(KeyError):
            ai_config.get_prompt(conn, "ai_prompt_nope", 0)


def test_get_prompt_from_path_survives_a_missing_database(tmp_path):
    key = "ai_prompt_watch_check"
    text = ai_config.get_prompt_from_path(tmp_path / "nope.db", key)
    assert text == ai_config._PROMPTS_BY_KEY[key].default_factory()

