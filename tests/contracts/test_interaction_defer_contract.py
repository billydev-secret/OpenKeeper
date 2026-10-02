"""An interaction handler answers Discord before its second REST call.

Discord gives an interaction three seconds to be acknowledged, and discord.py
will not retry a slow one — past the window the member sees "This interaction
failed" while the work carries on behind it. One REST call before the answer
fits comfortably; two or more in sequence is where a rate-limit wait or a slow
role create spends the budget. The rule (``.claude/agents/standards-review.md``,
defer before multi-call REST work) had one known instance, fixed by accident
in 53cbf4fe, and nothing checking for the next. Row 2.5 of the 2026-09-14
standards audit; six live instances were found and fixed when this landed.

The scan, per async function that owns the interaction's first response:

* **Owns** — a Discord entry point (``callback``/``on_submit``, or decorated
  as a button, select, command or context menu), or any function that calls
  ``interaction.response.*`` or a ``*defer*`` helper itself. A helper that does
  neither is answering through a caller that already did, and is judged
  there.
* **Acknowledged** — ``interaction.response.*``, or a helper taking the
  interaction whose name says it answers (``*defer*``, ``*ephemeral*``,
  ``*reply*``, ``*respond*``). A branch under ``if interaction.response
  .is_done():`` runs acknowledged.
* **REST** — an awaited Discord method from ``_REST_METHODS``, any
  ``fetch_*``, an ``async for`` over ``history()``, or a helper in
  ``_REST_HELPERS`` known to make one on first use. Calls on the interaction
  itself (``followup``, ``original_response``) are not counted.
* **Path-sensitive** — the arms of an ``if`` are alternatives, so an
  add-or-remove toggle counts once; a loop body counts twice; a ``return``
  ends its path.

**What it cannot see:** REST work inside a helper that isn't listed in
``_REST_HELPERS`` (the caller's count misses it), a helper that answers under
a name the pattern doesn't match, and a defer upstream of a handler that
also answers directly. The last is what ``KNOWN_PRE_ACKED`` records.

Bot-wide sweep: ``scripts/gate.py`` selects this test for any change under
``src/bot_modules/``.
"""

from __future__ import annotations

import ast
import re

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src" / "bot_modules"

_REST_METHODS = frozenset({
    "send", "edit", "delete", "add_roles", "remove_roles", "create_thread",
    "create_role", "add_reaction", "clear_reactions", "pin", "unpin", "move_to",
    "set_permissions", "ban", "kick", "timeout", "edit_message", "purge",
    "create_invite", "webhooks", "create_dm", "create_text_channel",
    "create_voice_channel", "create_category", "create_forum", "create_stage_channel",
})
# Helpers that reach Discord on first use: role_provision creates the role.
_REST_HELPERS = frozenset({"ensure_config_role", "ensure_feature_role"})
_ANSWERING_HELPER = re.compile(r"defer|ephemeral|reply|respond", re.I)
_ENTRY_NAMES = frozenset({"callback", "on_submit"})
_ENTRY_DECORATORS = frozenset({"button", "select", "command", "context_menu"})
_INTERACTION_PARAMS = ("interaction", "inter", "itx")
_CAP = 3
_ACK = "ack"

# (path under src/bot_modules, function) → why the flagged path is already
# answered. Each entry is a function the scan cannot see the whole of.
KNOWN_PRE_ACKED: dict[tuple[str, str], str] = {
    ("games/traditional/cog.py", "_do_close"): (
        "End Game reaches it through ConfirmCloseView.confirm, which answers "
        "with edit_message before calling back; the idle close passes no "
        "interaction. The response.edit_message arm is only for a call with "
        "no game message."
    ),
}


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _passes(call: ast.Call, param: str) -> bool:
    return any(isinstance(a, ast.Name) and a.id == param for a in call.args)


def _classify(call: ast.Call, param: str) -> str | None:
    func = call.func
    text = ast.unparse(func)
    name = _name(func)
    if text.startswith(f"{param}.response."):
        return _ACK
    if _ANSWERING_HELPER.search(name) and _passes(call, param):
        return _ACK
    if text.startswith(f"{param}."):
        return None  # followup, original_response, …: after the answer by definition
    if name in _REST_HELPERS:
        return "rest"
    if isinstance(func, ast.Attribute) and (name in _REST_METHODS or name.startswith("fetch_")):
        return "rest"
    return None


def _awaited(node: ast.AST, param: str) -> list[str]:
    """Answer/REST events in one statement or expression, in source order."""
    hits = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n is not node and isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
            kind = _classify(n.value, param)
            if kind:
                hits.append((n.lineno, n.col_offset, kind))
        stack.extend(ast.iter_child_nodes(n))
    return [kind for *_, kind in sorted(hits)]


def _is_done_test(test: ast.expr, param: str) -> bool | None:
    """True for ``param.response.is_done()``, False for its negation."""
    negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    inner = test.operand if negated else test
    if (
        isinstance(inner, ast.Call)
        and ast.unparse(inner.func) == f"{param}.response.is_done"
    ):
        return not negated
    return None


class _Scan:
    """Walks one function; ``worst`` is the most REST calls any path makes
    before the interaction is answered."""

    def __init__(self, param: str) -> None:
        self.param = param
        self.worst = 0

    def block(self, stmts, states: set) -> set:
        for stmt in stmts:
            live = {s for s in states if s != _ACK}
            if not live:
                return states
            states = (states - live) | self.stmt(stmt, live)
        return states

    def events(self, node: ast.AST, states: set) -> set:
        out = set()
        for s in states:
            for kind in _awaited(node, self.param):
                if kind == _ACK:
                    s = _ACK
                    break
                s = min(s + 1, _CAP)
                self.worst = max(self.worst, s)
            out.add(s)
        return out

    def stmt(self, stmt: ast.stmt, states: set) -> set:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return states
        if isinstance(stmt, ast.If):
            done = _is_done_test(stmt.test, self.param)
            if done is not None:
                acked, unacked = (stmt.body, stmt.orelse) if done else (stmt.orelse, stmt.body)
                return self.block(acked, {_ACK}) | self.block(unacked, states)
            states = self.events(stmt.test, states)
            return self.block(stmt.body, states) | self.block(stmt.orelse, states)
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            head = self.events(stmt.test if isinstance(stmt, ast.While) else stmt.iter, states)
            if isinstance(stmt, ast.AsyncFor) and _name(getattr(stmt.iter, "func", None) or stmt.iter) == "history":
                head = {s if s == _ACK else self._bump(s) for s in head}
            once = self.block(stmt.body, head)
            twice = self.block(stmt.body, {s for s in once if s != _ACK})
            return head | once | twice | self.block(stmt.orelse, head)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                states = self.events(item.context_expr, states)
            return self.block(stmt.body, states)
        if isinstance(stmt, ast.Try):
            body = self.block(stmt.body, states)
            handled = set()
            for handler in stmt.handlers:
                handled |= self.block(handler.body, states | body)
            after = self.block(stmt.orelse, body) | handled
            return self.block(stmt.finalbody, after) if stmt.finalbody else after
        if isinstance(stmt, ast.Match):
            subject = self.events(stmt.subject, states)
            out = set(subject)
            for case in stmt.cases:
                out |= self.block(case.body, subject)
            return out
        if isinstance(stmt, (ast.Return, ast.Raise)):
            self.events(stmt, states)
            return set()
        return self.events(stmt, states)

    def _bump(self, s: int) -> int:
        s = min(s + 1, _CAP)
        self.worst = max(self.worst, s)
        return s


def _is_entry_point(fn: ast.AsyncFunctionDef) -> bool:
    if fn.name in _ENTRY_NAMES:
        return True
    return any(
        _name(d.func if isinstance(d, ast.Call) else d) in _ENTRY_DECORATORS
        for d in fn.decorator_list
    )


def _owns_response(fn: ast.AsyncFunctionDef, param: str) -> bool:
    if _is_entry_point(fn):
        return True
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            func = node.value.func
            if ast.unparse(func).startswith(f"{param}.response.") or "defer" in _name(func):
                return True
    return False


def _late_answers(tree: ast.AST) -> list[tuple[int, str, int]]:
    """(line, function, REST calls) for each function that answers too late."""
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        param = next((p for p in params if p in _INTERACTION_PARAMS), None)
        if param is None or not _owns_response(fn, param):
            continue
        scan = _Scan(param)
        scan.block(fn.body, {0})
        if scan.worst >= 2:
            found.append((fn.lineno, fn.name, scan.worst))
    return found


def _scan_tree() -> dict[tuple[str, str], str]:
    out = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, name, calls in _late_answers(ast.parse(path.read_text(encoding="utf-8"))):
            out[(rel, name)] = f"src/bot_modules/{rel}:{line} {name} ({calls}{'+' if calls >= _CAP else ''} REST calls)"
    return out


def test_every_handler_answers_before_its_second_rest_call():
    flagged = _scan_tree()
    late = sorted(v for k, v in flagged.items() if k not in KNOWN_PRE_ACKED)
    assert not late, (
        "These answer the interaction only after two or more REST calls, so a "
        "slow Discord leaves the member looking at 'This interaction failed'. "
        "Defer first (interaction.response.defer, then followup), or answer "
        "before the REST work:\n  " + "\n  ".join(late)
    )


def test_every_known_exception_is_still_needed():
    flagged = _scan_tree()
    stale = sorted(f"{p}:{fn}" for p, fn in KNOWN_PRE_ACKED if (p, fn) not in flagged)
    assert not stale, "KNOWN_PRE_ACKED entries the scan no longer flags — delete them:\n  " + "\n  ".join(stale)


def _late(src: str) -> list[int]:
    return [line for line, _, _ in _late_answers(ast.parse(src))]


@pytest.mark.parametrize(
    ("src", "flagged"),
    [
        pytest.param(
            "async def callback(self, interaction):\n"
            "    await channel.set_permissions(r)\n"
            "    await msg.edit(embed=e)\n"
            "    await interaction.response.send_message('ok')\n",
            True, id="two-rest-calls-then-answer",
        ),
        pytest.param(
            "async def callback(self, interaction):\n"
            "    await interaction.response.defer()\n"
            "    await channel.set_permissions(r)\n"
            "    await msg.edit(embed=e)\n",
            False, id="deferred-first",
        ),
        pytest.param(
            "async def callback(self, interaction):\n"
            "    if grant:\n"
            "        await member.add_roles(r)\n"
            "    else:\n"
            "        await member.remove_roles(r)\n"
            "    await interaction.response.send_message('ok')\n",
            False, id="if-else-arms-count-once",
        ),
        pytest.param(
            "async def callback(self, interaction):\n"
            "    for r in roles:\n"
            "        await channel.set_permissions(r)\n"
            "    await interaction.response.send_message('ok')\n",
            True, id="a-loop-is-many-calls",
        ),
        pytest.param(
            "async def callback(self, interaction):\n"
            "    role = await ensure_config_role(ctx, guild, k, s)\n"
            "    await member.add_roles(role)\n"
            "    await safe_ephemeral(interaction, 'ok')\n",
            True, id="role-provisioning-helper-counts",
        ),
        pytest.param(
            "async def callback(self, interaction):\n"
            "    await channel.set_permissions(r)\n"
            "    async for m in channel.history(limit=5):\n"
            "        pass\n"
            "    await interaction.response.send_message('ok')\n",
            True, id="history-iteration-counts",
        ),
        pytest.param(
            "async def _helper(interaction):\n"
            "    await channel.send('a')\n"
            "    await channel.send('b')\n",
            False, id="helper-that-never-answers-is-judged-at-its-caller",
        ),
        pytest.param(
            "async def callback(self, interaction):\n"
            "    try:\n"
            "        await interaction.response.send_message('ok')\n"
            "        await store.save()\n"
            "    except Exception:\n"
            "        if interaction.response.is_done():\n"
            "            m = await channel.fetch_message(1)\n"
            "            await m.edit(content='failed')\n"
            "        raise\n",
            False, id="is-done-branch-runs-answered",
        ),
    ],
)
def test_the_scan(src, flagged):
    assert bool(_late(src)) is flagged
