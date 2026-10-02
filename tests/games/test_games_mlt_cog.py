"""Cog/view tests for Most Likely To.

The logic layer (lobby membership, vote recording, tallies, embeds) is
covered by ``tests/games/test_games_mlt_logic.py``. Only the glue that has
actually been wrong lives here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.games.mlt.cog import MLTVoteView
from tests.fakes import fake_interaction


def _view(*, guild) -> MLTVoteView:
    db = MagicMock()
    bot = MagicMock()
    bot.ctx = MagicMock()
    return MLTVoteView(
        game_id="g1",
        host_id=1,
        prompt="who's most likely to...",
        round_num=1,
        players=[10, 20],
        db=db,
        bot=bot,
        host_name="Host",
        guild=guild,
        advance_callback=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_vote_confirmation_escapes_the_voted_for_members_name():
    """The ephemeral "Voted for **name**" ack is the one place in this file
    that still read a raw ``display_name`` — every embed builder here routes
    names through the escaped ``build_name_fn`` resolver, and this ack must
    match or a markdown nickname renders as bot-authored formatting in every
    voter's own confirmation.
    """
    target = MagicMock(spec=discord.Member)
    target.id = 20
    target.display_name = "[Free Nitro](https://evil.example/phish)"
    guild = MagicMock(spec=discord.Guild)
    guild.get_member = lambda uid: target if uid == 20 else None

    view = _view(guild=guild)
    interaction = fake_interaction(
        data={"values": ["20"]},
        guild_id=5,
    )
    interaction.user.id = 10

    with patch(
        "bot_modules.games.mlt.cog.modify_payload",
        new=AsyncMock(),
    ), patch(
        "bot_modules.games.mlt.cog.asyncio.to_thread",
        new=AsyncMock(return_value=False),
    ):
        await view._vote_select_callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.await_args.args[0]
    escaped = discord.utils.escape_markdown(target.display_name)
    assert msg == f"✅ Voted for **{escaped}**"
    # escape_markdown only prefixes the leading `[` of a masked link, so a
    # substring check against the raw text would pass even unescaped —
    # compare the exact message instead (done above); this is a belt-and-
    # braces check on the specific failure mode.
    assert f"**{target.display_name}**" not in msg


@pytest.mark.asyncio
async def test_vote_confirmation_falls_back_to_the_bare_id_off_guild_cache():
    """No guild, or a target not in the member cache, still acks — as
    ``User <id>``, the plain form every game surface uses for a member
    nobody can name.
    """
    view = _view(guild=None)
    interaction = fake_interaction(data={"values": ["999999"]}, guild_id=5)
    interaction.user.id = 10

    with patch(
        "bot_modules.games.mlt.cog.modify_payload",
        new=AsyncMock(),
    ), patch(
        "bot_modules.games.mlt.cog.asyncio.to_thread",
        new=AsyncMock(return_value=False),
    ):
        await view._vote_select_callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert msg == "✅ Voted for **User 999999**"
