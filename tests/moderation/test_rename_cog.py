"""Cog tests for /rename's own confirmation message."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.moderation.cog_rename import RenameCog
from tests.fakes import fake_interaction

# Under RenameCog's own MAX_NICK_LENGTH (32) — a nickname is capped before
# it ever reaches the confirmation this test targets.
RAW_NICK = "[Nitro](https://evil.example)"


def _setup():
    bot = MagicMock()
    cog = RenameCog(bot)

    target = MagicMock(spec=discord.Member)
    target.mention = "<@999>"
    target.display_name = "Old Name"
    target.top_role = 1
    target.edit = AsyncMock()

    guild = MagicMock(spec=discord.Guild)
    bot_member = MagicMock(spec=discord.Member)
    bot_member.guild_permissions = MagicMock(manage_nicknames=True)
    bot_member.top_role = 2  # above target.top_role, so the hierarchy gate passes
    guild.me = bot_member
    guild.owner = MagicMock()  # distinct object from target

    interaction = fake_interaction(guild=guild)
    interaction.guild = guild
    bot.ctx = MagicMock()
    bot.ctx.is_mod = MagicMock(return_value=True)

    return cog, interaction, target


@pytest.mark.asyncio
async def test_rename_escapes_the_new_nickname_in_its_confirmation():
    """The confirmation already escapes the sibling ``old_display`` value in
    the same bold-wrapped f-string (a couple lines above) — the new ``nick``
    must match, or a moderator's own typed nickname can misrender the bold
    wrapper in their own ephemeral confirmation.
    """
    cog, interaction, target = _setup()

    await cog.rename.callback(cog, interaction, target, RAW_NICK)

    target.edit.assert_awaited_once()
    assert target.edit.await_args.kwargs["nick"] == RAW_NICK  # write-back stays raw

    msg = interaction.response.send_message.await_args.args[0]
    escaped_nick = discord.utils.escape_markdown(RAW_NICK)
    escaped_old = discord.utils.escape_markdown(target.display_name)
    assert msg == f"Renamed {target.mention} from **{escaped_old}** to **{escaped_nick}**."
    assert f"**{RAW_NICK}**" not in msg


@pytest.mark.asyncio
async def test_rename_reset_still_escapes_the_old_display_name():
    """A blank ``new_name`` resets the nickname — a different branch of the
    same confirmation, already escaped, must stay untouched by this fix.
    """
    cog, interaction, target = _setup()
    target.display_name = "__old__"

    await cog.rename.callback(cog, interaction, target, None)

    msg = interaction.response.send_message.await_args.args[0]
    assert msg == f"Reset {target.mention}'s nickname (was **\\_\\_old\\_\\_**)."
