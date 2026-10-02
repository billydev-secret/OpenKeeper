"""Starting a policy vote actually calls the mods and admins in (bug sweep,
Components V2 stage 1).

``/policy vote`` posts three things: an ephemeral "voting has begun", the
ballot card, and a ``🗳️ Vote now!`` line mentioning every mod and admin
role. That last one is the only summons — the card itself carries no
mention — and the bot pings nobody unless a send says so, so without an
allow-list it renders as inert grey text and no mod is told to vote.

The mentions are collected into a list in a loop, which is why the static
sweep in ``tests/contracts/test_mention_default_audit.py`` could not see it (a list
is never *assigned* a mention). It can now; this is the other half.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.moderation.jail.cog as jail_cog


MOD_ROLE = 4001
ADMIN_ROLE = 4002
DEPARTED_ROLE = 4003  # configured, but deleted from the guild since


def _role(rid: int) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = rid
    role.mention = f"<@&{rid}>"
    return role


async def _start_vote(monkeypatch, *, mod_ids: set[int], admin_ids: set[int]):
    """Run ``_PolicyVoteModal.on_submit`` and hand back the channel's sends."""
    roles = {MOD_ROLE: _role(MOD_ROLE), ADMIN_ROLE: _role(ADMIN_ROLE)}

    guild = MagicMock(spec=discord.Guild)
    guild.id = 9001
    guild.get_role = roles.get

    monkeypatch.setattr(jail_cog, "start_policy_vote", MagicMock())
    monkeypatch.setattr(jail_cog, "write_audit", MagicMock())
    monkeypatch.setattr(jail_cog, "_get_mod_role_ids", lambda ctx, gid: set(mod_ids))
    monkeypatch.setattr(jail_cog, "_get_admin_role_ids", lambda ctx, gid: set(admin_ids))
    monkeypatch.setattr(jail_cog, "guild_eligible_voters", lambda ctx, g: {11, 12})
    monkeypatch.setattr(
        jail_cog, "build_name_fn", AsyncMock(return_value=lambda uid: f"U{uid}")
    )
    monkeypatch.setattr(
        jail_cog, "build_policy_vote_initial_embed",
        lambda **kwargs: discord.Embed(title="Policy vote"),
    )

    ctx = SimpleNamespace(db_path=":memory:", open_db=MagicMock())
    modal = jail_cog._PolicyVoteModal(77, ctx)  # type: ignore[arg-type]
    modal.vote_text = SimpleNamespace(value="  No hats indoors.  ")  # type: ignore[assignment]

    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "mod-chat"
    channel.send = AsyncMock()

    interaction = MagicMock()
    interaction.guild = guild
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 11
    interaction.channel = channel
    interaction.response.send_message = AsyncMock()

    await modal.on_submit(interaction)
    return interaction, channel


async def test_the_vote_call_out_notifies_every_configured_role(monkeypatch):
    interaction, channel = await _start_vote(
        monkeypatch, mod_ids={MOD_ROLE}, admin_ids={ADMIN_ROLE}
    )

    # The "voting has begun" line names nobody and stays silent, as before.
    assert interaction.response.send_message.await_args.kwargs[
        "allowed_mentions"
    ].to_dict() == {"parse": []}

    call = channel.send.await_args
    text = call.args[0]
    assert "Vote now!" in text
    assert f"<@&{MOD_ROLE}>" in text and f"<@&{ADMIN_ROLE}>" in text
    allowed = call.kwargs["allowed_mentions"]
    assert sorted(o.id for o in allowed.roles) == sorted([MOD_ROLE, ADMIN_ROLE])
    assert allowed.everyone is False and allowed.users == []


async def test_a_deleted_role_is_neither_mentioned_nor_allow_listed(monkeypatch):
    """A role id left in config after the role was deleted has no mention to
    render, so it must not appear in the allow-list either — the text and the
    allow-list are built from the same resolved list."""
    _, channel = await _start_vote(
        monkeypatch, mod_ids={MOD_ROLE, DEPARTED_ROLE}, admin_ids=set()
    )

    call = channel.send.await_args
    assert f"<@&{DEPARTED_ROLE}>" not in call.args[0]
    assert [o.id for o in call.kwargs["allowed_mentions"].roles] == [MOD_ROLE]


async def test_no_configured_roles_posts_no_call_out_at_all(monkeypatch):
    """Nothing to summon: the ballot card goes up and that is the end of it —
    an empty allow-list on an empty mention line would be noise."""
    _, channel = await _start_vote(monkeypatch, mod_ids=set(), admin_ids=set())

    assert channel.send.await_count == 1  # the card, and nothing after it
    assert "Vote now!" not in str(channel.send.await_args)


@pytest.mark.parametrize("missing", ["guild", "member"])
async def test_a_dm_or_a_webhook_user_starts_no_vote(monkeypatch, missing):
    """The guard ahead of all of it: no guild, or a caller who is not a
    Member, means the modal returns before it writes anything."""
    started = MagicMock()
    monkeypatch.setattr(jail_cog, "start_policy_vote", started)

    modal = jail_cog._PolicyVoteModal(77, SimpleNamespace())  # type: ignore[arg-type]
    modal.vote_text = SimpleNamespace(value="x")  # type: ignore[assignment]
    interaction = MagicMock()
    interaction.guild = None if missing == "guild" else MagicMock(spec=discord.Guild)
    interaction.user = MagicMock()  # not spec'd as a Member

    await modal.on_submit(interaction)

    started.assert_not_called()
