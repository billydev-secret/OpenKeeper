"""_SetupWizardView._finish_setup: role-safety gate (2026-09-14).

The wellness role is a dashboard dial (``wellness_config.role_id``). A role
that gains elevated permissions, becomes managed, or ends up above the bot
after it was picked must not be handed to the next member who finishes the
setup wizard — mirrors the same fix applied across every other config-sourced
grant in this PR (see tests/contracts/test_role_grant_safety_contract.py).
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.wellness import cog as wellness_cog
from bot_modules.wellness.cog import _SetupWizardView
from bot_modules.wellness.service import WellnessConfig

GUILD_ID = 9001
ROLE_ID = 555
USER_ID = 42


class _Role:
    def __init__(self, position: int = 1, *, elevated: bool = False, managed: bool = False) -> None:
        self.id = ROLE_ID
        self.name = "Wellness Guardian"
        self.position = position
        self.managed = managed
        self.permissions = (
            discord.Permissions(manage_messages=True) if elevated else discord.Permissions.none()
        )

    def is_default(self) -> bool:
        return False

    def __ge__(self, other: "_Role") -> bool:
        return self.position >= other.position


def _view_and_guild(role, monkeypatch):
    ctx = SimpleNamespace(open_db=lambda: nullcontext(MagicMock()))
    monkeypatch.setattr(
        wellness_cog,
        "get_wellness_config",
        lambda _conn, _gid: WellnessConfig(
            guild_id=GUILD_ID,
            role_id=ROLE_ID,
            channel_id=0,
            active_list_message_id=0,
        ),
    )
    opt_in = MagicMock()
    monkeypatch.setattr(wellness_cog, "opt_in_user", opt_in)

    member = MagicMock(spec=discord.Member)
    member.id = USER_ID
    member.add_roles = AsyncMock()

    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_member = MagicMock(return_value=member)
    guild.get_role = MagicMock(return_value=role)
    guild.me = SimpleNamespace(top_role=_Role(position=10))

    view = _SetupWizardView(ctx, USER_ID)
    view._timezone = "UTC"
    view._enforcement = "gentle"
    return view, guild, member, opt_in


@pytest.mark.asyncio
async def test_finish_setup_refuses_an_elevated_role(monkeypatch):
    role = _Role(elevated=True)
    view, guild, member, opt_in = _view_and_guild(role, monkeypatch)
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()

    await view._finish_setup(interaction, guild)

    member.add_roles.assert_not_awaited()
    opt_in.assert_not_called()
    content = interaction.response.edit_message.call_args.kwargs["content"]
    assert "can't be handed out" in content


@pytest.mark.asyncio
async def test_finish_setup_grants_a_plain_role(monkeypatch):
    role = _Role(elevated=False)
    view, guild, member, opt_in = _view_and_guild(role, monkeypatch)
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()

    await view._finish_setup(interaction, guild)

    member.add_roles.assert_awaited_once_with(role, reason="Wellness Guardian opt-in")
    opt_in.assert_called_once()
