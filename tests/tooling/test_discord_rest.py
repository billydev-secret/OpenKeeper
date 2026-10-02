"""The one 429 wait the standalone Discord API scripts share.

Five scripts in ``scripts/`` talk to Discord over ``urllib`` without booting a
bot. They each had their own 429 handler and the three oldest disagreed with
the two newest: body-only, defaulting to 1.0s, and one of them called
``json.load`` straight on the exception — which raises on the HTML page an
edge-level 429 actually returns, inside the error handler that was supposed to
recover. This pins the shared answer, and pins ``watchdog.py``'s deliberate
private copy to the same answers so the exception can't quietly become a
second behaviour again.
"""

from __future__ import annotations

import email.message
import importlib.util
import io
import urllib.error

import pytest
from tests.paths import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rest = _load("discord_rest")
watchdog = _load("watchdog")


def _http_error(*, header: str | None = None, body: bytes = b"") -> urllib.error.HTTPError:
    headers = email.message.Message()
    if header is not None:
        headers["Retry-After"] = header
    return urllib.error.HTTPError(
        "https://discord.com/api/v10/x", 429, "Too Many Requests", headers, io.BytesIO(body)
    )


# ── what Discord told us, and what we wait ────────────────────────────────

CASES = [
    pytest.param({"header": "2"}, 2.3, id="header wins"),
    pytest.param({"header": "0"}, 0.3, id="a zero header is still a header"),
    pytest.param({"header": "1.5", "body": b'{"retry_after": 99}'}, 1.8, id="header beats body"),
    pytest.param({"body": b'{"retry_after": 1.2}'}, 1.5, id="body when no header"),
    pytest.param({"body": b'{"retry_after": "0.75"}'}, 1.05, id="body as a string"),
    pytest.param({"header": "soon"}, 1.0, id="a header that is not a number"),
    pytest.param({"body": b"<html>cloudflare</html>"}, 5.0, id="the edge's HTML page"),
    pytest.param({"body": b""}, 5.0, id="empty body"),
    pytest.param({"body": b"[1, 2]"}, 5.0, id="json that is not an object"),
    pytest.param({"body": b'{"retry_after": null}'}, 5.0, id="body says null"),
    pytest.param({"body": b'{"message": "slow down"}'}, 5.0, id="body without the field"),
]


@pytest.mark.parametrize("kwargs,expected", CASES)
def test_the_shared_wait_reads_what_discord_sent(kwargs, expected):
    assert rest.retry_after(_http_error(**kwargs)) == pytest.approx(expected)


@pytest.mark.parametrize("kwargs,expected", CASES)
def test_the_watchdogs_private_copy_has_not_drifted(kwargs, expected):
    """watchdog.py may not import this module; it must still agree with it."""
    assert watchdog._retry_after(_http_error(**kwargs)) == pytest.approx(expected)


def test_nothing_told_us_waits_longer_than_the_old_default():
    """The three scripts this replaced defaulted to 1.0s and hammered the edge."""
    assert rest.UNKNOWN_WAIT > 1.0


def test_the_edges_html_page_does_not_raise_inside_the_error_handler():
    """A deleted backfill script once called json.load on this and blew up."""
    assert rest.retry_after(_http_error(body=b"<!DOCTYPE html><h1>error 1015</h1>")) == 5.0


# ── every caller reaches the shared one ───────────────────────────────────


def test_every_standalone_api_script_shares_the_wait():
    """A sixth inline 429 handler is how the first five drifted apart."""
    sharers = [
        "post_testing_docs.py",
        # enroll_survivor_sim_players.py shared it too; it has run and sits in scripts/archive/.
    ]
    for name in sharers:
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "from discord_rest import retry_after" in source, f"{name} must share the wait"
        assert "get(\"retry_after\"" not in source, f"{name} still parses retry_after itself"

    # The one exception, stated in its own docstring rather than left implicit.
    watchdog_source = (SCRIPTS / "watchdog.py").read_text(encoding="utf-8")
    assert "from discord_rest import" not in watchdog_source
    assert "stdlib" in watchdog_source
