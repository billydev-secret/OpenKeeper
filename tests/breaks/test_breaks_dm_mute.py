"""Take a Break mutes bot DMs: the shared branded-DM helper and the DM
request precheck.

The rule is the no-contact one: suppress the delivery and give the answer a
closed DM gives, so nothing can tell a break from an ordinary outcome. The
economy helper, Whisper, the voice knock and Survivor's last call have their
break rows in their own test files, beside the paths they gate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.breaks.store import create_break
from bot_modules.core.db_utils import open_db
from bot_modules.shared.dm_branding import send_branded_dm
from tests.db_template import migrated_db

G, OTHER_G, UID = 100, 200, 5


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "m.db"
    migrated_db(path)
    return path


def _break(db, guild_id=G, user_id=UID):
    with open_db(db) as conn:
        create_break(
            conn, guild_id=guild_id, user_id=user_id, stored_roles=[], swapped_spicy=False
        )


@pytest.mark.parametrize(
    ("break_in", "bypass", "delivered"),
    [
        pytest.param(None, False, True, id="not-on-a-break"),
        pytest.param(G, False, False, id="on-a-break-dropped"),
        pytest.param(G, True, True, id="on-a-break-bypass"),
        # A break is per server: another server's DMs still arrive.
        pytest.param(OTHER_G, False, True, id="break-in-another-server"),
    ],
)
async def test_send_branded_dm_drops_for_a_member_on_a_break(db, break_in, bypass, delivered):
    if break_in is not None:
        _break(db, guild_id=break_in)
    user = MagicMock()
    user.id = UID
    user.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    guild = SimpleNamespace(id=G, name="Meadow", icon=None)

    out = await send_branded_dm(
        user, db_path=db, guild=guild, content="hi", bypass_break=bypass
    )

    assert (out is not None) is delivered
    assert user.send.await_count == (1 if delivered else 0)


async def test_send_branded_dm_delivers_when_the_break_check_faults(tmp_path):
    """A DB with no break table (a fault, not a break) still delivers — the
    mute is a courtesy, never a reason to lose a DM."""
    user = MagicMock()
    user.id = UID
    user.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    guild = SimpleNamespace(id=G, name="Meadow", icon=None)

    out = await send_branded_dm(
        user, db_path=tmp_path / "empty.db", guild=guild, content="hi"
    )
    assert out is not None


@pytest.mark.parametrize("on_break", [False, True], ids=["free", "on-a-break"])
def test_dm_request_to_a_member_on_a_break_reads_as_closed(db, on_break):
    """The consent card could never reach them, so the request is refused
    with the ordinary closed-DMs line — nothing about a break."""
    from bot_modules.dm.perms.cog import DmPermsCog

    if on_break:
        _break(db)
    cog = MagicMock()
    cog.bot.ctx.db_path = db
    cog._mode_roles_for.return_value = {}
    cog._is_mutual.return_value = False
    cog._has_pending_request.return_value = False
    guild = MagicMock()
    guild.id = G
    requester = MagicMock(spec=discord.Member)
    requester.id = 9
    target = MagicMock(spec=discord.Member)
    target.id, target.bot, target.display_name = UID, False, "Ash"
    target.roles = []

    error = DmPermsCog._precheck_dm_request(cog, guild, requester, target)

    if on_break:
        assert error == "❌ Ash isn't accepting DM requests right now."
    else:
        assert error is None
