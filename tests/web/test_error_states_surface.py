"""Failure states that lied: three shapes where an error never reached the user.

Each is the same class of bug — a rejection handled somewhere that produces a
worse outcome than not handling it at all.

  * **The refresh that keeps the old numbers.** Seven health panels defined
    ``function reload() { return load().then(decorate); }`` and guarded only
    the *first* call. The Show Bots toggle called ``reload()`` bare, and
    ``load()`` rejects before it touches innerHTML — so a failed refetch left
    the previous figures on screen with the checkbox already flipped and
    nothing said. Reading bot-excluded numbers under a ticked "include bots" is
    worse than an error, because it looks like an answer.

  * **The retry that could never run.** Five panels wrapped their mountAsync
    loader's own fetch in a try/catch that rendered a bare error and returned
    *normally*, so the rejection never reached mountAsync. Its ``renderFailure``
    draws the error plus a working "Try again" button, and the ``errorMsg``
    those panels carefully declare was dead code.

  * **The unsaved-edits warning that disarms itself.** ``_dirty`` was one
    module-global boolean cleared by any successful ``showStatus``. Fourteen
    panels guard two to four forms, so saving one form silently dropped the
    warning protecting half-typed values in all the others.

  * **The error with nothing to press.** The 2026-09-26 audit read all 45
    fetch-failure sites across 28 panels and found fifteen where the only way
    back was a browser reload — five saying so in their own copy. ``renderError``
    is a bare ``<div class="error">``: right for a failed *save*, where the form
    and its button are still there, and a dead end for a failed *fetch*, where
    the thing that would re-run it is usually the markup the error just
    replaced. ``showRetryableError`` is the answer for one region of a mounted
    panel, as ``mountAsync`` already was for a first load.

The lists in this file are history, not rules — each one records which panels
were repaired in one pass. The tests that will catch tomorrow's instance read
shapes across all 136 panels instead, and three of them below do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_JS = Path("src/web_server/static/js")
_PANELS = _JS / "panels"

# The seven that shared the copied reload block, byte for byte.
RELOADABLE = [
    # health-composite-score.js was here until main dropped the Health Score
    # page entirely (e6c06624) — the blend moved for six reasons at once and
    # nobody acted on the number.
    "health-heatmap.js", "health-dau-mau.js",
    "health-cohort-retention.js", "health-gini.js",
    "health-newcomer-funnel.js",
]


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("panel", RELOADABLE)
def test_health_panels_guard_every_refresh_not_just_the_first(panel: str) -> None:
    src = _src(_PANELS / panel)
    assert "mountReloadable(container" in src, f"{panel}: not using the shared helper"
    assert not re.search(r"^\s*reload\(\)\.catch\(", src, re.M), (
        f"{panel}: still guards only the initial load"
    )
    assert not re.search(r"function reload\(\) \{\n\s*return load\(\)\.then\(decorate\);", src), (
        f"{panel}: the unguarded reload is back"
    )


def test_the_shared_reloader_catches_on_every_pass() -> None:
    """Guard the guard: seven panels delegate their whole failure story here."""
    src = _src(_JS / "report-helpers.js")
    assert "export function mountReloadable" in src
    body = src.split("export function mountReloadable", 1)[1].split("\nexport ", 1)[0]
    assert ".then(decorate).catch(" in body, "the catch is not on the reload path"
    assert body.count("function reload()") == 1
    assert "return reload;" in body, "panels need the handle for their own controls"


def test_no_loader_swallows_its_own_rejection() -> None:
    """The seven-panel allowlist this replaces was a record of what had been
    fixed, not a rule: run over all 136 panels it found nothing, because every
    panel on it had already been repaired. What it could never have found is
    the same swallow in a panel nobody had thought to add. Read the shape
    instead — a catch that writes the failure straight into the container the
    loader was handed, and returns normally, so mountAsync's retry never draws.
    """
    offenders = []
    for path in sorted(_PANELS.glob("*.js")):
        if re.search(
            r'\} catch \([^)]*\) \{\s*\n\s*container\.querySelector\("\.panel"\)'
            r"\.innerHTML =?\s*\n?\s*renderError\(",
            _src(path),
        ):
            offenders.append(path.name)
    assert not offenders, (
        f"these loaders still swallow their own rejection: {offenders}"
    )


def test_the_bump_tracker_mounts_through_the_shared_loader() -> None:
    """It was a bare async IIFE whose catch wrote an error into .panel and
    returned, so there was no Try again and no way back from a failed first
    fetch but a manual reload. The RETHROWERS regex above passes trivially once
    renderError is gone, so pin what replaced it."""
    src = _src(_PANELS / "config-bump-tracker.js")
    assert "mountAsync(container, async () => {" in src
    assert "loading.unmount();" in src, "the interval handle must still be torn down"
    assert "(async () => {" not in src, "the unguarded IIFE is back"


@pytest.mark.parametrize("panel", ["wellness-blackouts.js", "wellness-caps.js"])
def test_the_wellness_panels_offer_a_retry_on_every_load(panel: str) -> None:
    """These two split first load from later loads — `if (firstLoad) throw e`
    hands the first to mountAsync — and for eight months the comment beside it
    said the later ones "keep the list up" and "report in place". They did not.
    Both panels build `.panel` once at mount and then rewrite its innerHTML
    wholesale on *every* pass, success or failure, so a failed reload took the
    header, the list, the sliders and the control that had just been clicked
    with it, leaving a bare error where the page had been. The split is still
    right; what the later branch renders had to change."""
    src = _src(_PANELS / panel)
    assert "if (firstLoad) throw e;" in src, "the first load must reach mountAsync"
    assert "showRetryableError(" in src, "the later loads have no way back"
    assert "renderError(" not in src, "a bare error state is back"


def test_the_dirty_flag_is_tracked_per_form() -> None:
    src = _src(_JS / "config-helpers.js")
    assert "const _dirtyForms = new Set()" in src, "still one page-wide boolean"
    assert "_dirtyForms.add(form)" in src
    assert 'el.closest?.("[data-dk-guard]")' in src, (
        "showStatus must attribute the save to the form it came from"
    )
    assert 'form.dataset.dkGuard = "1"' in src, "guarded containers must be findable"
    assert not re.search(r"^let _dirty = false;", src, re.M)


def test_a_guild_switch_drops_the_tracked_forms() -> None:
    """The set holds form *elements*, and a guild switch rebuilds every panel.
    Left alone it would retain detached nodes and warn about unsaved edits on
    forms that no longer exist."""
    src = _src(_JS / "config-helpers.js")
    reset = src.split("export function resetMetaCaches()", 1)[1].split("\n}", 1)[0]
    assert "_dirtyForms.clear()" in reset


# ── optimistic writes that outlive their own failure ────────────────────


def test_a_failed_separation_write_is_rolled_back() -> None:
    """Pen Pals mutates the separations list and re-renders before the PUT, so
    the row appears at once. `persistSeps` reported a failure and left it
    there — and this is the keep-them-apart list, so a separation on screen
    that the server never stored is the one failure this page must not have."""
    src = _src(_PANELS / "pen-pals-settings.js")
    assert "const persistSeps = async (undo)" in src, "no rollback is passed in"
    assert "undo();" in src and "renderSeps();" in src
    # Both mutation sites have to supply one, or the rollback is decorative.
    assert src.count("await persistSeps(() =>") == 2, (
        "a caller mutates the list without supplying its undo"
    )
    assert "await persistSeps();" not in src


def test_a_failed_order_fetch_does_not_eat_the_empty_state() -> None:
    """The failure message was written into the empty-state element itself, so
    once a fetch failed, every later successful fetch with nothing waiting
    still read "Couldn't load the orders."."""
    src = _src(_PANELS / "shop-approvals.js")
    assert 'emptyEl.textContent = "Couldn’t load the orders."' not in src
    # Its own element, which is also the one a Try again can repaint.
    assert 'showRetryableError(listEl, "Couldn’t load the orders.", refresh)' in src


def test_mahjong_holds_its_rebuild_while_house_rules_is_dirty() -> None:
    """A card upload or Set Active remounts the whole panel, which throws away
    anything half-typed in House Rules."""
    src = _src(_PANELS / "mahjong.js")
    assert "isFormDirty(form)" in src, "the rebuild still runs unconditionally"
    assert "isFormDirty," in src, "isFormDirty is used but not imported"


def test_mahjong_does_not_report_that_through_show_status() -> None:
    """[data-status] lives inside [data-form], and showStatus(el, true, …)
    clears the dirty flag for whichever guarded form its element sits in.
    Reporting the held rebuild through it would disarm the very state that
    produced the message, so the next upload would remount and take the edits
    with it. This is a real interaction between two fixes, not a style point."""
    src = _src(_PANELS / "mahjong.js")
    guard = src.split("function remount()", 1)[1].split("\n    }", 1)[0]
    # The comment explaining the choice names showStatus, so read code only.
    code = "\n".join(
        ln for ln in guard.splitlines() if not ln.lstrip().startswith("//")
    )
    assert "toast(" in code, "the held-rebuild notice must not go through showStatus"
    assert "showStatus(" not in code


def test_is_form_dirty_reads_the_same_registry_guard_form_writes() -> None:
    src = _src(_JS / "config-helpers.js")
    assert "export function isFormDirty(form)" in src
    assert "return _dirtyForms.has(form);" in src


# ── controls that changed state without telling anyone ──────────────────


def test_wellness_caps_guards_its_primary_control() -> None:
    """guardForm was attached only to the Add Manual Cap form, inside a
    collapsed <details> at the bottom. The panel's actual control — 24 sliders
    and a drag-a-point-on-the-chart interaction — sat outside every guarded
    container, so setting every cap and navigating away discarded the lot with
    no prompt."""
    src = _src(_PANELS / "wellness-caps.js")
    assert "data-histo-form" in src, "the histogram is not wrapped"
    assert 'guardForm(container.querySelector("[data-histo-form]"))' in src


def test_wellness_caps_drops_the_guarded_node_before_destroying_it() -> None:
    """load() rewrites the histogram on every mode or lookback change. Without
    this the registry keeps a detached element and reports unsaved edits on a
    form that is no longer in the document."""
    src = _src(_PANELS / "wellness-caps.js")
    assert "clearFormDirty(histoForm)" in src
    rebuild = src.index('container.querySelector(".panel").innerHTML = `')
    assert src.index("clearFormDirty(histoForm)") < rebuild, (
        "the node is forgotten after it is destroyed, which is too late"
    )


def test_a_canvas_drag_marks_the_form_dirty() -> None:
    """Dragging a cap point fires neither input nor change, so the most direct
    way to set a cap was invisible to the guard. dk:change is the repo's own
    answer — filter-select already dispatches it for the same reason."""
    src = _src(_PANELS / "wellness-caps.js")
    assert 'new CustomEvent("dk:change", { bubbles: true })' in src
    # Every way a drag can end, or the guard is armed only sometimes.
    for ending in ("mouseup", "mouseleave", "touchend"):
        block = src.split(f'canvas.addEventListener("{ending}"', 1)[1].split("});", 1)[0]
        assert "markDirty()" in block, f"a drag ending on {ending} leaves no trace"


def test_config_roles_announces_a_permission_change() -> None:
    """Add and remove are button clicks that mutate a JS array, and a click
    fires nothing guardForm listens for. Remove was untracked outright; add was
    tracked only by accident, because the picker beside it happens to dispatch
    dk:change — so the bug looked half-present."""
    src = _src(_PANELS / "config-roles.js")
    block = src.split("function refreshPermList", 1)[1].split("\n  }", 1)[0]
    assert 'dispatchEvent(new CustomEvent("dk:change"' in block, (
        "the permission list still changes silently"
    )


def test_the_quote_border_panel_does_not_invent_an_empty_state() -> None:
    """_quote_border_meta returns 200 with exists:false when no border is set,
    so a rejection is always a real failure. Swallowing it rendered "No custom
    border yet — quote cards use the bundled Golden Poppy frame", a confident
    claim about configuration that a 500 or a timeout makes false."""
    src = _src(_PANELS / "config-quote-border.js")
    assert "fall through to the empty state" not in src
    assert 'const meta = await api("/api/config/quote-border");' in src


def test_the_playlist_bulk_delete_uses_the_house_dialog() -> None:
    """It is the most destructive control on the page — an irreversible write
    to a real Spotify playlist, including songs the bot never added."""
    src = _src(_PANELS / "music-playlist.js")
    assert "window.confirm" not in src
    assert "await confirmDialog(" in src
    # Cancelling is not a no-op: the additions already ran. Reporting success
    # would also clear every guarded form on the page.
    cancel = src.split("if (!ok) {", 1)[1].split("}", 1)[0]
    assert "toast(" in cancel and "showStatus" not in cancel


def test_the_playlist_maintenance_card_is_guarded() -> None:
    """Its status element sits outside every other guarded container, so a
    successful Re-scan fell back to the page-wide clear and disarmed the
    unsaved-edits warning on the settings form above it."""
    src = _src(_PANELS / "music-playlist.js")
    assert "guardForm(cardMaint)" in src


def test_intake_report_points_at_the_real_settings_page() -> None:
    """The intake-off message names where the switch lives. Config → Intake
    Cards (config-intake) exists again since the 2026-08-29 split, so the
    message must name it — and must not point "below" at settings that left
    this page when the merged pane was split."""
    src = _src(_PANELS / "intake-report.js")
    assert "Config → Intake Cards" in src
    assert "Card Settings, below" not in src


# ── the same dead end, reached through a named load() ──────────────────


# A catch attached to a call at the top level of mount() — two-space indent, so
# a direct child of the function rather than something inside a setInterval or
# an event handler. This is the shape the lists above were written for, spelled
# a way none of their regexes could see.
#
# `\([^)]*\)` rather than `\(\)`: the 2026-09-26 audit found the same dead end
# behind a call that takes arguments — `refreshList(container, state).catch(…)`
# in photo-challenge.js — and the zero-argument version of this pattern walked
# straight past it.
_TOP_LEVEL_CATCH = re.compile(
    r"^  [A-Za-z_$][\w$]*\([^)]*\)\.catch\((.*?)^  \}\);$", re.M | re.S
)

# What makes it a dead end is that the handler paints a failure and stops.
#
# This used to allow anything that wrote into one named region, on the argument
# that a partial failure is a legitimate shape — channels.js renders its
# comparison table either way, and routing that through mountAsync would blank
# the working half of the page to report the broken half. That much is still
# true, and it is not what was at issue: the region still has to offer a way
# back, and `renderError` gives none. So the target no longer matters, only
# whether the reader is left with something to press. Widening it this far
# caught three live defects the old form missed — channels.js, photo-challenge
# and games-scheduling, the last of which left the whole schedule form standing
# with no listeners attached, because `init` wires them after its first await.
_DEAD_END = re.compile(r"renderError\(|\.innerHTML")


def test_no_panel_hand_rolls_its_own_first_load_failure() -> None:
    """health-mod-workload.js sat on none of the lists above and had the bug
    anyway: `load().catch(err => container.querySelector(".panel").innerHTML =
    renderError(...))`, ending "Reload the page to try again" because there was
    nothing to click. A named load() with a trailing .catch was invisible to
    every list. The lists pinned the panels that were fixed; this pins the
    shape, and a panel added tomorrow is covered without anyone remembering to
    add it anywhere.
    """
    offenders = []
    for path in sorted(_PANELS.glob("*.js")):
        for body in _TOP_LEVEL_CATCH.findall(_src(path)):
            if _DEAD_END.search(body):
                offenders.append(path.name)
    assert not offenders, (
        "these panels paint a first-load failure with no way back — hand it to "
        f"mountAsync, or use showRetryableError for one region: {sorted(set(offenders))}"
    )


# ── the copy that admits it ────────────────────────────────────────────

# Every argument to one of the error renderers, brackets balanced one level
# deep so a `${...}` or a nested esc() call doesn't end the match early.
_ERROR_CALL = re.compile(
    r"(?:renderError|showRetryableError)\((?:[^()]|\([^()]*\))*", re.S
)


def test_no_error_state_tells_the_reader_to_reload_the_page() -> None:
    """Seventeen sites across twelve panels said some version of "Reload the
    page to try again", and the 2026-09-26 audit judged every one of them a
    dead end without reading the copy — the DOM said so on its own. The
    sentence is the author noticing, at the moment of writing it, that there is
    nothing on screen to press. That makes it the cheapest possible tripwire
    for a shape the other tests here can only catch in the forms they were
    taught, so ban the confession and the fix has to be real.

    It is deliberately narrow: only what a *rendered error state* says. An
    Error thrown from a save handler may still say it (economy-sinks does, and
    correctly — the item really is gone), and a status line beside a form that
    still works is not a dead end.
    """
    offenders = []
    for path in sorted(_JS.rglob("*.js")):
        for call in _ERROR_CALL.findall(_src(path)):
            if re.search(r"[Rr]eload the page", call):
                offenders.append(path.name)
    assert not offenders, (
        "an error state with nothing to click tells the reader to reload; give "
        f"it a Try again instead (showRetryableError): {sorted(set(offenders))}"
    )


def test_mod_workload_mounts_through_the_shared_loader() -> None:
    """Guard the fix, not just the shape: the spinner had to move inside the
    loader, or a retry would re-run the fetch underneath the stale error."""
    src = _src(_PANELS / "health-mod-workload.js")
    assert "mountAsync(container, async () => {" in src
    assert "renderError" not in src, "the hand-rolled failure render is back"
    loader = src.split("mountAsync(container, async () => {", 1)[1]
    assert "panel-loading" in loader.split("await load();", 1)[0], (
        "the spinner must be redrawn by the retry, not left outside the loader"
    )


# ── the shared retryable error state ───────────────────────────────────


def test_the_retryable_error_state_stays_clickable_when_read_only() -> None:
    """The router disables every control on the page for a viewer without write
    access. Try again re-runs a GET they were already allowed to make, and it is
    the only way out of the error — disabling it would strand exactly the reader
    who cannot fix anything by saving."""
    src = _src(_JS / "states.js")
    block = src.split("export function renderRetryableError", 1)[1]
    assert "data-readonly-ok" in block.split("\n}", 1)[0]
    # config-helpers owns the opt-out; if it stops reading the attribute this
    # is decorative.
    assert 'el.closest("[data-readonly-ok]")' in _src(_JS / "config-helpers.js")


def test_a_second_failure_lands_back_on_the_retry() -> None:
    """One attempt is not a retry. An outage lasts longer than a click, so the
    handler has to re-render itself when the retry fails too — and show that it
    is working in the meantime, or an impatient second click fires the fetch
    twice."""
    src = _src(_JS / "states.js")
    body = src.split("export function showRetryableError", 1)[1]
    assert "renderLoading(" in body, "the in-flight state is gone"
    assert ".catch((err) => showRetryableError(el, err, onRetry))" in body


def test_mount_async_renders_the_same_error_state() -> None:
    """Two copies of an error state is two copies to keep in step. mountAsync's
    was the original and is now the same markup, so the read-only opt-out and
    the button it protects cannot drift apart."""
    src = _src(_JS / "config-helpers.js")
    assert "renderRetryableError(errorMsg, err)" in src
    failure = src.split("function renderFailure(err) {", 1)[1].split("\n  }", 1)[0]
    assert "panel-missing" not in failure, "the markup is hand-rolled again"
