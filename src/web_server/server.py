"""FastAPI app factory and uvicorn launcher for the dashboard.

The launcher coroutine ``serve_forever`` is designed to be appended to
``bot.startup_task_factories`` in ``dungeonkeeper.py``. It shares the bot's
event loop, reuses ``AppContext`` / ``ctx.open_db()``, and reads live
``ctx.bot.get_guild(...)`` data.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from bot_modules.auto_delete.service import init_auto_delete_tables
from bot_modules.core.branding_service import init_db as init_branding_db
from bot_modules.confessions.service import init_db as init_confessions_db
from bot_modules.dm.perms.service import init_db as init_dm_perms_db
from bot_modules.analytics.health_service import init_health_tables
from bot_modules.moderation.service import init_moderation_tables
from bot_modules.wellness.service import init_wellness_tables
from web_server.auth import (
    SESSION_COOKIE,
    AuthBackend,
    AuthenticatedUser,
    DiscordOAuthAuth,
    OpenAuth,
)
from web_server.deps import require_perms

_STATIC_DIR = Path(__file__).parent / "static"
_log = logging.getLogger("dungeonkeeper.web")

# Per-boot cache-buster. Every module import in served JS, and every ?v= in
# served HTML, gets rewritten to carry this token so browsers (and Cloudflare)
# treat each reboot as a fresh URL set.
_BOOT_ID = str(int(time.time()))

# Matches `from "./x.js"`, `from '../x.js'`, `import("./x.js")`, etc.
# Only relative specifiers (./ or ../) so we don't touch bare imports or URLs.
_JS_IMPORT_RE = re.compile(
    r'''((?:\bfrom|\bimport)\s*\(?\s*["'])(\.{1,2}/[^"']+\.js)(["'])'''
)


@lru_cache(maxsize=4)
def _html_with_boot_id(name: str) -> str:
    text = (_STATIC_DIR / name).read_text(encoding="utf-8")
    return re.sub(r"\?v=\d+", f"?v={_BOOT_ID}", text)


# ── Rate limiting ────────────────────────────────────────────────────
# Simple per-IP token bucket. No external dependency needed.

# Tier definitions: (max_tokens, refill_per_second)
_RATE_TIERS: dict[str, tuple[int, float]] = {
    "ai":      (5, 0.1),     # 5 burst, 1 per 10s — expensive AI calls
    "auth":    (10, 0.2),    # 10 burst, 1 per 5s — login/callback
    "default": (60, 2.0),    # 60 burst, 2/s — normal API
}

# Map path prefixes to tiers. Every route that can reach the Anthropic API
# belongs in "ai" — tests/web/test_rate_limit_tiers.py source-scans every
# registered route for the Anthropic call sites and fails if one is missing
# here.
_TIER_ROUTES: list[tuple[str, str]] = [
    ("/api/help/advisor",            "ai"),
    ("/api/games/legitlibs/ai-prep", "ai"),
    ("/login",                       "auth"),
    ("/callback",                    "auth"),
]


class _RateBucket:
    __slots__ = ("tokens", "last_refill", "max_tokens", "refill_rate")

    def __init__(self, max_tokens: float, refill_rate: float) -> None:
        self.tokens = max_tokens
        self.last_refill = time.monotonic()
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate

    def consume(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ip → tier → bucket
_buckets: dict[str, dict[str, _RateBucket]] = defaultdict(dict)
_BUCKET_EXPIRY = 600  # prune IPs not seen in 10 minutes
_last_prune = time.monotonic()


def _get_tier(path: str) -> str:
    for prefix, tier in _TIER_ROUTES:
        if path.startswith(prefix):
            return tier
    return "default"


def _client_ip(request: Request) -> str:
    """Real client IP for rate-limiting.

    Behind the Cloudflare tunnel the socket peer is always the loopback origin,
    so every user would otherwise share one bucket. Cloudflare sets
    ``CF-Connecting-IP``; trust it (and X-Forwarded-For's first hop as a
    fallback) since the origin is only reachable through the tunnel. Falls back
    to the socket peer for direct/LAN access.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class _RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        global _last_prune  # noqa: PLW0603

        path = request.url.path
        # Skip static files — no rate limit needed
        if path.startswith("/static/"):
            return await call_next(request)

        ip = _client_ip(request)
        tier = _get_tier(path)
        max_tokens, refill_rate = _RATE_TIERS[tier]

        ip_buckets = _buckets[ip]
        bucket = ip_buckets.get(tier)
        if bucket is None:
            bucket = _RateBucket(max_tokens, refill_rate)
            ip_buckets[tier] = bucket

        if not bucket.consume():
            _log.warning("Rate limited %s on %s (tier=%s)", ip, path, tier)
            return JSONResponse(
                {"detail": "Too many requests. Please slow down."},
                status_code=429,
                headers={"Retry-After": str(int(1 / refill_rate))},
            )

        # Periodic prune of stale buckets (every 10 min)
        now = time.monotonic()
        if now - _last_prune > _BUCKET_EXPIRY:
            _last_prune = now
            stale = [
                k for k, v in _buckets.items()
                if all(now - b.last_refill > _BUCKET_EXPIRY for b in v.values())
            ]
            for k in stale:
                del _buckets[k]

        return await call_next(request)


# A session forged from a guessed secret is a full admin session — the cookie
# carries the permission bits the cache-miss path falls back on. 32 chars is
# `secrets.token_urlsafe(24)`; the documented recipe in deployment.md produces
# 43. The distinct-character floor catches the other failure shape: a long but
# degenerate value ("changemechangeme…") that passes a length check.
_MIN_SESSION_SECRET_LEN = 32
_MIN_SESSION_SECRET_DISTINCT = 8


def _require_strong_session_secret(secret: str) -> None:
    """Refuse to start on a session secret weak enough to be forged.

    Same failure direction as the rest of this function: the tunnel publishes
    this dashboard, so a weak secret is not a warning-and-continue situation
    (2026-08 review, Art 32 / web portal security).
    """
    problems = []
    if len(secret) < _MIN_SESSION_SECRET_LEN:
        problems.append(
            f"it is {len(secret)} characters, minimum {_MIN_SESSION_SECRET_LEN}"
        )
    if len(set(secret)) < _MIN_SESSION_SECRET_DISTINCT:
        problems.append(
            f"it uses only {len(set(secret))} distinct characters, "
            f"minimum {_MIN_SESSION_SECRET_DISTINCT}"
        )
    if problems:
        raise RuntimeError(
            "SESSION_SECRET is too weak: "
            + "; ".join(problems)
            + '. Generate one with: python -c "import secrets; '
            'print(secrets.token_urlsafe(32))" — then restart. Anyone who '
            "guesses this value can forge an admin session cookie."
        )


def _auto_detect_auth(guild_id: int) -> AuthBackend:
    """Pick an auth backend based on environment variables — failing *closed*.

    Discord OAuth2 when DISCORD_CLIENT_ID + SESSION_SECRET are set. The open
    (no-auth, full-admin) backend is served ONLY when explicitly opted into via
    ``DASHBOARD_OPEN_AUTH=1``. A missing OAuth secret must NOT silently fall back
    to admin-to-everyone: the cloudflared tunnel bridges the loopback bind to the
    public internet, so a dropped env var would otherwise publish an unguarded
    admin dashboard. When neither is configured we refuse to start.
    """
    client_id = os.getenv("DISCORD_CLIENT_ID")
    session_secret = os.getenv("SESSION_SECRET")

    if client_id and session_secret:
        _require_strong_session_secret(session_secret)
        support_user_id = int(os.getenv("SUPPORT_USER_ID", "0") or "0")
        _log.info("Discord OAuth2 authentication enabled (client_id=%s)", client_id)
        return DiscordOAuthAuth(session_secret, guild_id, support_user_id=support_user_id)

    if os.getenv("DASHBOARD_OPEN_AUTH") == "1":
        _log.warning(
            "Open (no-auth, full-admin) dashboard — DASHBOARD_OPEN_AUTH=1. Intended "
            "for a trusted LAN only; never expose this backend through a tunnel."
        )
        return OpenAuth()

    raise RuntimeError(
        "Dashboard auth is unconfigured. Set DISCORD_CLIENT_ID + SESSION_SECRET "
        "to enable Discord OAuth, or DASHBOARD_OPEN_AUTH=1 to explicitly run the "
        "open (no-auth, full-admin) backend on a trusted LAN. Refusing to start "
        "rather than silently granting admin access to everyone."
    )


# ── Request body ceiling ─────────────────────────────────────────────
#: Largest request body the dashboard will accept, in bytes.
#:
#: Every upload route already caps its own files at 8 MB, but nothing capped
#: the *request*: the colour-swatch upload takes a `list[UploadFile]` with no
#: limit on the count, and every JSON route would happily have Starlette read
#: an arbitrarily large body into memory before the route saw a byte of it.
#: One oversized POST was enough to push the bot's process into swap.
#:
#: 32 MB is deliberately generous — four files at the 8 MB per-file cap, which
#: is more than any panel sends — because the purpose here is to stop the
#: absurd request, not to second-guess the per-route limits. Those stay where
#: they are and still do the real validation.
MAX_BODY_BYTES = 32 * 1024 * 1024


class _BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Refuse an oversized request on its declared ``Content-Length``.

    Checked before the body is read, so an absurd upload costs one header
    parse rather than the memory to buffer it.

    **This does not cover a chunked request that sends no Content-Length at
    all.** Enforcing that means counting bytes as they stream, which a
    BaseHTTPMiddleware cannot do without taking over the receive channel from
    every route beneath it. Both real clients here — the dashboard's own
    fetch() calls and the reverse proxy in front of it — always declare a
    length, and cloudflared caps what it will forward in any case; this is the
    cheap ceiling, not the whole of it.
    """

    async def dispatch(self, request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                return JSONResponse(
                    {"detail": "Malformed Content-Length header."},
                    status_code=400,
                )
            # Read the module global per request so a test can lower it
            # without having to send tens of megabytes to prove the limit.
            if size > MAX_BODY_BYTES:
                return JSONResponse(
                    {
                        "detail": (
                            "Request body too large — the limit is "
                            f"{MAX_BODY_BYTES // (1024 * 1024)} MB."
                        )
                    },
                    status_code=413,
                )
        return await call_next(request)


# ── Security headers ─────────────────────────────────────────────────
# Sent on every response by _SecurityHeadersMiddleware. None of these depend
# on the request, so they are a middleware rather than a per-route dependency
# — a header a new route can forget is not a control.
_SECURITY_HEADERS: dict[str, str] = {
    # Stop a browser guessing a content type. The dashboard serves member-
    # supplied strings back as JSON; sniffed as HTML, one of them is stored XSS.
    "X-Content-Type-Options": "nosniff",
    # Clickjacking. frame-ancestors below is the modern form and wins where
    # both are understood; this stays for anything that only knows the old one.
    "X-Frame-Options": "DENY",
    # Don't leak a dashboard URL — several carry ids in the query string — into
    # a third-party site's referer log.
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

#: The policy was chosen by checking what the panels actually load, not by
#: writing the strictest string that parses — a CSP that breaks a panel is
#: worse than no CSP, because the next person deletes it wholesale.
#:
#: * ``script-src 'self'`` with no 'unsafe-inline': the three inline scripts
#:   the dashboard had (login.html's error decoder, manual.html's TOC builder,
#:   and its print button's onclick) move to login.js/manual.js in this same
#:   commit. There is no nonce because a nonce does not cover an inline
#:   handler attribute anyway, and a policy carrying both a nonce and
#:   'unsafe-inline' makes the browser ignore the 'unsafe-inline'.
#: * ``style-src`` keeps 'unsafe-inline'. The panels build markup as strings
#:   with style="..." in them in dozens of places; dropping it would restyle
#:   half the dashboard into unreadability for a much smaller prize than the
#:   script rule buys. (Element.style assignments from JS are CSSOM and are
#:   not governed by this at all.)
#: * ``img-src`` names cdn.discordapp.com because avatars come from there.
#: * ``connect-src 'self'``: every fetch the dashboard makes is same-origin.
_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: https://cdn.discordapp.com",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    )
)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the static security headers to every response.

    Registered last, so it is the outermost layer: a 429 from the rate
    limiter or a 401 from an auth dependency carries them too, which matters
    because those are exactly the responses an attacker sees most of.

    ``setdefault`` rather than assignment so a route that has a reason to send
    its own value keeps it.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path not in _CSP_EXEMPT:
            response.headers.setdefault("Content-Security-Policy", _CSP)
        return response


DOCS_PATH = "/api/_docs"
OPENAPI_PATH = "/api/openapi.json"

#: Swagger UI loads its own CSS and JS from cdn.jsdelivr.net, so `script-src
#: 'self'` renders it as a blank page. Vendoring those assets to buy a CSP on
#: one admin-gated developer tool is not worth it, so the page is exempt and
#: the rest of the dashboard keeps the strict policy. The schema route it
#: fetches is JSON and is not exempt.
_CSP_EXEMPT = frozenset({DOCS_PATH})


def _install_gated_docs(app: FastAPI) -> None:
    """Serve Swagger UI and the OpenAPI schema to admins only.

    FastAPI's built-in ``docs_url``/``openapi_url`` routes accept no
    dependency, so the only way to gate them is to turn them off and register
    the pair by hand. Until now anyone who found the URL — it is not linked
    from anywhere, but it is not secret either — got a complete, browsable
    inventory of every route on the dashboard, including the shape of every
    request body. That is a reconnaissance shortcut with no upside: the people
    who need the schema are the people who can already sign in as an admin.

    Both are ``include_in_schema=False``: the docs routes have no business
    documenting themselves, and the authz sweep reads ``app.openapi()``, so a
    self-listing route would have to be re-added to its public allowlist. They
    get explicit tests instead, exactly as the session-gated manual does.
    """
    from fastapi.openapi.docs import get_swagger_ui_html

    _admin = Depends(require_perms({"admin"}))

    @app.get(OPENAPI_PATH, include_in_schema=False)
    async def _openapi_schema(_: AuthenticatedUser = _admin):
        return JSONResponse(app.openapi())

    @app.get(DOCS_PATH, include_in_schema=False)
    async def _swagger_ui(_: AuthenticatedUser = _admin):
        # Swagger UI fetches OPENAPI_PATH from the browser, same-origin, so the
        # session cookie rides along and the schema route's own gate applies.
        return get_swagger_ui_html(
            openapi_url=OPENAPI_PATH,
            title=f"{app.title} — API",
        )


def create_app(ctx, auth: AuthBackend | None = None) -> FastAPI:  # noqa: ANN001
    # Swagger UI lives at /api/_docs, NOT /api/docs — the latter is a real REST
    # resource (the Docs feature's list endpoint). FastAPI registers the docs_url
    # route before our routers, so pointing it at /api/docs would shadow
    # GET /api/docs and return Swagger HTML where JSON is expected.
    #
    # Both are registered by hand at the bottom of this function instead of by
    # FastAPI, because the built-in routes take no dependency and so cannot be
    # gated. See _install_gated_docs.
    app = FastAPI(
        title="Dungeon Keeper Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = ctx
    app.state.auth = auth or _auto_detect_auth(ctx.guild_id)

    # Ensure tables exist (in case bot hasn't restarted yet)
    with ctx.open_db() as conn:
        init_moderation_tables(conn)
        init_wellness_tables(conn)
        init_health_tables(conn)
        init_auto_delete_tables(conn)
    init_confessions_db(ctx.db_path)
    init_dm_perms_db(ctx.db_path)
    init_branding_db(ctx.db_path)

    # ── OAuth routes (login / callback / logout) ────────────────────
    from web_server.routes import oauth as oauth_routes
    from web_server.routes import spotify_oauth as spotify_oauth_routes

    app.include_router(oauth_routes.router, tags=["auth"])
    app.include_router(spotify_oauth_routes.router, tags=["spotify-auth"])

    # ── API routes ──────────────────────────────────────────────────
    from web_server.routes import ai_routes
    from web_server.routes import config as config_routes
    from web_server.routes import economy as economy_routes
    from web_server.routes import economy_manager as economy_manager_routes
    from web_server.routes import feature_rotation as feature_rotation_routes
    from web_server.routes import home as home_routes
    from web_server.routes import logs as logs_routes
    from web_server.routes import mention_awards as mention_awards_routes
    from web_server.routes import meta as meta_routes
    from web_server.routes import moderation as moderation_routes
    from web_server.routes import privacy as privacy_routes
    from web_server.routes import reports as reports_routes
    from web_server.routes import telemetry as telemetry_routes

    app.include_router(telemetry_routes.router, prefix="/api", tags=["telemetry"])
    app.include_router(home_routes.router, prefix="/api", tags=["home"])
    app.include_router(meta_routes.router, prefix="/api", tags=["meta"])
    app.include_router(reports_routes.router, prefix="/api/reports", tags=["reports"])
    app.include_router(config_routes.router, prefix="/api", tags=["config"])
    app.include_router(economy_routes.router, prefix="/api", tags=["economy"])
    app.include_router(
        economy_manager_routes.router, prefix="/api", tags=["economy-manager"]
    )
    app.include_router(
        feature_rotation_routes.router, prefix="/api", tags=["feature-rotation"]
    )
    app.include_router(ai_routes.router, prefix="/api", tags=["ai"])
    app.include_router(moderation_routes.router, prefix="/api", tags=["moderation"])
    app.include_router(privacy_routes.router, prefix="/api", tags=["privacy"])
    app.include_router(logs_routes.router, prefix="/api", tags=["logs"])
    app.include_router(
        mention_awards_routes.router, prefix="/api", tags=["mention-awards"]
    )

    from web_server.routes import advisor as advisor_routes

    app.include_router(advisor_routes.router, prefix="/api", tags=["advisor"])

    from web_server.routes import todo as todo_routes

    app.include_router(todo_routes.router, prefix="/api", tags=["todos"])

    from web_server.routes import docs as docs_routes

    app.include_router(docs_routes.router, prefix="/api", tags=["docs"])

    from web_server.routes import panels as panels_routes

    app.include_router(panels_routes.router, prefix="/api", tags=["panels"])

    from web_server.routes import games_external as games_external_routes

    app.include_router(
        games_external_routes.router, prefix="/api", tags=["games-external"]
    )

    from web_server.routes import role_menus as role_menus_routes

    app.include_router(role_menus_routes.router, prefix="/api", tags=["role-menus"])

    from web_server.routes import onboarding as onboarding_routes

    app.include_router(onboarding_routes.router, prefix="/api", tags=["onboarding"])

    from web_server.routes import bot_roles as bot_roles_routes

    app.include_router(bot_roles_routes.router, prefix="/api", tags=["bot-roles"])

    from web_server.routes import chat_revive as chat_revive_routes

    app.include_router(chat_revive_routes.router, prefix="/api", tags=["chat-revive"])

    from web_server.routes import survivor as survivor_routes

    app.include_router(survivor_routes.router, prefix="/api", tags=["survivor"])

    from web_server.routes import mahjong as mahjong_routes

    app.include_router(mahjong_routes.router, prefix="/api", tags=["mahjong"])

    from web_server.routes import breaks as breaks_routes

    app.include_router(breaks_routes.router, prefix="/api", tags=["breaks"])

    from web_server.routes import no_contact as no_contact_routes

    app.include_router(no_contact_routes.router, prefix="/api", tags=["no-contact"])

    from web_server.routes import qa as qa_routes

    app.include_router(qa_routes.router, prefix="/api", tags=["qa"])

    # ── Health dashboard routes ────────────────────────────────────
    from web_server.routes import health as health_routes

    app.include_router(health_routes.router, prefix="/api", tags=["health"])

    # ── Wellness routes ─────────────────────────────────────────────
    from web_server.routes import wellness_admin as wellness_admin_routes
    from web_server.routes import wellness_api as wellness_api_routes

    app.include_router(wellness_api_routes.router, prefix="/api/wellness", tags=["wellness"])
    app.include_router(
        wellness_admin_routes.router, prefix="/api/wellness/admin", tags=["wellness-admin"]
    )

    # ── Gender classification ───────────────────────────────────────


    # ── Voice Control admin ──────────────────────────────────────────
    from web_server.routes import voice_master as voice_master_routes

    app.include_router(voice_master_routes.router, prefix="/api", tags=["voice-master"])

    # ── Party Games (PoppyBot) ──────────────────────────────────────
    from web_server.routes import games as games_routes

    app.include_router(games_routes.router, prefix="/api/games", tags=["games"])

    from web_server.routes import scheduled_games as scheduled_games_routes

    app.include_router(
        scheduled_games_routes.router, prefix="/api/games/schedule", tags=["games"]
    )

    from web_server.routes import photo_challenge as photo_challenge_routes

    app.include_router(
        photo_challenge_routes.router, prefix="/api/photo-challenge", tags=["photo-challenge"]
    )

    # ── Announcements ───────────────────────────────────────────────────
    from web_server.routes import announcements as announcements_routes

    app.include_router(
        announcements_routes.router, prefix="/api/announcements", tags=["announcements"]
    )

    # ── Quotes ──────────────────────────────────────────────────────────
    from web_server.routes import quotes as quotes_routes

    app.include_router(quotes_routes.router, prefix="/api", tags=["quotes"])

    # ── Bios ────────────────────────────────────────────────────────────
    from web_server.routes import bios as bios_routes

    app.include_router(bios_routes.router, prefix="/api/bios", tags=["bios"])

    # ── Music Playlist ──────────────────────────────────────────────────
    from web_server.routes import music_playlist as music_playlist_routes

    app.include_router(
        music_playlist_routes.router, prefix="/api", tags=["music-playlist"]
    )

    # Install the log handler so records flow to the SSE stream
    logs_routes.install_log_handler()

    # ── Rate limiting ──────────────────────────────────────────────────
    app.add_middleware(_RateLimitMiddleware)

    # ── Per-boot cache-busting for JS/CSS ──────────────────────────────
    # JS responses get their relative imports rewritten to `...?v={BOOT_ID}`,
    # which cascades through the whole module graph. Content is then safe to
    # cache long because a new boot → new URLs. CSS gets the same treatment
    # via the HTML ?v= token (rewritten in _html_with_boot_id).
    class _CacheBustJS(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            path = request.url.path
            if not (path.startswith("/static/") and path.endswith(".js")):
                return response
            # Never cache a non-200 (e.g. a transient 404 for a not-yet-deployed
            # module) as immutable — doing so poisons the browser/CDN for a year
            # against that exact ?v= URL, even after the file appears.
            if response.status_code != 200:
                response.headers["cache-control"] = "no-store"
                return response
            raw_chunks: list[Any] = []
            async for chunk in cast(StreamingResponse, response).body_iterator:
                raw_chunks.append(chunk)
            body = b"".join(
                c if isinstance(c, bytes) else bytes(c) if isinstance(c, memoryview) else c.encode()
                for c in raw_chunks
            )
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                )
            text = _JS_IMPORT_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}?v={_BOOT_ID}{m.group(3)}",
                text,
            )
            # Also rewrite any standalone ?v=NN tokens (e.g. hardcoded
            # dynamic-import cache-bust constants) so every per-boot URL
            # changes, forcing the browser to refetch the module graph.
            new_body = re.sub(r"\?v=\d+", f"?v={_BOOT_ID}", text).encode("utf-8")
            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers["cache-control"] = "public, max-age=31536000, immutable"
            return Response(
                content=new_body,
                status_code=response.status_code,
                headers=headers,
                media_type="application/javascript",
            )

    app.add_middleware(_CacheBustJS)

    # ── Session idle clock ─────────────────────────────────────────────
    # A session dies after 7 days without a request (``auth.SESSION_IDLE_MAX_AGE``),
    # so a live one is re-signed about hourly. A response that already sets
    # or clears the cookie — login, logout, a guild switch — owns it; a
    # re-stamp there would resurrect a logged-out cookie or undo the switch.
    class _SessionRestamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            auth = app.state.auth
            cookie = request.cookies.get(SESSION_COOKIE)
            if not (isinstance(auth, DiscordOAuthAuth) and cookie):
                return response
            if response.status_code == 401 or any(
                h.startswith(f"{SESSION_COOKIE}=")
                for h in response.headers.getlist("set-cookie")
            ):
                return response
            restamped = auth.restamp_session(cookie)
            if restamped is not None:
                value, remaining = restamped
                from web_server.routes.oauth import _is_secure

                response.set_cookie(
                    SESSION_COOKIE,
                    value,
                    max_age=remaining,
                    httponly=True,
                    samesite="lax",
                    secure=_is_secure(),
                    path="/",
                )
            return response

    app.add_middleware(_SessionRestamp)

    app.add_middleware(_BodySizeLimitMiddleware)

    # Added last → outermost, so every response leaves with the headers,
    # including ones produced by the middleware above it — the 413 above and
    # the rate limiter's 429 included.
    app.add_middleware(_SecurityHeadersMiddleware)

    # ── API documentation (admin only) ──────────────────────────────
    _install_gated_docs(app)

    # ── Static files & HTML entry points ────────────────────────────
    if _STATIC_DIR.exists():
        # The staff/mod manual is session-gated: it documents the whole mod
        # toolkit, so it must not be world-readable. This explicit route is
        # registered BEFORE the /static mount so it wins route matching for
        # exactly this path; every other static asset (css/js/images) stays
        # public. Same auth pattern as the index route below. Covered by the
        # manual-gating tests in tests/web/test_authz_sweep.py.
        @app.get("/static/manual.html", include_in_schema=False)
        async def _manual(request: Request):
            if isinstance(app.state.auth, DiscordOAuthAuth):
                user = await app.state.auth.authenticate(request)
                if user is None:
                    return RedirectResponse("/login", status_code=302)
            return HTMLResponse(
                content=_html_with_boot_id("manual.html"),
                headers={"cache-control": "no-cache"},
            )

        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/login", include_in_schema=False)
        async def _login():
            return HTMLResponse(
                content=_html_with_boot_id("login.html"),
                headers={"cache-control": "no-cache"},
            )

        @app.get("/", include_in_schema=False)
        async def _index(request: Request):
            # When OAuth is active, require a valid session to view the dashboard
            if isinstance(app.state.auth, DiscordOAuthAuth):
                user = await app.state.auth.authenticate(request)
                if user is None:
                    return RedirectResponse("/login", status_code=302)
            return HTMLResponse(
                content=_html_with_boot_id("index.html"),
                headers={"cache-control": "no-cache"},
            )

    return app


async def serve_forever(ctx, host: str, port: int) -> None:  # noqa: ANN001
    """Run the FastAPI app under uvicorn on the current event loop."""
    app = create_app(ctx)
    # Fail closed: never expose the open (no-auth) backend on a non-loopback
    # bind. If OAuth env is missing, force loopback so a dropped env var can't
    # silently publish a full-admin dashboard to all interfaces.
    if isinstance(app.state.auth, OpenAuth) and host not in ("127.0.0.1", "::1", "localhost"):
        _log.critical(
            "Open (no-auth) dashboard requested on %s — forcing 127.0.0.1. Set "
            "DISCORD_CLIENT_ID + SESSION_SECRET to enable OAuth for public access.",
            host,
        )
        host = "127.0.0.1"
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        loop="asyncio",
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    _log.info("Dashboard starting on http://%s:%d", host, port)
    try:
        await server.serve()
    except Exception:  # noqa: BLE001 — top-level guard for a bot startup task: a dashboard crash must not become an unhandled task exception that takes the bot process down; already logged via _log.exception (ruff's default logger-objects doesn't match `_log`)
        _log.exception("Dashboard server crashed")
