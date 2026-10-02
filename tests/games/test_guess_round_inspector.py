"""Tests for /guess round mod-only inspector."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import discord
import pytest

from bot_modules.games.guess.models import GuessRound
from tests.fakes import FakeGuild, FakeMember, fake_interaction


GUILD_ID = 9001
GUESS_CHANNEL_ID = 8001
ROUND_ID = 42


@pytest.fixture(autouse=True)
def _stub_name_resolver(monkeypatch):
    """The embeds resolve member ids to display names; build_name_fn reads
    ``known_users``, which a bare ``:memory:`` db_path has no schema for. The
    resolver's own fallback chain is covered in
    tests/shared/test_name_resolver_logic.py, and the builders in
    tests/games/test_guess_layouts.py — here it only has to answer.

    monkeypatch, not mock.patch: a cog fixture whose mock outlives it stubs the
    real helper for a whole xdist worker (see the no-contact stub leak).
    """
    async def _fake_build_name_fn(**_kwargs):
        return lambda uid: f"Member{uid}"

    monkeypatch.setattr(
        "bot_modules.games.guess.cog.build_name_fn", _fake_build_name_fn
    )

def _make_round() -> GuessRound:
    return GuessRound(
        id=ROUND_ID, guild_id=GUILD_ID, submitter_id=1001,
        answer_id=1001, channel_id=GUESS_CHANNEL_ID, message_id=12345,
        crop_path="", crop_url="", original_path="",
        difficulty="medium", candidate_count=1, reroll_count=0,
        created_at=1000.0, solved_at=None, solver_id=None,
        guesses_to_solve=None, unique_guessers_to_solve=None,
        answer_optout=False, deleted_at=None,
    )


def _make_cog():
    from bot_modules.games.guess.cog import GuessCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return GuessCog(bot)


async def _round(cog: Any, interaction: Any, rid: int) -> None:
    await cog.guess_round.callback(cog, interaction, rid)


@pytest.mark.asyncio
async def test_round_inspector_rejects_non_mod():
    user = FakeMember(id=2002)
    user.guild_permissions = MagicMock(manage_guild=False, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[user.id] = user

    interaction = fake_interaction(user=user, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._do_load_round", return_value=_make_round()):
        await _round(cog, interaction, ROUND_ID)

    msg = interaction.followup.send.call_args.args[0]
    assert "mod" in msg.lower() or "permission" in msg.lower()


@pytest.mark.asyncio
async def test_round_inspector_returns_a_card_for_mod():
    mod = FakeMember(id=3003)
    mod.guild_permissions = MagicMock(manage_guild=True, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[mod.id] = mod

    interaction = fake_interaction(user=mod, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._do_load_round", return_value=_make_round()), \
         patch("bot_modules.games.guess.cog._do_count_guesses_for_round", return_value=12), \
         patch("bot_modules.games.guess.cog._do_count_unique_guessers_for_round", return_value=4):
        await _round(cog, interaction, ROUND_ID)

    call = interaction.followup.send.call_args
    view = call.kwargs.get("view")
    assert view is not None
    desc = "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "1001" in desc  # submitter / answer, with the copyable id
    assert "12" in desc and "4 unique" in desc
    assert call.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_round_inspector_handles_unknown_round():
    mod = FakeMember(id=3003)
    mod.guild_permissions = MagicMock(manage_guild=True, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[mod.id] = mod

    interaction = fake_interaction(user=mod, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._do_load_round", return_value=None):
        await _round(cog, interaction, ROUND_ID)

    msg = interaction.followup.send.call_args.args[0]
    assert "not found" in msg.lower()
