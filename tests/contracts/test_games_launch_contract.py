"""``launch`` must tell the truth about whether a game is live.

The scheduler reads that return value as "did this work": a truthy id marks the
row ``launched`` and, since todo #97, fires the announcement. Two games used to
return an id for a game that had already ended itself — an empty question bank
posted a notice, called ``end_game``, and unwound normally — which put a
``🎮 X is starting now!`` ping directly beneath "the bank is empty".

Since 2026-09-04 an empty bank no longer ends the game at all (vote-games-50):
the round opens *waiting* for a posed prompt, with only Pose, End and Help
live. That board is a real, live game, so ``launch`` reports its id and the
row survives; ``None`` is reserved for a launch that posted nothing.
"""

import discord
import pytest

from bot_modules.games.nhie.cog import NHIECog
from bot_modules.games.wyr.cog import WYRCog
from bot_modules.games.db import GamesDb


class _FakeMessage:
    id = 5555

    async def edit(self, **kwargs):
        return None


class _FakeChannel:
    guild = None
    id = 4242
    name = "games"

    def __init__(self):
        self.sends: list = []

    async def send(self, *args, **kwargs):
        # The waiting board is a Components V2 card (2026-09-30): its text
        # is the card's Text Displays, not an embed description.
        view = kwargs.get("view")
        if view is not None:
            self.sends.append("\n".join(
                i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
            ))
        else:
            self.sends.append(kwargs.get("embed") or (args[0] if args else kwargs.get("content")))
        return _FakeMessage()


class _FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}

    def get_channel(self, cid: int):
        return None


@pytest.mark.parametrize(
    "cog_cls, game_type, pose_label",
    [
        pytest.param(WYRCog, "wyr", "Pose Question", id="wyr"),
        pytest.param(NHIECog, "nhie", "Pose Statement", id="nhie"),
    ],
)
async def test_launch_with_an_empty_bank_opens_a_waiting_round(
    sync_db_path, cog_cls, game_type, pose_label
):
    """The board is live (waiting for a posed prompt), so the launch reports it."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = cog_cls(bot)  # type: ignore[arg-type]
    channel = _FakeChannel()

    gid = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester", guild_id=9001, options={},
    )

    assert gid is not None, "a waiting board is a live game and must report as launched"
    assert pose_label in channel.sends[0], "expected the waiting notice"
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE channel_id = ?", (channel.id,)
    )
    assert row is not None
    view = bot.active_views[gid]
    assert view.waiting is True
    # A waiting round carries no vote buttons and no Next — Pose and Help only.
    ids = [i.custom_id for i in view.walk_children() if getattr(i, "custom_id", None)]
    assert ids == [f"{game_type}_htp", f"{game_type}_pose"]
    # End Game is the host panel's (Ben, 2026-09-30), and it answers on a
    # waiting round too — the panel reaches the live view by game id.
    assert hasattr(view, "press_end")


async def test_launch_returns_none_when_the_board_could_not_be_posted(sync_db_path):
    """The other half of the contract: no board, no id, no row."""
    from unittest.mock import MagicMock

    import discord

    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = WYRCog(bot)  # type: ignore[arg-type]

    class _Forbidden(_FakeChannel):
        async def send(self, *args, **kwargs):
            raise discord.Forbidden(MagicMock(status=403), "nope")

    gid = await cog.launch(
        channel=_Forbidden(), host_id=2001, host_name="Tester", guild_id=9001, options={},
    )
    assert gid is None
    assert await db.fetchone("SELECT * FROM games_active_games WHERE channel_id = 4242") is None
