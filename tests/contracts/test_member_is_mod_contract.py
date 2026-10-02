"""The bot has one definition of "staff": ``AppContext.member_is_mod``.

``GuildConfig.member_is_mod`` answers only half of it — does the member hold a
configured mod/admin role — and leaves Discord's Manage Server/Administrator
short-circuit to whoever calls it. So a call outside ``app_context.py`` is a
caller composing its own definition of "staff", and that is how six of them
came to exist: four exact copies (jail commands, jail apply, inactive apply,
the inactive sweep) and two that widened it to Manage Roles (intake cards,
promotion-review cards), until 2026-09-26. A widened copy makes one member
staff on two surfaces and an ordinary member on every other.

Call ``ctx.member_is_mod(member)`` with a Member, or ``ctx.is_mod(interaction)``
with an interaction. Bot-wide sweep: ``scripts/gate.py`` selects this test
for any change under ``src/bot_modules/``.
"""

from __future__ import annotations

import ast
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src" / "bot_modules"
OWNER = SRC / "core" / "app_context.py"

_CONFIG_SOURCES = {"guild_config", "load"}  # ctx.guild_config(...), GuildConfig.load(...)


def _is_config_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CONFIG_SOURCES
    )


def _role_only_calls(tree: ast.AST) -> list[int]:
    """Lines calling ``.member_is_mod`` on a GuildConfig rather than an AppContext."""
    config_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_config_call(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    hits = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "member_is_mod"
        ):
            continue
        receiver = node.func.value
        if _is_config_call(receiver) or (
            isinstance(receiver, ast.Name) and receiver.id in config_names
        ):
            hits.append(node.lineno)
    return sorted(hits)


def test_nothing_outside_app_context_composes_its_own_mod_check():
    offenders = [
        f"{path.relative_to(SRC.parent.parent)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        if path != OWNER
        for line in _role_only_calls(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, (
        "These call GuildConfig.member_is_mod — the role-only half of the staff "
        "check — which means the caller supplies (or forgets, or widens) the "
        "Manage Server/Administrator short-circuit itself. Use "
        "ctx.member_is_mod(member):\n  " + "\n  ".join(offenders)
    )


def test_the_scan_sees_both_shapes_of_a_copy():
    """A scan that finds nothing must be finding nothing, not unable to look."""
    tree = ast.parse(
        "def a(ctx, m):\n"
        "    return m.guild_permissions.manage_roles or ctx.guild_config(1).member_is_mod(m)\n"
        "def b(ctx, m):\n"
        "    cfg = ctx.guild_config(1)\n"
        "    return cfg.member_is_mod(m)\n"
        "def c(ctx, m):\n"
        "    return ctx.member_is_mod(m)\n"
    )
    assert _role_only_calls(tree) == [2, 5]


def test_the_owner_still_holds_the_definition():
    """If app_context stops calling the role half, the exemption is stale."""
    assert _role_only_calls(ast.parse(OWNER.read_text(encoding="utf-8")))
