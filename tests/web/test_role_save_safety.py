"""Dashboard saves refuse a role the bot would refuse to hand out.

Every config-sourced grant re-checks the role at grant time
(``core.role_safety``; the contract is tests/contracts/test_role_grant_safety_contract.py),
but there a refusal is a log line. These are the write-path half: the admin
hears it at Save, naming the role and the reason.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from tests.fakes import FakeRole

PLAIN = 1_234_567_890_123_456_001
ELEVATED = 1_234_567_890_123_456_002
ABOVE_BOT = 1_234_567_890_123_456_003


class _Guild:
    def __init__(self, guild_id: int):
        self.id = guild_id
        self.me = SimpleNamespace(top_role=FakeRole(id=1, name="Poppy", position=100))
        roles = [
            FakeRole(id=PLAIN, name="Regular", position=5),
            FakeRole(
                id=ELEVATED, name="STAFF", position=5,
                permissions=discord.Permissions(manage_messages=True),
            ),
            FakeRole(id=ABOVE_BOT, name="Owner", position=150),
        ]
        self._roles = {r.id: r for r in roles}

    def get_role(self, role_id):
        return self._roles.get(int(role_id))

    def get_channel(self, _channel_id):
        return None


class _Bot:
    def __init__(self, guild: _Guild):
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None

    def get_cog(self, _name):
        return None

    def dispatch(self, *_args, **_kwargs):
        pass


@pytest.fixture
def gateway(fake_ctx):
    fake_ctx.bot = _Bot(_Guild(fake_ctx.guild_id))
    yield
    fake_ctx.bot = None


def _xp(rid):
    return ("put", "/api/config/xp", {"level_5_role_id": str(rid)})


# (method, path, body-for-role-id). Only the dials whose role is handed to
# members — a ping-only or required role is never granted, so never checked.
SAVES = [
    pytest.param(_xp, id="level-5-role"),
    pytest.param(
        lambda rid: ("put", "/api/config/xp", {"promotion_review_grant_role_id": str(rid)}),
        id="promotion-grant-role",
    ),
    pytest.param(
        lambda rid: ("put", "/api/config/roles/member", {"role_id": str(rid)}),
        id="role-grant",
    ),
    pytest.param(
        lambda rid: ("put", "/api/config/auto-role", {"auto_role_ids": [str(rid)]}),
        id="auto-role",
    ),
    pytest.param(
        lambda rid: ("put", "/api/config/guess", {"role_id": str(rid)}), id="guess"
    ),
    pytest.param(
        lambda rid: ("put", "/api/config/whisper", {"role_id": str(rid)}), id="whisper"
    ),
    pytest.param(
        lambda rid: ("put", "/api/config/dms", {"open_role_id": str(rid)}), id="dm-mode"
    ),
    pytest.param(
        lambda rid: ("put", "/api/economy/config", {"game_role_id": rid}),
        id="economy-notifications",
    ),
    pytest.param(
        lambda rid: (
            "post", "/api/economy/shop-items",
            {"name": "Badge", "price": 10, "kind": "role", "role_id": str(rid)},
        ),
        id="shop-item",
    ),
    pytest.param(
        lambda rid: ("put", "/api/chat-revive/config", {"mode": "off", "role_id": rid}),
        id="chat-revive",
    ),
    pytest.param(
        lambda rid: (
            "put", "/api/config/intake",
            {"steps": [{"label": "Main chat", "auto": "grant", "role_id": str(rid)}]},
        ),
        id="intake-grant-step",
    ),
    pytest.param(
        lambda rid: (
            "put", "/api/config/intake",
            {"stage2_steps": [{"label": "NSFW granted", "auto": "grant", "role_id": str(rid)}]},
        ),
        id="intake-stage2-grant-step",
    ),
]


@pytest.mark.parametrize("save", SAVES)
@pytest.mark.parametrize(
    ("role_id", "needle"),
    [
        pytest.param(ELEVATED, "elevated permissions", id="elevated"),
        pytest.param(ABOVE_BOT, "above my highest role", id="above-bot"),
    ],
)
def test_save_refuses_an_ungrantable_role(open_client, gateway, save, role_id, needle):
    method, path, body = save(role_id)
    resp = getattr(open_client, method)(path, json=body)
    assert resp.status_code == 400, resp.text
    assert needle in resp.json()["detail"]


@pytest.mark.parametrize("save", SAVES)
def test_save_accepts_a_plain_role(open_client, gateway, save):
    method, path, body = save(PLAIN)
    resp = getattr(open_client, method)(path, json=body)
    assert resp.status_code < 400, resp.text


def test_save_is_permissive_without_a_gateway(open_client, fake_ctx):
    # No bot, no role to inspect: the grant-time check still stands behind it.
    fake_ctx.bot = None
    method, path, body = _xp(ELEVATED)
    assert getattr(open_client, method)(path, json=body).status_code < 400


def test_a_manual_shop_item_ignores_its_role_field(open_client, gateway):
    # Only kind="role" hands the role out.
    resp = open_client.post(
        "/api/economy/shop-items",
        json={"name": "Shoutout", "price": 10, "kind": "manual", "role_id": str(ELEVATED)},
    )
    assert "elevated" not in resp.text
