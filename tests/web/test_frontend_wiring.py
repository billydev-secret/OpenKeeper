"""Source-level sweeps over the dashboard's JS wiring.

Cheap string checks, deliberately not a browser test: they catch a whole class
of "the two halves drifted" bug that only shows up when a user reloads or
switches servers, which no per-panel test exercises.

From the 2026-08-06 website deep review:

  * **todo hash id.** ``panels/todo.js`` mirrored its state into ``#/todo``
    while app.js registers the page as ``mod-todo``, so every render rewrote
    the URL to a route that does not exist and a reload or bookmark landed on
    "Page Not Available". The sweep generalises it: *every* hash a panel writes
    must name a route the router knows.
  * **S2 — guild-switch cache reset.** ``applyMeData`` is the one place that
    runs on boot and on every guild switch. If it stops clearing the
    guild-scoped module caches, config panels silently show the previous
    guild's channels/roles/members and saves write foreign snowflakes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_JS = REPO_ROOT / "src" / "web_server" / "static" / "js"
_PANELS = _JS / "panels"


def _known_route_ids() -> set[str]:
    """Every id the hash router can mount: nav pages, extra routes, help pages."""
    ids = set(re.findall(r'\bid:\s*"([A-Za-z0-9_-]+)"', (_JS / "app.js").read_text(encoding="utf-8")))
    # Help pages are generated from help-sections.js rather than listed in app.js.
    ids |= set(
        re.findall(
            r'page:\s*"([A-Za-z0-9_-]+)"',
            (_PANELS / "help-sections.js").read_text(encoding="utf-8"),
        )
    )
    return ids


def _panel_files() -> list[Path]:
    return sorted(_PANELS.glob("*.js"))


@pytest.fixture(scope="module")
def route_ids() -> set[str]:
    ids = _known_route_ids()
    # Sanity: the registry parse must not silently come back empty, or every
    # assertion below passes for the wrong reason.
    assert "mod-todo" in ids and len(ids) > 50, "route-id scrape looks broken"
    return ids


def test_every_synced_hash_names_a_real_route(route_ids):
    """`syncHash(id, …)` writes the URL a reload will be asked to restore."""
    bad = [
        (path.name, panel_id)
        for path in _panel_files()
        for panel_id in re.findall(r'syncHash\(\s*"([^"]+)"', path.read_text(encoding="utf-8"))
        if panel_id not in route_ids
    ]
    assert not bad, (
        "panels sync the URL to routes the router doesn't know — reload/bookmark "
        f"lands on 'Page Not Available': {bad}"
    )


def test_every_hand_written_hash_names_a_real_route(route_ids):
    """The same check for panels that call history.replaceState directly.

    Only literal ids are checked; a templated `#/${to}` is skipped because its
    target isn't knowable from the source.
    """
    bad = [
        (path.name, panel_id)
        for path in _panel_files()
        for panel_id in re.findall(
            r"""replaceState\([^;]*?["'`]\#/([A-Za-z0-9_-]+)""",
            path.read_text(encoding="utf-8"),
            re.S,
        )
        if panel_id not in route_ids
    ]
    assert not bad, f"panels rewrite the URL to unknown routes: {bad}"


def test_every_rendered_link_names_a_real_route(route_ids):
    """`href="#/id"` links inside rendered panel markup, and in manual.html.

    The two tests above cover the URL a panel *writes*. They do not cover the
    links a panel *draws*, which is most of the cross-referencing on the
    dashboard — every "set that on <a href=#/economy-config>Settings</a>" hint.
    A typo there produces a link that looks fine, renders fine, and lands on
    "Page Not Available"; nothing failed when the Spending section was split
    three ways and every one of those hints had to be repointed.

    A retired id is legitimate if MOVED_PAGES redirects it, so those count as
    known routes too.
    """
    app = (_JS / "app.js").read_text(encoding="utf-8")
    moved = set(re.findall(r'^\s*"([A-Za-z0-9_-]+)":\s*"[A-Za-z0-9_-]+",', app, re.M))
    known = route_ids | moved

    targets: list[tuple[str, str]] = []
    sources = [*_panel_files(), _JS.parent / "manual.html"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        # Both forms occur: "#/id" inside panels, "/#/id" in the standalone manual.
        for hit in re.findall(r'href="/?#/([A-Za-z0-9_-]+)', text):
            targets.append((path.name, hit))

    assert targets, "link scrape found nothing — the pattern must have drifted"
    bad = sorted({t for t in targets if t[1] not in known})
    assert not bad, (
        "rendered links point at routes the router cannot mount — they render "
        f"normally and land on 'Page Not Available': {bad}"
    )


def test_apply_me_data_drops_guild_scoped_caches():
    """S2: the guild switch must invalidate every guild-scoped module cache.

    `/api/config` and every `/api/meta/*` list is scoped to the active guild,
    but config-helpers.js memoizes them in module globals and switchGuild
    re-mounts panels without reloading the page.
    """
    app = (_JS / "app.js").read_text(encoding="utf-8")
    body = app.split("function applyMeData(", 1)[1].split("\nfunction ", 1)[0]
    assert "resetMetaCaches()" in body, (
        "applyMeData no longer clears config-helpers' meta caches — config "
        "panels will show the previous guild's channels/roles/members"
    )
    assert "_resetPanelSpecCache()" in body
    assert 'from "./config-helpers.js"' in app


def test_reset_meta_caches_clears_every_cache_it_owns():
    """A cache added to config-helpers.js must be cleared on guild switch too.

    Two declaration shapes now: the `let _x = null` list memos, and the Map
    memos the bounded member list brought (search results and the id index).
    Both hold guild-scoped rows, so both have to be dropped on a switch.
    """
    src = (_JS / "config-helpers.js").read_text(encoding="utf-8")
    reset = src.split("export function resetMetaCaches()", 1)[1].split("\n}", 1)[0]

    nulled = set(re.findall(r"^let (_[A-Za-z]+) = null;", src, re.M))
    maps = set(re.findall(r"^const (_[A-Za-z]+) = new (?:Map|Set)\(\);", src, re.M))
    assert nulled and maps, "cache-declaration scrape looks broken"

    missing = {n for n in nulled if f"{n} = null" not in reset}
    missing |= {n for n in maps if f"{n}.clear()" not in reset}
    assert not missing, (
        f"guild-scoped caches not cleared by resetMetaCaches(): {sorted(missing)}"
    )


# ── the bounded member list (2026-08 review residue) ───────────────────────
#
# /api/meta/members is paginated now. A picker that only filters the page it was
# handed makes everyone below the cut unselectable — the exact regression that
# kept the endpoint uncapped through the security pass. These two keep the
# server-side lookup wired wherever member options are built.


def test_shared_member_pickers_wire_the_server_search():
    """The two mount helpers most panels go through carry it by default."""
    src = (_JS / "config-helpers.js").read_text(encoding="utf-8")
    for fn in ("mountMemberPicker", "mountMemberMultiPicker"):
        body = src.split(f"export function {fn}(", 1)[1].split("\n}", 1)[0]
        assert "search: memberSearch()" in body, (
            f"{fn} no longer passes the server-side member lookup — every "
            "member outside the bounded first page just became unselectable"
        )
        assert "_hydrateSavedMembers" in body, (
            f"{fn} no longer resolves a saved id the page didn't include; a "
            "config naming a departed member renders as a bare snowflake"
        )
    # …and the widget has to honour it.
    fs = (_JS / "filter-select.js").read_text(encoding="utf-8")
    assert "opts.search" in fs, "filter-select dropped remote search support"


_MEMBER_OPTION_BUILDERS = ("toMemberOptions(", "toSortedMemberOptions(")


def test_panels_building_member_options_wire_the_server_search():
    """A panel assembling member options by hand owns the lookup itself.

    Going through mountMemberPicker/mountMemberMultiPicker is the easy path and
    needs nothing; a panel that reaches for the option builder directly (because
    it wants a different ordering, say) has opted out of that wiring and has to
    pass `search:` itself.
    """
    offenders = []
    for path in _panel_files():
        src = path.read_text(encoding="utf-8")
        if not any(b in src for b in _MEMBER_OPTION_BUILDERS):
            continue
        if "memberSearch(" not in src:
            offenders.append(path.name)
    assert not offenders, (
        "panels building member picker options without a server-side search: "
        f"{offenders} — /api/meta/members is a bounded page, so anyone below "
        "the cut can no longer be picked"
    )


# ── IA1 / IA3 (2026-08 information-architecture pass) ───────────────────────


# Route ids are frozen: deep links, bookmarks, the 47 `help:` mappings and the
# never-opened telemetry list all key off them, so the Games regroup moved nav
# *entries*, never ids. These are the ones the regroup touched.
_REGROUPED_ROUTE_IDS = [
    # Games → Operations
    "games-logs", "games-scheduling", "games-config", "games-external",
    # Games → Live Games (photo-challenge folded in from its own section)
    "games-legitlibs", "config-risky-rolls", "config-games-pressure",
    "config-games-quickdraw", "config-games-hotpotato",
    "config-games-hotpotatogroup", "config-games-chicken",
    "config-games-musicalchairs", "photo-challenge",
    # ...and games-ama, moved here from Question Banks in 2026-08 when its bank
    # UI came out: AMA questions come from members mid-round, never the bank.
    "games-ama",
    # Games → Question Banks
    "games-wyr", "games-nhie", "games-mlt", "games-rushmore", "games-price",
    "games-clapback", "games-ffa", "games-traditional",
    # → the new Social section
    "config-guess", "config-whisper", "pen-pals", "config-confessions",
]


def test_regrouped_routes_kept_their_ids(route_ids):
    missing = [pid for pid in _REGROUPED_ROUTE_IDS if pid not in route_ids]
    assert not missing, (
        "the IA regroup was supposed to move nav entries, not rename routes — "
        f"these ids no longer exist: {missing}"
    )


# The mod workflow panels (IA3). The browser suite proves the round trip; this
# is the cheap always-on guard that the wiring is still there at all.
_URL_STATE_PANELS = {
    "mod-tickets.js": "mod-tickets",
    "mod-jails.js": "mod-jails",
    "qa-tracker.js": "qa-tracker",
    "todo.js": "mod-todo",
    # The todo page mounted over the bug list's routes; it passes its own id.
    "bugs.js": "bugs",
}


@pytest.mark.parametrize("filename,page_id", sorted(_URL_STATE_PANELS.items()))
def test_mod_workflow_panels_keep_url_state(filename, page_id):
    """Each mod queue reads its state from the hash and writes it back."""
    src = (_PANELS / filename).read_text(encoding="utf-8")
    assert "initialParams" in src, f"{filename} ignores the hash params on mount"
    assert f'syncHash("{page_id}"' in src, f"{filename} stopped mirroring state into the URL"


# ── table.js column contract (S1 follow-through) ────────────────────────────
#
# table.js escapes every cell by default; a column that deliberately renders
# markup opts in with `html: true` and then owns its own escaping. Two ways for
# a panel to get that wrong, and both are invisible until someone looks at the
# page:
#
#   * a markup-returning column WITHOUT `html: true` prints literal `<span…>`
#     text at the reader;
#   * a plain-text column that still calls esc() double-escapes, so "Tom &
#     Jerry" renders as "Tom &amp; Jerry".
#
# The allow-list below is the point of the second test: adding a new markup
# column is a deliberate act that has to be justified here, which is the review
# gate that keeps a member-controlled name from ever getting `html: true`.


def _column_specs() -> list[tuple[str, str]]:
    """Every column object literal in every renderSortableTable consumer.

    Returns (filename, source-of-the-object) pairs. Brace-matched rather than
    line-based because a format() body can span several lines.
    """
    specs: list[tuple[str, str]] = []
    for path in _panel_files():
        src = path.read_text(encoding="utf-8")
        if "renderSortableTable" not in src:
            continue
        for match in re.finditer(r"\{\s*key:\s*\"", src):
            depth = 0
            start = match.start()
            for i in range(start, len(src)):
                if src[i] == "{":
                    depth += 1
                elif src[i] == "}":
                    depth -= 1
                    if depth == 0:
                        specs.append((path.name, src[start : i + 1]))
                        break
    return specs


def test_column_specs_scrape_finds_the_known_consumers():
    """Guard the guard: an empty or tiny scrape would pass both tests below."""
    files = {name for name, _ in _column_specs()}
    assert "channels.js" in files and "usage-telemetry.js" in files
    assert len(_column_specs()) > 40, "column scrape looks broken"


def test_no_text_column_escapes_its_own_output():
    """table.js escapes cells now — a leftover esc() double-escapes."""
    bad = [
        (name, spec.split("\n")[0][:70])
        for name, spec in _column_specs()
        if "esc(" in spec and "html: true" not in spec
    ]
    assert not bad, (
        "these columns escape text that table.js already escapes, so names with "
        f"& < > render mangled: {bad}"
    )


# (file, key) of every column that opts out of escaping, with why it is safe.
# Adding a row here is a security decision: the column then owns its escaping,
# so it must interpolate computed values (numbers, fixed class names) only —
# never a member-supplied name.
_HTML_COLUMNS = {
    # colored percentage/± figures: the value is a number, the color a literal
    ("retention.js", "drop_pct"),
    ("retention.js", "normalized_drop_pct"),
    ("xp-leaderboard.js", "_diff"),
    ("channels.js", "avg_sentiment"),
    ("channels.js", "trend_pct"),
    # <code>-wrapped command / panel identifier; CODE() esc()s the value itself
    ("usage-telemetry.js", "name"),
    # <span class="num-err|num-dim">: a Number()-coerced count
    ("usage-telemetry.js", "errors"),
    # anchor built by sourceCell(): href/host both esc()'d, non-http(s) falls back to esc(url)
    ("music-playlist.js", "source_url"),
    # reason badge from reasonCell(): class name is a literal, label esc()'d
    ("music-playlist.js", "removal_reason"),
    # action buttons keyed by a server-issued row id, Number()-coerced into data-* attrs
    ("music-playlist.js", "id"),
    # Set Active button keyed by the card's DB row id, Number()-coerced into data-activate
    ("mahjong.js", "row_id"),
    # Survivor weekly clock: nextCell() is a <strong> literal around week
    # numbers plus an esc()'d guild-local time; resetCell() is a button whose
    # task key and fixed post label are esc()'d into data-* attrs
    ("survivor.js", "next_ts"),
    ("survivor.js", "task"),
    # Survivor host Remove button: the host's snowflake string esc()'d into
    # data-remove-host; the host's name is the separate, escaped column
    ("survivor.js", "id"),
    # Take a Break End Break button: the member's snowflake string esc()'d
    # into data-end; the member's name is the separate, escaped column
    ("take-a-break.js", "user_id"),
    # approvalActionCell(): link/scroll anchors whose href, target and label
    # come from the KIND_ACTION literal table, each esc()'d
    ("shop-approvals.js", "handled"),
    # "Open" anchor to the pinging message: href is jumpLink() over two
    # server-issued snowflakes (channel_id, message_id), esc()'d again on the
    # way in, and the link text is the fixed literal "Open" — no member text
    # reaches this cell
    ("ping-response.js", "message_id"),
    # outcome badge: the class name is one of four literals picked by an
    # if/else over the row's own outcome, and the fallback branch esc()s. No
    # member-supplied text reaches it — the ballot's question is a separate,
    # escaped column.
    ("policy-ballots.js", "closed_at"),
    # roll-call button keyed by the ballot's integer row id, Number()-coerced
    # into a data-* attribute
    ("policy-ballots.js", "id"),
    # Retry / Leave alone buttons keyed by the queued member's id. Unlike the
    # row ids above it is a snowflake, which Number() would round, so it is
    # esc(String(v))'d into the data-* attribute instead. The id is the
    # server's own, and the two member-supplied strings on this table — the
    # display name and a failed job's error text — are plain escaped columns.
    ("member-data.js", "user_id"),
    # Live Rooms Transfer/Delete buttons: the room's snowflake string esc()'d
    # into data-room-* attributes; the room and owner names are separate,
    # escaped columns
    ("voice-settings.js", "channel_id"),
    # "Set up on" link to a dashboard page: route id and label both come from
    # the panel's own ROWS literal, each esc()'d — no server or member text
    ("server-features.js", "pageLabel"),
}


def test_every_html_opt_in_column_is_on_the_reviewed_list():
    """A new `html: true` column must be added here — deliberately, with a
    reason — rather than slipping in beside a display name."""
    found = {
        (name, re.search(r'key:\s*"([^"]+)"', spec).group(1))
        for name, spec in _column_specs()
        if "html: true" in spec
    }
    unreviewed = found - _HTML_COLUMNS
    assert not unreviewed, (
        "columns opted out of table.js escaping without review — if any of these "
        f"renders member-supplied text it is stored XSS: {sorted(unreviewed)}"
    )
    assert not _HTML_COLUMNS - found, (
        f"reviewed html columns that no longer exist: {sorted(_HTML_COLUMNS - found)}"
    )


def test_member_name_columns_never_opt_out_of_escaping():
    """The six columns S1 was actually about: raw Discord display names."""
    member_keys = {
        "user_name", "display_name", "greeter_name", "pair_name", "channel_name",
    }
    bad = [
        (name, key)
        for name, key in {
            (n, re.search(r'key:\s*"([^"]+)"', s).group(1))
            for n, s in _column_specs()
            if "html: true" in s
        }
        if key in member_keys
    ]
    assert not bad, f"member-controlled column rendered as HTML: {bad}"


# ── Hand-built markup: the raw interpolations the 2026-09-14 audit found ────
# The 35 panels that build tables by string were audited site by site
# (docs/reviews/2026-09-13-privacy-reuse-standards-review.md § Open questions).
# Every member-typed value was already escaped; these are the three sites that
# were not.
#
# Each pin asserts the escape is *present*, rather than that one old spelling
# is absent: `assert "${unit}" not in src` passes again the moment someone
# renames the local, so a reopened hole would sail past a green suite. The
# locals are derived from the source for the same reason.


def _qotd_currency_interps(src: str) -> list[str]:
    """Every interpolation of whatever local the QOTD settings card binds to
    the currency name — free admin text, length-checked only."""
    local = re.search(r"const\s+(\w+)\s*=[^;]*\bcfg\.currency_(?:name|plural)\b", src)
    assert local, "currency-unit binding not found — this scrape has drifted"
    return re.findall(r"\$\{([^}]*\b%s\b[^}]*)\}" % re.escape(local.group(1)), src)


def _policy_badge_fallback(src: str) -> list[str]:
    """The `STATUS_BADGE[...] || <fallback>` arm. The map's own values are
    trusted static spans — it is the fallback that renders a bare status."""
    return re.findall(r"STATUS_BADGE\[[^\]]+\]\s*\|\|\s*([^;\n]+)", src)


def _survivor_num_field_value(src: str) -> list[str]:
    """The `value=""` interpolation inside Survivor's shared number-field
    builder. Its callers pass server-validated ints today; the helper escapes
    because a shared builder escapes whatever it is handed, as table.js does."""
    body = src.split("function numField(", 1)[1].split("\n}", 1)[0]
    return re.findall(r'value="\$\{([^}]*)\}"', body)


@pytest.mark.parametrize(
    ("filename", "extract"),
    [
        pytest.param(
            "economy-qotd-submissions.js", _qotd_currency_interps, id="qotd-currency-name"
        ),
        pytest.param(
            "mod-policy-tickets.js", _policy_badge_fallback, id="policy-ticket-status-fallback"
        ),
        pytest.param("survivor.js", _survivor_num_field_value, id="survivor-num-field-value"),
    ],
)
def test_audited_interpolations_escape_their_value(filename, extract):
    found = extract((_PANELS / filename).read_text(encoding="utf-8"))
    assert found, f"{filename}: the audited interpolation is gone — re-point this pin"
    unescaped = [expr.strip() for expr in found if "esc(" not in expr]
    assert not unescaped, (
        f"{filename} interpolates a value into HTML without esc(): {unescaped}"
    )


# A local escaper is as likely to come back under another name as under `esc`
# (config-helpers.js already re-exports the canonical one as `escapeHtml`), so
# match the shape too: an inline map of & < > to entities, whatever it is bound
# to. mahjong.js's copy — which skipped `'`, and so broke inside a
# single-quoted attribute — is the one this sweep was written for.
_ESCAPER_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var|function)\s+(esc|esc[A-Z]\w*|escape[A-Z]\w*)\b\s*[=(]",
    re.M,
)


def test_no_module_defines_its_own_escaper():
    bad = []
    for path in sorted(_JS.rglob("*.js")):
        if path.name == "api.js":
            continue
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(_JS).as_posix()
        named = _ESCAPER_DECL.search(src)
        if named:
            bad.append(f"{rel} (declares {named.group(1)})")
            continue
        for hit in re.finditer(r"&amp;", src):
            window = src[max(0, hit.start() - 200) : hit.start() + 300]
            if "&lt;" in window and "replace" in window:
                bad.append(f"{rel} (inlines an entity map)")
                break
    assert not bad, f"import esc from api.js instead of redefining it: {bad}"


# ── F1: no panel may mount a loader it never catches ───────────────────────


# Panels whose bare `(async () => {…})()` is genuinely safe: the whole body is
# inside its own try/catch that renders a failure state, or the awaited calls
# cannot reject. Everything else must go through mountAsync so a failed first
# fetch is an error state with a retry, not a spinner that never clears.
_IIFE_EXEMPT = {
    # secondary loads inside an already-mounted panel, each with its own catch
    "config-ai.js",        # model-status poller
    "grant-audit.js",      # try/catch → statusEl error text
    "inactive-report.js",  # loadChannels/loadRoles never reject; refresh() catches
    "activity.js",         # loadDropdowns/refresh both catch
    "todo.js",             # documented in-file; owned by the mod-queue work
    "qa-tracker.js",
}


def test_no_panel_mounts_an_uncaught_async_loader():
    """F1 was ~27 panels deep; this stops the 28th.

    `(async () => { … })()` with no `.catch` turns one failed fetch into a
    permanent "Loading…" plus an unhandled rejection, with nothing on screen to
    tell the user which it is.
    """
    offenders = []
    for path in _panel_files():
        if path.name in _IIFE_EXEMPT:
            continue
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\(async \(\) => \{", src):
            tail_start = src.find("})()", match.end())
            if tail_start == -1:
                continue
            if ".catch(" in src[tail_start : tail_start + 12]:
                continue
            offenders.append((path.name, src[: match.start()].count("\n") + 1))
    assert not offenders, (
        "async loaders with no rejection handling — a failed fetch leaves the "
        f"panel on 'Loading…' forever: {offenders}"
    )


def test_the_iife_exemption_list_has_no_dead_entries():
    """An exempted panel that no longer has the pattern should leave the list,
    or the exemption silently covers a future regression in that file."""
    stale = [
        name
        for name in _IIFE_EXEMPT
        if "(async () => {" not in (_PANELS / name).read_text(encoding="utf-8")
    ]
    assert not stale, f"exempt panels with no async IIFE left: {sorted(stale)}"


def test_converted_panels_pass_a_specific_error_message():
    """A generic "Couldn't load this page." tells the reader nothing about
    which of the ~130 panels failed or what to retry."""
    generic = []
    for path in _panel_files():
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(r"errorMsg:\s*\"([^\"]*)\"", src):
            msg = match.group(1)
            if msg in {"", "Couldn't load this page.", "Couldn’t load this page."}:
                generic.append((path.name, msg))
    assert not generic, f"panels with a non-specific mountAsync message: {generic}"


# ── the shared member-picker option builder ────────────────────────────────
#
# config-prune and config-inactive each carried a byte-identical private
# `memberOpts` (departed members sorted last, labelled "(left the server)") and
# a private `nameById`/`memberName` pair. Both now live in config-helpers.js as
# toSortedMemberOptions() and memberNameLookup(). The review found several
# helper pairs that had drifted silently once copied, so these two checks keep
# the shape in exactly one place rather than trusting nobody re-inlines it.


_SHARED_MEMBER_HELPER_PANELS = ("config-prune.js", "config-inactive.js")


@pytest.mark.parametrize("filename", _SHARED_MEMBER_HELPER_PANELS)
def test_member_picker_panels_use_the_shared_helpers(filename):
    """Neither panel may re-declare the private copies that were hoisted."""
    src = (_PANELS / filename).read_text(encoding="utf-8")
    assert "toSortedMemberOptions" in src, (
        f"{filename} no longer imports the shared member-option builder"
    )
    assert "memberNameLookup" in src, (
        f"{filename} no longer imports the shared chip-name lookup"
    )
    # Binding the shared call to a local `memberOpts` is fine; building one is
    # not. A `nameById` Map is the private half of memberNameLookup and has no
    # business back in a panel at all.
    rebuilt = [
        line.strip()[:70]
        for line in src.splitlines()
        if re.match(r"\s*(?:const|let)\s+(?:memberOpts|nameById)\s*=", line)
        and "toSortedMemberOptions(" not in line
    ]
    assert not rebuilt, (
        f"{filename} builds its own member options again: {rebuilt} — the "
        "private copy this pair was hoisted out of is back, and the two panels "
        "can drift apart again"
    )


def test_only_config_helpers_builds_the_departed_member_label():
    """The wording is the tell: a panel spelling it out inline is a fresh copy.

    (activity.js uses the *terse* " (left)" from
    toMemberOptions, which is a different, intentional label — not this one.)
    """
    offenders = [
        path.name
        for path in _panel_files()
        if "(left the server)" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "panels rebuilding the sorted member-picker options inline instead of "
        f"calling toSortedMemberOptions(): {offenders}"
    )
    helpers = (_JS / "config-helpers.js").read_text(encoding="utf-8")
    assert "(left the server)" in helpers, "the shared builder lost its label"


def test_mount_async_conversion_covered_the_config_family():
    """The F1 sweep's headline set — if one of these regresses to a bare IIFE
    the test above catches it, but this names the contract directly."""
    missing = [
        path.name
        for path in _panel_files()
        if path.name.startswith("config-")
        and path.name not in {"config-bios.js"}
        and "(async () => {" not in path.read_text(encoding="utf-8")
        and "mountAsync" not in path.read_text(encoding="utf-8")
        # The six duel/lobby game panels are specs for one shared module, which
        # owns the loader (games-duel-panel.js mounts through mountAsync).
        and "mountDuelPanel(" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"config panels with neither a loader nor mountAsync: {missing}"
    assert "mountAsync(" in (_PANELS / "games-duel-panel.js").read_text(encoding="utf-8")


def test_income_sources_panel_edits_only_faucets_the_route_returns():
    """Every FAUCET_FIELDS key must appear in the route's ``faucets`` dict.

    The panel renders ``data.faucets[key] ?? 0`` and posts the whole table
    back on Save, so a key the GET omits is not merely blank — it saves a 0
    over the guild's configured value. That is how the two host-bounty dials
    became destructive (2026-08-29 config audit, finding #60).
    """
    panel = (_PANELS / "economy-income-sources.js").read_text(encoding="utf-8")
    block = re.search(r"const FAUCET_FIELDS = \[(.*?)\n\];", panel, re.S)
    assert block, "FAUCET_FIELDS table not found — did the panel rename it?"
    edited = set(re.findall(r'\[\s*"([a-z0-9_]+)"', block.group(1)))
    assert edited, "FAUCET_FIELDS parsed empty"

    route = (
        REPO_ROOT
        / "src" / "web_server" / "routes" / "economy_manager.py"
    ).read_text(encoding="utf-8")
    served = set(re.findall(r'"([a-z0-9_]+)": settings\.\1,', route))
    assert not edited - served, (
        "Income Sources inputs whose value the route never returns — each one "
        f"renders 0 and zeroes the real setting on Save: {sorted(edited - served)}"
    )


def test_every_data_f_field_a_panel_reads_is_one_it_renders():
    """A save handler must only read inputs its own template still draws.

    From the 2026-09-02 games deep review (rotation-rooms-155): the Feature
    Rotation panel's Save Settings handler kept reading
    ``[data-f="tz_offset_hours"]`` after the input moved to Server Settings.
    ``querySelector`` returned null, ``.value`` threw, and every Save died
    before the PUT — no toast, no request, and the rotation could never be
    switched on. The panel-load suite mounts panels without pressing buttons,
    so nothing caught it. This sweep catches the drift at the source: a
    ``[data-f=…]`` selector in a panel must match a ``data-f="…"`` attribute
    that the same panel renders.
    """
    read_re = re.compile(r"""querySelector(?:All)?\(\s*['"`]\[data-f=\\?["']([A-Za-z0-9_-]+)\\?["']\]""")
    rendered_re = re.compile(r"""(?<!\[)data-f=\\?["']([A-Za-z0-9_-]+)\\?["']""")
    seen = 0
    bad: list[tuple[str, str]] = []
    for path in _panel_files():
        text = path.read_text(encoding="utf-8")
        reads = set(read_re.findall(text))
        seen += len(reads)
        rendered = set(rendered_re.findall(text))
        bad.extend((path.name, name) for name in sorted(reads - rendered))
    assert seen, "data-f scrape found nothing — the pattern must have drifted"
    assert not bad, (
        "panels read data-f inputs their template no longer renders — the save "
        f"handler throws on a null querySelector before any request is sent: {bad}"
    )


def test_every_home_widget_navigates_to_a_panel_that_exists():
    """A tile's `nav` must name a panel id `app.js` can actually load.

    Removing a feature deletes its panel from `app.js`, and every referrer has
    to be repointed or removed with it. When the NSFW-by-Gender panel went in
    2026-09 the Home "NSFW (24h)" tile was missed, and its click silently
    stopped working for every moderator — a dangling id is invisible to ruff,
    to eslint, and to every other tier, because nothing joined the two files.
    """
    js = REPO_ROOT / "src" / "web_server" / "static" / "js"
    app = (js / "app.js").read_text(encoding="utf-8")
    registry = (js / "widget-registry.js").read_text(encoding="utf-8")

    # An id is loadable when its own entry also names a panel module.
    panels = set(re.findall(r'id:\s*"([a-z0-9-]+)"[^\n]*?module:\s*"\./panels/', app))
    assert panels, "no panel ids parsed from app.js — the entry shape changed"

    dangling = sorted(
        {
            (widget, target)
            for widget, target in re.findall(
                r'id:\s*"([a-z0-9-]+)"[^}]*?nav:\s*"([a-z0-9-]+)"', registry
            )
            if target not in panels
        }
    )
    assert not dangling, (
        "home widgets point at panels that no longer exist: "
        + ", ".join(f"{w} -> {t}" for w, t in dangling)
    )


def test_retired_page_ids_redirect_to_a_panel_that_exists():
    """A retired id keeps working as a deep link through `MOVED_PAGES`.

    Route ids are frozen, so removing a panel adds a redirect rather than
    leaving saved links on "Page Not Available". The redirect's target has to
    be loadable, and the retired id must not also still be a live nav entry.
    """
    js = REPO_ROOT / "src" / "web_server" / "static" / "js"
    app = (js / "app.js").read_text(encoding="utf-8")
    panels = set(re.findall(r'id:\s*"([a-z0-9-]+)"[^\n]*?module:\s*"\./panels/', app))
    block = re.search(r"const MOVED_PAGES = \{(.*?)\n\};", app, re.S)
    assert block, "MOVED_PAGES not found in app.js"
    moved = dict(re.findall(r'^\s*"([a-z0-9-]+)":\s*"([a-z0-9-]+)"', block.group(1), re.M))
    # Contributors, retired 2026-09-16 (docs/plans/remove-contributors.md).
    assert moved.get("quality-score") == "activity"
    bad = sorted(
        (old, new) for old, new in moved.items() if new not in panels or old in panels
    )
    assert not bad, f"MOVED_PAGES entries that dangle or shadow a live page: {bad}"


# ── Games: one flat list, banks behind a tab (2026-09-02, todo #165) ────────


def _games_section_src() -> str:
    """The Games section's SECTIONS entry, from `id: "games"` to the next one."""
    src = (_JS / "app.js").read_text(encoding="utf-8")
    start = src.index('id: "games", label: "Games"')
    end = src.index('id: "social"', start)
    return src[start:end]


# The twenty-two game pages, as one alphabetical list. Before 2026-09-02 these
# sat in two subgroups (Live Games / Question Banks); the split divided them by
# a property of the page rather than by anything you know while looking one up,
# and the bank half is now a tab on each game's own page instead.
_GAME_PAGE_IDS = {
    "games-ama", "config-games-chicken", "games-clapback", "fantasies",
    "games-ffa", "config-games-hotpotato", "config-games-hotpotatogroup",
    "hottakes", "games-legitlibs", "mahjong", "games-mlt",
    "config-games-musicalchairs", "games-nhie", "photo-challenge",
    "config-games-pressure", "games-price", "config-games-quickdraw",
    "config-risky-rolls", "games-rushmore", "survivor", "games-traditional",
    "games-wyr",
}
_OPERATIONS_IDS = {
    "games-logs", "games-scheduling", "games-config", "games-external",
    "config-event-echo",
}
# The eight whose page leads with a question bank.
_BANK_PAGE_IDS = {
    "games-wyr", "games-nhie", "games-mlt", "games-rushmore", "games-price",
    "games-clapback", "games-ffa", "games-traditional",
}


def test_games_section_is_one_flat_list():
    """The twenty-two games are section-level items; only Operations keeps a heading.

    Un-headed ``items`` render *above* ``groups`` (app.js's renderNav), which is
    what puts Operations at the bottom and avoids a "Games" heading inside the
    Games section.
    """
    block = _games_section_src()
    items_block = block[block.index("items: ["):block.index("groups: [")]
    flat = set(re.findall(r'\{ id: "([A-Za-z0-9_-]+)"', items_block))
    assert flat == _GAME_PAGE_IDS, (
        "the Games section's flat list drifted — missing "
        f"{sorted(_GAME_PAGE_IDS - flat)}, unexpected {sorted(flat - _GAME_PAGE_IDS)}"
    )

    headings = re.findall(r'heading: "([^"]+)"', block)
    assert headings == ["Operations"], (
        f"Games should keep exactly one heading, Operations — found {headings}"
    )

    groups_block = block[block.index("groups: ["):]
    ops = set(re.findall(r'\{ id: "([A-Za-z0-9_-]+)"', groups_block))
    assert ops == _OPERATIONS_IDS, f"Operations drifted: {sorted(ops)}"


def test_bank_games_keep_a_question_bank_search_term():
    """The nav filter matches heading text, so dropping "Question Banks" would
    have stopped "question bank" from finding the eight bank pages. Each one
    carries the term as a keyword instead."""
    block = _games_section_src()
    missing = [
        pid for pid in sorted(_BANK_PAGE_IDS)
        if not re.search(
            r'\{ id: "' + re.escape(pid) + r'".*?keywords: "[^"]*question bank', block
        )
    ]
    assert not missing, (
        "bank pages a sidebar search for 'question bank' can no longer find: "
        f"{missing}"
    )


def test_game_panel_tabs_are_scoped_to_full_page_bank_panels():
    """`mountGamePanel` must only tab the eight full-page bank panels.

    The seven ``config-games-*`` live games, LegitLibs, Pen Pals and Photo
    Challenge mount this shared module into a slot of a page that already has
    its own shell and headings — a tab strip there would nest one panel's
    chrome inside another's. AMA, Hot Takes and Fantasies & Dealbreakers
    have no bank to lead with. The `!bare` and
    `hasBank && hasStatus` terms are what keeps them out.
    """
    src = (_PANELS / "games-panel-shared.js").read_text(encoding="utf-8")
    assert re.search(r"const tabbed = hasBank && hasStatus && !bare;", src), (
        "the tabbed-layout gate changed — it must stay `hasBank && hasStatus "
        "&& !bare` so embedded callers never render a tab strip"
    )
    # Questions leads: it is the default pane, so the bank still loads on arrival.
    keys = re.findall(r'\{ key: "(\w+)",\s*label: "([^"]+)"', src)
    assert keys[:2] == [("questions", "Questions"), ("settings", "Settings")], (
        f"the bank panel's tab order changed: {keys[:2]}"
    )


# ── one submit at a time (W-A1) ──────────────────────────────────────────


def test_the_bank_manager_guards_both_money_forms():
    """Grant and Remove awaited apiPost with nothing disabled, so a double
    click posted twice and moved the amount twice. This is the money path; the
    route logic was correct and the whole defect was here."""
    src = (_PANELS / "economy-bank-manager.js").read_text(encoding="utf-8")
    assert src.count("guardedSubmit(form, async") == 2, (
        "grant and remove must both go through the shared guard"
    )
    assert "guardedSubmit," in src, "guardedSubmit is used but not imported"
    assert 'addEventListener("submit"' not in src


# A panel that receives a submit event without going through guardedSubmit.
# Empty on purpose: there is no known form whose submit should be re-entrant.
# An entry here needs a sentence in the file saying what it is doing instead.
_UNGUARDED_SUBMIT_OK: dict[str, str] = {}

_RAW_SUBMIT = re.compile(r'addEventListener\(\s*["\']submit["\']')


def test_no_panel_wires_its_own_submit_listener():
    """The double-pay bug was one panel's handler awaiting an apiPost with the
    button still live. Eighty-four others had the same shape and had simply not
    been clicked twice yet, so fixing the one that was reported would have left
    the class intact. guardedSubmit is the only way in."""
    offenders = []
    for path in _panel_files():
        if path.name in _UNGUARDED_SUBMIT_OK:
            continue
        src = path.read_text(encoding="utf-8")
        for m in _RAW_SUBMIT.finditer(src):
            offenders.append((path.name, src[: m.start()].count("\n") + 1))
    assert not offenders, (
        "submit handlers outside guardedSubmit — the button stays live for the "
        f"whole request, so a double click posts twice: {offenders}"
    )


def test_the_unguarded_submit_list_has_no_dead_entries():
    stale = [n for n in _UNGUARDED_SUBMIT_OK if not (_PANELS / n).exists()]
    assert not stale, f"exempted panels that no longer exist: {stale}"


def test_the_shared_layer_keeps_submit_to_the_guard_and_the_lock():
    """config-helpers.js is not a panel, so the sweep above doesn't reach it.
    Two listeners belong there and no more: guardedSubmit's own, and
    lockSection's capture-phase preventDefault, which is a lock rather than a
    handler — it runs no work and awaits nothing."""
    src = (_JS / "config-helpers.js").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("//"))
    assert len(_RAW_SUBMIT.findall(code)) == 2
    assert 'root.addEventListener("submit", (e) => e.preventDefault(), true);' in code


def test_every_guarded_form_imports_the_guard():
    """A panel calling guardedSubmit without importing it throws a ReferenceError
    on the first submit — the one interaction the page exists for."""
    missing = []
    for path in _panel_files():
        src = path.read_text(encoding="utf-8")
        if "guardedSubmit(" not in src:
            continue
        header = src.split("\n\n", 1)[0]
        if "guardedSubmit" not in header and not re.search(
            r"import\s*\{[^}]*\bguardedSubmit\b[^}]*\}", src
        ):
            missing.append(path.name)
    assert not missing, f"guardedSubmit used but not imported: {missing}"


# ── two written-down rules that eslint now enforces ──────────────────────
#
# The pre-commit gate runs ruff and pytest, never eslint, so a rule deleted
# from package.json's eslintConfig would only surface on a push. These read the config as
# data: they prove the ban is declared, not that the tree is clean — CI's lint
# job does that, and it is blocking.
#
# The remote test runner syncs an explicit path list (SYNC_PATHS in
# scripts/remote_test.py) that does not include package.json, so the
# config simply is not there and these two skip rather than fail. Everything
# that reads a file under src/ still runs there. CI checks out the whole repo,
# so the pins below are enforced on every push — which is also where the eslint
# job that actually applies the rules runs.

_PACKAGE_JSON = REPO_ROOT / "package.json"
_needs_eslintrc = pytest.mark.skipif(
    not _PACKAGE_JSON.exists(), reason="partial checkout: package.json is not synced here"
)


def _eslint_rules() -> dict:
    import json

    return json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))["eslintConfig"]["rules"]


@_needs_eslintrc
@pytest.mark.parametrize("banned", ["confirm", "prompt", "alert"])
def test_the_native_dialogs_are_banned(banned: str):
    """confirmDialog/promptDialog/toast are themed, focus-trapped and keyboard
    accessible; the native ones are none of that and look like a different
    product. The rule was written down in the design guide and enforced
    nowhere."""
    rules = _eslint_rules()
    props = rules["no-restricted-properties"][1:]
    assert any(p["object"] == "window" and p["property"] == banned for p in props), (
        f"window.{banned} is no longer banned"
    )
    # window.confirm() is the spelling in the tree; confirm() is the one that
    # would quietly get past a properties-only rule.
    globs = rules["no-restricted-globals"][1:]
    assert any(g["name"] == banned for g in globs), f"bare {banned}() is no longer banned"
    # ...and `window` is not the only name for the global object. A rule that
    # names only `window` leaves `globalThis.confirm()` and `self.confirm()`
    # passing silently, which is a bypass by typo rather than by intent.
    for obj in ("globalThis", "self"):
        assert any(pr["object"] == obj and pr["property"] == banned for pr in props), (
            f"{obj}.{banned} slips past the ban"
        )


@_needs_eslintrc
def test_network_is_banned_outside_api_js():
    """api.js owns same-origin credentials, the error envelope and the 401
    redirect. A panel that reaches for fetch() re-invents all three."""
    selectors = [r["selector"] for r in _eslint_rules()["no-restricted-syntax"][1:]]
    assert "CallExpression[callee.name='fetch']" in selectors
    assert "NewExpression[callee.name='XMLHttpRequest']" in selectors
    # `window` is not the only name for the global object, and a selector that
    # spells it out literally is a bypass anyone can reach for by accident --
    # `self.fetch()` is ordinary worker-flavoured code. Both alternatives are
    # banned in the same breath, for the member form of fetch and of XHR:
    # `new window.XMLHttpRequest()` has a MemberExpression callee, so the
    # bare-identifier rule above never saw it either.
    fetch_member = [s for s in selectors if "property.name='fetch'" in s]
    assert fetch_member, "the member form of fetch is no longer banned"
    for name in ("window", "globalThis", "self"):
        assert any(name in s for s in fetch_member), (
            f"{name}.fetch() is not covered by any selector"
        )
    assert any("callee.property.name='XMLHttpRequest'" in s for s in selectors), (
        "new <global>.XMLHttpRequest() is not covered by any selector"
    )


def test_every_network_exception_says_why_it_is_one():
    """Three files opt out, and an opt-out with no reason is how a ban becomes
    decorative. api.js is the sanctioned home; app.js needs the raw Response to
    tell a failed guild switch from a 401; help.js fetches the manual's HTML,
    which is not an API call at all."""
    expected = {
        _JS / "api.js": 1,
        _JS / "app.js": 1,
        _PANELS / "help.js": 1,
    }
    for path, count in expected.items():
        lines = path.read_text(encoding="utf-8").splitlines()
        disables = [
            i for i, ln in enumerate(lines)
            if "eslint-disable-next-line no-restricted-syntax" in ln
        ]
        assert len(disables) == count, f"{path.name}: {len(disables)} opt-outs, expected {count}"
        for i in disables:
            # A reason, not just the disable: the line above must be a comment
            # that is not itself the disable directive.
            above = lines[i - 1].strip()
            assert above.startswith("//") and "eslint-disable" not in above, (
                f"{path.name}:{i + 1} opts out of the network ban with no reason"
            )


def test_the_dirty_guard_is_the_only_native_confirm():
    """It has to answer synchronously — every caller is `if
    (!confirmLeaveDirty()) return;` on a navigation already under way, and
    confirmDialog returns a promise."""
    src = (_JS / "app.js").read_text(encoding="utf-8")
    assert src.count("window.confirm(") == 1
    assert src.count("eslint-disable-next-line no-restricted-properties") == 1
    for path in _panel_files():
        # The call, not the word: wellness-home.js names it in a comment saying
        # why it uses confirmDialog instead.
        assert "window.confirm(" not in path.read_text(encoding="utf-8"), path.name


def test_no_panel_ships_an_inline_event_handler():
    """A `script-src 'self'` CSP will not run one, and the attribute still
    sitting in the HTML reads as handled while doing nothing. md-preview.js was
    the last one — its link guard is a delegated listener now."""
    offenders = []
    for path in [*_panel_files(), *_JS.glob("*.js")]:
        # Comments stripped: md-preview.js and table.js both explain the attack
        # or the CSP rule in prose, and the words are not handlers.
        src = re.sub(r"//.*", "", path.read_text(encoding="utf-8"))
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        for attr in ("onclick=", "onchange=", "onsubmit=", "oninput=", "onerror="):
            if attr in src:
                offenders.append(f"{path.name}: {attr}")
    assert not offenders, offenders


def test_the_home_panel_survives_a_refused_tiles_fetch():
    """Tiles are moderator-only and home's loader renders an error over the
    whole panel if any source rejects, so an un-caught 403 cost a member the
    entire page — for a widget the nav never offered them."""
    src = (_PANELS / "home.js").read_text(encoding="utf-8")
    i = src.index('api("/api/health/tiles")')
    # The catch belongs to this fetch, not to one further down the function.
    assert ".catch(" in src[i:i + 200], "the tiles fetch is unguarded again"


_WIDGET_ENTRY = re.compile(
    r'\{\s*id:\s*"(?P<id>[^"]+)".*?perms:\s*\[(?P<perms>[^\]]*)\].*?'
    r'source:\s*"(?P<source>[^"]+)"',
    re.S,
)


def _sources_only_reachable_when_gated() -> set[str]:
    """Sources every one of whose widgets is permission-gated.

    ``home`` is deliberately not one of them: it also backs a ``perms: []``
    widget, so ``/api/home`` is the page's own fetch and a failure there
    *should* surface as an error over the whole panel.
    """
    src = (_JS / "widget-registry.js").read_text(encoding="utf-8")
    gated: dict[str, list[bool]] = {}
    for m in _WIDGET_ENTRY.finditer(src):
        gated.setdefault(m["source"], []).append(bool(m["perms"].strip()))
    return {s for s, flags in gated.items() if all(flags)}


def test_every_permission_gated_home_source_degrades_instead_of_blanking():
    """home.js awaits Promise.all over one fetch per widget source, so a single
    403 takes the whole page down — not just the widget that caused it.

    getLayout() drops a saved id whose widget no longer exists, never one the
    viewer may no longer *read*, so a demoted admin keeps the widget and loses
    the page. /health/tiles was fixed for exactly this; /economy/metrics is the
    harder case, since it answers a non-admin with a flat 403 rather than
    self-gating. Anything reachable only behind a perm needs its own .catch.
    """
    src = (_PANELS / "home.js").read_text(encoding="utf-8")
    unguarded = []
    for source in sorted(_sources_only_reachable_when_gated()):
        flag = re.search(
            r'const\s+(\w+)\s*=\s*layout\.some\([^;]*source\s*===\s*"%s"' % re.escape(source),
            src,
        )
        assert flag, f"no needs-flag in home.js for gated source {source!r}"
        block = re.search(
            r'if\s*\(%s\)\s*\{(.*?)\}\s*else' % re.escape(flag.group(1)), src, re.S
        )
        assert block, f"{flag.group(1)} is not a guarded push block"
        if ".catch(" not in block.group(1):
            unguarded.append(source)
    assert not unguarded, (
        f"these gated home sources blank the page on a 403: {unguarded}"
    )
