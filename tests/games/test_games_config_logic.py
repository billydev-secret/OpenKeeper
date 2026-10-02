"""Tests for the extracted games-config pure-logic modules.

Covers ``bot_modules/games/config/logic.py`` (row → string transforms and
permission predicates) and ``bot_modules/games/config/layouts.py`` (Components
V2 card builders). The cog itself stays Discord-glue; this proves the
extracted helpers work without spinning up a bot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot_modules.games.config.layouts import (
    build_force_end_layout,
    build_game_status_layout,
)
from bot_modules.games.config.logic import (
    channel_ids_from_rows,
    describe_active_game,
    describe_force_end,
    has_admin_permissions,
)
from bot_modules.core.utils import has_mod_or_admin_permissions


def _text_of(items) -> str:
    return "\n".join(getattr(item, "content", "") for item in items)


# ── describe_active_game ─────────────────────────────────────────────


def test_describe_active_game_none_returns_no_active_message():
    title, body = describe_active_game(None)
    assert title == "No Active Game"
    assert "no game running" in body.lower()


def test_describe_active_game_with_row_includes_all_fields():
    row = {
        "game_type": "traditional",
        "state": "open",
        "host_id": 12345,
        "game_id": "abc-def",
    }
    title, body = describe_active_game(row, name_fn=lambda uid: f"User{uid}")
    assert title == "Active Game"
    assert "traditional" in body
    assert "open" in body
    # Mod-facing: the host's name with the id alongside, never a <@id> (a
    # Text Display mention renders as digits to a client that hasn't cached
    # them).
    assert "User12345 (`12345`)" in body
    assert "<@12345>" not in body
    assert "abc-def" in body


def test_describe_active_game_default_resolver_keeps_a_mention():
    """An un-wired caller still renders; the AST test below forces the cog
    to pass a resolver."""
    row = {"game_type": "wyr", "state": "open", "host_id": 7, "game_id": "g"}
    _title, body = describe_active_game(row)
    assert "<@7> (`7`)" in body


# ── describe_force_end ───────────────────────────────────────────────


def test_describe_force_end_mentions_game_type():
    assert "traditional" in describe_force_end("traditional")
    assert "ama" in describe_force_end("ama")


def test_describe_force_end_mentions_admin_or_mod():
    body = describe_force_end("ttl")
    assert "admin" in body.lower() or "mod" in body.lower()


# ── permission predicates ────────────────────────────────────────────


def test_has_admin_permissions_true_for_administrator():
    perms = SimpleNamespace(administrator=True)
    assert has_admin_permissions(perms) is True


def test_has_admin_permissions_false_for_non_admin():
    perms = SimpleNamespace(administrator=False)
    assert has_admin_permissions(perms) is False


def test_has_admin_permissions_false_when_attribute_missing():
    """Defensive: a stripped-down stub mustn't crash the predicate."""
    perms = SimpleNamespace()
    assert has_admin_permissions(perms) is False


@pytest.mark.parametrize(
    "perms,expected",
    [
        (SimpleNamespace(administrator=True, manage_guild=False, manage_channels=False), True),
        (SimpleNamespace(administrator=False, manage_guild=True, manage_channels=False), True),
        (SimpleNamespace(administrator=False, manage_guild=False, manage_channels=True), True),
        (SimpleNamespace(administrator=False, manage_guild=False, manage_channels=False), False),
    ],
)
def test_has_mod_or_admin_permissions_accepts_any_elevated_perm(perms, expected):
    assert has_mod_or_admin_permissions(perms) is expected


def test_has_mod_or_admin_permissions_false_when_perms_is_none():
    assert has_mod_or_admin_permissions(None) is False


# ── channel_ids_from_rows ────────────────────────────────────────────


def test_channel_ids_from_rows_empty():
    assert channel_ids_from_rows([]) == []


def test_channel_ids_from_rows_projects_first_column():
    rows = [(101,), (202,), (303,)]
    assert channel_ids_from_rows(rows) == [101, 202, 303]


def test_channel_ids_from_rows_coerces_to_int():
    rows = [("404",), ("505",)]
    assert channel_ids_from_rows(rows) == [404, 505]


# ── layout builders ──────────────────────────────────────────────────


def test_build_game_status_layout_no_active_game():
    items = build_game_status_layout(None)
    text = _text_of(items)
    assert text.startswith("## 💤 No Active Game")


def test_build_game_status_layout_renders_row_fields():
    row = {
        "game_type": "wyr",
        "state": "playing",
        "host_id": 42,
        "game_id": "xx-yy",
    }
    items = build_game_status_layout(row, name_fn=lambda uid: f"User{uid}")
    text = _text_of(items)
    assert text.startswith("## 🎲 Active Game")
    assert "wyr" in text
    assert "playing" in text
    assert "User42 (`42`)" in text
    assert "<@42>" not in text


def test_game_status_render_site_passes_a_resolver():
    """``name_fn`` defaults to ``mention``; the cog must hand a resolver over
    or the mod card shows the host as digits."""
    import ast
    import inspect
    import pathlib

    from bot_modules.games.config import cog as games_config_cog

    source = pathlib.Path(inspect.getfile(games_config_cog)).read_text(
        encoding="utf-8"
    )
    missed = [
        f"games_config_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_game_status_layout"
        and not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


def test_build_force_end_layout_names_the_game_type():
    items = build_force_end_layout("traditional")
    text = _text_of(items)
    assert "traditional" in text
    assert text.startswith("## 🛑 Game Force-Closed")


# ── sanity: builders return real discord.ui items ──────────────────────


def test_all_builders_return_text_displays():
    """Smoke-test: every builder produces real items (not a MagicMock leak)."""
    import discord

    for items in (
        build_game_status_layout(None),
        build_force_end_layout("x"),
    ):
        assert items
        for item in items:
            assert isinstance(item, discord.ui.Item)


# ── /games join dispatches a ping it cannot read ────────────────────
#
# The message is built by whichever cog registered the game's roster handler
# (Clapback tells a bumped bye player they are back in, with a raw <@id>) and
# comes back through ``bot.game_joiners[game_type]`` — a different file, a
# different class, resolved at runtime. The bot pings nobody unless the send
# says so, so the handler returns the ids alongside its text.

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import discord  # noqa: E402

import bot_modules.games.config.cog as games_config_cog  # noqa: E402


async def _dispatch_join(monkeypatch, handler):
    member = MagicMock(spec=discord.Member)
    member.id = 7
    member.display_name = "Nora"

    bot = SimpleNamespace(
        games_db=None, game_joiners={"clapback": handler}, game_leavers={},
    )
    cog = games_config_cog.GamesConfigCog(bot)  # type: ignore[arg-type]

    async def _get_active_game(db, channel_id):
        return {"game_type": "clapback", "game_id": "g1", "host_id": 7}

    monkeypatch.setattr(games_config_cog, "get_active_game", _get_active_game)

    interaction = MagicMock()
    interaction.user = member
    interaction.channel = SimpleNamespace(name="games", guild=None)
    interaction.channel_id = 100
    interaction.response.send_message = AsyncMock()

    await cog._membership_command(interaction, None, joining=True)
    return interaction.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_games_join_notifies_the_player_the_handler_named(monkeypatch):
    async def handler(channel, game_id, target):
        return True, "🪑 <@5> you're back in this round — hit **Submit**!", [5]

    kwargs = await _dispatch_join(monkeypatch, handler)

    # Public (the room sees the roster change) and it actually notifies — a
    # dropped allow-list here leaves the mention rendering as inert text.
    assert kwargs["ephemeral"] is False
    allowed = kwargs["allowed_mentions"]
    assert [o.id for o in allowed.users] == [5]
    assert allowed.everyone is False and allowed.roles == []


@pytest.mark.asyncio
async def test_games_join_pings_nobody_when_the_handler_names_nobody(monkeypatch):
    """A handler that returns the plain two-element contract gets a fully
    suppressed send — including a display name a member wrote themselves,
    which is why the allow-list is never scraped back out of the message."""
    async def handler(channel, game_id, target):
        return True, "🎮 **@everyone** joined Most Likely To!"

    allowed = (await _dispatch_join(monkeypatch, handler))["allowed_mentions"]

    assert not allowed.users and not allowed.roles and allowed.everyone is False
