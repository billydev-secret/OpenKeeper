"""A request body has a ceiling, checked before the body is read.

Every upload route caps its own *files* at 8 MB, but nothing capped the
*request*. The colour-swatch upload takes a `list[UploadFile]` with no limit on
the count, and every JSON route would have had Starlette read an arbitrarily
large body into memory before the route saw a byte of it — one oversized POST
was enough to push the bot's process into swap.

`_BodySizeLimitMiddleware` refuses on the declared `Content-Length`, so an
absurd upload costs one header parse instead of the memory to buffer it.
"""

from __future__ import annotations

import pytest

from web_server.server import MAX_BODY_BYTES

# Any authenticated POST will do; the middleware runs before the route.
POST_ROUTE = "/api/telemetry/panel"
BODY = {"panel": "home"}


def _post(client, declared: str | None):
    headers = {"content-length": declared} if declared is not None else {}
    return client.post(POST_ROUTE, json=BODY, headers=headers)


def test_an_absurd_content_length_is_refused(open_client):
    resp = _post(open_client, str(MAX_BODY_BYTES + 1))
    assert resp.status_code == 413
    assert "32 MB" in resp.json()["detail"], "the limit belongs in the message"


def test_the_limit_itself_is_allowed(open_client):
    """A ceiling, not a fence one byte inside it."""
    assert _post(open_client, str(MAX_BODY_BYTES)).status_code != 413


def test_an_ordinary_request_is_untouched(open_client):
    """The cheap check must not cost every normal request its result."""
    resp = _post(open_client, None)
    assert resp.status_code == 200


def test_a_get_with_no_body_is_untouched(open_client):
    assert open_client.get("/api/health/tiles").status_code == 200


def test_a_malformed_content_length_is_refused(open_client):
    """A header that isn't a number is a broken client, not a big upload —
    400 rather than 413, and neither is a 500."""
    resp = _post(open_client, "not-a-number")
    assert resp.status_code == 400
    assert "Content-Length" in resp.json()["detail"]


def test_the_refusal_carries_the_security_headers(open_client):
    """Proves the ordering: the security-headers middleware is registered
    after this one, so it wraps it and the 413 is not a bare response."""
    resp = _post(open_client, str(MAX_BODY_BYTES + 1))
    assert resp.status_code == 413
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_the_ceiling_leaves_room_for_the_per_route_caps(open_client):
    """The two limits are layered on purpose. Every upload route caps its own
    files at 8 MB and still does the real validation; this one exists to stop
    the absurd request, so it sits well above a legitimate multi-file post
    rather than second-guessing those caps."""
    assert MAX_BODY_BYTES >= 4 * 8 * 1024 * 1024


@pytest.mark.xfail(
    reason=(
        "Known and deliberate gap: a chunked request declares no "
        "Content-Length, so there is nothing to check before the body is "
        "read. Counting bytes as they stream means taking over the receive "
        "channel from every route beneath the middleware. Both real clients "
        "here — the dashboard's own fetch() and the reverse proxy in front of "
        "it — always declare a length. This is xfail rather than deleted so "
        "the gap stays visible, and it flips to a pass the day someone closes "
        "it."
    ),
    strict=True,
)
def test_a_chunked_request_is_also_refused(open_client):
    def _chunks():
        yield b'{"panel":"home"}'

    resp = open_client.post(POST_ROUTE, content=_chunks())
    assert resp.status_code == 413
