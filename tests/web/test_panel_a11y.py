"""Accessibility gate: axe-core finds no WCAG 2.2 AA violation on any panel.

Standards audit row 3.14. The panel-console sweep proves a panel *loads*; this
runs the axe-core engine (an npm devDependency, pinned in ``package.json``)
over the same panels once they settle, with the WCAG 2.0/2.1/2.2 A and AA rule
tags, and fails on any violation — contrast, a control with no accessible
name, a target under 24px with nothing to spare around it, a scroll box a
keyboard can't reach.

It landed born green: the first sweep found 177 nodes across five rules, all
fixed in the same commit, so there is no allowlist. A new finding is a
regression in the change that made it.

What it can't see is what a bare load doesn't draw — a row that only exists
once there is data, a dialog behind a click. ``.filter-chip-x`` and
``.transcript-close`` were named by the audit and are exactly that; their 24px
floor is set in ``app.css`` by hand.

The dashboard's CSP forbids inline script, so each context bypasses it to
inject axe — the test's own script, never the page's. axe reads
``node_modules/axe-core``; CI's browser job installs it. Locally the test
skips without it (``npm install --no-save``), but under CI a missing engine is
a failure, so the gate can't go quiet by losing its dependency.
"""

from __future__ import annotations

import os

import pytest

from tests.web import test_panel_console as _console
from tests.web.test_panel_console import _goto_panel, _scoped_ids, _settle
from tests.paths import REPO_ROOT

# The console sweep's served dashboard and browser, re-registered here: a
# pytest fixture is found by the name it has in the test's own module.
dashboard = _console.dashboard
browser = _console.browser

pytestmark = pytest.mark.browser

AXE = REPO_ROOT / "node_modules" / "axe-core" / "axe.min.js"
WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]

if not AXE.exists():
    if os.environ.get("CI"):
        raise RuntimeError(f"axe-core missing at {AXE} — the browser job must npm install")
    pytest.skip("axe-core not installed — run `npm install --no-save`", allow_module_level=True)

_RUN = """async (tags) => {
  const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}});
  return r.violations.map(v => ({
    id: v.id,
    nodes: v.nodes.slice(0, 3).map(n => n.target.join(' ')),
    count: v.nodes.length,
  }));
}"""


def _violations(browser, base: str, pid: str) -> list[dict]:
    context = browser.new_context(viewport={"width": 1280, "height": 900}, bypass_csp=True)
    page = context.new_page()
    try:
        _goto_panel(page, f"{base}/#/{pid}")
        _settle(page)
        page.add_script_tag(path=str(AXE))
        return page.evaluate(_RUN, WCAG_TAGS)
    finally:
        context.close()


def test_no_panel_has_an_axe_violation(dashboard, browser):
    ids = _scoped_ids(browser, dashboard.base)
    assert ids, "no panels enumerated from the nav — did the dashboard render?"

    failures = [
        f"{pid}: {v['id']} ×{v['count']} — {'; '.join(v['nodes'])}"
        for pid in ids
        for v in _violations(browser, dashboard.base, pid)
    ]
    assert not failures, (
        "WCAG violations (axe-core):\n" + "\n".join(failures)
        + "\nRule reference: https://dequeuniversity.com/rules/axe/4.13/<rule-id>"
    )
