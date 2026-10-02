"""Every route that can reach the Anthropic API must sit on the "ai" rate tier.

``_TIER_ROUTES`` (web_server/server.py) is a hand-maintained list of path
prefixes -> tier name; anything not listed falls through to the 60-burst
"default" tier. LegitLibs' AI Prep route (``POST /api/games/legitlibs/
ai-prep``) shipped on that default tier because nothing enforced the mapping:
its handler reaches the Anthropic API via ``generate_text()``, imported
*locally* inside the function body (games.py, inside ``ll_ai_prep``) rather
than at module level, so it wouldn't show up in a module-import scan either.

This test source-scans each registered ``/api`` route's own endpoint
function for the markers that indicate an Anthropic call, and asserts every
route it finds is mapped to the "ai" tier — so a future route that reaches
Anthropic without an entry in ``_TIER_ROUTES`` fails the suite instead of
shipping on the 60-burst default.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator

from fastapi import FastAPI

from web_server.auth import OpenAuth
from web_server.server import _get_tier, create_app

# Substrings that indicate a handler's own source reaches the Anthropic API.
# Tied to real call sites as of this writing:
#   - "generate_text(" -> bot_modules/games/utils/ai_client.py (Anthropic client)
#   - "generate_json(" -> the same client with a JSON schema (LegitLibs AI prep)
#   - "AsyncAnthropic"  -> the raw SDK client, in case a future route talks to
#                          Anthropic directly instead of through ai_client
#   - "answer_advisor(" -> bot_modules/advisor/service.py
_ANTHROPIC_MARKERS = (
    "generate_text(", "generate_json(", "AsyncAnthropic", "answer_advisor(",
)


def _iter_endpoints(app: FastAPI) -> Iterator[tuple[str, Callable[..., object]]]:
    """Yield (full path, endpoint function) for every registered route.

    FastAPI 0.139 stopped flattening ``include_router()`` into ``app.routes``:
    an included router stays nested as an ``_IncludedRouter`` entry with no
    ``path`` or ``endpoint`` of its own, so a flat walk of ``app.routes``
    sees only FastAPI's three built-in routes and this test would find no
    Anthropic routes at all (the remote runner installs from the lock, which
    pins 0.139). ``fastapi.routing.iter_route_contexts`` is that version's
    supported walk of the effective routes, prefixes already applied; older
    versions have no such helper and keep the flat list.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        pairs = [(getattr(r, "path", None), getattr(r, "endpoint", None)) for r in app.routes]
    else:
        pairs = [(ctx.path, getattr(ctx.route, "endpoint", None))
                 for ctx in iter_route_contexts(app.routes)]
    for path, endpoint in pairs:
        if path is not None and endpoint is not None:
            yield path, endpoint


def _anthropic_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Return (path, handler qualname) for every /api route whose own
    endpoint source contains an Anthropic call marker.

    Scans the endpoint function's *own* source via ``inspect.getsource``,
    not the module's top-level imports — games.py's
    ``from bot_modules.games.utils.ai_client import generate_text`` is a
    local import inside ``ll_ai_prep`` itself, invisible to a module-level
    import scan.
    """
    found: list[tuple[str, str]] = []
    for path, endpoint in _iter_endpoints(app):
        if not path.startswith("/api"):
            continue
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        if any(marker in source for marker in _ANTHROPIC_MARKERS):
            found.append((path, endpoint.__qualname__))
    return found


def test_every_anthropic_route_is_in_the_ai_rate_tier(fake_ctx):
    app = create_app(fake_ctx, auth=OpenAuth())
    routes = _anthropic_routes(app)

    assert routes, (
        "found no /api routes matching the Anthropic call markers "
        f"{_ANTHROPIC_MARKERS} — either the app failed to register routes, "
        "or the marker list has gone stale and needs updating to match "
        "however routes now call the Anthropic API"
    )

    offenders = [
        f"{path} ({qualname}) -> tier {_get_tier(path)!r}"
        for path, qualname in routes
        if _get_tier(path) != "ai"
    ]
    assert not offenders, (
        "route(s) reach the Anthropic API but aren't mapped to the ai rate "
        "tier in _TIER_ROUTES (web_server/server.py) — add a prefix entry "
        "for each: " + "; ".join(offenders)
    )
