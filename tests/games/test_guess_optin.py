"""Cog-level tests for /guess optin (consent-gated) and /guess optout."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.games.guess.models import GuessConfig
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

GUESS_ROLE_ID = 7001
GUILD_ID = 9001


def _view_text(view) -> str:
    """Every line of text on a Components V2 card."""
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _answered(interaction) -> str:
    """What the consent card was replaced with."""
    return _view_text(interaction.response.edit_message.call_args.kwargs["view"])


def _make_cog(db_path: str = ":memory:"):
    from bot_modules.games.guess.cog import GuessCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    return GuessCog(bot)


def _config(*, guess_role_id: int = GUESS_ROLE_ID) -> GuessConfig:
    return GuessConfig(guild_id=GUILD_ID, guess_role_id=guess_role_id)


async def _optin(cog, interaction):
    await cog.guess_optin.callback(cog, interaction)


async def _optout(cog, interaction):
    await cog.guess_optout.callback(cog, interaction)


# ── optin: consent gate (2026-08 review, guess U1) ─────────────────────


@pytest.mark.asyncio
async def test_optin_shows_consent_and_does_not_grant_until_confirmed():
    """The command discloses retention and sends a view — the role is only
    granted by the view's Join button, never on invocation."""
    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.games.guess.cog._load_config", return_value=_config()):
        await _optin(cog, interaction)

    member.add_roles.assert_not_awaited()
    view = interaction.followup.send.call_args.kwargs["view"]
    sent = _view_text(view)
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    # The disclosure names what is stored and the way out.
    assert "cached on the bot" in sent
    assert "visible to admins" in sent
    assert "/guess optout" in sent
    from bot_modules.games.guess.cog import GuessOptinConsentView

    assert isinstance(view, GuessOptinConsentView)


@pytest.mark.asyncio
async def test_consent_confirm_grants_role():
    from bot_modules.games.guess.cog import GuessOptinConsentView

    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    view = GuessOptinConsentView(member, role)
    interaction = MagicMock()
    interaction.guild = None  # no bot member: the safety check skips hierarchy
    interaction.response.edit_message = AsyncMock()

    await view.confirm(interaction)

    member.add_roles.assert_awaited_once()
    assert "Welcome to the Guess Pool" in _answered(interaction)
    # The card is done: its timeout must not grey it back over the answer.
    assert view.is_finished()


@pytest.mark.asyncio
async def test_consent_cancel_changes_nothing():
    from bot_modules.games.guess.cog import GuessOptinConsentView

    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    view = GuessOptinConsentView(member, role)
    interaction = MagicMock()
    interaction.guild = None  # no bot member: the safety check skips hierarchy
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.delete_original_response = AsyncMock()

    await view.cancel(interaction)

    member.add_roles.assert_not_awaited()
    # Cancel is silent: the card is deleted, not answered.
    interaction.delete_original_response.assert_awaited_once()
    interaction.response.edit_message.assert_not_called()


@pytest.mark.asyncio
async def test_consent_confirm_handles_forbidden():
    from bot_modules.games.guess.cog import GuessOptinConsentView

    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "missing perms")
    )
    view = GuessOptinConsentView(member, role)
    interaction = MagicMock()
    interaction.guild = None  # no bot member: the safety check skips hierarchy
    interaction.response.edit_message = AsyncMock()

    await view.confirm(interaction)

    assert "permission" in _answered(interaction).lower()


@pytest.mark.asyncio
async def test_optin_skips_when_already_in_pool():
    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[role])
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.games.guess.cog._load_config", return_value=_config()):
        await _optin(cog, interaction)

    member.add_roles.assert_not_awaited()
    sent = interaction.followup.send.call_args.args[0]
    assert "already" in sent.lower()


@pytest.mark.asyncio
async def test_optin_rejects_when_role_not_configured():
    member = FakeMember(id=1001)
    guild = FakeGuild(id=GUILD_ID, members={member.id: member})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.games.guess.cog._load_config", return_value=_config(guess_role_id=0)):
        await _optin(cog, interaction)

    member.add_roles.assert_not_awaited()
    sent = interaction.followup.send.call_args.args[0]
    assert "configured" in sent.lower() or "setup" in sent.lower()


@pytest.mark.asyncio
async def test_optin_handles_role_deleted_from_guild():
    member = FakeMember(id=1001)
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.games.guess.cog._load_config", return_value=_config()):
        await _optin(cog, interaction)

    member.add_roles.assert_not_awaited()
    sent = interaction.followup.send.call_args.args[0]
    assert "no longer exists" in sent.lower() or "re-run" in sent.lower()


# ── optout: self-service leave (2026-08 review, guess U1) ──────────────


@pytest.mark.asyncio
async def test_optout_removes_role_and_explains_consequences():
    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[role])
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.games.guess.cog._load_config", return_value=_config()):
        await _optout(cog, interaction)

    member.remove_roles.assert_awaited_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "no longer solvable" in sent
    assert "stats stay" in sent


@pytest.mark.asyncio
async def test_optout_rejects_when_not_in_pool():
    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.games.guess.cog._load_config", return_value=_config()):
        await _optout(cog, interaction)

    member.remove_roles.assert_not_awaited()
    sent = interaction.followup.send.call_args.args[0]
    assert "not in the guess pool" in sent.lower()


@pytest.mark.asyncio
async def test_optout_handles_forbidden():
    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[role])
    member.remove_roles = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "missing perms")
    )
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.games.guess.cog._load_config", return_value=_config()):
        await _optout(cog, interaction)

    sent = interaction.followup.send.call_args.args[0]
    assert "permission" in sent.lower()


@pytest.mark.asyncio
async def test_consent_confirm_writes_the_evidence_row(sync_db_path):
    """The one wiring assertion: the view must actually persist the Art 7(1)
    record. Repo-level behaviour is covered in tests/games/test_guess_repo.py —
    what can only break here is the view not calling it at all."""
    from bot_modules.games.guess.cog import GuessOptinConsentView
    from bot_modules.core.db_utils import open_db
    from bot_modules.games.guess.repo import get_guess_consent

    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    view = GuessOptinConsentView(member, role, sync_db_path)
    interaction = MagicMock()
    interaction.guild = None  # no bot member: the safety check skips hierarchy
    interaction.guild_id = GUILD_ID
    interaction.response.edit_message = AsyncMock()

    await view.confirm(interaction)

    with open_db(sync_db_path) as conn:
        row = get_guess_consent(conn, guild_id=GUILD_ID, user_id=1001)
    assert row is not None, "consent view granted the role but recorded nothing"
    assert row["withdrawn_at"] is None


@pytest.mark.asyncio
async def test_no_consent_row_when_the_role_grant_fails(sync_db_path):
    """A consent record for someone who never got the role would misrepresent
    what happened — the write is deliberately after the grant succeeds."""
    from bot_modules.games.guess.cog import GuessOptinConsentView
    from bot_modules.core.db_utils import open_db
    from bot_modules.games.guess.repo import get_guess_consent

    role = FakeRole(id=GUESS_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no"))
    view = GuessOptinConsentView(member, role, sync_db_path)
    interaction = MagicMock()
    interaction.guild = None  # no bot member: the safety check skips hierarchy
    interaction.guild_id = GUILD_ID
    interaction.response.edit_message = AsyncMock()

    await view.confirm(interaction)

    with open_db(sync_db_path) as conn:
        assert get_guess_consent(conn, guild_id=GUILD_ID, user_id=1001) is None


@pytest.mark.asyncio
async def test_consent_confirm_refuses_an_elevated_role(sync_db_path):
    """The Guess role is a dial: if it points at a mod-level role, Join
    grants nothing and records no consent."""
    from bot_modules.games.guess.cog import GuessOptinConsentView
    from bot_modules.core.db_utils import open_db
    from bot_modules.games.guess.repo import get_guess_consent

    role = FakeRole(id=GUESS_ROLE_ID, permissions=discord.Permissions(kick_members=True))
    member = FakeMember(id=1001, roles=[])
    view = GuessOptinConsentView(member, role, sync_db_path)
    interaction = MagicMock()
    interaction.guild = None
    interaction.guild_id = GUILD_ID
    interaction.response.edit_message = AsyncMock()

    await view.confirm(interaction)

    member.add_roles.assert_not_awaited()
    assert "can't be handed out" in _answered(interaction)
    with open_db(sync_db_path) as conn:
        assert get_guess_consent(conn, guild_id=GUILD_ID, user_id=1001) is None
