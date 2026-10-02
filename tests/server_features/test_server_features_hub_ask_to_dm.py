"""The Help & Settings hub's 📨 Ask to DM row.

It is the DM Requests panel's **Open Form** button, moved onto the hub so
the panel's channel can be dropped (Ben, 2026-09-30). The row sends the
same ``DmRequestModal`` — never a copy of it — and answers the two guards
every borrowed row on the hub answers: not in a server, and the owning cog
not loaded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.dm.perms.cog import DmRequestModal
from bot_modules.server_features.cog import ServerFeaturesHubView


def _hub(dm_cog) -> ServerFeaturesHubView:
    bot = MagicMock()
    bot.get_cog = lambda name: dm_cog if name == "DmPermsCog" else None
    return ServerFeaturesHubView(SimpleNamespace(bot=bot))


def _interaction(*, member: bool = True) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member) if member else MagicMock(spec=discord.User)
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done.return_value = False
    return interaction


@pytest.mark.asyncio
async def test_ask_to_dm_row_sends_the_panels_request_modal():
    dm_cog = SimpleNamespace(bot=MagicMock())
    interaction = _interaction()

    await _hub(dm_cog).open_ask_to_dm(interaction)

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, DmRequestModal)
    assert modal.cog is dm_cog
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    ("dm_cog", "member"),
    [
        pytest.param(None, True, id="cog-not-loaded"),
        pytest.param(SimpleNamespace(bot=MagicMock()), False, id="not-in-a-server"),
    ],
)
@pytest.mark.asyncio
async def test_ask_to_dm_row_refuses_with_a_line_not_a_form(dm_cog, member):
    interaction = _interaction(member=member)

    await _hub(dm_cog).open_ask_to_dm(interaction)

    interaction.response.send_modal.assert_not_awaited()
    args, kwargs = interaction.response.send_message.await_args
    assert args[0].startswith("❌") and kwargs["ephemeral"] is True
