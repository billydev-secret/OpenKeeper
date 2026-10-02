#!/usr/bin/env python3
"""How long to wait when Discord's REST API says 429, in one place.

Five scripts in this folder call the Discord API directly with ``urllib``
rather than through discord.py — backfills, the QA-card poster, the survivor
enrolment tool — because they run standalone against prod without booting a
bot. Each had grown its own 429 handler, and they disagreed: three read only
the JSON body and defaulted to 1.0 second, which is wrong twice over. The
``Retry-After`` header is what Discord actually documents as authoritative,
and an edge-level 429 from Cloudflare arrives as an HTML error page with no
JSON at all — one of those three called ``json.load(exc)`` on it and would
have raised inside its own error handler.

Deliberately stdlib-only, and deliberately in ``scripts/`` rather than
``src/``: the callers are standalone tools that must run against a checkout
whose venv or bot code may be mid-deploy, and a sibling import costs them
nothing. ``watchdog.py`` still carries its own copy — it is the one script
that must survive a broken tree with no repo import whatsoever, and
``tests/tooling/test_discord_rest.py`` pins the two to the same answers so they
cannot drift apart unnoticed.
"""

from __future__ import annotations

import json
import urllib.error

# Discord's own pad. A retry that lands on the exact boundary is a 429 again,
# so every caller here has always added a fraction; this is that fraction.
PAD = 0.3

# Nothing told us a number. Cloudflare's edge window is ~10s, so wait long
# rather than hammer it — the opposite of the 1.0 default these scripts had.
UNKNOWN_WAIT = 5.0

# The header was there but wasn't a number. Something is off, but the server
# did answer, so a short wait is safer than a long stall.
UNREADABLE_HEADER_WAIT = 1.0


def retry_after(exc: urllib.error.HTTPError) -> float:
    """Seconds to wait before retrying the call that raised ``exc``.

    Header first, then the JSON body, then :data:`UNKNOWN_WAIT`. The body
    parse has to tolerate an HTML page, an empty body and JSON that isn't an
    object, because all three arrive in practice and this runs *inside* an
    error handler, where a second exception loses the original failure.
    """
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if raw is None:
        try:
            raw = json.loads(exc.read() or b"{}").get("retry_after")
        except (ValueError, AttributeError):  # HTML page, or JSON that isn't an object
            raw = None
        if raw is None:
            return UNKNOWN_WAIT
    try:
        return float(raw) + PAD
    except (TypeError, ValueError):
        return UNREADABLE_HEADER_WAIT
