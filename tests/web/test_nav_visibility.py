"""Per-audience nav visibility for the Games / Social / Photo Challenge routes.

The 2026-08 IA pass (IA1) regroups the Games section into subgroups and moves
the four social features (Guess Who, Whisper, Pen Pals, Confessions) into their
own **Social** section, retiring the per-item ``perms`` hack they needed to
survive the game-host section gate. A regroup is only safe if it is *invisible*
to permissions: the same audiences must keep the same pages, and an admin-only
page must not become openable by a moderator.

Nothing else pins that. ``app.js``'s gating (``sectionGateOk`` + ``resolveItem``)
lives entirely in the browser, so this drives the real dashboard with a stubbed
``/api/me`` per audience and reads what the sidebar actually rendered:

  * ``"open"``   — an enabled nav button the user can click through to;
  * ``"locked"`` — rendered but disabled (the W-N5 "you can see it exists,
    admins only" treatment);
  * ``"hidden"`` — no nav entry at all.

The expectation table below was captured against the **pre-regroup** structure
and must survive the regroup unchanged. A row only moves when someone decides
it should — the Confessions row is the one that has, and says why in place.

Marked ``browser``. Auto-skips without Playwright / Chromium.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

pytestmark = pytest.mark.browser

playwright_sync = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright not installed (pip install playwright && playwright install chromium)",
)

_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from mobile_layout_scan import serve  # noqa: E402

from tests.db_template import migrated_db  # noqa: E402


def _chromium_available() -> bool:
    try:
        with playwright_sync.sync_playwright() as pw:
            path = pw.chromium.executable_path
            return bool(path) and Path(path).exists()
    except Exception:  # noqa: BLE001 — chromium/playwright detection must degrade to "unavailable" (skip) on any failure, never surface as a test error on a machine missing the browser
        return False


if not _chromium_available():
    pytest.skip(
        "Chromium not installed — run `python -m playwright install chromium`",
        allow_module_level=True,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, tmp: Path):
        db = tmp / "nav-visibility.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("nav-visibility"))
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", srv.port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def browser(dashboard) -> Iterator[object]:
    with playwright_sync.sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


# ── audiences ───────────────────────────────────────────────────────────────

_GUILD = "1"
_HOST_ROLE = "555000111"

# `admin` implies `moderator` and `manage_server` server-side (auth.py), so the
# admin profile carries all three — modelling it as admin-only would test a
# state that cannot exist.
AUDIENCES: dict[str, dict] = {
    "admin": {"perms": ["admin", "manage_server", "moderator"], "role_ids": []},
    "moderator": {"perms": ["moderator"], "role_ids": []},
    # A game-host who is *not* a moderator: the audience the Games section gate
    # exists for.
    "game-host": {"perms": [], "role_ids": [_HOST_ROLE]},
    # Moderator who also holds the host role — the only audience that sees
    # admin-only Games pages as locked rather than hidden today.
    "mod-host": {"perms": ["moderator"], "role_ids": [_HOST_ROLE]},
    "member": {"perms": [], "role_ids": []},
}


def _me_payload(profile: dict) -> dict:
    return {
        "user_id": "42",
        "username": "nav-test",
        "perms": profile["perms"],
        "role_ids": profile["role_ids"],
        "role_names": [],
        "guild_id": _GUILD,
        "guild_name": "Test Guild",
        "guilds": [{"id": _GUILD, "name": "Test Guild", "icon": None}],
        "primary_guild_id": _GUILD,
        "avatar_url": None,
        "status": "online",
        "games_editor_role_id": _HOST_ROLE,
        "economy_manager_role_id": None,
        "wellness_opted_in": False,
    }


_READ_NAV = """
() => {
  const out = {};
  for (const b of document.querySelectorAll('.nav-item[data-page-id]')) {
    out[b.dataset.pageId] = b.disabled || b.classList.contains('nav-locked')
      ? 'locked' : 'open';
  }
  return out;
}
"""


def _nav_state(browser, base: str, audience: str) -> dict[str, str]:
    """Render the dashboard as `audience` and return {page id: open|locked}."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route(
        "**/api/me",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_me_payload(AUDIENCES[audience])),
        ),
    )
    try:
        page.goto(base, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".nav-item", timeout=30_000)
        return page.evaluate(_READ_NAV)
    finally:
        ctx.close()


# ── the expectation table ───────────────────────────────────────────────────
#
# Captured from the pre-regroup nav (Games as a 23-item flat list + the
# one-item Photo Challenge section). Order: admin, moderator, game-host,
# mod-host, member. Anything not "open"/"locked" must be absent from the nav.

_O, _L, _H = "open", "locked", "hidden"

EXPECTED: dict[str, dict[str, str]] = {
    # ── Games: operations ───────────────────────────────────────────
    "games-logs":        {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-scheduling":  {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-config":      {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    # Explicit `perms: ["moderator"]`, so it survives a failed section gate.
    "games-external":    {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    # ── Games: live games ───────────────────────────────────────────
    "games-legitlibs":   {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "config-risky-rolls":            {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-pressure":         {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-quickdraw":        {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-hotpotato":        {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-hotpotatogroup":   {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-chicken":          {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-musicalchairs":    {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    # Its own one-item section before the regroup; folded into Games after.
    # Same gate either way (admin or game host).
    "photo-challenge":   {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    # ── Games: question banks ───────────────────────────────────────
    "games-wyr":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-nhie":        {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-mlt":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-rushmore":    {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-price":       {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-clapback":    {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-ama":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    # Live Games pages added 2026-09-04 (one pacing dial each), same gate as AMA.
    "hottakes":          {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "fantasies":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-ffa":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-traditional": {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    # ── the four social features (moved to their own section) ───────
    "config-guess":      {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    "config-whisper":    {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    "pen-pals":          {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    # Read-only for moderators since 2026-09-16 (D2): the entry opens, the
    # router holds every control disabled, and every Confessions endpoint
    # stays admin-gated. A game-host who is not a moderator still fails the
    # Social section gate and sees nothing.
    "config-confessions": {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
}


@pytest.fixture(scope="module")
def nav_by_audience(browser, dashboard) -> dict[str, dict[str, str]]:
    return {name: _nav_state(browser, dashboard.base, name) for name in AUDIENCES}


@pytest.mark.parametrize("audience", list(AUDIENCES))
def test_moved_route_visibility_is_unchanged(nav_by_audience, audience):
    """Every regrouped route keeps the audience it had before the regroup."""
    rendered = nav_by_audience[audience]
    actual = {pid: rendered.get(pid, _H) for pid in EXPECTED}
    expected = {pid: states[audience] for pid, states in EXPECTED.items()}
    assert actual == expected


def test_audience_stubs_actually_differ(nav_by_audience):
    """Guard the harness: if the /api/me stub stopped taking effect, every
    audience would render the same (admin) nav and every assertion above would
    pass for the wrong reason."""
    admin = nav_by_audience["admin"]
    member = nav_by_audience["member"]
    assert len(admin) > len(member) + 20, (admin.keys(), member.keys())
    assert "games-logs" in admin and "games-logs" not in member


def test_no_admin_only_page_is_openable_by_a_non_admin(nav_by_audience):
    """The failure mode a regroup must never introduce: an admin page whose
    nav entry a moderator (or host, or member) can actually click."""
    # `mod-host` passes the Games gate and is a moderator, so it is the one
    # audience that renders a `mod: "locked"` page as locked — which makes "locked
    # for mod-host" the reliable way to name the admin-only set from the table.
    admin_only = [pid for pid, s in EXPECTED.items() if s["mod-host"] == _L]
    assert len(admin_only) >= 8, admin_only
    for audience in ("moderator", "game-host", "mod-host", "member"):
        openable = [
            pid for pid in admin_only if nav_by_audience[audience].get(pid) == _O
        ]
        assert not openable, f"{audience} can open admin-only pages: {openable}"


# ── Read-only settings pages for moderators (2026-09-16) ────────────────────

#: A sample of the pages carrying ``mod: "read"``. Three is enough: the flag is
#: read in one place (``resolveItem``) and held in one place (``holdReadOnly``),
#: so a fourth page proves nothing a third didn't — but two plain config pages
#: plus a panel that renders its controls *after* mount is the spread that
#: matters, since the lock is a MutationObserver precisely because the first
#: draft missed late-rendered rows.
#:
#: Not ``bot-roles``: its reads need the gateway, so offline it renders nothing
#: but ``mountAsync``'s failure card and there is no control left to lock.
READ_ONLY_SAMPLE = ["config-welcome", "config-starboard", "music-playlist"]


@pytest.mark.parametrize("page_id", READ_ONLY_SAMPLE)
def test_a_moderator_opens_a_settings_page_held_read_only(browser, dashboard, page_id):
    """The page opens, says so, and nothing on it can be operated."""
    ctx, page = _booted(browser, dashboard.base, audience="moderator")
    try:
        _open_page(page, dashboard.base, page_id)
        page.wait_for_selector("#panel-root [data-readonly-banner]", timeout=30_000)
        # The banner is prepended at mount; the panel's own controls arrive with
        # its first fetch. Waiting for one is what makes this a test of the
        # observer rather than of the initial pass.
        page.wait_for_function(
            "() => document.querySelector('#panel-root')"
            ".querySelectorAll('input, select, textarea, button').length > 0",
            timeout=30_000,
        )

        counts = page.evaluate(
            """
            () => {
              const root = document.querySelector('#panel-root');
              const all = [...root.querySelectorAll('input, select, textarea, button')]
                .filter(el => !el.closest('[data-readonly-ok]'));
              return { total: all.length, live: all.filter(el => !el.disabled).length };
            }
            """
        )
        assert counts["total"] > 0, f"{page_id} rendered no controls to lock"
        assert counts["live"] == 0, f"{page_id} left {counts['live']} controls live"
    finally:
        ctx.close()


#: Pages that call `lockUnlessAdmin` on their own settings block *and* are
#: opened read-only, so both notices could fire. Voice Control and XP were two
#: of the five that grew a duplicate when IA6 opened them.
DOUBLE_LOCK_CANDIDATES = ["config-voice-master", "config-xp"]


@pytest.mark.parametrize("page_id", DOUBLE_LOCK_CANDIDATES)
def test_a_held_page_says_read_only_exactly_once(browser, dashboard, page_id):
    """`lockSection`'s chip and `holdReadOnly`'s banner said the same thing a
    few lines apart. The per-panel lock still runs — it guards a region of a
    page an admin can write — but inside a held page only the banner speaks."""
    ctx, page = _booted(browser, dashboard.base, audience="moderator")
    try:
        _open_page(page, dashboard.base, page_id)
        page.wait_for_selector("#panel-root [data-readonly-banner]", timeout=30_000)
        # Let the panel finish rendering: the chip is appended by the panel's
        # own code, so asserting before it runs would pass for the wrong reason.
        page.wait_for_function(
            "() => document.querySelector('#panel-root')"
            ".querySelectorAll('input, select, textarea, button').length > 0",
            timeout=30_000,
        )
        page.wait_for_timeout(500)
        counts = page.evaluate(
            """
            () => {
              const root = document.querySelector('#panel-root');
              return {
                banners: root.querySelectorAll('[data-readonly-banner]').length,
                chips: root.querySelectorAll('[data-lock-chip]').length,
              };
            }
            """
        )
        assert counts["banners"] == 1, f"{page_id} shows {counts['banners']} banners"
        assert counts["chips"] == 0, (
            f"{page_id} still appends {counts['chips']} lock chip(s) under the "
            f"read-only banner — two notices for one reason"
        )
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "audience,expected", [("admin", 1), ("moderator", 0)], ids=["admin", "moderator"]
)
def test_support_access_is_hidden_rather_than_shown_wrong(browser, dashboard, audience, expected):
    """Global's one admin-only card must not render for a moderator.

    Disabling it would not have been enough: its GET is admin-gated, and the
    panel's `.catch` fallback drew an unchecked box — "the developer has no
    access to this server" — which is a false statement, not a blank one.
    """
    ctx, page = _booted(browser, dashboard.base, audience=audience)
    try:
        _open_page(page, dashboard.base, "config-global")
        page.wait_for_selector("#panel-root form[data-form]", timeout=30_000)
        page.wait_for_timeout(500)
        assert page.locator("#panel-root [data-support-card]").count() == expected
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "audience,expected", [("admin", 1), ("moderator", 0)], ids=["admin", "moderator"]
)
def test_setup_suggestions_are_hidden_rather_than_shown_failing(
    browser, dashboard, audience, expected
):
    """The AI Assistant page's second admin-only card, same rule as Global's.

    ``GET /api/help/suggestions`` stays admin-only deliberately — a list of
    what this server has *not* set up is reconnaissance. Drawing the card for a
    moderator anyway left them a red "Setup suggestions failed to load:
    Insufficient permissions" on an otherwise readable page, which reads as a
    broken dashboard rather than a boundary.
    """
    ctx, page = _booted(browser, dashboard.base, audience=audience)
    try:
        _open_page(page, dashboard.base, "config-advisor")
        page.wait_for_selector("#panel-root form[data-form]", timeout=30_000)
        page.wait_for_timeout(500)
        assert page.locator('#panel-root [data-sec="suggestions"]').count() == expected
        # Whatever else is on the page, the moderator never sees the 403.
        assert "Setup suggestions failed to load" not in page.inner_text("#panel-root")
    finally:
        ctx.close()


def test_leaving_a_read_only_page_releases_the_hold(browser, dashboard):
    """``holdReadOnly`` is handed the router's one persistent ``#panel-root``,
    not a fresh element per page — so anything it puts *on that node* outlives
    the page unless ``unmount`` takes it off again. Announcements is the
    moderator's one write grant: if the greyed-out class or the banner survived
    the navigation, the page they are allowed to use would come up looking
    locked.
    """
    ctx, page = _booted(browser, dashboard.base, audience="moderator")
    try:
        _open_page(page, dashboard.base, "config-welcome")
        page.wait_for_selector("#panel-root [data-readonly-banner]", timeout=30_000)

        _open_page(page, dashboard.base, "announcements")
        page.wait_for_function(
            "() => !document.querySelector('#panel-root .panel-loading')", timeout=30_000
        )
        page.wait_for_timeout(500)
        assert page.locator("#panel-root [data-readonly-banner]").count() == 0
        assert not page.evaluate(
            "() => document.querySelector('#panel-root').classList.contains('section-locked')"
        )
    finally:
        ctx.close()


def test_a_failed_load_on_a_held_page_still_offers_its_retry(browser, dashboard):
    """``mountAsync``'s error state must stay operable under the read-only hold.

    The hold disables every control it finds, and the retry button is a
    ``<button>`` like any other — so a moderator whose first fetch failed got a
    "Try again" they could not press, and no way off the error but a reload.
    The failure wrapper opts out with ``data-readonly-ok``; the retry re-runs
    the same GET they are allowed to make.
    """
    ctx, page = _booted(browser, dashboard.base, audience="moderator")
    try:
        page.route("**/api/config*", lambda route: route.abort())
        _open_page(page, dashboard.base, "config-welcome")
        retry = page.locator("#panel-root [data-retry]")
        retry.wait_for(timeout=30_000)
        page.wait_for_timeout(500)
        assert retry.is_enabled(), "the read-only hold disabled the only way out"
    finally:
        ctx.close()


def test_a_moderator_can_still_switch_tabs_on_a_held_page(browser, dashboard):
    """The one opt-out the lock grants, and the reason it exists.

    A tab strip changes nothing on the server, but it is made of ``<button>``
    elements — so disabling every control would strand a moderator on whichever
    tab opened first, with the rest of the page unreadable. ``mountTabs`` marks
    its strip ``data-readonly-ok``; this is Music Playlist, which has four.
    """
    ctx, page = _booted(browser, dashboard.base, audience="moderator")
    try:
        _open_page(page, dashboard.base, "music-playlist")
        page.wait_for_selector("#panel-root .tab-btn", timeout=30_000)
        page.wait_for_selector("#panel-root [data-readonly-banner]", timeout=30_000)

        tabs = page.locator("#panel-root .tab-btn")
        assert tabs.count() > 1, "expected a multi-tab panel"
        assert page.locator("#panel-root .tab-btn:not([disabled])").count() == tabs.count()

        second = tabs.nth(1)
        key = second.get_attribute("data-tab")
        second.click()
        page.wait_for_function(
            "(k) => document.querySelector(`#panel-root .tab-btn[data-tab=\"${k}\"]`)"
            ".classList.contains('active')",
            arg=key,
            timeout=10_000,
        )
    finally:
        ctx.close()


def test_a_plain_member_gets_no_read_only_pages(nav_by_audience):
    """Read-only is a moderator grant, not a public one: a member who can log
    in sees no nav entry for these at all."""
    member = nav_by_audience["member"]
    assert not [pid for pid in READ_ONLY_SAMPLE if pid in member], member.keys()


def test_a_moderator_sees_the_read_only_pages_as_open_entries(nav_by_audience):
    """Open, not locked — the W-N5 locked treatment would hide the settings a
    moderator is now meant to be able to read."""
    mod = nav_by_audience["moderator"]
    assert {pid: mod.get(pid) for pid in READ_ONLY_SAMPLE} == {
        pid: _O for pid in READ_ONLY_SAMPLE
    }


# ── IA5: the Help nav carries the guild's own name for the assistant ────────


_BRANDED = "Sparkles"


def _page_with_brand(browser, base: str, hash_: str = ""):
    """A page whose /api/help/advisor/name says the guild renamed its assistant."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route(
        "**/api/help/advisor/name",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"assistant_name": _BRANDED}),
        ),
    )
    page.goto(f"{base}/#/{hash_}", wait_until="domcontentloaded", timeout=15000)
    return ctx, page


def test_help_nav_label_uses_the_guilds_assistant_name(browser, dashboard):
    """The nav hardcoded "Ask Billy-bot (AI)" while Config had already been
    neutralised for per-guild branding. It now re-labels itself from the same
    endpoint the help panel uses."""
    ctx, page = _page_with_brand(browser, dashboard.base)
    try:
        page.wait_for_selector(".nav-item", timeout=30_000)
        page.wait_for_function(
            "() => [...document.querySelectorAll('.nav-item')]"
            ".some(b => b.dataset.pageId === 'help-ask' && /Ask Sparkles/.test(b.textContent))",
            timeout=10_000,
        )
        # The old name stays searchable so the sidebar filter still finds it.
        search = page.evaluate(
            "() => document.querySelector('.nav-item[data-page-id=\"help-ask\"]').dataset.search"
        )
        assert "billy" in search
    finally:
        ctx.close()


def test_help_panel_title_uses_the_guilds_assistant_name(browser, dashboard):
    """Same for the panel's own title — and the manual's duplicate heading is
    still dropped, now that the manual's own heading names nobody and so can
    match neither the branded title nor the static fallback. The entry states
    the heading to strip (`manualHeading`); without that, a guild would see
    "Ask Sparkles (AI)" with "Ask the AI Assistant" stacked under it."""
    ctx, page = _page_with_brand(browser, dashboard.base, "help-ask")
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.wait_for_selector("#panel-root h2", timeout=30_000)
        page.wait_for_function(
            "() => /Ask Sparkles \\(AI\\)/.test(document.querySelector('#panel-root h2').textContent)",
            timeout=10_000,
        )
        # Structural, not textual: the manual's heading is replaced by an empty
        # anchor div, so the section content starts with a paragraph. The
        # previous version of this check looked for the heading's text in
        # inner_text() and could never have failed — the stylesheet uppercases
        # h3s, so the needle never matched however broken the de-duplication was.
        first = page.evaluate(
            "() => (document.querySelector('#panel-root .dk-help')"
            ".firstElementChild || {}).tagName"
        )
        assert first == "DIV", f"manual heading rendered as a second title (<{first}>)"
        body = page.inner_text("#panel-root .dk-help").lower()
        assert "ask the ai assistant" not in body, "manual heading rendered as a second title"
        assert "billy" not in body, "the guide still names the assistant for every guild"
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("page_id,label", [
    ("help-word-cloud", "Word Cloud"),
    ("help-usage-telemetry", "Command & Panel Usage"),
])
def test_a_permission_chip_does_not_leave_the_heading_behind(browser, dashboard, page_id, label):
    """Two help pages drew their title twice, and nothing noticed.

    Three manual subsections carry their audience as a chip inside the heading
    itself (``Word Cloud <span class="perm">Mod</span>``). The chip lands in
    textContent, so the heading read "Word Cloud Mod" and never matched the nav
    label "Word Cloud" — the de-duplication silently declined, and the page
    showed the panel header's title with the manual's own heading stacked
    underneath it. Found while making the assistant's heading name-free
    (todo #164); these two are the same defect with a different cause.
    """
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(f"{dashboard.base}/#/{page_id}", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector("#panel-root .dk-help", timeout=30_000)
        page.wait_for_selector("#panel-root .dk-help .panel-loading", state="detached", timeout=30_000)
        first = page.evaluate(
            "() => (document.querySelector('#panel-root .dk-help')"
            ".firstElementChild || {}).outerHTML || ''"
        )
        assert "<h3" not in first.lower(), f"{label}: manual heading kept — {first[:120]}"
        headings = page.eval_on_selector_all(
            "#panel-root .dk-help h3", "els => els.map(e => e.textContent.trim())"
        )
        assert not any(h.startswith(label) for h in headings), (
            f"{label} still appears as a heading inside the page body: {headings[:3]}"
        )
    finally:
        ctx.close()


# ── IA4: the Ctrl/Cmd+K command palette ─────────────────────────────────────
#
# Additive to the sidebar filter (which these tests also re-check is untouched).
# The load-bearing property is the last one: a palette that surfaces a page the
# viewer can't open would be a permission leak dressed as a convenience.


def _open_palette(page):
    page.keyboard.press("Control+k")
    page.wait_for_selector(".dk-palette", timeout=5_000)


def _palette_settled(page, timeout=10_000):
    """Wait until the result list stops changing, not merely until it appears.

    The palette re-renders as the filter resolves and resets the highlight to
    the first option each time. A test that pressed ArrowDown on the first
    paint raced that reset: the keypress moved the selection to option 2 and
    the re-render put it straight back, so the assertion saw opt-0 twice and
    the test failed roughly one run in three — for everybody, on any commit
    that touched a dashboard panel.

    Polls on animation frames and requires three identical readings of both
    the option ids and the active one, which is settled without a fixed sleep.
    """
    page.wait_for_function(
        """
        () => {
          const opts = [...document.querySelectorAll('.dk-palette-option')];
          const active = document.querySelector('.dk-palette-option.active');
          if (opts.length < 2 || !active) { window.__dkPaletteKey = null; return false; }
          const key = opts.map(o => o.id).join(',') + '|' + active.id;
          if (window.__dkPaletteKey === key) {
            window.__dkPaletteStable = (window.__dkPaletteStable || 0) + 1;
          } else {
            window.__dkPaletteKey = key;
            window.__dkPaletteStable = 0;
          }
          return window.__dkPaletteStable >= 3;
        }
        """,
        timeout=timeout,
    )


def _results(page) -> list[str]:
    return page.evaluate(
        "() => [...document.querySelectorAll('.dk-palette-option')]"
        ".map(o => o.textContent.trim())"
    )


def _booted(browser, base: str, audience: str = "admin"):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route(
        "**/api/me",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_me_payload(AUDIENCES[audience])),
        ),
    )
    page.goto(base, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_selector(".nav-item", timeout=30_000)
    return ctx, page


def _open_page(page, base: str, page_id: str) -> None:
    """Navigate a booted dashboard to `page_id` and wait for it to stick.

    The boot lands on Home, which mounts asynchronously and writes its own
    markup into ``#panel-root`` when its fetches land — so a page navigated to
    while those are still in flight is rendered and then overwritten by a Home
    render that arrives afterwards. Settling the network first means Home has
    finished writing before the hash changes. Booting straight onto the hash is
    not an alternative: the router reads the hash once, after the boot has
    already mounted Home.
    """
    page.wait_for_load_state("networkidle", timeout=30_000)
    page.goto(f"{base}/#/{page_id}", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_function(
        "() => !document.querySelector('#panel-root .home-panel')", timeout=30_000
    )


def test_palette_opens_on_ctrl_k_and_closes_on_escape(browser, dashboard):
    ctx, page = _booted(browser, dashboard.base)
    try:
        page.click("#panel-root")  # focus somewhere outside the palette
        _open_palette(page)
        assert page.evaluate("() => document.activeElement.className") == "dk-palette-input"
        page.press(".dk-palette-input", "Escape")
        page.wait_for_selector(".dk-palette", state="detached", timeout=5_000)
        # Focus is returned rather than dropped on <body>.
        assert page.evaluate("() => document.activeElement !== document.body")
    finally:
        ctx.close()


def test_palette_lists_section_and_label_and_opens_with_the_keyboard(browser, dashboard):
    ctx, page = _booted(browser, dashboard.base)
    try:
        _open_palette(page)
        # Pages tier: results read "Label" over "Section", not a filtered tree.
        page.fill(".dk-palette-input", "jails")
        page.wait_for_function(
            "() => document.querySelectorAll('.dk-palette-option').length > 0",
            timeout=10_000,
        )
        rows = _results(page)
        assert any("Jails" in r and "Moderation" in r for r in rows), rows

        # Second tier: the manual's own headings, routed to the help page.
        page.fill(".dk-palette-input", "jail")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.dk-palette-option')]"
            ".some(o => o.textContent.includes('Guide'))",
            timeout=10_000,
        )

        # Enter opens whatever is selected.
        page.fill(".dk-palette-input", "jails")
        page.wait_for_function(
            "() => (document.querySelector('.dk-palette-option.active') || {}).textContent"
            "     ?.includes('Jails')",
            timeout=10_000,
        )
        page.press(".dk-palette-input", "Enter")
        page.wait_for_function("() => location.hash.startsWith('#/mod-jails')", timeout=5_000)
        assert page.query_selector(".dk-palette") is None
    finally:
        ctx.close()


def test_palette_arrow_keys_move_the_selection(browser, dashboard):
    ctx, page = _booted(browser, dashboard.base)
    try:
        _open_palette(page)
        page.fill(".dk-palette-input", "config")
        _palette_settled(page)
        first = page.evaluate(
            "() => document.querySelector('.dk-palette-option.active').id"
        )
        # page.press(selector, …) focuses first, then sends the key. The bare
        # keyboard.press sends to whatever happens to hold focus, and the
        # palette binds its keydown on the palette element — so a key dispatched
        # while focus has drifted to <body> targets body, never bubbles to the
        # palette, and is simply lost. Under a parallel browser run that is what
        # kept happening, which the first fix here turned from a wrong answer
        # into an honest five-second timeout without curing it.
        page.press(".dk-palette-input", "ArrowDown")
        # Wait for the move rather than reading straight after the keypress —
        # the assertion below should fail because the arrow key does nothing,
        # never because the read landed a frame early.
        page.wait_for_function(
            "(prev) => { const a = document.querySelector('.dk-palette-option.active');"
            " return a && a.id !== prev; }",
            arg=first,
            timeout=5_000,
        )
        second = page.evaluate(
            "() => document.querySelector('.dk-palette-option.active').id"
        )
        assert first != second
        assert page.evaluate(
            "() => document.querySelector('.dk-palette-input')"
            ".getAttribute('aria-activedescendant')"
        ) == second
    finally:
        ctx.close()


def test_palette_never_surfaces_a_page_the_viewer_cannot_open(browser, dashboard):
    """The permission property: same filtering as the nav, no back door."""
    ctx, page = _booted(browser, dashboard.base, audience="moderator")
    try:
        _open_palette(page)
        # One query, two Confessions pages, and the split is the whole point:
        # the settings page opens read-only for a moderator, while Confessions
        # Audit names the confessor and stays admin-only. The palette must
        # offer the first and not the second.
        page.fill(".dk-palette-input", "confessions")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.dk-palette-option')]"
            ".some(o => (o.dataset.href || '').startsWith('#/config-confessions'))",
            timeout=10_000,
        )
        hrefs = page.evaluate(
            "() => [...document.querySelectorAll('.dk-palette-option')]"
            ".map(o => o.dataset.href)"
        )
        assert not any(h.startswith("#/confessions-audit") for h in hrefs), hrefs
    finally:
        ctx.close()


def test_sidebar_filter_still_works_alongside_the_palette(browser, dashboard):
    """IA4 is additive: the existing nav filter is untouched."""
    ctx, page = _booted(browser, dashboard.base)
    try:
        page.fill("[data-nav-filter]", "jails")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.nav-item:not(.filtered-out)')]"
            ".some(b => b.dataset.pageId === 'mod-jails')",
            timeout=5_000,
        )
        hidden = page.evaluate(
            "() => document.querySelector('.nav-item[data-page-id=\"home\"]')"
            ".classList.contains('filtered-out')"
        )
        assert hidden
    finally:
        ctx.close()
