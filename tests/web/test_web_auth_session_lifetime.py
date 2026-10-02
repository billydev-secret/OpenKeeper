"""A dashboard session ends 7 days idle or 30 days after login, whichever first.

The cookie's signing time is the idle clock, re-stamped about hourly by the
``_SessionRestamp`` middleware; ``iat`` in the payload is the login time and
never moves. Before 2026-09-26 the only limit was a 30-day ``max_age`` on the
signing time, and a guild switch re-signed the cookie — so a session nobody
used for three weeks was still good, and one that switched guilds now and then
never ended at all (standards status row 3.12).
"""

from __future__ import annotations

import time

import pytest

from web_server.auth import (
    SESSION_COOKIE,
    SESSION_IDLE_MAX_AGE,
    SESSION_MAX_AGE,
    SESSION_RESTAMP_INTERVAL,
    DiscordOAuthAuth,
)
from web_server.server import create_app

from tests.web.conftest import set_session_cookie, web_client

_ADMIN_BITS = 0x8
_DAY = 86400


def _cookie(auth, fake_ctx, monkeypatch, *, signed_ago: float, iat_ago: float | None = None) -> str:
    """A session signed ``signed_ago`` seconds back, logged in ``iat_ago`` back.

    ``iat_ago=None`` mints a legacy cookie with no ``iat`` at all.
    """
    now = time.time()
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - signed_ago)
        fresh = auth.create_session_cookie(
            user_id=1,
            username="tester",
            permission_bits=_ADMIN_BITS,
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": fake_ctx.guild_id, "name": "Home", "icon": None}],
        )
        payload = auth._serializer.loads(fresh)
        if iat_ago is None:
            payload.pop("iat")
        else:
            payload["iat"] = int(now - iat_ago)
        return auth._serializer.dumps(payload)


@pytest.fixture
def auth(fake_ctx):
    return DiscordOAuthAuth("test-secret", fake_ctx.guild_id)


def _client_with(auth, fake_ctx, cookie):
    client = web_client(create_app(fake_ctx, auth=auth))
    set_session_cookie(client, cookie)
    return client


def _session_set_cookies(response) -> list[str]:
    return [
        h for h in response.headers.get_list("set-cookie") if h.startswith(f"{SESSION_COOKIE}=")
    ]


# ── the two limits ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "signed_ago, iat_ago, live",
    [
        pytest.param(0, 0, True, id="fresh"),
        pytest.param(6 * _DAY, 6 * _DAY, True, id="six-days-idle"),
        pytest.param(SESSION_IDLE_MAX_AGE + 60, 8 * _DAY, False, id="idle-past-seven-days"),
        pytest.param(_DAY, 29 * _DAY, True, id="active-inside-the-cap"),
        pytest.param(60, SESSION_MAX_AGE + 60, False, id="active-past-the-cap"),
        pytest.param(6 * _DAY, None, True, id="legacy-cookie-inside-both"),
    ],
)
def test_a_session_is_live_only_inside_both_limits(
    auth, fake_ctx, monkeypatch, signed_ago, iat_ago, live
):
    cookie = _cookie(auth, fake_ctx, monkeypatch, signed_ago=signed_ago, iat_ago=iat_ago)
    assert (auth.read_session(cookie) is not None) is live
    client = _client_with(auth, fake_ctx, cookie)
    assert client.get("/api/system/stats").status_code == (200 if live else 401)
    client.close()


def test_a_new_login_records_when_it_happened(auth, fake_ctx):
    before = int(time.time())
    cookie = auth.create_session_cookie(user_id=1, username="t", guild_id=fake_ctx.guild_id)
    assert auth.read_session(cookie)["iat"] >= before


def test_a_guild_switch_keeps_the_login_time(auth, fake_ctx, monkeypatch):
    """Switching guilds re-signs the cookie; it must not restart the 30-day cap."""
    other = fake_ctx.guild_id + 1
    cookie = _cookie(auth, fake_ctx, monkeypatch, signed_ago=0, iat_ago=29 * _DAY)
    payload = auth._serializer.loads(cookie)
    payload["guilds"].append({"id": other, "name": "Other", "icon": None})
    switched = auth.update_session_guild(auth._serializer.dumps(payload), other)
    assert switched is not None
    assert auth.read_session(switched)["iat"] == payload["iat"]


# ── the hourly re-stamp ──────────────────────────────────────────────


def test_an_active_session_is_restamped_once_the_interval_passes(auth, fake_ctx, monkeypatch):
    cookie = _cookie(
        auth, fake_ctx, monkeypatch, signed_ago=SESSION_RESTAMP_INTERVAL + 60, iat_ago=2 * _DAY
    )
    client = _client_with(auth, fake_ctx, cookie)
    response = client.get("/api/system/stats")
    assert response.status_code == 200
    [header] = _session_set_cookies(response)
    remaining = SESSION_MAX_AGE - 2 * _DAY
    max_age = int(header.split("Max-Age=")[1].split(";")[0])
    assert remaining - 60 <= max_age <= remaining, "the browser lifetime runs to the cap"

    restamped = client.cookies.get(SESSION_COOKIE)
    assert restamped != cookie
    assert auth.read_session(restamped)["iat"] == auth.read_session(cookie)["iat"]
    client.close()


def test_a_recently_stamped_session_is_left_alone(auth, fake_ctx, monkeypatch):
    cookie = _cookie(auth, fake_ctx, monkeypatch, signed_ago=600, iat_ago=_DAY)
    client = _client_with(auth, fake_ctx, cookie)
    assert not _session_set_cookies(client.get("/api/system/stats"))
    client.close()


def test_a_legacy_cookie_gains_its_login_time_on_restamp(auth, fake_ctx, monkeypatch):
    """Its signing time stands in for the login it never recorded, then holds."""
    signed_ago = 2 * SESSION_RESTAMP_INTERVAL
    cookie = _cookie(auth, fake_ctx, monkeypatch, signed_ago=signed_ago, iat_ago=None)
    value, _remaining = auth.restamp_session(cookie)
    assert abs(auth.read_session(value)["iat"] - (time.time() - signed_ago)) < 5


def test_logout_is_not_undone_by_a_restamp(auth, fake_ctx, monkeypatch):
    """The logout response clears the cookie; a re-stamp there would put it back."""
    cookie = _cookie(auth, fake_ctx, monkeypatch, signed_ago=2 * SESSION_RESTAMP_INTERVAL, iat_ago=_DAY)
    client = _client_with(auth, fake_ctx, cookie)
    response = client.get("/logout", follow_redirects=False)
    headers = _session_set_cookies(response)
    assert len(headers) == 1 and 'dk_session=""' in headers[0]
    client.close()


def test_a_refused_request_is_not_restamped(auth, fake_ctx, monkeypatch):
    cookie = _cookie(auth, fake_ctx, monkeypatch, signed_ago=2 * SESSION_RESTAMP_INTERVAL, iat_ago=_DAY)
    auth.revoke_sessions(fake_ctx, 1)
    client = _client_with(auth, fake_ctx, cookie)
    response = client.get("/api/system/stats")
    assert response.status_code == 401
    assert not _session_set_cookies(response)
    client.close()
