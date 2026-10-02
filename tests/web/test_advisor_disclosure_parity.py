"""help.js's AI/human disclosure fallback must match disclosure_line()'s.

AUP ("Additional Use Case Guidelines"): every consumer-facing chatbot must
disclose it's AI, not a person, at the start of each session. Dungeon Keeper
defines that string exactly once — ``advisor_service.disclosure_line()`` —
and every advisor surface (``/ask``, the Discord Ask panel + its chat window,
the dashboard Help panel's ask box) renders it. The dashboard can't call
Python at render time, so ``help.js`` carries a hardcoded fallback literal for
the moment before (or unless) ``/api/help/advisor/name`` resolves; nothing
enforces that the two stay byte-identical except this test. Pattern follows
``tests/contracts/test_jump_link_contract.py`` — read the JS source as text and check it
against the Python contract, rather than running a JS engine.

File placement: this lives in ``tests/web/`` (not top-level ``tests/``) so
``scripts/gate.py --scoped`` selects it automatically whenever any
``src/web_server/*`` file changes (its unconditional glob over
``tests/web/test_*.py`` — ``help.js`` and ``routes/advisor.py`` both live
there); the ``advisor`` in this filename also matches the token
``_tokens_for`` derives from ``advisor_service.py`` (``.../advisor_service.py``
→ token ``advisor``), so an ``advisor_service.py``-only change selects it too.
"""

from __future__ import annotations

import re

from bot_modules.advisor.service import DEFAULT_ASSISTANT_NAME, disclosure_line
from tests.paths import REPO_ROOT

_HELP_JS = (
    REPO_ROOT
    / "src" / "web_server" / "static" / "js" / "panels" / "help.js"
)

# Matches: const DEFAULT_DISCLOSURE = "...";
_LITERAL_RE = re.compile(r'const DEFAULT_DISCLOSURE\s*=\s*"([^"]*)"')


def _js_fallback() -> str:
    text = _HELP_JS.read_text(encoding="utf-8")
    m = _LITERAL_RE.search(text)
    assert m, "help.js: DEFAULT_DISCLOSURE literal not found — did it get renamed?"
    return m.group(1)


def test_help_js_disclosure_fallback_matches_disclosure_line():
    # disclosure_line()'s own default parameter is DEFAULT_ASSISTANT_NAME —
    # asserting both spellings agree catches either one drifting.
    assert disclosure_line() == disclosure_line(DEFAULT_ASSISTANT_NAME)
    assert _js_fallback() == disclosure_line()
