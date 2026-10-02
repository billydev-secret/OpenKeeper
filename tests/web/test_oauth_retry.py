"""Discord's Retry-After is obeyed during login, within a browser-sized budget.

A 429 on the OAuth callback used to read as a failed login: the caller saw a
non-200 and redirected to /login?error=…, so a member hit "something went
wrong" on a request that would have succeeded a second later.

The budget is the interesting part and is what most of this file covers. A
backfill in scripts/ can wait as long as Discord asks; a callback cannot,
because someone is watching a blank tab. So a long Retry-After is deliberately
*not* waited out — it falls through to the existing error redirect.
"""

from __future__ import annotations

import httpx
import pytest

from web_server.routes.oauth import (
    _MAX_RETRY_WAIT,
    _OAUTH_ATTEMPTS,
    _discord_call,
    _retry_after_seconds,
)


def _resp(status: int, *, headers: dict | None = None, json_body=None) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers or {},
        json=json_body if json_body is not None else {"ok": True},
        request=httpx.Request("GET", "https://discord.com/api/v10/users/@me"),
    )


class _FakeClient:
    """Replays a scripted list of responses and records the sleeps requested."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls += 1
        return self._responses.pop(0) if self._responses else _resp(200)


@pytest.fixture
def slept(monkeypatch):
    """Capture asyncio.sleep durations instead of actually waiting."""
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("web_server.routes.oauth.asyncio.sleep", fake_sleep)
    return waits


# ── _retry_after_seconds ────────────────────────────────────────────────────


def test_reads_the_retry_after_header():
    assert _retry_after_seconds(_resp(429, headers={"Retry-After": "1.5"})) == 1.5


def test_falls_back_to_the_json_body():
    """Discord sends the same number in the body; the header is authoritative."""
    assert _retry_after_seconds(_resp(429, json_body={"retry_after": 2.0})) == 2.0


def test_header_wins_over_the_body():
    resp = _resp(429, headers={"Retry-After": "1"}, json_body={"retry_after": 99})
    assert _retry_after_seconds(resp) == 1.0


def test_an_html_edge_429_yields_no_delay():
    """Cloudflare's 429 has no header and an unparseable body — don't guess."""
    resp = httpx.Response(
        429,
        text="<html>rate limited</html>",
        request=httpx.Request("GET", "https://discord.com/"),
    )
    assert _retry_after_seconds(resp) is None


@pytest.mark.parametrize("value", ["", "soon", "null"])
def test_a_nonnumeric_retry_after_yields_no_delay(value):
    assert _retry_after_seconds(_resp(429, headers={"Retry-After": value})) is None


# ── _discord_call ───────────────────────────────────────────────────────────


async def test_a_successful_call_is_made_once(slept):
    client = _FakeClient([_resp(200)])
    resp = await _discord_call(client, "GET", "https://discord.com/x")
    assert resp.status_code == 200
    assert client.calls == 1
    assert slept == []


async def test_a_429_is_retried_after_the_requested_wait(slept):
    client = _FakeClient([_resp(429, headers={"Retry-After": "1.5"}), _resp(200)])
    resp = await _discord_call(client, "GET", "https://discord.com/x")
    assert resp.status_code == 200
    assert client.calls == 2
    assert slept == [1.75]  # the requested wait plus the clock-skew pad


async def test_a_long_retry_after_is_not_waited_out(slept):
    """A member on a blank tab would rather be told to try again."""
    client = _FakeClient([_resp(429, headers={"Retry-After": str(_MAX_RETRY_WAIT + 1)})])
    resp = await _discord_call(client, "GET", "https://discord.com/x")
    assert resp.status_code == 429  # caller turns this into its error redirect
    assert client.calls == 1
    assert slept == []


async def test_a_429_with_no_usable_delay_is_not_retried(slept):
    client = _FakeClient([_resp(429, json_body={"nothing": "here"})])
    resp = await _discord_call(client, "GET", "https://discord.com/x")
    assert resp.status_code == 429
    assert client.calls == 1


async def test_retries_are_bounded_by_the_attempt_budget(slept):
    """Discord staying rate-limited must not hold the request open forever."""
    client = _FakeClient([_resp(429, headers={"Retry-After": "0.1"})] * 10)
    resp = await _discord_call(client, "GET", "https://discord.com/x")
    assert resp.status_code == 429
    assert client.calls == _OAUTH_ATTEMPTS
    assert len(slept) == _OAUTH_ATTEMPTS - 1


async def test_a_non_429_error_is_returned_without_retrying(slept):
    """Only rate limits are retried — a 401 will not get better by waiting."""
    client = _FakeClient([_resp(401), _resp(200)])
    resp = await _discord_call(client, "GET", "https://discord.com/x")
    assert resp.status_code == 401
    assert client.calls == 1
