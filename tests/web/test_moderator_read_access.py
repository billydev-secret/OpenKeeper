"""What a moderator may read, write, and still not touch (2026-09-16).

Moderators get the settings pages read-only: the nav opens them, the router
holds every control disabled, and the write routes stay admin-gated. That only
works if the *reads* those pages make accept a moderator — most already did
through ``GET /api/config``, and the handful that didn't were opened one at a
time. Each one gets a row here, next to a write beside it that must still
refuse, because "opened the read" and "opened the write" are one line apart in
the same file and nothing else would notice the second following the first.

Two deliberate asymmetries are pinned here too:

* **Announcements is a write grant**, not a read one — moderators compose and
  post, ``@everyone`` included (Billy, 2026-09-16).
* **The anonymity logs stay admin-only.** DM, whisper, confessions, anon and
  Guess Who each name the member behind an anonymous action, and Quotes names
  the quoter of a card that credits nobody. The moderation Audit Log — the
  moderators' own actions — is the one that opened.

See docs/plans/moderator-dashboard-access.md.
"""

from __future__ import annotations

import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from tests.web.conftest import set_session_cookie, web_client
from web_server.auth import DiscordOAuthAuth
from web_server.server import create_app
from tests.paths import REPO_ROOT

MOD_ROLE_ID = 7788

_JS = REPO_ROOT / "src" / "web_server" / "static" / "js"


def _client_as(fake_ctx, *, user_id: int, username: str, role_ids: list[int]) -> TestClient:
    """A logged-in caller holding no permission bits at all.

    ``permission_bits=0`` is the point: the moderator tier is the guild's
    configured staff role, not a bit (``resolve_guild_perms``), so a fixture
    that leaned on Manage Server would prove nothing about the role path and
    would quietly carry ``manage_server`` into every assertion below.
    """
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = web_client(create_app(fake_ctx, auth=auth))
    set_session_cookie(
        client,
        auth.create_session_cookie(
            user_id=user_id,
            username=username,
            permission_bits=0,
            role_ids=role_ids,
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": str(fake_ctx.guild_id), "name": "Test Guild", "icon": None}],
        ),
    )
    return client


@pytest.fixture
def mod_client(fake_ctx) -> Generator[TestClient, None, None]:
    """A caller who is a moderator and explicitly **not** an administrator."""
    fake_ctx.set_config_value("mod_role_ids", str(MOD_ROLE_ID))
    client = _client_as(fake_ctx, user_id=99, username="mod", role_ids=[MOD_ROLE_ID])
    yield client
    client.close()


@pytest.fixture
def member_client(fake_ctx) -> Generator[TestClient, None, None]:
    """A logged-in member of the guild holding no staff role."""
    fake_ctx.set_config_value("mod_role_ids", str(MOD_ROLE_ID))
    client = _client_as(fake_ctx, user_id=100, username="member", role_ids=[])
    yield client
    client.close()


def test_the_fixture_is_a_moderator_and_not_an_admin(mod_client):
    """Guard the harness. Every "a moderator can read this" row below asserts
    on a non-403; if the role stopped resolving, the client would silently
    become a plain member and the interesting half of the file would still
    pass on the permissive assertions."""
    me = mod_client.get("/api/me")
    assert me.status_code == 200
    perms = set(me.json()["perms"])
    assert "moderator" in perms
    assert not perms & {"admin", "manage_server"}


def test_a_plain_member_is_not_a_moderator(member_client):
    me = member_client.get("/api/me")
    assert me.status_code == 200
    assert "moderator" not in set(me.json()["perms"])


# ── the reads opened for the read-only pages ─────────────────────────
#
# (path, the page it backs). A 403 here means a moderator opens that page to an
# error where the plan says they see its settings.
READS_OPENED = [
    ("/api/bot-roles", "bot-roles"),
    ("/api/bot-roles/state", "bot-roles"),
    ("/api/onboarding", "onboarding"),
    ("/api/music-playlist/status", "music-playlist"),
    ("/api/music-playlist/window", "music-playlist"),
    ("/api/music-playlist/unmatched", "music-playlist"),
    ("/api/music-playlist/history", "music-playlist"),
    ("/api/feature-rotation", "feature-rotation"),
    ("/api/config/quote-border", "config-quote-border"),
    ("/api/config/advisor", "config-advisor"),
    ("/api/moderation/audit", "mod-audit"),
    # The bulk of the read-only pages ride this one, which was already
    # moderator-readable — the flag flip would be a lie if it regressed.
    ("/api/config", "every config-* page"),
]


@pytest.mark.parametrize("path,page", READS_OPENED, ids=[p for p, _ in READS_OPENED])
def test_moderator_can_read(mod_client, path, page):
    resp = mod_client.get(path)
    assert resp.status_code != 403, f"{page} opens to a 403 for a moderator"


@pytest.mark.parametrize("path,page", READS_OPENED, ids=[p for p, _ in READS_OPENED])
def test_a_plain_member_cannot_read(member_client, path, page):
    """The grant stops at the staff role — opening a page to moderators must
    not open it to everyone who can log in."""
    assert member_client.get(path).status_code == 403


# ── what a moderator still may not do ────────────────────────────────

WRITES_REFUSED = [
    ("put", "/api/music-playlist/settings", {"enabled": True}),
    ("post", "/api/music-playlist/rescan", {}),
    ("put", "/api/feature-rotation/config", {"enabled": True}),
    ("post", "/api/bot-roles/create", {"key": "jailed_role_id"}),
    ("post", "/api/onboarding/add-roles", {"role_ids": ["1"]}),
    ("post", "/api/config/quote-border", {"border_color": "#ffffff"}),
    ("put", "/api/config/welcome", {"welcome_channel_id": "1"}),
    ("put", "/api/config/moderation", {"warn_threshold": 3}),
    ("put", "/api/config/advisor", {"enabled": True}),
    # The panel registry's one posting path — admin-gated for every panel,
    # confessions included since it folded in (§ F). A moderator reading the
    # Confessions page sees the Post Button card and must not be able to fire
    # it.
    ("post", "/api/panels/confessions/post", {"channel_id": "1"}),
]


@pytest.mark.parametrize(
    "method,path,body", WRITES_REFUSED, ids=[p for _, p, _ in WRITES_REFUSED]
)
def test_moderator_cannot_write(mod_client, method, path, body):
    resp = getattr(mod_client, method)(path, json=body)
    assert resp.status_code == 403, f"{path} accepted a moderator write"


ADMIN_ONLY_READS = [
    ("/api/moderation/dm-audit", "names who sent an anonymous DM"),
    ("/api/moderation/whisper-audit", "names both ends of a whisper"),
    ("/api/moderation/confessions-audit", "names the confessor"),
    ("/api/moderation/anon-audit", "names every anonymous poster"),
    ("/api/guess/audit", "submit rows carry the actor"),
    ("/api/quotes/audit", "names the quoter of an uncredited card"),
    ("/api/help/suggestions", "a list of what isn't set up is reconnaissance"),
    ("/api/config/support-access", "the support-access card"),
    ("/api/privacy/departures", "Member Data stays adminOnly"),
]


@pytest.mark.parametrize("path,why", ADMIN_ONLY_READS, ids=[p for p, _ in ADMIN_ONLY_READS])
def test_moderator_cannot_read_admin_only(mod_client, path, why):
    """Revert guard for decision D3: no surface that de-anonymises a member
    opens to the mod team, however ordinary its neighbours look."""
    resp = mod_client.get(path)
    assert resp.status_code == 403, f"a moderator can read {path} — {why}"


# ── announcements: the one write grant ───────────────────────────────


def test_moderator_can_list_and_create_an_announcement(mod_client):
    assert mod_client.get("/api/announcements").status_code == 200
    resp = mod_client.post(
        "/api/announcements",
        json={
            "channel_id": "12345",
            "title": "Server event",
            "body": "Come along",
            "mention_kind": "none",
        },
    )
    assert resp.status_code == 200, resp.text


def test_moderator_may_ping_everyone_and_post_it_now(mod_client):
    """No moderator-specific limit, decided (Billy, 2026-09-16). The bot sends
    the message, so Discord's own Mention @everyone permission never enters
    into it — a moderator pinging the server is the grant, not a hole in it."""
    created = mod_client.post(
        "/api/announcements",
        json={
            "channel_id": "12345",
            "title": "All hands",
            "body": "Everyone please read",
            "mention_kind": "everyone",
        },
    )
    assert created.status_code == 200, created.text
    ann_id = created.json()["id"]

    posted = mod_client.post(f"/api/announcements/{ann_id}/post-now")
    # No gateway in tests, so the send itself fails — what matters is that the
    # request got past the gate rather than bouncing off it.
    assert posted.status_code != 403


def test_moderator_can_edit_clone_and_delete(mod_client):
    created = mod_client.post(
        "/api/announcements",
        json={"channel_id": "12345", "title": "Draft", "body": "b"},
    )
    ann_id = created.json()["id"]

    edited = mod_client.put(
        f"/api/announcements/{ann_id}",
        json={"channel_id": "12345", "title": "Edited", "body": "b"},
    )
    assert edited.status_code == 200, edited.text
    assert mod_client.post(f"/api/announcements/{ann_id}/clone").status_code == 200
    assert mod_client.delete(f"/api/announcements/{ann_id}").status_code == 200


def test_a_plain_member_still_cannot_touch_announcements(member_client):
    assert member_client.get("/api/announcements").status_code == 403
    resp = member_client.post(
        "/api/announcements",
        json={"channel_id": "12345", "title": "Nope", "body": "b"},
    )
    assert resp.status_code == 403


# ── the nav flag that decides which pages they see ───────────────────

#: Every state ``resolveItem`` knows how to act on. A value outside this set is
#: not a no-op: ``resolveItem`` treats any truthy ``mod`` as at least "locked",
#: so a typo degrades a read-only page to a locked one silently — and a *falsy*
#: typo (``mod: ""``) opens an admin page to every moderator.
MOD_STATES = {'"locked"', '"read"'}

_MOD_DECL = re.compile(r"\bmod:\s*([^,}\s]+)")


def test_the_mod_flag_only_ever_holds_a_state_resolveitem_knows():
    """IA6 folded ``adminOnly``/``modReadOnly`` into one field so that no item
    could carry both. One field removes that failure mode and introduces this
    one: the value is now a string nothing validates, in a file with no types.
    This is the check that replaces "assert no item sets both flags"."""
    app = (_JS / "app.js").read_text(encoding="utf-8")
    declared = {
        m.group(1)
        for line in app.splitlines()
        # Skip the comment block that documents the states.
        if not line.lstrip().startswith("//")
        for m in [_MOD_DECL.search(line)]
        if m
    }
    assert declared, "no `mod:` nav declarations found — did the flag get renamed again?"
    unknown = declared - MOD_STATES
    assert not unknown, (
        f"nav items declare `mod` states resolveItem does not handle: {sorted(unknown)}. "
        f"Known states are {sorted(MOD_STATES)}."
    )


def test_the_retired_flag_names_never_come_back():
    """IA6 renamed ``adminOnly``/``modReadOnly`` to one ``mod`` field, and
    ``resolveItem`` no longer reads the old names at all. That is a quiet
    failure: an item carrying ``adminOnly: true`` and no ``mod`` falls past
    every gate in ``resolveItem`` and returns unmodified — visible *and*
    openable to any moderator, which is the exact opposite of what the flag
    was asking for.

    Nothing in the language catches that. A parallel branch written before the
    rename, or a copy-paste from an older nav entry, reintroduces it silently
    and the page it guards is the kind that gets guarded for a reason. This
    check is cheap and is the only thing standing between that merge and a
    leaked admin page.
    """
    app = (_JS / "app.js").read_text(encoding="utf-8")
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(app.splitlines(), 1)
        if ("adminOnly" in line or "modReadOnly" in line) and not line.lstrip().startswith("//")
    ]
    assert not offenders, (
        "app.js uses a nav flag resolveItem retired in IA6 — these items are "
        'open to every moderator right now. Use `mod: "locked"` or '
        '`mod: "read"`:\n' + "\n".join(offenders)
    )


def test_both_mod_states_are_actually_in_use():
    """Guard the guard above: if every page drifted onto one state, the test
    would still pass while covering half the branch it exists for."""
    app = (_JS / "app.js").read_text(encoding="utf-8")
    for state in sorted(MOD_STATES):
        assert f"mod: {state}" in app, f"no nav item carries mod: {state} any more"
