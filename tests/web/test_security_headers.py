"""Every response carries the security headers, and the CSP is the real one.

The dashboard sent no `X-Content-Type-Options`, no framing control and no
Content-Security-Policy at all. `server.py` now attaches all three from one
outermost middleware, because a header a newly added route has to remember is
not a control.

The CSP half of this file is deliberately specific. A policy is only worth
having if it is the *verified* one — the temptation with CSP is to write the
strictest string that parses, watch a panel break in production, and delete
the header wholesale. So these pin both directions: `script-src` really has no
`'unsafe-inline'`, and `style-src` really does, for the reason given in
`server.py`.
"""

from __future__ import annotations

import re

import pytest
from tests.paths import REPO_ROOT

_STATIC = REPO_ROOT / "src" / "web_server" / "static"

HEADERS = [
    pytest.param("x-content-type-options", "nosniff", id="nosniff"),
    pytest.param("x-frame-options", "DENY", id="frame-options"),
    pytest.param("referrer-policy", "strict-origin-when-cross-origin", id="referrer"),
]


def _csp(resp) -> dict[str, str]:
    """The policy as {directive: value}, so a test can name one directive."""
    raw = resp.headers["content-security-policy"]
    return {
        part.split(" ", 1)[0]: part.split(" ", 1)[1] if " " in part else ""
        for part in (p.strip() for p in raw.split(";"))
        if part
    }


# ── The headers reach every kind of response ────────────────────────────────


@pytest.mark.parametrize("name,value", HEADERS)
def test_an_api_response_carries_the_headers(open_client, name, value):
    resp = open_client.get("/api/health/tiles")
    assert resp.status_code == 200
    assert resp.headers[name] == value


@pytest.mark.parametrize("name,value", HEADERS)
def test_a_refusal_carries_the_headers(noperms_client, name, value):
    """The middleware is registered last, so it wraps the auth dependencies.

    This is the case that matters most: a 401/403 body is what an attacker
    probing the dashboard actually receives, and it used to arrive bare.
    """
    resp = noperms_client.get("/api/help/suggestions")
    assert resp.status_code == 403
    assert resp.headers[name] == value


def test_a_static_asset_carries_the_headers(open_client):
    """Including the responses the StaticFiles mount produces, which no route
    of ours ever touches."""
    resp = open_client.get("/static/app.css")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_the_login_page_carries_the_headers(open_client):
    """The one page an unauthenticated stranger is meant to reach."""
    resp = open_client.get("/login")
    assert resp.status_code == 200
    assert resp.headers["x-frame-options"] == "DENY"


# ── The policy itself ───────────────────────────────────────────────────────


def test_scripts_are_same_origin_only(open_client):
    """No `'unsafe-inline'`, no CDN, no `'unsafe-eval'`. This is the directive
    the three inline-script removals in this commit bought."""
    directives = _csp(open_client.get("/login"))
    assert directives["script-src"] == "'self'"


def test_styles_keep_unsafe_inline_on_purpose(open_client):
    """A deliberate anomaly: the panels build markup strings containing
    `style="..."` in dozens of places, so dropping this would restyle half the
    dashboard into unreadability. Reverting it is a real project, not a
    tightening — this test is here so a stranger's cleanup pass has to read
    the reason in server.py first."""
    directives = _csp(open_client.get("/login"))
    assert "'unsafe-inline'" in directives["style-src"]


@pytest.mark.parametrize(
    "directive,expected",
    [
        pytest.param("default-src", "'self'", id="default"),
        pytest.param("frame-ancestors", "'none'", id="no-framing"),
        pytest.param("object-src", "'none'", id="no-plugins"),
        pytest.param("base-uri", "'self'", id="no-base-hijack"),
        pytest.param("form-action", "'self'", id="no-offsite-post"),
        pytest.param("connect-src", "'self'", id="same-origin-fetch"),
    ],
)
def test_the_rest_of_the_policy(open_client, directive, expected):
    assert _csp(open_client.get("/login"))[directive] == expected


def test_avatars_are_allowed(open_client):
    """Member avatars come from Discord's CDN; `img-src 'self'` would blank
    every avatar on the dashboard."""
    assert "cdn.discordapp.com" in _csp(open_client.get("/login"))["img-src"]


# ── Swagger's exemption ─────────────────────────────────────────────────────


def test_swagger_is_exempt_from_the_csp_but_not_the_rest(authed_client):
    """Swagger UI loads its assets from cdn.jsdelivr.net, so the strict policy
    renders it blank. Vendoring them to buy a CSP on one admin-gated developer
    tool isn't worth it — but the other headers still apply."""
    resp = authed_client.get("/api/_docs")
    assert resp.status_code == 200
    assert "content-security-policy" not in resp.headers
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


def test_the_schema_route_is_not_exempt(authed_client):
    """Only the HTML page is exempt. The JSON it fetches is ordinary."""
    resp = authed_client.get("/api/openapi.json")
    assert resp.status_code == 200
    assert "content-security-policy" in resp.headers


# ── Source guards: keep the policy true ─────────────────────────────────────
# `script-src 'self'` silently stops working the moment someone adds an inline
# script back. Silently is the problem — the handler just never runs, and the
# page half-works. These fail at commit time instead.

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>")
_INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=\s*[\"']")


@pytest.mark.parametrize("name", ["index.html", "login.html", "manual.html"])
def test_no_inline_script_in_the_html_pages(name):
    text = (_STATIC / name).read_text(encoding="utf-8")
    assert not _INLINE_SCRIPT.search(text), (
        f"{name} has an inline <script>; the CSP blocks it. Move it to a "
        f"sibling .js file, as login.js and manual.js were."
    )
    assert not _INLINE_HANDLER.search(text), (
        f"{name} has an inline event handler; the CSP blocks it — and unlike a "
        f"<script>, no nonce or hash covers one without 'unsafe-hashes'. Use "
        f"addEventListener from the page's .js file."
    )


#: Comments are stripped before the walk below, and that is not tidiness: the
#: two files that *explain* this rule quote a handler to do it. md-preview.js
#: describes the `onclick="return false"` it used to render, and matching that
#: prose is what kept an allowlist here long after the handler itself was gone
#: — an allowlist that would equally have forgiven a real one coming back.
_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


_RUNTIME_EVAL = re.compile(r"\bnew\s+Function\s*\(|(?<![.\w$])eval\s*\(")


def test_no_panel_js_builds_a_function_from_a_string():
    """`script-src 'self'` carries no 'unsafe-eval', so `eval` and
    `new Function` throw in the page — the same silent death as an inline
    handler, and with no allowlist because nothing here has ever needed one.

    This is not hypothetical: the CSP caught tests/web/test_shared_js_safety.py
    reviving column formatters with `new Function`, which had worked only
    because no policy had ever been sent.
    """
    offenders = sorted(
        str(path.relative_to(_STATIC))
        for path in _STATIC.rglob("*.js")
        if _RUNTIME_EVAL.search(path.read_text(encoding="utf-8"))
    )
    assert not offenders, (
        "dashboard JS builds a function from a string; the CSP refuses it and "
        f"the code silently stops running: {offenders}"
    )


def test_no_new_inline_handlers_in_the_panel_js():
    """No dashboard JS may emit an inline handler the CSP will silently kill.

    tests/web/test_frontend_wiring.py sweeps `static/js/` for the same thing
    from the panel side. This one keeps the *root* of the static tree, where
    login.js and manual.js sit — the two files this branch created by moving
    inline <script> out of the HTML pages, and the ones a walk that starts at
    `js/` cannot see.
    """
    offenders = sorted(
        str(path.relative_to(_STATIC))
        for path in _STATIC.rglob("*.js")
        if _INLINE_HANDLER.search(
            _JS_COMMENT.sub("", path.read_text(encoding="utf-8"))
        )
    )
    assert not offenders, (
        "dashboard JS emits an inline event handler. The CSP blocks it, so it "
        "is a control that silently stops working — bind it with "
        f"addEventListener instead: {offenders}"
    )
