"""A refused request must leave a trace in the log.

The four auth dependencies in ``deps.py`` each raised a bare ``HTTPException``
and logged nothing, so an operator reading the log could not tell a
misconfigured permission from an afternoon with no visitors. The admin sees
"Insufficient permissions"; the server used to say nothing at all.

These pin the line itself — level, logger, and the three facts that make it
actionable: which route, who asked, and what they would have needed.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from tests.web.conftest import web_client
from web_server.auth import DiscordOAuthAuth
from web_server.server import create_app

AUTHZ_LOGGER = "dungeonkeeper.web.auth"

# An admin-only route, chosen because its guard is the plain `require_perms`
# path that every panel route uses.
ADMIN_ROUTE = "/api/help/suggestions"


@pytest.fixture
def anon_client(fake_ctx) -> Generator[TestClient, None, None]:
    """The real OAuth backend with no session cookie at all."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = web_client(create_app(fake_ctx, auth=auth))
    yield client
    client.close()


def _authz_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == AUTHZ_LOGGER and r.levelno == logging.WARNING
    ]


def test_unauthenticated_refusal_is_logged_once(anon_client, caplog):
    """A 401 names the route and says plainly that there was no session."""
    client = anon_client
    with caplog.at_level(logging.WARNING, logger=AUTHZ_LOGGER):
        resp = client.get(ADMIN_ROUTE)
    assert resp.status_code == 401

    records = _authz_warnings(caplog)
    assert len(records) == 1, [r.getMessage() for r in records]
    message = records[0].getMessage()
    assert "401" in message
    assert ADMIN_ROUTE in message
    assert "GET" in message


def test_forbidden_refusal_names_the_caller_and_what_was_needed(
    noperms_client, caplog
):
    """A 403 is the line that was missing: *who* was refused, what they hold,
    and what the route wanted — the three facts a misconfigured role needs."""
    with caplog.at_level(logging.WARNING, logger=AUTHZ_LOGGER):
        resp = noperms_client.get(ADMIN_ROUTE)
    assert resp.status_code == 403

    records = _authz_warnings(caplog)
    assert len(records) == 1, [r.getMessage() for r in records]
    message = records[0].getMessage()
    assert "403" in message
    assert ADMIN_ROUTE in message
    assert "4242" in message, "the refused user id is what ties a report to a person"
    assert "admin" in message, "the line must say what the route required"
    assert "no permissions" in message, "and what the caller actually held"


def test_the_response_body_still_says_nothing_extra(noperms_client):
    """The log gets the detail; the caller does not. A 403 body that named the
    required permission would tell an attacker which routes are worth probing."""
    resp = noperms_client.get(ADMIN_ROUTE)
    assert resp.json()["detail"] == "Insufficient permissions"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/games/legitlibs/templates", id="games-host"),
        pytest.param("/api/economy/quests", id="economy-manager"),
        pytest.param("/api/survivor/overview", id="survivor-host"),
    ],
)
def test_the_role_backed_tiers_log_too(noperms_client, caplog, path):
    """``require_game_host``, ``require_economy_manager`` and
    ``require_survivor_host`` each had their own duplicated raise pair. These
    are the tiers a *misconfigured role* actually breaks — the games editor
    role pointing at a deleted role looks identical to an idle afternoon."""
    with caplog.at_level(logging.WARNING, logger=AUTHZ_LOGGER):
        resp = noperms_client.get(path)
    assert resp.status_code == 403
    records = _authz_warnings(caplog)
    assert len(records) == 1, [r.getMessage() for r in records]
    assert "403" in records[0].getMessage()


@pytest.mark.parametrize(
    ("fixture", "path", "code", "detail"),
    [
        pytest.param(
            "anon_client", "/api/wellness/me", 401, "Not authenticated",
            id="wellness-member-401",
        ),
        pytest.param(
            "anon_client", "/api/wellness/admin/dashboard", 401, "Not authenticated",
            id="wellness-admin-401",
        ),
        pytest.param(
            "noperms_client", "/api/wellness/admin/dashboard", 403,
            "Insufficient permissions", id="wellness-admin-403",
        ),
    ],
)
def test_wellness_refusals_use_the_shared_builder(
    request, caplog, fixture, path, code, detail
):
    """The wellness routes kept a private ``require_user``/``require_perms``
    pair that raised a bare 401/403 in silence — ~30 handlers whose refusals
    the operator never saw. They must now refuse exactly as every other panel
    does: the shared body, and one warning line naming the route."""
    client = request.getfixturevalue(fixture)
    with caplog.at_level(logging.WARNING, logger=AUTHZ_LOGGER):
        resp = client.get(path)
    assert resp.status_code == code
    assert resp.json() == {"detail": detail}

    records = _authz_warnings(caplog)
    assert len(records) == 1, [r.getMessage() for r in records]
    message = records[0].getMessage()
    assert str(code) in message
    assert path in message
    if code == 403:
        assert "4242" in message and "manage_server" in message
