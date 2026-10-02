"""Cog-level: optin / optout."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.models import WhisperConfig
from tests.fakes import FakeMember, FakeRole, fake_interaction

ROLE_ID = 7001


def _cog_with_role():
    from bot_modules.whisper.cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    return cog


def _card_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


@pytest.mark.asyncio
async def test_optin_sends_confirmation_view():
    """/whisper optin shows a consent card with Opt In / Cancel rather than
    granting the role immediately."""
    from bot_modules.whisper.cog import WhisperOptinView

    cog = _cog_with_role()
    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)
    interaction.original_response = AsyncMock(return_value=MagicMock())

    cfg = WhisperConfig(guild_id=9001, role_id=ROLE_ID, channel_id=8001, log_channel_id=8002)
    with patch("bot_modules.whisper.cog._load_config", return_value=cfg):
        await cog._optin_impl(interaction)

    # Role is NOT granted yet — only after Opt In.
    member.add_roles.assert_not_awaited()
    interaction.response.send_message.assert_awaited()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    view = kwargs.get("view")
    assert isinstance(view, WhisperOptinView)
    assert "opt" in _card_text(view).lower()
    # The stored handle is what lets the card grey out on timeout.
    assert view.message is interaction.original_response.return_value


def _find_button_by_label(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    for item in view.walk_children():
        if isinstance(item, discord.ui.Button) and item.label == label:
            return item
    raise AssertionError(f"button {label!r} not found on view")


@pytest.mark.asyncio
async def test_optin_confirm_button_grants_role():
    """The Opt In button on the consent card actually grants the role, and
    the card turns into the receipt."""
    from bot_modules.whisper.cog import WhisperOptinView

    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)

    bot = MagicMock()
    view = WhisperOptinView(bot, role)  # type: ignore[arg-type]
    await _find_button_by_label(view, "Opt In").callback(interaction)

    member.add_roles.assert_awaited_once_with(role, reason="Whisper opt-in")
    receipt = interaction.response.edit_message.call_args.kwargs["view"]
    assert "You're In" in _card_text(receipt)
    assert view.is_finished()


@pytest.mark.asyncio
async def test_optin_confirm_refuses_an_elevated_role():
    """The Whisper role is a dashboard dial; Opt In never hands out a mod role."""
    from bot_modules.whisper.cog import WhisperOptinView

    role = FakeRole(id=ROLE_ID, name="Whisper", permissions=discord.Permissions(moderate_members=True))
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)

    view = WhisperOptinView(MagicMock(), role)  # type: ignore[arg-type]
    await _find_button_by_label(view, "Opt In").callback(interaction)

    member.add_roles.assert_not_awaited()
    refusal = interaction.response.edit_message.call_args.kwargs["view"]
    assert "can't be handed out" in _card_text(refusal)


@pytest.mark.asyncio
async def test_optin_confirm_reports_a_forbidden_grant():
    from bot_modules.whisper.cog import WhisperOptinView

    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    interaction = fake_interaction(user=member)

    view = WhisperOptinView(MagicMock(), role)  # type: ignore[arg-type]
    await _find_button_by_label(view, "Opt In").callback(interaction)

    refusal = interaction.response.edit_message.call_args.kwargs["view"]
    assert "permission" in _card_text(refusal)


@pytest.mark.asyncio
async def test_optin_cancel_button_does_not_grant_role():
    """Cancel is silent: the consent card is deleted, nothing is granted."""
    from bot_modules.whisper.cog import WhisperOptinView

    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.delete_original_response = AsyncMock()

    bot = MagicMock()
    view = WhisperOptinView(bot, role)  # type: ignore[arg-type]
    await _find_button_by_label(view, "Cancel").callback(interaction)

    member.add_roles.assert_not_awaited()
    interaction.response.defer.assert_awaited_once()
    interaction.delete_original_response.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()
    assert view.is_finished()


@pytest.mark.asyncio
async def test_optin_not_configured():
    cog = _cog_with_role()
    member = FakeMember(id=1001, roles=[])
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    cfg = WhisperConfig(guild_id=9001)  # no role configured
    with patch("bot_modules.whisper.cog._load_config", return_value=cfg):
        await cog._optin_impl(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_optout_removes_role():
    cog = _cog_with_role()
    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[role])
    member.remove_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    cfg = WhisperConfig(guild_id=9001, role_id=ROLE_ID, channel_id=8001, log_channel_id=8002)
    with patch("bot_modules.whisper.cog._load_config", return_value=cfg):
        await cog._optout_impl(interaction)

    member.remove_roles.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    assert "Opted Out" in _card_text(kwargs["view"])
