"""Every role grant passes the role-safety check or says why it needn't.

On 2026-09-03 a promotion-review dial pointed at STAFF and a routine Level 5
press handed it out: the role came from config and nothing asked whether it
was safe to grant. A grant whose role comes from config — a dial, a grant
definition, a shop item, a posted button — must reach
``core.role_safety.role_block_reason`` (or its logged wrapper
``grant_block_reason``) in the same function or class as the ``add_roles``
call. The allowlist holds the sites that aren't handing out new access, each
with its reason; a new site fails here until someone decides which it is.
"""

from __future__ import annotations

import ast

import pytest
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"
GUARDS = frozenset({"role_block_reason", "grant_block_reason"})

_RESTORE = (
    "puts back the member's own snapshotted roles; refusing an elevated one "
    "would strand a jailed or sleeping mod without theirs"
)
_LOCKDOWN = "assigns the bot's own lock-down role, which removes access rather than granting it"
_BREAK_RESTORE = (
    "gives a member back the roles they held before their break, snapshotted "
    "from their own role list; the config-sourced hold roles are guarded"
)

ALLOWLIST: dict[tuple[str, str], str] = {
    ("bot_modules/moderation/jail/jail_commands.py", "_do_unjail"): _RESTORE,
    ("bot_modules/inactive/apply.py", "reactivate_member"): _RESTORE,
    ("bot_modules/moderation/jail/apply.py", "apply_jail"): _LOCKDOWN,
    ("bot_modules/moderation/jail/jail_commands.py", "check_jail_rejoin"): _LOCKDOWN,
    ("bot_modules/inactive/apply.py", "apply_inactive"): _LOCKDOWN,
    ("bot_modules/breaks/service.py", "_roll_back"): _BREAK_RESTORE,
    ("bot_modules/breaks/service.py", "_end_break"): _BREAK_RESTORE,
    ("bot_modules/economy/perk_actions.py", "apply_role_perks"): (
        "the member's personal role, created by the bot for that member alone"
    ),
}


def _names(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def unguarded_grants(source: str) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` for each ``add_roles`` call with no guard.

    A guard counts anywhere in the enclosing function or, for a method, its
    class — a button's callback checks and a sibling ``_apply`` grants.
    """
    out: list[tuple[str, int]] = []

    def visit(node: ast.AST, scopes: tuple[ast.AST, ...], func: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if not isinstance(child, ast.ClassDef) else func
                visit(child, (*scopes, child), name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "add_roles"
            ):
                guarded = False
                for scope in reversed(scopes):
                    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if _names(scope) & GUARDS:
                            guarded = True
                            break
                        if isinstance(scope, ast.ClassDef):
                            break
                if not guarded:
                    out.append((func, child.lineno))
            visit(child, scopes, func)

    visit(ast.parse(source), (), "<module>")
    return out


def _scan() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for func, line in unguarded_grants(path.read_text(encoding="utf-8")):
            found[(rel, func)] = line
    return found


def test_every_unguarded_grant_is_allowlisted():
    missing = {k: v for k, v in _scan().items() if k not in ALLOWLIST}
    assert not missing, (
        "add_roles without core.role_safety.grant_block_reason / "
        "role_block_reason — guard it, or allowlist it with a reason: "
        + ", ".join(f"{f}:{line} in {fn}" for (f, fn), line in missing.items())
    )


def test_no_allowlist_entry_is_stale():
    stale = sorted(set(ALLOWLIST) - set(_scan()))
    assert not stale, f"allowlisted but now guarded or gone — remove: {stale}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "async def grant(m, r):\n    await m.add_roles(r)\n",
            [("grant", 2)],
            id="bare-grant-flagged",
        ),
        pytest.param(
            "async def grant(m, r, g):\n"
            "    if grant_block_reason(r, g, feature='x'):\n        return\n"
            "    await m.add_roles(r)\n",
            [],
            id="guarded-in-function",
        ),
        pytest.param(
            "class B:\n"
            "    async def callback(self, r):\n        role_block_reason(r, None)\n"
            "    async def _apply(self, m, r):\n        await m.add_roles(r)\n",
            [],
            id="guarded-in-class",
        ),
        pytest.param(
            "def check(r):\n    return role_block_reason(r, None)\n"
            "async def grant(m, r):\n    await m.add_roles(r)\n",
            [("grant", 4)],
            id="guard-in-another-function-does-not-count",
        ),
        pytest.param(
            "async def add_roles(request):\n    return 1\n",
            [],
            id="a-function-named-add_roles-is-not-a-call",
        ),
    ],
)
def test_scanner(source, expected):
    assert unguarded_grants(source) == expected
