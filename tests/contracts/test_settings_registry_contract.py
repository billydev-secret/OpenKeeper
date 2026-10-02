"""The AI advisor's settings registry must agree with the owning panel.

The 2026-08-29 IA audit found six duplicated-control defects with one root:
the registry is a second, hand-written schema for keys the panels own, and it
had drifted — different bounds, a different *unit*, missing side effects, and
one feature description that matched nothing in the codebase. These tests pin
the registry to the routes' own validation so the drift class stays fixed:
where a route model bounds a key, the registry entry must carry the same
bounds; where a panel write has side effects the registry can't reproduce,
the key must not be model-writable.
"""

from __future__ import annotations

import pytest

from bot_modules.core.settings_registry import FEATURES, SETTINGS_BY_KEY
from web_server.routes.bios import BiosConfigBody
from web_server.routes.config import (
    BirthdaySettingsUpdate,
    GreetingWatchConfigUpdate,
    GuessConfigUpdate,
    InactiveConfigUpdate,
    IntakeConfigUpdate,
)
from web_server.routes.voice_master import ConfigPayload as VoiceMasterConfig


def _setting(key: str):
    for feature in FEATURES:
        for s in feature.settings:
            if s.key == key:
                return feature, s
    raise AssertionError(f"registry has no setting {key!r}")


def _field_bounds(model, name: str) -> tuple[int, int]:
    field = model.model_fields[name]
    ge = le = None
    for m in field.metadata:
        ge = getattr(m, "ge", ge)
        le = getattr(m, "le", le)
    assert ge is not None and le is not None, f"{name} carries no ge/le bounds"
    return ge, le


@pytest.mark.parametrize(
    "key,model,field",
    [
        pytest.param("bios_questions_per_bio", BiosConfigBody, "questions_per_bio",
                     id="bios-questions"),
        pytest.param("bios_archive_grace", BiosConfigBody, "archive_grace",
                     id="bios-archive-grace"),
        # The other seven bounded keys. Each had a published range in the
        # registry and no row here, which is how five of them came to be
        # enforced by a clamp (or not at all) while the registry advertised a
        # range the route never held anyone to.
        pytest.param("inactive_threshold_days", InactiveConfigUpdate,
                     "threshold_days", id="inactive-threshold"),
        pytest.param("inactive_sweep_cap", InactiveConfigUpdate,
                     "sweep_cap", id="inactive-sweep-cap"),
        pytest.param("birthday_announce_hour", BirthdaySettingsUpdate,
                     "birthday_announce_hour", id="birthday-hour"),
        pytest.param("guess_guess_cooldown_seconds", GuessConfigUpdate,
                     "guess_cooldown_seconds", id="guess-cooldown"),
        pytest.param("voice_master_max_per_member", VoiceMasterConfig,
                     "max_per_member", id="voice-max-per-member"),
        pytest.param("greeting_watch_window_minutes", GreetingWatchConfigUpdate,
                     "window_minutes", id="greeting-window"),
        pytest.param("intake_stale_hours", IntakeConfigUpdate,
                     "stale_hours", id="intake-stale-hours"),
    ],
)
def test_registry_bounds_match_the_owning_route(key, model, field):
    """An advisor Apply must not accept a value the panel's route would
    reject — the panel could then never re-save the form (the QA daily-cap
    5000 case), and the enforcing reader may not clamp at all."""
    _, setting = _setting(key)
    ge, le = _field_bounds(model, field)
    assert (setting.minimum, setting.maximum) == (ge, le), (
        f"{key}: registry allows {setting.minimum}-{setting.maximum}, "
        f"route allows {ge}-{le}"
    )


def test_every_bounded_key_is_pinned_to_its_route():
    """A bounded key with no row above is a bound nothing checks.

    That is how this gap lasted: the registry published a range for nine keys
    and only two were pinned here, so five of the other seven drifted — the
    route clamped instead of refusing, or (``voice_master_max_per_member``)
    took any number at all while the advisor's own UI offered 1-10. The
    parametrize table can't be trusted to keep up by hand, so this fails the
    day a bounded key is added without one.
    """
    covered = {
        p.values[0]
        for p in test_registry_bounds_match_the_owning_route.pytestmark[0].args[1]
    }
    bounded = {
        s.key
        for f in FEATURES
        for s in f.settings
        if s.minimum is not None or s.maximum is not None
    }
    missing = sorted(bounded - covered)
    assert not missing, (
        "bounded registry keys with no route to check them against: "
        f"{missing} — add a pytest.param row naming the body model and field"
    )


def test_bios_archive_grace_is_labelled_in_seconds():
    """The 86,400x bug: the panel writes *seconds* the wizard room stays open
    (0-3600); the registry described 'days before an old bio is archived'
    (0-3650) for the same key, so an advisor-applied 'days' value was
    enforced as seconds. The unit belongs in the label."""
    _, setting = _setting("bios_archive_grace")
    assert "second" in setting.label.lower(), setting.label
    assert "day" not in setting.label.lower(), setting.label


def test_side_effect_keys_are_not_model_writable():
    """These keys' panel writes run extra plumbing a bare config write skips:
    the inactive pair goes through POST /config/inactive/channel (creates the
    role, grants and revokes channel access), and the intake reference PUT is
    the only caller of sync_channel. An advisor Apply writing the raw key
    strands the old channel/blocks exactly as the setup flows document."""
    for key in ("inactive_channel_id", "inactive_role_id",
                "intake_reference_channel_id"):
        _, setting = _setting(key)
        assert not setting.writable, f"{key} must not be model-writable"


def test_welcome_trigger_is_a_closed_choice():
    """The Welcome panel is a two-option select and enforcement matches only
    the exact strings 'join' and 'verified' — any other advisor-applied text
    silently disables welcome messages."""
    _, setting = _setting("welcome_trigger")
    assert setting.choices and set(setting.choices) == {"join", "verified"}, (
        setting.choices
    )


def test_qa_keys_are_not_in_the_registry():
    """The registry sold a fictional 'Q&A rewards' feature ('coins for
    answering questions in a help channel', panel 'Config → Q&A rewards')
    that never existed; the qa_* keys belong to Dev → QA Tracker, whose
    panel is the sole writer. The whole feature came out: an entry both
    duplicated the write path (with 10-100x the route's bounds) and made
    gap detection nudge every unconfigured guild to set up dev tooling."""
    for key in ("qa_enabled", "qa_channel_id", "qa_reward",
                "qa_daily_cap", "qa_role_id"):
        assert key not in SETTINGS_BY_KEY, f"{key} is back in the registry"


def test_photo_left_all_game_types():
    """PUT /api/games/config/games/photo was a second live write path to the
    same games_game_config row the standalone Photo Challenge panel owns —
    'photo' stayed in ALL_GAME_TYPES when the feature went standalone."""
    from web_server.routes.games import ALL_GAME_TYPES

    assert "photo" not in ALL_GAME_TYPES
