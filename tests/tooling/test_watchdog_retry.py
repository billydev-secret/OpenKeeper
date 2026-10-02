"""The watchdog's DM path honours Discord's ``Retry-After``.

This script is the only channel through which an outage is reported. Before
this, a single 429 on ``POST /users/@me/channels`` raised straight out of
``send_dm``, which logged "Failed to send DM" and set ``alerted = True``
anyway — the bot stayed down and nobody was told. Discord rate-limits the DM
bucket per-recipient, so the outage burst is exactly when it happens.

Loaded by path: watchdog.py is stdlib-only and runs under the *system* python,
deliberately outside the repo venv, so it is not importable as a package.
"""

from __future__ import annotations

import email.message
import importlib.util
import io
import urllib.error
import urllib.request

import pytest
from tests.paths import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("watchdog", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wd = _load()


def _http_error(code: int, *, header: str | None = None, body: bytes = b"") -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if header is not None:
        hdrs["Retry-After"] = header
    return urllib.error.HTTPError("https://discord.com/api/v10/x", code, "nope", hdrs, io.BytesIO(body))


class _Response(io.BytesIO):
    """Just enough of an ``http.client.HTTPResponse`` for ``_post``."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def slept(monkeypatch):
    """Record every sleep instead of taking it."""
    waits: list[float] = []
    monkeypatch.setattr(wd.time, "sleep", waits.append)
    return waits


@pytest.fixture
def replies(monkeypatch):
    """Replay a scripted list of responses/errors from ``urlopen``."""

    def install(*scripted):
        queue = list(scripted)

        def fake_urlopen(req, timeout=None):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return queue

    return install


# ── _retry_after ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error, expected",
    [
        pytest.param(_http_error(429, header="1.5"), 1.8, id="header wins"),
        pytest.param(_http_error(429, body=b'{"retry_after": 2.0}'), 2.3, id="json body"),
        pytest.param(_http_error(429, header="0"), 0.3, id="zero still pads"),
        # Cloudflare's edge 429 is an HTML page, not the API's JSON envelope.
        pytest.param(_http_error(429, body=b"<html>rate limited</html>"), 5.0, id="html body"),
        pytest.param(_http_error(429, body=b""), 5.0, id="empty body"),
        pytest.param(_http_error(429, body=b'{"retry_after": null}'), 5.0, id="null in body"),
        pytest.param(_http_error(429, body=b"[1, 2]"), 5.0, id="json that is not an object"),
        # A header we can't read is still evidence of a *short* bucket limit;
        # only a body that told us nothing at all earns the long edge wait.
        pytest.param(_http_error(429, header="soon"), 1.0, id="non-numeric header"),
    ],
)
def test_retry_after_reads_what_discord_sent(error, expected):
    assert wd._retry_after(error) == pytest.approx(expected)


# ── _post ─────────────────────────────────────────────────────────────────


def test_a_rate_limited_post_waits_and_succeeds(replies, slept):
    replies(_http_error(429, header="1.5"), _Response(b'{"id": "42"}'))
    assert wd._post("tok", "/users/@me/channels", {}) == {"id": "42"}
    assert slept == [pytest.approx(1.8)]


def test_a_successful_post_never_sleeps(replies, slept):
    replies(_Response(b'{"id": "42"}'))
    assert wd._post("tok", "/x", {}) == {"id": "42"}
    assert slept == []


def test_a_forbidden_post_raises_immediately(replies, slept):
    """403 means the DM is closed — retrying it just delays the log line."""
    replies(_http_error(403), _Response(b'{"id": "42"}'))
    with pytest.raises(urllib.error.HTTPError):
        wd._post("tok", "/x", {})
    assert slept == []


def test_a_server_error_backs_off_exponentially(replies, slept):
    replies(_http_error(500), _http_error(502), _Response(b'{"id": "42"}'))
    assert wd._post("tok", "/x", {}) == {"id": "42"}
    assert slept == [1, 2]


def test_relentless_rate_limiting_gives_up_rather_than_looping(replies, slept):
    replies(*[_http_error(429, header="1") for _ in range(wd.MAX_ATTEMPTS)])
    with pytest.raises(RuntimeError, match="gave up"):
        wd._post("tok", "/x", {})
    assert len(slept) == wd.MAX_ATTEMPTS


def test_a_final_server_error_raises_the_http_error_not_the_runtime_one(replies, slept):
    """The last attempt re-raises, so the log keeps Discord's status code."""
    replies(*[_http_error(503) for _ in range(wd.MAX_ATTEMPTS)])
    with pytest.raises(urllib.error.HTTPError):
        wd._post("tok", "/x", {})


# ── send_dm ───────────────────────────────────────────────────────────────


def test_send_dm_opens_the_channel_then_posts(replies, slept):
    replies(_Response(b'{"id": "chan"}'), _Response(b"{}"))
    assert wd.send_dm("tok", "123", "hi") is True


def test_send_dm_survives_a_rate_limit_on_the_channel_open(replies, slept):
    replies(_http_error(429, header="0.7"), _Response(b'{"id": "chan"}'), _Response(b"{}"))
    assert wd.send_dm("tok", "123", "hi") is True
    assert slept == [pytest.approx(1.0)]


def test_send_dm_reports_failure_rather_than_raising(replies, slept):
    """A give-up must stay contained: main()'s poll loop has no handler."""
    replies(*[_http_error(429, header="1") for _ in range(wd.MAX_ATTEMPTS)])
    assert wd.send_dm("tok", "123", "hi") is False


# ── main()'s alert latch ──────────────────────────────────────────────────
#
# send_dm returning False is only useful if the caller reads it. It did not:
# main() set ``alerted = True`` next to the call regardless, so one failed DM
# silenced every later attempt for the rest of the outage — the retry budget
# above bought nothing once it was exhausted. These two pin both directions.


class _StopLoop(Exception):
    """Breaks main()'s `while True` from inside the patched sleep."""


def _run_main(monkeypatch, *, send_results, max_sleeps=6):
    """Drive main() against a permanently-down unit; return the DMs attempted."""
    sent: list[str] = []
    results = list(send_results)

    def fake_send_dm(token, user_id, content):
        sent.append(content)
        return results.pop(0) if results else False

    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= max_sleeps:
            raise _StopLoop

    monkeypatch.setattr(wd, "parse_env", lambda _p: {"DISCORD_TOKEN_PROD": "tok", "WATCHDOG_USER_ID": "1"})
    monkeypatch.setattr(wd, "unit_state", lambda _u: "failed")
    monkeypatch.setattr(wd, "send_dm", fake_send_dm)
    monkeypatch.setattr(wd.time, "sleep", fake_sleep)
    monkeypatch.setattr(wd.sys, "argv", ["watchdog.py"])

    with pytest.raises(_StopLoop):
        wd.main()
    return sent


def test_a_failed_alert_is_retried_on_the_next_poll(monkeypatch):
    sent = _run_main(monkeypatch, send_results=[False, False, False])
    assert len(sent) > 1, "a DM that never arrived must not latch the alert"


def test_a_delivered_alert_is_sent_once(monkeypatch):
    sent = _run_main(monkeypatch, send_results=[True])
    assert len(sent) == 1, "one alert per outage once it actually lands"
