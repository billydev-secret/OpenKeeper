"""Every finite-timeout ``discord.ui.View`` or ``LayoutView`` overrides
``on_timeout`` — or is named in ``KNOWN_MISSING_ON_TIMEOUT``.

Past its timeout, discord.py's default ``on_timeout`` is a no-op: the view
stops listening but its buttons keep rendering as live, so the next press
answers with Discord's generic "This interaction failed" — the member gets
no signal anything expired. ``core.utils.ExpiringView`` (see
docs/embed_style_guide.md § Expiring views) fixes that; new finite-timeout
views should inherit it rather than hand-roll ``on_timeout`` again.

A Components V2 card is a ``discord.ui.LayoutView``, which is not a
``View`` subclass — a sweep that only knew the one name would have gone
quietly blind to every card the migration lands, and the stale expired
buttons this contract exists to catch would have come straight back on the
new surface. So both names are swept, and ``core.layout.ExpiringLayoutView``
counts as covered exactly the way ``core.utils.ExpiringView`` does.

This is a static AST sweep, not an import-and-introspect one (it has to
tolerate files that can't all be imported into one process at once, and it
mirrors what a scout audit runs against the repo on disk). It only follows
*same-module* base-class chains, plus one hard-coded cross-module case:
``core.utils.ExpiringView`` — the whole reason the sweep needs that special
case is that it's the fix this contract exists to enforce, so the moment a
view is switched onto it (as ``economy_cog._MemberScopedView`` was), the
sweep has to see that it's now covered. Anything else that subclasses a
View-derived class defined in *another* module (a real but rarer pattern)
is invisible to this sweep and won't be discovered either way — a known,
accepted limitation of staying static rather than importing 60+ modules.

``duels.views.ChallengeView`` does NOT appear in the ledger below — not
because it's excluded, but because it is covered: since its Components V2
conversion it subclasses ``core.layout.ExpiringLayoutView``, whose
``on_timeout`` greys the Accept/Decline row in place once the response
window closes. ``base_game.py``'s ``_expire_loop`` sweep rewrites the
challenge card to "Challenge Expired" on its own 1-minute tick, and the
greyed row is a deliberate second layer under it, not a gap. If
someone ever moves the class back onto a bare ``LayoutView`` without an
``on_timeout``, this test *should* start flagging it — so it is
deliberately not special-cased out of the sweep, only out of the ledger.
(Re-attached after a restart it is persistent — ``timeout=None`` — and
enforces its original deadline itself; that arm is outside this sweep by
design.)

Having an ``on_timeout`` is not the same as the member seeing it fire.
Disabling the buttons in memory changes nothing on screen until something
edits the message with the disabled view, and ``mod_cog.CogPager``/``HelpView``
did exactly that — defined an ``on_timeout`` that passed the check above and
left /help's buttons live — until 2026-09-26, when both moved onto
``ExpiringView``. So a *hand-rolled* ``on_timeout`` must also reach an edit:
``.edit(``, ``edit_original_response``, ``edit_message``, or a
``super().on_timeout()`` — directly, or through a ``self.`` method of its own
class chain (the casino steps delegate to ``self.expire()``). A handler that
leaves the edit to someone else on purpose goes in ``KNOWN_SILENT_ON_TIMEOUT``
with who that someone is.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

BOT_MODULES = REPO_ROOT / "src" / "bot_modules"

_INHERIT = object()  #: this class's own __init__ doesn't set timeout — look at its base
_UNRESOLVED = object()  #: timeout= is some expression the sweep can't read at all
#: timeout= names a required param or an imported module-level constant —
#: finite by this repo's own convention (a required ``timeout:`` kwonly
#: param, or an ALL_CAPS *_TIMEOUT*/*_SECONDS constant), just not a literal
#: the sweep can read without following an import.
_FINITE_UNKNOWN = object()

#: (module, name) of every base that brings a real ``on_timeout`` with it.
#: These are the cross-module special cases the sweep resolves by name; see
#: the module docstring for why it does not follow imports in general.
_EXPIRING_BASES = frozenset({
    ("bot_modules.core.utils", "ExpiringView"),
    ("bot_modules.core.layout", "ExpiringLayoutView"),
})

#: ``discord.ui`` names a view class can inherit from directly.
_VIEW_BASE_NAMES = frozenset({"View", "LayoutView"})


class _ModuleInfo:
    def __init__(self, dotted: str, tree: ast.Module) -> None:
        self.dotted = dotted
        self.classes: dict[str, ast.ClassDef] = {}
        self.import_map: dict[str, tuple[str, str]] = {}  # local -> (module, orig)
        #: local names bound to discord.ui.View / discord.ui.LayoutView
        self.view_import_names: set[str] = set()
        self.module_consts: dict[str, object] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.import_map[local] = (node.module, alias.name)
                    if node.module == "discord.ui" and alias.name in _VIEW_BASE_NAMES:
                        self.view_import_names.add(local)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.module_consts[target.id] = node.value.value


def _dotted_for(path: Path) -> str:
    rel = path.relative_to(BOT_MODULES.parent)
    return ".".join(rel.with_suffix("").parts)


def _load_modules() -> dict[str, _ModuleInfo]:
    mods: dict[str, _ModuleInfo] = {}
    for path in sorted(BOT_MODULES.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dotted = _dotted_for(path)
        mods[dotted] = _ModuleInfo(dotted, tree)
    return mods


def _is_discord_view_attr(node: ast.expr) -> bool:
    """Matches the ``discord.ui.View`` / ``discord.ui.LayoutView`` spelling."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _VIEW_BASE_NAMES
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "ui"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "discord"
    )


def _classify_base(mi: _ModuleInfo, base: ast.expr):
    """('same', ClassDef) | ('expiring',) | ('view',) | None."""
    if _is_discord_view_attr(base):
        return ("view",)
    if isinstance(base, ast.Name):
        if base.id in mi.classes:
            return ("same", mi.classes[base.id])
        if base.id in mi.view_import_names:
            return ("view",)
        if base.id in mi.import_map:
            if mi.import_map[base.id] in _EXPIRING_BASES:
                return ("expiring",)
    return None


def _resolve_base(mi: _ModuleInfo, cls: ast.ClassDef):
    for base in cls.bases:
        kind = _classify_base(mi, base)
        if kind is not None:
            return kind
    return None


def _is_view_subclass(mi: _ModuleInfo, cls: ast.ClassDef, _seen: set | None = None) -> bool:
    _seen = _seen if _seen is not None else set()
    key = (mi.dotted, cls.name)
    if key in _seen:
        return False
    _seen.add(key)
    base = _resolve_base(mi, cls)
    if base is None:
        return False
    if base[0] in ("view", "expiring"):
        return True
    return _is_view_subclass(mi, base[1], _seen)  # base[0] == "same"


def _defines_on_timeout(cls: ast.ClassDef) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "on_timeout"
        for node in cls.body
    )


def _has_on_timeout(mi: _ModuleInfo, cls: ast.ClassDef, _seen: set | None = None) -> bool:
    _seen = _seen if _seen is not None else set()
    key = (mi.dotted, cls.name)
    if key in _seen:
        return False
    _seen.add(key)
    if _defines_on_timeout(cls):
        return True
    base = _resolve_base(mi, cls)
    if base is None:
        return False
    if base[0] == "expiring":
        return True
    if base[0] == "view":
        return False
    return _has_on_timeout(mi, base[1], _seen)  # base[0] == "same"


def _find_init(cls: ast.ClassDef):
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__":
            return node
    return None


def _param_literal_defaults(fn) -> dict[str, object]:
    """param name -> its literal default, for params with a Constant default."""
    out: dict[str, object] = {}
    args = fn.args
    tail = args.args[len(args.args) - len(args.defaults):] if args.defaults else []
    for name_node, default in zip(tail, args.defaults):
        if isinstance(default, ast.Constant):
            out[name_node.arg] = default.value
    for name_node, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None and isinstance(default, ast.Constant):
            out[name_node.arg] = default.value
    return out


def _find_super_init_call(fn):
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__init__"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
        ):
            return node
    return None


def _own_timeout_default(mi: _ModuleInfo, cls: ast.ClassDef):
    fn = _find_init(cls)
    if fn is None:
        return _INHERIT
    call = _find_super_init_call(fn)
    if call is None:
        return _INHERIT
    defaults = _param_literal_defaults(fn)
    for kw in call.keywords:
        if kw.arg != "timeout":
            continue
        if isinstance(kw.value, ast.Constant):
            return kw.value.value
        if isinstance(kw.value, ast.Name):
            if kw.value.id in defaults:
                return defaults[kw.value.id]
            if kw.value.id in mi.module_consts:
                return mi.module_consts[kw.value.id]
            # A required param, or a constant imported from elsewhere.
            return _FINITE_UNKNOWN
        return _UNRESOLVED
    return _INHERIT  # super().__init__(...) never mentions timeout at all


def _resolve_effective_timeout(mi: _ModuleInfo, cls: ast.ClassDef, _seen: set | None = None):
    _seen = _seen if _seen is not None else set()
    key = (mi.dotted, cls.name)
    if key in _seen:
        return _UNRESOLVED
    _seen.add(key)
    own = _own_timeout_default(mi, cls)
    if own is not _INHERIT:
        return own
    base = _resolve_base(mi, cls)
    if base is None:
        return _UNRESOLVED
    if base[0] in ("view", "expiring"):
        return 180.0  # discord.py's compiled-in View default
    return _resolve_effective_timeout(mi, base[1], _seen)  # base[0] == "same"


def _missing_in(mods: dict[str, _ModuleInfo]) -> set[str]:
    """Fully-qualified names of every View subclass with a finite timeout
    (a real number, or a caller-required/imported constant we can't read
    the magnitude of) and no ``on_timeout`` anywhere in its resolvable
    base chain."""
    missing: set[str] = set()
    for dotted, mi in mods.items():
        for name, cls in mi.classes.items():
            if not _is_view_subclass(mi, cls):
                continue
            timeout = _resolve_effective_timeout(mi, cls)
            finite = isinstance(timeout, (int, float)) or timeout is _FINITE_UNKNOWN
            if finite and not _has_on_timeout(mi, cls):
                missing.add(f"{dotted}.{name}")
    return missing


def _sweep_missing() -> set[str]:
    return _missing_in(_load_modules())


def _sweep_sources(sources: dict[str, str]) -> set[str]:
    """Run the same analysis over synthetic modules, for the twin tests."""
    return _missing_in(
        {dotted: _ModuleInfo(dotted, ast.parse(src)) for dotted, src in sources.items()}
    )


# Regenerated 2026-09-13 from this module's own sweep against the current
# tree (do not copy an older count from a plan doc — regenerate it, the
# same way this file was built). 55 pre-existing gaps, matched against the
# claimed 56 for exactly one intentional removal: economy_cog's
# _MemberScopedView family, fixed by this same commit onto ExpiringView.
KNOWN_MISSING_ON_TIMEOUT = frozenset({
    "bot_modules.bios.views.BrowseQuestionsView",
    "bot_modules.bios.views.CombinedStepView",
    "bot_modules.bios.views.ResumeRestartView",
    "bot_modules.advisor.cog._ApplyConfigView",
    "bot_modules.advisor.cog._PublicPostView",
    "bot_modules.emoji_stealer.cog._ConfirmDupeView",
    "bot_modules.emoji_stealer.cog._GuildPickView",
    "bot_modules.emoji_stealer.cog._StealView",
    "bot_modules.games.legitlibs.modals._ContinueView",
    "bot_modules.games.legitlibs.views._CancelConfirmView",
    "bot_modules.games.story.cog.StoryTurnView",
    "bot_modules.music.playlist.cog.MusicPlaylistPanelView",
    "bot_modules.privacy.cog._ConfirmDeleteView",
    "bot_modules.quotes.cog.QuotePreviewView",
    "bot_modules.quotes.cog.QuoteStyleView",
    "bot_modules.wellness.cog._SetupWizardView",
    "bot_modules.voice.master.voice_master_commands._KnockResponseView",
    "bot_modules.voice.master.voice_master_commands._LimitPickerView",
    "bot_modules.voice.master.voice_master_commands._ResetConfirmView",
    "bot_modules.voice.master.voice_master_commands._TransferPickerView",
    "bot_modules.voice.master.voice_master_commands._UserPickerView",
    "bot_modules.confessions.approval_views.ConfessionReviewView",
    "bot_modules.economy.bounty_views._AwardSelectView",
    "bot_modules.games.utils.game_manager.ConfirmCloseView",
    "bot_modules.survivor.views.JoinConfirmView",
    "bot_modules.survivor.views.PickPanel",
})


def test_every_timed_view_has_on_timeout_or_is_a_known_gap():
    missing = _sweep_missing()
    new_gaps = sorted(missing - KNOWN_MISSING_ON_TIMEOUT)
    assert not new_gaps, (
        "New finite-timeout View(s) with no on_timeout. Inherit "
        "core.utils.ExpiringView (preferred — see docs/embed_style_guide.md "
        "§ Expiring views) or add an explicit KNOWN_MISSING_ON_TIMEOUT "
        "entry:\n  " + "\n  ".join(new_gaps)
    )


def test_known_gap_ledger_has_no_stale_entries():
    """A ledger entry that was fixed (gained on_timeout, or its timeout
    became None/infinite) or renamed/removed must be deleted here, not left
    to rot — otherwise the ledger stops meaning anything."""
    missing = _sweep_missing()
    stale = sorted(KNOWN_MISSING_ON_TIMEOUT - missing)
    assert not stale, (
        "KNOWN_MISSING_ON_TIMEOUT entries that no longer need to be here "
        "(fixed, made infinite, renamed, or moved) — remove them so the "
        "ledger stays honest:\n  " + "\n  ".join(stale)
    )


# ── the sweep can actually see a violation ──────────────────────────────
#
# Green above means "no gaps", which is also what a sweep that matches
# nothing at all reports. These pin the matcher to synthetic sources, so a
# refactor that quietly stops recognising a base class fails here instead
# of passing silently.


def _src(base: str, *, on_timeout: bool, timeout: str = "60") -> str:
    body = "    async def on_timeout(self): ...\n" if on_timeout else "    pass\n"
    return (
        "import discord\n"
        "from bot_modules.core.utils import ExpiringView\n"
        "from bot_modules.core.layout import ExpiringLayoutView\n"
        f"class Card({base}):\n"
        "    def __init__(self):\n"
        f"        super().__init__(timeout={timeout})\n"
        f"{body}"
    )


def test_sweep_flags_a_bare_finite_timeout_view():
    assert _sweep_sources({"m": _src("discord.ui.View", on_timeout=False)}) == {"m.Card"}


def test_sweep_flags_a_bare_finite_timeout_layout_view():
    """The V2 surface: a LayoutView is not a View, and must still be seen."""
    assert _sweep_sources(
        {"m": _src("discord.ui.LayoutView", on_timeout=False)}
    ) == {"m.Card"}


def test_sweep_clears_a_layout_view_that_handles_its_own_timeout():
    assert not _sweep_sources({"m": _src("discord.ui.LayoutView", on_timeout=True)})


def test_sweep_clears_the_expiring_base_classes():
    """Both spellings of "inherited the fix" count as covered."""
    assert not _sweep_sources({"m": _src("ExpiringView", on_timeout=False)})
    assert not _sweep_sources({"m": _src("ExpiringLayoutView", on_timeout=False)})


def test_sweep_ignores_a_persistent_layout_view():
    """timeout=None is a persistent card; it never expires, so nothing to say."""
    assert not _sweep_sources(
        {"m": _src("discord.ui.LayoutView", on_timeout=False, timeout="None")}
    )


# ── a hand-rolled on_timeout has to reach the message ───────────────────

#: Calls that put the expired view on screen, or hand off to a base that does.
_EDIT_CALLS = frozenset({"edit", "edit_original_response", "edit_message", "on_timeout"})

#: Views whose ``on_timeout`` leaves the edit to another owner, on purpose.
#: Each value names that owner — an entry nobody can name is a gap, not an
#: exception.
KNOWN_SILENT_ON_TIMEOUT = {
    "bot_modules.games.clapback.cog.ClapbackRoundSummaryView": (
        "the round loop waits 10s — inside the view's 15s — then disables "
        "and edits the summary itself; on_timeout only releases that wait"
    ),
}


def _methods(mi: _ModuleInfo, cls: ast.ClassDef) -> dict[str, ast.AST]:
    """Every method visible on ``cls`` through its same-module base chain,
    nearest definition winning."""
    chain = []
    seen: set[str] = set()
    node: ast.ClassDef | None = cls
    while node is not None and node.name not in seen:
        seen.add(node.name)
        chain.append(node)
        base = _resolve_base(mi, node)
        node = base[1] if base and base[0] == "same" else None
    found: dict[str, ast.AST] = {}
    for c in reversed(chain):
        for item in c.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[item.name] = item
    return found


def _reaches_an_edit(fn: ast.AST, methods: dict[str, ast.AST], seen: set[str]) -> bool:
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        if attr in _EDIT_CALLS:
            return True
        receiver = node.func.value
        if (
            isinstance(receiver, ast.Name)
            and receiver.id == "self"
            and attr in methods
            and attr not in seen
        ):
            seen.add(attr)
            if _reaches_an_edit(methods[attr], methods, seen):
                return True
    return False


def _silent_in(mods: dict[str, _ModuleInfo]) -> set[str]:
    """Views that define their own ``on_timeout`` and never edit a message."""
    silent: set[str] = set()
    for dotted, mi in mods.items():
        for name, cls in mi.classes.items():
            if not (_is_view_subclass(mi, cls) and _defines_on_timeout(cls)):
                continue
            methods = _methods(mi, cls)
            if not _reaches_an_edit(methods["on_timeout"], methods, {"on_timeout"}):
                silent.add(f"{dotted}.{name}")
    return silent


def test_a_hand_rolled_on_timeout_edits_the_message():
    silent = _silent_in(_load_modules())
    new = sorted(silent - KNOWN_SILENT_ON_TIMEOUT.keys())
    assert not new, (
        "These define on_timeout but never edit the message, so the member "
        "still sees live buttons that fail when pressed. Inherit "
        "core.utils.ExpiringView (and set view.message after sending), or "
        "name who edits it in KNOWN_SILENT_ON_TIMEOUT:\n  " + "\n  ".join(new)
    )
    stale = sorted(KNOWN_SILENT_ON_TIMEOUT.keys() - silent)
    assert not stale, (
        "KNOWN_SILENT_ON_TIMEOUT entries that now edit or are gone:\n  " + "\n  ".join(stale)
    )


def _timeout_src(body: str) -> str:
    return (
        "import discord\n"
        "class Card(discord.ui.View):\n"
        "    def __init__(self):\n"
        "        super().__init__(timeout=60)\n"
        f"{body}"
    )


@pytest.mark.parametrize(
    "body, silent",
    [
        pytest.param(
            "    async def on_timeout(self):\n        self.button.disabled = True\n",
            True,
            id="disables-in-memory-only",
        ),
        pytest.param(
            "    async def on_timeout(self):\n        await self.message.edit(view=self)\n",
            False,
            id="edits",
        ),
        pytest.param(
            "    async def on_timeout(self):\n        await super().on_timeout()\n",
            False,
            id="defers-to-base",
        ),
        pytest.param(
            "    async def expire(self):\n        await self._i.edit_original_response(view=self)\n"
            "    async def on_timeout(self):\n        await self.expire()\n",
            False,
            id="delegates-to-a-method-that-edits",
        ),
        pytest.param(
            "    async def expire(self):\n        self.stop()\n"
            "    async def on_timeout(self):\n        await self.expire()\n",
            True,
            id="delegates-to-a-method-that-does-not",
        ),
    ],
)
def test_the_silent_scan(body, silent):
    """A scan that clears everything must be clearing, not blind."""
    mods = {"m": _ModuleInfo("m", ast.parse(_timeout_src(body)))}
    assert (_silent_in(mods) == {"m.Card"}) is silent
