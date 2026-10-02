"""Posted panels redraw themselves — the registry, the posted_panels record and
the runner (docs/plans/panel-refresh.md).

What matters most is what a refresh must *not* do: post a panel that isn't
there, keep a row for a message a moderator deleted, or let one broken panel
stop every other panel from redrawing.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core import panel_refresh
from bot_modules.core.sticky import PanelContent
from bot_modules.panels import refresh_service as svc
from db_template import migrated_db

GUILD = 1469491362444480666
CHANNEL = 1531999999999999638
MESSAGE = 1539999999999999847


@pytest.fixture
def db(tmp_path):
    return migrated_db(tmp_path / "panels.db")


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    monkeypatch.setattr(panel_refresh, "_REFRESHERS", {})


def _content() -> PanelContent:
    return PanelContent(embed=discord.Embed(title="panel"))


def _not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404), "gone")


def _channel(*, edit_error: Exception | None = None):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHANNEL
    message = MagicMock()
    message.edit = AsyncMock(side_effect=edit_error)
    channel.get_partial_message = MagicMock(return_value=message)
    sent = MagicMock()
    sent.id = MESSAGE + 1
    channel.send = AsyncMock(return_value=sent)
    return channel, message


def _guild(channel=None):
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel.return_value = channel
    return guild


def _post(post_id: int = 1, key: str = "ticket-panel", options=None):
    return svc.PostedPanel(
        id=post_id, guild_id=GUILD, panel_key=key, channel_id=CHANNEL,
        message_id=MESSAGE, options=options or {},
    )


# ── the registry ──────────────────────────────────────────────────────


def test_register_replaces_an_entry_of_the_same_name():
    first, second = AsyncMock(), AsyncMock()
    panel_refresh.register("x", first)
    panel_refresh.register("x", second)
    assert panel_refresh.registered() == [("x", second)]


def test_unregister_leaves_a_newer_instance_alone():
    """A reloaded cog registers its new instance before the old one's
    unload runs; the old unload must not take the new entry with it."""
    old, new = AsyncMock(), AsyncMock()
    panel_refresh.register("x", new)
    panel_refresh.unregister("x", old)
    assert panel_refresh.registered() == [("x", new)]
    panel_refresh.unregister("x", new)
    assert panel_refresh.registered() == []


# ── the store ─────────────────────────────────────────────────────────


def test_record_find_list_delete_round_trip(db):
    with sqlite3.connect(db) as conn:
        svc.record_post(conn, GUILD, "grant-audit", CHANNEL, MESSAGE, {"min_level": 5})
        found = svc.find_post(conn, GUILD, "grant-audit", CHANNEL)
        assert found is not None
        # Snowflakes past 2^53 survive exactly.
        assert (found.channel_id, found.message_id) == (CHANNEL, MESSAGE)
        assert found.options == {"min_level": 5}
        assert svc.list_posts(conn, GUILD) == [found]
        assert svc.list_posts(conn, GUILD + 1) == []
        svc.delete_post(conn, found.id)
        assert svc.find_post(conn, GUILD, "grant-audit", CHANNEL) is None


def test_a_second_post_in_the_same_channel_replaces_the_row(db):
    with sqlite3.connect(db) as conn:
        svc.record_post(conn, GUILD, "ask-panel", CHANNEL, MESSAGE)
        svc.record_post(conn, GUILD, "ask-panel", CHANNEL, MESSAGE + 7)
        svc.record_post(conn, GUILD, "ask-panel", CHANNEL + 1, MESSAGE + 9)
        posts = svc.list_posts(conn, GUILD)
    assert [(p.channel_id, p.message_id) for p in posts] == [
        (CHANNEL, MESSAGE + 7), (CHANNEL + 1, MESSAGE + 9),
    ]


# ── refresh_posted ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_recorded_panel_is_edited_in_place():
    channel, message = _channel()
    render = AsyncMock(return_value=_content())
    forget = AsyncMock()
    edited = await svc.refresh_posted(
        _guild(channel), [_post(options={"a": 1})], render, forget
    )
    assert edited == 1
    assert render.await_args.args[1:] == ("ticket-panel", {"a": 1})
    channel.get_partial_message.assert_called_once_with(MESSAGE)
    message.edit.assert_awaited_once()
    channel.send.assert_not_awaited()
    forget.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gone",
    [pytest.param("message", id="message-404"), pytest.param("channel", id="channel-gone")],
)
async def test_a_deleted_panel_is_forgotten_never_reposted(gone):
    channel, message = _channel(edit_error=_not_found())
    guild = _guild(None if gone == "channel" else channel)
    forget = AsyncMock()
    edited = await svc.refresh_posted(
        guild, [_post(post_id=4)], AsyncMock(return_value=_content()), forget
    )
    assert edited == 0
    forget.assert_awaited_once_with(4)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_transient_error_keeps_the_row_and_the_walk_goes_on():
    channel, message = _channel()
    message.edit = AsyncMock(
        side_effect=[discord.HTTPException(MagicMock(status=500), "x"), None]
    )
    forget = AsyncMock()
    edited = await svc.refresh_posted(
        _guild(channel), [_post(1), _post(2)], AsyncMock(return_value=_content()), forget
    )
    assert edited == 1
    forget.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "render",
    [
        pytest.param(AsyncMock(return_value=None), id="nothing-to-draw"),
        pytest.param(AsyncMock(side_effect=RuntimeError("boom")), id="build-raises"),
    ],
)
async def test_a_panel_that_cannot_be_built_is_left_alone(render):
    channel, message = _channel()
    forget = AsyncMock()
    assert await svc.refresh_posted(_guild(channel), [_post()], render, forget) == 0
    message.edit.assert_not_awaited()
    forget.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_posted_refresher_renders_through_the_spec_and_retires_404s(db):
    """End to end over a real table: a spec with ``render`` is drawn by its
    cog; a key with no render (or no loaded cog) is left as posted; a 404
    deletes the row."""
    with sqlite3.connect(db) as conn:
        svc.record_post(conn, GUILD, "ticket-panel", CHANNEL, MESSAGE)
        svc.record_post(conn, GUILD, "grant-audit", CHANNEL, MESSAGE + 1)
        svc.record_post(conn, GUILD, "ask-panel", CHANNEL, MESSAGE + 2)
    channel, message = _channel()
    cog = MagicMock()
    cog.build_ticket_panel = AsyncMock(return_value=_content())
    bot = MagicMock()
    bot.get_cog.side_effect = lambda name: cog if name == "JailCog" else None

    refresh = svc.posted_panels_refresher(bot, db, spacing=0)
    assert await refresh(_guild(channel)) == 1
    cog.build_ticket_panel.assert_awaited_once()

    message.edit = AsyncMock(side_effect=_not_found())
    assert await refresh(_guild(channel)) == 0
    with sqlite3.connect(db) as conn:
        keys = {p.panel_key for p in svc.list_posts(conn, GUILD)}
    assert keys == {"grant-audit", "ask-panel"}


@pytest.mark.asyncio
async def test_refresh_panel_key_redraws_only_that_panel(db):
    """The Mod Settings panel redraws on a dashboard save; the other panels'
    copies wait for the boot refresh, and nothing is posted when there is no
    copy."""
    guild_empty = _guild(_channel()[0])
    bot = MagicMock()
    assert await svc.refresh_panel_key(bot, db, guild_empty, "mod-settings") == 0
    bot.get_cog.assert_not_called()

    with sqlite3.connect(db) as conn:
        svc.record_post(conn, GUILD, "mod-settings", CHANNEL, MESSAGE)
        svc.record_post(conn, GUILD, "ticket-panel", CHANNEL, MESSAGE + 1)
    channel, _message = _channel()
    mods, jail = MagicMock(), MagicMock()
    mods.build_mod_settings_panel = AsyncMock(return_value=_content())
    jail.build_ticket_panel = AsyncMock(return_value=_content())
    bot.get_cog.side_effect = {"ModSettingsCog": mods, "JailCog": jail}.get

    assert await svc.refresh_panel_key(bot, db, _guild(channel), "mod-settings") == 1
    mods.build_mod_settings_panel.assert_awaited_once()
    jail.build_ticket_panel.assert_not_awaited()


# ── the runner ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_broken_refresher_never_stops_the_rest():
    broken = AsyncMock(side_effect=RuntimeError("boom"))
    fine = AsyncMock(return_value=True)
    also = AsyncMock(return_value=2)
    report = await svc.refresh_guild(
        _guild(), [("broken", broken), ("fine", fine), ("also", also)]
    )
    assert report.edited == 3
    assert report.failed == ["broken"]
    fine.assert_awaited_once()
    also.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_all_walks_every_guild_through_the_registry():
    seen: list[int] = []

    async def refresher(guild):
        seen.append(guild.id)
        return 1

    panel_refresh.register("p", refresher)
    a, b = _guild(), _guild()
    b.id = GUILD + 1
    report = await svc.refresh_all([a, b], reason="test", spacing=0)
    assert seen == [GUILD, GUILD + 1]
    assert report.edited == 2


# ── post_and_record ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_post_is_recorded_and_a_second_one_moves_it(db):
    """Posting again is how an admin brings a scrolled-off panel back down:
    the new copy goes up, then the channel's old copy is deleted."""
    channel, old = _channel()
    old.delete = AsyncMock()
    guild = _guild(channel)
    post = AsyncMock(side_effect=[MagicMock(id=MESSAGE), MagicMock(id=MESSAGE + 1)])

    await svc.post_and_record(db, guild, channel, "ask-panel", {}, post=post)
    old.delete.assert_not_awaited()
    await svc.post_and_record(db, guild, channel, "ask-panel", {}, post=post)

    channel.get_partial_message.assert_called_once_with(MESSAGE)
    old.delete.assert_awaited_once()
    with sqlite3.connect(db) as conn:
        [row] = svc.list_posts(conn, GUILD)
    assert row.message_id == MESSAGE + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_not_found(), id="already-gone"),
        pytest.param(TimeoutError(), id="network-timeout"),
    ],
)
async def test_an_old_copy_that_will_not_delete_still_records_the_new_one(db, error):
    with sqlite3.connect(db) as conn:
        svc.record_post(conn, GUILD, "ask-panel", CHANNEL, MESSAGE)
    channel, old = _channel()
    old.delete = AsyncMock(side_effect=error)
    await svc.post_and_record(
        db, _guild(channel), channel, "ask-panel", {"k": 1},
        post=AsyncMock(return_value=MagicMock(id=MESSAGE + 5)),
    )
    with sqlite3.connect(db) as conn:
        row = svc.find_post(conn, GUILD, "ask-panel", CHANNEL)
    assert (row.message_id, row.options) == (MESSAGE + 5, {"k": 1})


@pytest.mark.asyncio
async def test_a_refused_post_keeps_the_old_copy_and_its_row(db):
    with sqlite3.connect(db) as conn:
        svc.record_post(conn, GUILD, "ask-panel", CHANNEL, MESSAGE)
    channel, old = _channel()
    old.delete = AsyncMock()
    result = await svc.post_and_record(
        db, _guild(channel), channel, "ask-panel", {},
        post=AsyncMock(return_value=None),
    )
    assert result is None
    old.delete.assert_not_awaited()
    with sqlite3.connect(db) as conn:
        assert svc.find_post(conn, GUILD, "ask-panel", CHANNEL).message_id == MESSAGE
