"""Authorization sweep — every API route must reject an unauthenticated caller.

The dashboard's whole security model is "everything is gated" (CLAUDE.md: never
ship an unenforced control). Individual route tests check their own guard, but
nothing guarantees a *newly added* route remembered `Depends(require_perms(...))`.
This sweep enumerates every registered route and, with a real auth backend and
no session, asserts each non-public one comes back 401/403 — never a success.

It runs the *real* `DiscordOAuthAuth` backend (not the `OpenAuth` bypass the
other route tests use, which would pass everything as admin) with no cookie, so
a route missing its dependency shows up as a 2xx/redirect and fails the sweep.
"""

from __future__ import annotations

import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from web_server.auth import DiscordOAuthAuth
from web_server.server import create_app

# Routes intentionally reachable without a session. Matched exactly (or by the
# static-mount prefix). Everything else under the app must be gated.
PUBLIC_PATHS = {
    "/",              # index — redirects to /login when unauthenticated
    "/login",         # login page HTML
    "/auth/discord",  # OAuth start
    "/callback",      # OAuth callback
    "/logout",        # session teardown
}
# Swagger UI and the OpenAPI schema used to sit in the set above — FastAPI's
# built-in routes take no dependency, so they were public by construction and
# handed anyone who found the URL a browsable inventory of every route and
# request body. server.py now registers both by hand behind an admin gate;
# they are include_in_schema=False, so this sweep can't see them, and they get
# explicit tests at the bottom of this file like the session-gated manual.
# /static assets (css/js) are public — except manual.html, which is served by a
# dedicated session-gated route registered ahead of the mount (server.py). The
# openapi()-driven sweep below can't see that route (include_in_schema=False),
# so it gets its own explicit tests at the bottom of this file.
PUBLIC_PREFIXES = ("/static",)

# Auth rejects an unauthenticated caller with one of these. A 2xx or a redirect
# to anywhere but /login means the route let an anonymous request through.
REJECT_CODES = {401, 403}


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _concrete(path: str) -> str:
    """Fill path params with a throwaway value so the URL actually resolves."""
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture
def noauth_client(fake_ctx) -> Generator[tuple[TestClient, object], None, None]:
    """A client on the real OAuth backend with **no** session cookie."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    # follow_redirects=False so the index's 302→/login is visible, not chased.
    client = TestClient(app, follow_redirects=False)
    yield client, app
    client.close()


def _iter_routes(app):
    """(path, method) for every registered operation, skipping HEAD/OPTIONS.

    Derived from ``app.openapi()`` — the same public schema Swagger UI and
    real clients use — rather than walking ``app.routes`` directly. FastAPI's
    ``include_router`` doesn't reliably flatten a sub-router's routes into
    ``app.routes`` as plain ``APIRoute`` instances across versions (a newer
    FastAPI defers them behind an internal lazy wrapper instead), so a direct
    ``isinstance(route, APIRoute)`` walk can silently see almost nothing.
    ``openapi()`` reflects whatever is actually reachable regardless of that
    internal representation.
    """
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue  # skip head/options and any non-verb PathItem key
            yield path, method.upper()


def test_every_api_route_is_registered_under_auth(noauth_client):
    """Sanity: the app actually mounted a substantial API surface to check."""
    _, app = noauth_client
    api_routes = [p for p, _ in _iter_routes(app) if p.startswith("/api")]
    assert len(api_routes) > 50, f"only {len(api_routes)} /api routes — did routers mount?"


def test_no_route_serves_an_unauthenticated_caller(noauth_client):
    """Every non-public route rejects a request that carries no session."""
    client, app = noauth_client

    leaks: list[str] = []
    for i, (path, method) in enumerate(_iter_routes(app)):
        if _is_public(path):
            continue
        url = _concrete(path)
        # The app rate-limits per client IP (bucketed on CF-Connecting-IP); a
        # unique IP per request gives each its own fresh bucket, so the limiter
        # can't 429 the sweep and mask the auth result we're actually checking.
        resp = client.request(method, url, headers={"cf-connecting-ip": f"10.9.{i // 256}.{i % 256}"})
        code = resp.status_code
        # The security property: an anonymous caller must never succeed. 401/403
        # are the intended rejections; a 2xx or a 3xx (session-less redirect into
        # the app) is a leak. 404/405/422 mean the request didn't take effect for
        # an unrelated reason — not a leak, but flagged separately below.
        if code < 400:
            leaks.append(f"{method} {path} → {code} (served without auth!)")
        elif code not in REJECT_CODES and code not in (404, 405):
            leaks.append(f"{method} {path} → {code} (expected 401/403)")

    assert not leaks, "Auth gaps:\n" + "\n".join(leaks)


# ── The tier below moderator ────────────────────────────────────────────────
# The sweep above proves an *anonymous* caller gets nothing. It said nothing
# about the next tier up: a guild member who has signed in and holds no
# dashboard permission at all. That caller carries a valid session cookie, so
# every "is there a user?" check passes for them — only the permission sets
# stop them, and nothing tested that the permission sets were there.
#
# The distinction matters because the two mistakes look identical from
# outside. A route with no dependency at all is caught above. A route that
# writes `require_perms(set())` where it meant `require_perms({"admin"})` is
# caught by nothing — it refuses the anonymous caller exactly as it should,
# and hands the whole panel to any member of the server.
#
# The tier is real and deliberate, which is why it needs a floor rather than a
# ban: Wellness is member self-service, the Mahjong card viewer is
# session-gated by an explicit design decision, and the Help advisor answers
# members. Every one of those is listed below by hand. Adding a route to this
# list should feel like a decision, because it is one.

MEMBER_PATHS: set[tuple[str, str]] = {
    # Session identity and the guild switcher — a member has to be able to
    # find out who the dashboard thinks they are and which guild is active.
    ("GET", "/api/me"),
    ("GET", "/api/guilds"),
    ("POST", "/api/guilds/{guild_id}/select"),
    # The member landing panel.
    ("GET", "/api/home"),
    # Mahjong's card viewer: session-gated to any logged-in member on purpose
    # (docs/plans/meadow-mahjong.md D6) rather than anonymous or admin-only.
    ("GET", "/api/mahjong/card"),
    # The Help advisor answers members; what it is allowed to say is scoped
    # inside the route by `can_see_config`/`is_staff`, not by this gate. It
    # sits in the "ai" rate-limit tier (5 burst, 1 per 10s per IP).
    ("GET", "/api/help/advisor/name"),
    ("POST", "/api/help/advisor"),
    # Wellness is member self-service end to end: a member reads and edits
    # their own break settings, caps, blackouts and pause state.
    ("GET", "/api/wellness/me"),
    ("GET", "/api/wellness/history"),
    ("GET", "/api/wellness/activity-histogram"),
    ("POST", "/api/wellness/settings"),
    ("POST", "/api/wellness/pause"),
    ("GET", "/api/wellness/away"),
    ("POST", "/api/wellness/away"),
    ("GET", "/api/wellness/caps"),
    ("POST", "/api/wellness/caps"),
    ("PUT", "/api/wellness/caps/{cap_id}"),
    ("GET", "/api/wellness/blackouts"),
    ("POST", "/api/wellness/blackouts"),
    ("PUT", "/api/wellness/blackouts/{blackout_id}/toggle"),
}


def test_no_route_serves_a_permissionless_member(noperms_client):
    """Every route not on the member list refuses a signed-in member."""
    client = noperms_client

    leaks: list[str] = []
    for i, (path, method) in enumerate(_iter_routes(client.app)):
        if _is_public(path) or (method, path) in MEMBER_PATHS:
            continue
        url = _concrete(path)
        resp = client.request(
            method, url, headers={"cf-connecting-ip": f"10.8.{i // 256}.{i % 256}"}
        )
        code = resp.status_code
        if code < 400:
            leaks.append(f"{method} {path} → {code} (served to a member!)")
        elif code not in REJECT_CODES and code not in (404, 405):
            # A 422 here would mean body validation ran before the permission
            # check — i.e. the guard is not the first thing the route does.
            leaks.append(f"{method} {path} → {code} (expected 401/403)")

    assert not leaks, (
        "Routes reachable by a signed-in member with no permissions. Either "
        "the route is missing its permission set, or it is a deliberate "
        "member surface and belongs in MEMBER_PATHS above — with a line "
        "saying why.\n" + "\n".join(leaks)
    )


def test_the_member_allowlist_has_no_stale_entries(noperms_client):
    """An allowlist that outlives its routes quietly widens as paths move."""
    registered = {(method, path) for path, method in _iter_routes(noperms_client.app)}
    stale = MEMBER_PATHS - registered
    assert not stale, f"MEMBER_PATHS names routes that no longer exist: {sorted(stale)}"


# ── Manual gating (W-H1) ────────────────────────────────────────────────────
# The staff/mod manual is the one static file that must NOT be world-readable:
# it documents the entire mod toolkit. server.py serves it via a session-gated
# route registered ahead of the /static mount.


def test_manual_requires_a_session(noauth_client):
    """Anonymous GET of the manual redirects to login instead of serving it."""
    client, _ = noauth_client
    resp = client.get("/static/manual.html")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_manual_served_to_authenticated_session(authed_client):
    resp = authed_client.get("/static/manual.html")
    assert resp.status_code == 200
    assert "Dungeon Keeper" in resp.text


def test_manual_served_under_open_auth(open_client):
    """The explicit LAN-only OpenAuth backend keeps the manual reachable."""
    resp = open_client.get("/static/manual.html")
    assert resp.status_code == 200


def test_other_static_assets_stay_public(noauth_client):
    """Gating the manual must not gate the rest of /static (login page & app
    assets load pre-session)."""
    client, _ = noauth_client
    resp = client.get("/static/app.css")
    assert resp.status_code == 200


# ── API documentation gating (row 3.27) ─────────────────────────────────────
# Swagger UI and the schema behind it are an admin tool, not a public one.
# Kept as their own tests because `include_in_schema=False` hides them from the
# openapi()-driven sweep above — the same reason manual.html has tests here.

DOC_ROUTES = [
    pytest.param("/api/_docs", id="swagger-ui"),
    pytest.param("/api/openapi.json", id="openapi-schema"),
]


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_docs_refuse_an_anonymous_caller(noauth_client, path):
    """No session, no schema — and no redirect into the app either."""
    client, _ = noauth_client
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_docs_refuse_a_signed_in_member(noperms_client, path):
    """Signing in is not enough. A member with no dashboard permissions is the
    realistic attacker here: they already hold a valid session cookie."""
    assert noperms_client.get(path).status_code == 403


def test_swagger_ui_is_served_to_an_admin(authed_client):
    resp = authed_client.get("/api/_docs")
    assert resp.status_code == 200
    assert "swagger-ui" in resp.text


def test_openapi_schema_is_served_to_an_admin(authed_client):
    resp = authed_client.get("/api/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    assert "/api/health/tiles" in body["paths"], "schema should be the real one"


def test_the_ungated_builtin_docs_routes_are_gone(noauth_client):
    """FastAPI's defaults must stay switched off; a `docs_url` left set would
    re-register an ungated twin alongside the gated pair."""
    client, _ = noauth_client
    for path in ("/docs", "/redoc", "/openapi.json", "/api/_docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path
