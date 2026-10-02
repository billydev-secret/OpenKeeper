"""Tests for the Games Help modules.

Covers ``bot_modules/games/help/logic.py`` (command and description
lookups, alignment guarantees against the canonical ``GAME_ICONS``
registry, the grouped overview and the per-game detail), the play registry
in ``games/constants.py`` that feeds both the panel and the ``/games play``
picker descriptions (discovery-13), and
``bot_modules/games/help/layouts.py`` (the overview, detail and
``/support`` cards).
"""

from __future__ import annotations

import re

import discord
import pytest

from bot_modules.core.extension_discovery import cog_files
from bot_modules.games.constants import (
    AUTO_START_LOBBY_TYPES,
    DUEL_GAME_KEYS,
    GAME_ICONS,
    GAME_MIN_PLAYERS,
    GAME_NAMES,
    GAME_TYPICAL_LENGTH,
    HOW_TO_PLAY,
    LOBBY_GAME_TYPES,
    LOBBY_MIN_PLAYERS,
    hosting_kind,
    play_description,
)
from bot_modules.games.help.layouts import (
    build_game_detail_layout,
    build_help_layout,
    build_support_layout,
)
from bot_modules.games.help.logic import (
    FOLDED_VARIANTS,
    GAME_COMMANDS,
    GAME_DESCRIPTIONS,
    OTHER_COMMANDS_VALUE,
    SELECT_OPTION_LIMIT,
    support_invite_url,
    game_detail,
    help_groups,
    room_help_lines,
    select_options,
    survivor_help_line,
)
from tests.paths import REPO_ROOT


def _text_of(items) -> str:
    return "\n".join(
        item.content for item in items if isinstance(item, discord.ui.TextDisplay)
    )

_DOCS_ROOT = REPO_ROOT


# ── alignment guarantees ─────────────────────────────────────────────


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_every_game_icon_has_a_command(key):
    """Each entry in GAME_ICONS (except the internal-only ``pressure``
    key) must have a slash-command listed — otherwise the help embed
    silently falls back to ``"/<key>"``."""
    assert key in GAME_COMMANDS, f"GAME_COMMANDS missing entry for {key!r}"


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_every_game_icon_has_a_description(key):
    """Each entry in GAME_ICONS (except ``pressure``) must have a
    description so the help embed never renders a blank tail."""
    assert key in GAME_DESCRIPTIONS, (
        f"GAME_DESCRIPTIONS missing entry for {key!r}"
    )


def test_no_orphan_command_entries():
    """Every key in GAME_COMMANDS should correspond to a real game."""
    for key in GAME_COMMANDS:
        assert key in GAME_ICONS, f"GAME_COMMANDS has orphan {key!r}"


def test_no_orphan_description_entries():
    for key in GAME_DESCRIPTIONS:
        assert key in GAME_ICONS, f"GAME_DESCRIPTIONS has orphan {key!r}"


def test_all_commands_start_with_slash():
    for key, cmd in GAME_COMMANDS.items():
        assert cmd.startswith("/"), f"{key} command {cmd!r} missing leading /"


def test_support_invite_url_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("SUPPORT_INVITE_URL", raising=False)
    assert support_invite_url() == ""
    monkeypatch.setenv("SUPPORT_INVITE_URL", " https://discord.gg/example ")
    assert support_invite_url() == "https://discord.gg/example"


def test_other_commands_value_references_recap_and_support():
    """The block names ``/support`` — the top-level command that exists — not
    the ``/games support`` subcommand it advertised for months (platform-25)."""
    assert "/recap" in OTHER_COMMANDS_VALUE
    assert "/support" in OTHER_COMMANDS_VALUE
    assert "/games support" not in OTHER_COMMANDS_VALUE


# ── the play registry (discovery-13) ─────────────────────────────────


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_every_game_has_a_floor_and_a_length(key):
    assert key in GAME_MIN_PLAYERS, f"GAME_MIN_PLAYERS missing {key!r}"
    assert key in GAME_TYPICAL_LENGTH, f"GAME_TYPICAL_LENGTH missing {key!r}"


@pytest.mark.parametrize("key", sorted(LOBBY_GAME_TYPES))
def test_lobby_floors_agree_with_the_sweep(key):
    """The floor the help panel quotes is the one the idle-lobby sweep
    enforces — except Fantasies, whose sweep floor of one is a "somebody
    opened the panel" fact, not a player count."""
    if key == "fantasies":
        assert GAME_MIN_PLAYERS[key] >= LOBBY_MIN_PLAYERS[key]
    else:
        assert GAME_MIN_PLAYERS[key] == LOBBY_MIN_PLAYERS[key]


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_play_description_fits_discord_and_names_the_floor(key):
    """≤100 chars is Discord's ceiling on a command description; the floor
    and the name are the two things a member scanning the picker needs."""
    desc = play_description(key)
    assert len(desc) <= 100, f"{key}: {len(desc)} chars"
    assert desc.startswith(GAME_NAMES[key])
    floor = GAME_MIN_PLAYERS[key]
    assert ("any number of players" if floor <= 1 else f"{floor}+ players") in desc


def _play_subcommands():
    """Every ``/games play`` subcommand as its cog class declares it — the
    cogs attach them to the shared ``play`` group only when added to a bot,
    so read the class-level list discord.py builds at import."""
    import importlib
    import inspect

    from discord.ext import commands

    for mod in (
        "bot_modules.games.ama.cog", "bot_modules.games.clapback.cog",
        "bot_modules.games.compliment.cog", "bot_modules.games.fantasies.cog",
        "bot_modules.games.ffa.cog", "bot_modules.games.hottakes.cog",
        "bot_modules.games.mfk.cog", "bot_modules.games.mlt.cog",
        "bot_modules.games.nhie.cog", "bot_modules.games.price.cog",
        "bot_modules.games.rushmore.cog", "bot_modules.games.story.cog",
        "bot_modules.games.traditional.cog", "bot_modules.games.ttl.cog",
        "bot_modules.games.wyr.cog", "bot_modules.games.legitlibs",
    ):
        module = importlib.import_module(mod)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == module.__name__:
                yield from cls.__cog_app_commands__


def test_every_play_subcommand_description_comes_from_the_registry():
    """Each ``/games play`` description must be the registry's line, so the
    picker and the help panel can never disagree about a floor."""
    by_command = {cmd.split()[-1]: key for key, cmd in GAME_COMMANDS.items()
                  if cmd.startswith("/games play ")}
    seen = set()
    for cmd in _play_subcommands():
        key = by_command.get(cmd.name)
        assert key is not None, f"/games play {cmd.name} has no GAME_COMMANDS entry"
        assert cmd.description == play_description(key), cmd.name
        seen.add(key)
    assert seen == set(by_command.values())


# ── the hosting registry (P4 leftover: a scheduled Clapback starts itself) ─


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        pytest.param("ffa", "self", id="prompt-card"),
        pytest.param("wyr", "timer", id="timer-round"),
        pytest.param("clapback", "countdown", id="clapback-auto-start"),
        pytest.param("price", "countdown", id="price-auto-start"),
        pytest.param("mlt", "host", id="plain-lobby"),
        pytest.param("ama", "host", id="host-driven"),
    ],
)
def test_hosting_kind_follows_the_registry(key, kind):
    assert hosting_kind(key) == kind


def test_auto_start_registry_mirrors_the_cogs():
    """``AUTO_START_LOBBY_TYPES`` is a static mirror of the cogs that
    register a ``lobby_auto_starters`` entry in ``setup()``; a cog gaining
    or losing one must move its tag too."""
    registered = set()
    for path in cog_files(_DOCS_ROOT / "src" / "bot_modules"):
        registered.update(
            re.findall(
                r"lobby_auto_starters\[\"(\w+)\"\]", path.read_text(encoding="utf-8")
            )
        )
    assert registered == set(AUTO_START_LOBBY_TYPES)


# ── copy contradictions (discovery-8) ────────────────────────────────


def test_legitlibs_help_names_classic_as_the_default():
    """The slash entry defaults ``mode`` to classic; the in-game Help said
    Quiplash was the default."""
    text = HOW_TO_PLAY["legitlibs"]
    assert "Classic mode (default)" in text
    assert "Quiplash mode (default)" not in text


def test_ttl_floor_matches_the_start_guard():
    """The picker line and help card say what Start Guessing enforces (T1)."""
    from bot_modules.games.ttl.logic import MIN_PLAYERS

    assert GAME_MIN_PLAYERS["ttl"] == MIN_PLAYERS


def test_mahjong_is_named_but_not_on_the_menu():
    """Settled hands write history rows as 'mahjong' (Play Statistics needs
    a display name); the table is a room, not a /games play launch (M3)."""
    assert GAME_NAMES["mahjong"] == "Meadow Mahjong"
    assert "mahjong" not in GAME_ICONS


def test_photo_has_no_dead_how_to_play():
    """Photo Challenge left the games menu; its HOW_TO_PLAY entry was text
    nothing rendered."""
    assert "photo" not in HOW_TO_PLAY
    assert "photo" not in GAME_ICONS


# ── doc-count tripwires ──────────────────────────────────────────────
# The feature map and web manual advertise the party-game count in prose.
# These numbers have drifted twice (16 → 17 → 18); fail loudly when a game is
# added to GAME_ICONS without updating the docs. The prose list used to live
# in README.md and moved to docs/features.md 2026-09-03 when the README became
# a pitch — the tripwire follows the list, not the filename.

_PARTY_GAME_KEYS = [k for k in GAME_ICONS if k not in DUEL_GAME_KEYS]


def test_features_doc_party_game_count_matches_code():
    features = (_DOCS_ROOT / "docs" / "features.md").read_text(encoding="utf-8")
    expected = f"{len(_PARTY_GAME_KEYS)}-game"
    assert expected in features, (
        f"docs/features.md should say '{expected}' (GAME_ICONS has "
        f"{len(_PARTY_GAME_KEYS)} party games) — update the count."
    )


def test_manual_party_game_count_matches_code():
    manual = (
        _DOCS_ROOT / "src" / "web_server" / "static" / "manual.html"
    ).read_text(encoding="utf-8")
    expected = f"{len(_PARTY_GAME_KEYS)} party games"
    assert expected in manual, (
        f"manual.html's Feature Map should say '{expected}' — update the count."
    )


# ── help_groups / select_options / game_detail ───────────────────────


def test_help_groups_cover_every_game_once():
    groups = dict(help_groups())
    listed = "\n".join(line for lines in groups.values() for line in lines)
    for key in GAME_ICONS:
        if key in FOLDED_VARIANTS:
            assert GAME_COMMANDS[key] not in listed, "folded variant listed on its own"
            continue
        assert f"`{GAME_COMMANDS[key]}`" in listed, f"{key} missing from the overview"
    party = "\n".join(groups["🎉 Party Games"])
    duels = "\n".join(groups["⚔️ Duels & Group Games"])
    for key in DUEL_GAME_KEYS:
        assert GAME_COMMANDS[key] in duels and GAME_COMMANDS[key] not in party


def test_help_groups_rooms_carry_extra_lines_and_whisper():
    rooms = dict(help_groups(["🏈 **Survivor** — open in <#5>"]))["🏠 Rooms & Tables"]
    assert rooms[0] == "🏈 **Survivor** — open in <#5>"
    assert any("Whisper" in line for line in rooms)


def test_select_options_fold_the_banner_and_fit_the_menu():
    keys = [k for k, _, _ in select_options()]
    assert "ffa" in keys and "ffa_banner" not in keys
    assert len(keys) == len(GAME_ICONS) - len(FOLDED_VARIANTS)
    assert len(keys) <= SELECT_OPTION_LIMIT
    assert all(len(label) <= 100 and len(desc) <= 100 for _, label, desc in select_options())


def test_game_detail_reads_the_registry():
    d = game_detail("clapback")
    assert d.rules == HOW_TO_PLAY["clapback"]
    assert d.floor == "3+ players"
    assert d.hosting_label == "Starts itself after a countdown"
    assert d.length == GAME_TYPICAL_LENGTH["clapback"]
    assert d.command == "/games play clapback"
    assert d.startable is True


def test_game_detail_folds_the_banner_into_ffa():
    d = game_detail("ffa")
    assert any("/games play ffa_banner" in line for line in d.variant_lines)
    assert game_detail("wyr").variant_lines == ()


@pytest.mark.parametrize("key", sorted(DUEL_GAME_KEYS))
def test_duels_are_not_startable_from_the_panel(key):
    """A duel needs an opponent picked at the command — no headless
    ``launch()`` to press."""
    assert game_detail(key).startable is False


# ── build_help_layout ────────────────────────────────────────────────


def test_build_help_layout_has_title_and_description():
    text = _text_of(build_help_layout())
    assert text.startswith("## 🌸 Community Games")
    assert "Start Here" in text


def test_build_help_layout_lists_every_game_in_a_group():
    """One line per game inside its group — not one item per game."""
    text = _text_of(build_help_layout())
    for key in GAME_ICONS:
        if key in FOLDED_VARIANTS:
            continue
        assert f"{GAME_ICONS[key]} **{GAME_NAMES[key]}**" in text, f"missing line for {key}"
    assert "### 🎉 Party Games" in text
    assert "### ⚔️ Duels & Group Games" in text
    assert "### 🏠 Rooms & Tables" in text


def test_build_help_layout_stays_under_the_components_v2_budget():
    """The tripwire discovery-7 asked for the embed's field ceiling; the
    layout ceiling is the whole card's component/character budget."""
    from bot_modules.core.layout import check_budget

    view = discord.ui.LayoutView(timeout=None)
    for item in build_help_layout(extra_lines=[f"line {i}" for i in range(10)]):
        view.add_item(item)
    check_budget(view)  # raises if the card overflows


def test_build_help_layout_includes_other_commands_section():
    text = _text_of(build_help_layout())
    assert "### ⚙️ Other Commands" in text
    assert "/recap" in text
    assert "/games support" not in text


# ── build_game_detail_layout ─────────────────────────────────────────


@pytest.mark.parametrize("key", list(GAME_ICONS))
def test_build_game_detail_layout_for_every_game(key):
    text = _text_of(build_game_detail_layout(key))
    assert text.startswith(f"## {GAME_ICONS[key]} {GAME_NAMES[key]}")
    assert text  # the rules, or at least the one-liner
    assert (
        "any number" if GAME_MIN_PLAYERS[key] <= 1 else f"{GAME_MIN_PLAYERS[key]}+"
    ) in text
    assert f"`{GAME_COMMANDS[key]}`" in text


def test_build_game_detail_layout_pacing_names_the_hosting_kind():
    text = _text_of(build_game_detail_layout("wyr"))
    assert "Self-running with a round timer" in text


# ── build_support_layout ─────────────────────────────────────────────


def test_build_support_layout_has_title():
    text = _text_of(build_support_layout())
    assert "🛟 Support Server" in text


def test_build_support_layout_includes_invite_url(monkeypatch):
    monkeypatch.setenv("SUPPORT_INVITE_URL", "https://discord.gg/example")
    assert "https://discord.gg/example" in _text_of(build_support_layout())


def test_build_support_layout_without_an_invite_says_so(monkeypatch):
    monkeypatch.delenv("SUPPORT_INVITE_URL", raising=False)
    text = _text_of(build_support_layout())
    assert "no support server" in text and "discord.gg" not in text


# ── channel-native games: the Survivor pointer and the rooms ──────────


def test_build_help_layout_folds_extra_lines_into_rooms():
    text = _text_of(
        build_help_layout(extra_lines=["🏈 **Survivor** — open in <#5>"])
    )
    assert "🏈 **Survivor** — open in <#5>" in text
    assert "Survivor" not in _text_of(build_help_layout())


# ── channel-native games: the Survivor pointer ───────────────────────


_GID = 100
_NOW = 1_800_000_000.0


@pytest.fixture
def survivor_db(tmp_path):
    from tests.db_template import migrated_db

    db_path = tmp_path / "help.db"
    migrated_db(db_path)
    return db_path


def _season(conn, **config):
    from bot_modules.survivor.service import create_season

    return create_season(conn, _GID, "S", 2026, overrides=config or None)


def _elapse_week_one(conn) -> None:
    from datetime import datetime, timezone

    kicked = datetime.fromtimestamp(_NOW - 86400 * 3, timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
        " kickoff_utc, status, winner) VALUES (2026, 1, 'g1', 'SEA', 'NE', ?,"
        " 'final', 'SEA')",
        (kicked,),
    )


def test_survivor_help_line_is_absent_without_a_season(survivor_db):
    from bot_modules.core.db_utils import open_db

    with open_db(survivor_db) as conn:
        assert survivor_help_line(conn, _GID, _NOW) is None


def test_survivor_help_line_needs_a_wired_channel(survivor_db):
    from bot_modules.core.db_utils import open_db

    with open_db(survivor_db) as conn:
        _season(conn)
        assert survivor_help_line(conn, _GID, _NOW) is None


@pytest.mark.parametrize(
    ("late_entry", "elapsed", "shown"),
    [
        pytest.param("gauntlet", False, True, id="enrolling"),
        pytest.param("gauntlet", True, True, id="gauntlet-door-stays-open"),
        pytest.param("ghost_only", True, True, id="ghost-only-door-stays-open"),
        pytest.param("closed", False, True, id="closed-before-kickoff"),
        pytest.param("closed", True, False, id="closed-after-kickoff"),
    ],
)
def test_survivor_help_line_follows_the_door(survivor_db, late_entry, elapsed, shown):
    from bot_modules.core.db_utils import open_db

    with open_db(survivor_db) as conn:
        _season(conn, channel_id=5551, late_entry=late_entry)
        if elapsed:
            _elapse_week_one(conn)
        line = survivor_help_line(conn, _GID, _NOW)
    if shown:
        assert line is not None and "<#5551>" in line and "Survivor" in line
    else:
        assert line is None


def test_room_help_lines_need_a_wired_channel(survivor_db):
    """Guess Who and the casino get a pointer only once a channel is
    configured — a closed room gets no dead link."""
    from bot_modules.core.db_utils import open_db
    from bot_modules.core.db_utils import set_config_value

    with open_db(survivor_db) as conn:
        assert room_help_lines(conn, _GID) == []
        set_config_value(conn, "guess_channel_id", "777", _GID)
        set_config_value(conn, "casino_channel_id", "888", _GID)
        conn.commit()
        lines = room_help_lines(conn, _GID)
    assert any("Guess Who" in line and "<#777>" in line for line in lines)
    assert any("Casino" in line and "<#888>" in line for line in lines)


#: Slash options the party games no longer take; pacing moved to the dashboard
#: dials and schedule options (2026-09). HOW_TO_PLAY told players to add
#: take_seconds: and entry_seconds: until 2026-10-01, and Discord rejected both.
_RETIRED_SLASH_OPTIONS = ("round_seconds:", "rounds:", "take_seconds:", "entry_seconds:")


@pytest.mark.parametrize("game", sorted(HOW_TO_PLAY))
def test_how_to_play_names_no_retired_slash_option(game):
    text = HOW_TO_PLAY[game]
    stale = [opt for opt in _RETIRED_SLASH_OPTIONS if f"`{opt}`" in text]
    assert not stale, f"{game} help tells players to use {stale}"
