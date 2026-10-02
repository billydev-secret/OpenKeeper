"""Headless game-launch seam: the scheduler launches games with no Interaction.

Proves WYRCog.launch() works without a Discord interaction — the contract every
party cog's launcher must satisfy for the scheduler to drive it.
"""

from types import SimpleNamespace

from bot_modules.games.ama.cog import AMACog
from bot_modules.games.clapback.cog import ClapbackCog
from bot_modules.games.price.cog import PriceCog
import discord

from bot_modules.games.wyr.cog import WYRCog
from bot_modules.core.db_utils import open_db
from bot_modules.games.db import GamesDb


class _FakeMessage:
    id = 555
    jump_url = "http://discord/x"


class _FakeChannel:
    id = 4242
    name = "games"

    def __init__(self, with_guild: bool = False):
        # A scheduled launch always lands in a guild channel. The games that
        # only read ``channel.guild`` to colour an embed are happy without
        # one and leave it None; AMA's sticky bottom bar needs a real guild,
        # so it asks for one.
        self.sends: list[tuple] = []
        self.guild = (
            SimpleNamespace(
                id=9001,
                roles=(),
                # None so the embed accent falls back to the default rather
                # than trying to read the bot member's avatar.
                me=None,
                # What core.sticky resolves a stored panel id through.
                get_channel_or_thread=(
                    lambda cid: self if cid == _FakeChannel.id else None
                ),
            )
            if with_guild
            else None
        )

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return _FakeMessage()


class _FakeBot:
    def __init__(self, db: GamesDb, db_path=None):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}
        # ``ctx.open_db`` matches AppContext's, for the sticky placer: it
        # persists a panel's ids through a sync callback run off-thread, so
        # it needs a connection of its own rather than the async GamesDb.
        self.ctx = SimpleNamespace(
            db_path=db_path,
            open_db=(lambda: open_db(db_path)) if db_path is not None else None,
        )


async def test_wyr_headless_launch_creates_active_game(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = WYRCog(bot)  # type: ignore[arg-type]
    channel = _FakeChannel()

    # Custom question bypasses the question bank, guaranteeing a round posts
    # (an empty bank would take the bank-empty branch and end the game).
    game_id = await cog.launch(
        channel=channel,
        host_id=2001,
        host_name="Tester",
        guild_id=9001,
        options={"question": "fly | be invisible"},
    )

    assert game_id is not None

    # A round message was posted: a Components V2 card, which is its own
    # interactive view and carries no embed.
    assert channel.sends, "launch posted no message"
    _, kwargs = channel.sends[-1]
    assert isinstance(kwargs["view"], discord.ui.LayoutView)
    assert "embed" not in kwargs

    # The active-game row exists for the channel (so the busy-check sees it).
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE channel_id = ?", (channel.id,)
    )
    assert row is not None
    assert row["game_type"] == "wyr"
    assert row["host_id"] == 2001

    # The view is tracked for interaction routing.
    assert game_id in bot.active_views


async def _assert_active_row(db, channel_id, game_type):
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE channel_id = ?", (channel_id,)
    )
    assert row is not None, f"{game_type}: no active game row created"
    assert row["game_type"] == game_type


async def test_ama_headless_launch(sync_db_path):
    # AMA posts two messages (main view + bottom bar) — both must go via channel.send.
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db, sync_db_path)
    cog = AMACog(bot)  # type: ignore[arg-type]
    channel = _FakeChannel(with_guild=True)

    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester",
        guild_id=9001, options={"mode": "unfiltered"},
    )

    assert game_id is not None
    assert len(channel.sends) >= 2  # main view + bottom bar
    # The bar is a line of text under three buttons, not a second embed.
    _, bar_kwargs = channel.sends[-1]
    assert bar_kwargs["content"] and bar_kwargs["embed"] is None
    await _assert_active_row(db, channel.id, "ama")


async def test_ama_panel_format_launch(sync_db_path):
    # Open-panel format: the game stores format=panel and the hot-seat-only
    # host controls are pruned from both the main view and the bottom bar.
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db, sync_db_path)
    cog = AMACog(bot)  # type: ignore[arg-type]
    channel = _FakeChannel(with_guild=True)

    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester",
        guild_id=9001, options={"mode": "unfiltered", "format": "panel"},
    )
    assert game_id is not None

    import json

    row = await db.fetchone(
        "SELECT payload FROM games_active_games WHERE channel_id = ?", (channel.id,)
    )
    assert json.loads(row["payload"])["format"] == "panel"

    main_view = bot.active_views[game_id]
    assert main_view.game_format == "panel"
    custom_ids = {getattr(c, "custom_id", None) for c in main_view.walk_children()}
    assert "ama_skip" not in custom_ids
    assert "ama_new_hs" not in custom_ids
    assert "ama_volunteer" in custom_ids and "ama_ask" in custom_ids

    bottom_view = bot.active_views[f"{game_id}_bottom"]
    bottom_ids = {getattr(c, "custom_id", None) for c in bottom_view.children}
    assert "ama_notify_toggle" not in bottom_ids
    assert "ama_bottom_ask" in bottom_ids and "ama_bottom_volunteer" in bottom_ids


async def test_clapback_headless_launch(sync_db_path):
    # Clapback is bank-only: launch() refuses when the bank is empty, so seed
    # one prompt first (mirrors the /games play clapback slash pre-check).
    db = GamesDb(sync_db_path)
    await db.execute(
        "INSERT INTO games_question_bank (game_type, question_text) VALUES ('clapback', 'Roast me')",
    )
    bot = _FakeBot(db)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    channel = _FakeChannel()

    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester",
        guild_id=9001, options={"source": "both"},
    )

    assert game_id is not None
    assert channel.sends
    await _assert_active_row(db, channel.id, "clapback")


async def test_price_headless_launch(sync_db_path):
    # Price posts an intro then create_task(_run_round). launch() returns before the
    # background round runs; we assert the synchronous setup (intro + active row).
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = PriceCog(bot)  # type: ignore[arg-type]
    channel = _FakeChannel()

    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester",
        guild_id=9001, options={"rounds": 3, "source": "bank"},
    )

    assert game_id is not None
    assert channel.sends
    await _assert_active_row(db, channel.id, "price")
