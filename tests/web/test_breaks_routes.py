"""Take a Break's dashboard routes: settings, setup, the list and End break."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_modules.breaks import service
from bot_modules.breaks.store import (
    BREAK_CHANNEL_KEY,
    BREAK_ROLE_KEY,
    SPICY_LITE_ROLE_KEY,
    create_break,
    get_break,
)
from bot_modules.core.db_utils import get_config_value, open_db

BIG = 1476000000000000484  # > 2**53: must travel as a string


def _admin():
    """The signed-in dashboard user (id 1) as the live guild sees them —
    auth resolves perms from the gateway member once a bot is attached."""
    m = MagicMock()
    m.roles = []
    m.guild_permissions.value = 0x8
    m.display_name = "tester"
    return m


@pytest.fixture
def guild(fake_ctx):
    """A live guild in which the breaking member (id 77) is absent."""
    g = MagicMock()
    g.id = fake_ctx.guild_id
    g.members = {1: _admin()}
    g.get_member = MagicMock(side_effect=lambda uid: g.members.get(uid))
    bot = MagicMock()
    bot.get_guild = MagicMock(side_effect=lambda gid: g if gid == g.id else None)
    bot.get_cog = MagicMock(return_value=None)
    fake_ctx.bot = bot
    g._bot = bot
    return g


def _open_break(fake_ctx, uid=BIG):
    with open_db(fake_ctx.db_path) as conn:
        create_break(conn, guild_id=fake_ctx.guild_id, user_id=uid, stored_roles=[7], swapped_spicy=True)


def test_the_page_reads_dark_until_set_up_and_lists_breaks_as_strings(authed_client, fake_ctx):
    body = authed_client.get("/api/breaks").json()
    assert body["config"]["enabled"] is False
    assert body["breaks"] == []

    fake_ctx.set_config_value(BREAK_ROLE_KEY, str(BIG))
    fake_ctx.set_config_value(BREAK_CHANNEL_KEY, "42")
    _open_break(fake_ctx)
    body = authed_client.get("/api/breaks").json()
    assert body["config"]["enabled"] is True
    assert body["config"]["break_role_id"] == str(BIG)
    assert body["breaks"][0]["user_id"] == str(BIG)
    assert body["breaks"][0]["roles"] == 1
    assert body["breaks"][0]["swapped_spicy"] is True


def _lite(fake_ctx) -> str:
    with open_db(fake_ctx.db_path) as conn:
        return get_config_value(conn, SPICY_LITE_ROLE_KEY, "0", fake_ctx.guild_id)


@pytest.mark.parametrize(
    ("value", "status", "stored"),
    [
        pytest.param("555", 200, "555", id="picked"),
        pytest.param("0", 200, "0", id="none-means-no-swap"),
        pytest.param("900", 400, "0", id="same-as-break-role"),
        pytest.param("spicy", 400, "0", id="not-an-id"),
    ],
)
def test_spicy_lite_role_save(authed_client, fake_ctx, value, status, stored):
    fake_ctx.set_config_value(BREAK_ROLE_KEY, "900")
    resp = authed_client.put("/api/breaks/config", json={"spicy_lite_role_id": value})
    assert resp.status_code == status
    assert _lite(fake_ctx) == stored


def test_moving_the_spicy_lite_dial_retires_the_old_role(authed_client, fake_ctx):
    """Members on a break still wear the old one; it must keep counting."""
    from bot_modules.breaks.store import read_retired_role_ids

    fake_ctx.set_config_value(BREAK_ROLE_KEY, "900")
    fake_ctx.set_config_value(SPICY_LITE_ROLE_KEY, "555")
    assert authed_client.put(
        "/api/breaks/config", json={"spicy_lite_role_id": "556"}
    ).status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert read_retired_role_ids(conn, fake_ctx.guild_id) == {555}


def test_setup_is_503_when_the_cog_is_not_loaded(authed_client, guild):
    import discord

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 42
    guild.get_channel = MagicMock(return_value=channel)
    resp = authed_client.post("/api/breaks/setup", json={"channel_id": "42"})
    assert resp.status_code == 503


def test_setup_posts_the_cogs_bound_view(authed_client, guild, monkeypatch):
    import discord

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 42
    guild.get_channel = MagicMock(return_value=channel)
    view = object()
    guild_cog = MagicMock()
    # Async since 2026-09-27: the view reads the guild's panel art off the loop.
    guild_cog.return_panel_view = AsyncMock(return_value=view)
    setup = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(service, "setup_break_channel", setup)

    bot = guild._bot
    bot.get_cog = MagicMock(return_value=guild_cog)
    resp = authed_client.post("/api/breaks/setup", json={"channel_id": "42"})
    assert resp.status_code == 200
    assert setup.await_args.args[2] is channel
    assert setup.await_args.kwargs == {"return_panel": view}
    assert guild_cog.return_panel_view.await_args.args == (guild,)


def test_end_break_for_a_member_who_left_clears_the_row_once(authed_client, fake_ctx, guild):
    _open_break(fake_ctx, uid=77)
    resp = authed_client.post("/api/breaks/77/end")
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert get_break(conn, fake_ctx.guild_id, 77) is None
    assert authed_client.post("/api/breaks/77/end").status_code == 404


def test_end_break_for_a_present_member_goes_through_the_service_as_the_mod(
    authed_client, guild, monkeypatch
):
    member = MagicMock()
    guild.members[77] = member
    end = AsyncMock(return_value=service.BreakResult(True, "Welcome back!"))
    monkeypatch.setattr(service, "end_break", end)

    resp = authed_client.post("/api/breaks/77/end")
    assert resp.status_code == 200
    assert end.await_args.args[1] is member
    assert end.await_args.kwargs == {"actor_id": 1}  # the signed-in moderator


def test_a_failed_end_surfaces_the_services_words(authed_client, guild, monkeypatch):
    guild.members[77] = MagicMock()
    monkeypatch.setattr(
        service, "end_break", AsyncMock(return_value=service.BreakResult(False, "kept"))
    )
    resp = authed_client.post("/api/breaks/77/end")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "kept"
