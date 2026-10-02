"""An unreadable session generation refuses; it never reads as "never logged out".

Logout revokes every cookie by bumping the member's generation in the config
table. Until 2026-09-27 a failed read of that generation was answered with 0
and *cached*: for the rest of the process, every cookie the member had ever
been issued passed the check, the revoked ones included. And a logout during
the failure bumped from that 0, writing a generation below the stored one and
reviving cookies an earlier logout had retired.

Now the read raises ``SessionStoreUnavailable``, nothing is cached, and each
caller fails closed: the request is refused, the logout writes nothing, the
login mints no cookie.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from bot_modules.core import db_utils
from bot_modules.core.db_utils import get_config_value
from web_server.auth import (
    SESSION_COOKIE,
    SESSION_GEN_KEY_PREFIX,
    DiscordOAuthAuth,
    SessionStoreUnavailable,
)
from web_server.routes import oauth
from web_server.server import create_app

from tests.web.conftest import set_session_cookie, web_client

_UID = 1


@pytest.fixture
def auth(fake_ctx):
    return DiscordOAuthAuth("test-secret", fake_ctx.guild_id)


def _cookie(auth, fake_ctx, generation: int) -> str:
    return auth.create_session_cookie(
        user_id=_UID,
        username="tester",
        permission_bits=0x8,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Home", "icon": None}],
        generation=generation,
    )


def _stored_generation(fake_ctx) -> int:
    with fake_ctx.open_db() as conn:
        return int(
            get_config_value(conn, f"{SESSION_GEN_KEY_PREFIX}{_UID}", "0", 0, allow_legacy_fallback=False)
            or 0
        )


def _unreadable(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db_utils, "get_config_value", boom)


def _status(auth, fake_ctx, cookie) -> int:
    client = web_client(create_app(fake_ctx, auth=auth))
    set_session_cookie(client, cookie)
    try:
        return client.get("/api/system/stats").status_code
    finally:
        client.close()


def test_a_revoked_cookie_is_refused_while_the_generation_is_unreadable(fake_ctx, monkeypatch):
    """The exploit: two logouts, a restart (empty cache), then a DB hiccup.
    The generation-1 cookie the second logout retired must not come back."""
    DiscordOAuthAuth("test-secret", fake_ctx.guild_id).revoke_sessions(fake_ctx, _UID)
    DiscordOAuthAuth("test-secret", fake_ctx.guild_id).revoke_sessions(fake_ctx, _UID)
    assert _stored_generation(fake_ctx) == 2
    after_restart = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    _unreadable(monkeypatch)
    assert _status(after_restart, fake_ctx, _cookie(after_restart, fake_ctx, 1)) == 401


def test_a_failed_read_is_not_cached(auth, fake_ctx, monkeypatch):
    """Refusing during the outage is the price; staying refused after it isn't."""
    cookie = _cookie(auth, fake_ctx, 0)
    with monkeypatch.context() as m:
        _unreadable(m)
        assert _status(auth, fake_ctx, cookie) == 401
    assert _status(auth, fake_ctx, cookie) == 200


def test_current_generation_raises_rather_than_answering_zero(auth, fake_ctx, monkeypatch):
    _unreadable(monkeypatch)
    with pytest.raises(SessionStoreUnavailable):
        auth.current_generation(fake_ctx, _UID)
    assert _UID not in auth._gen_cache


def test_a_logout_during_the_failure_does_not_roll_the_generation_back(fake_ctx, monkeypatch):
    """Only the read fails here, so a write would land: from a guessed 0 it
    would store 1 over 2, and the generation-1 cookie would live again."""
    for _ in range(2):
        DiscordOAuthAuth("test-secret", fake_ctx.guild_id).revoke_sessions(fake_ctx, _UID)
    fresh = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    with monkeypatch.context() as m:
        _unreadable(m)
        assert fresh.revoke_sessions(fake_ctx, _UID) is None
    assert _stored_generation(fake_ctx) == 2
    assert _status(fresh, fake_ctx, _cookie(fresh, fake_ctx, 1)) == 401


# ── login ────────────────────────────────────────────────────────────


def _fake_discord(fake_ctx):
    async def call(_client, method, url, **_kwargs):
        request = httpx.Request(method, url)
        if url == oauth.DISCORD_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "t"}, request=request)
        if url.endswith("/users/@me"):
            return httpx.Response(200, json={"id": str(_UID), "username": "tester"}, request=request)
        guild = {"id": str(fake_ctx.guild_id), "name": "Home", "icon": None, "permissions": "8"}
        return httpx.Response(200, json=[guild], request=request)

    return call


def _login(auth, fake_ctx, monkeypatch):
    monkeypatch.setattr(oauth, "_discord_call", _fake_discord(fake_ctx))
    client = web_client(create_app(fake_ctx, auth=auth))
    client.cookies.set("dk_oauth_state", "s")
    try:
        return client.get("/callback", params={"state": "s", "code": "c"}, follow_redirects=False)
    finally:
        client.close()


def test_a_login_mints_a_cookie_when_the_store_answers(auth, fake_ctx, monkeypatch):
    """The control for the test below: the faked Discord round trip reaches
    the cookie, so a refusal there is the generation read, not the harness."""
    response = _login(auth, fake_ctx, monkeypatch)
    assert response.status_code == 302
    assert "error" not in parse_qs(urlparse(response.headers["location"]).query)
    assert any(h.startswith(f"{SESSION_COOKIE}=") for h in response.headers.get_list("set-cookie"))


def test_a_login_mints_no_cookie_when_the_generation_is_unreadable(auth, fake_ctx, monkeypatch):
    _unreadable(monkeypatch)
    response = _login(auth, fake_ctx, monkeypatch)
    assert parse_qs(urlparse(response.headers["location"]).query)["error"] == ["session_failed"]
    assert not any(h.startswith(f"{SESSION_COOKIE}=") for h in response.headers.get_list("set-cookie"))
