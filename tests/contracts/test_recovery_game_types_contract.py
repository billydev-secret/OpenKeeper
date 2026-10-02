"""Contract: every game type that opens a live-game row has a recoverer.

``create_game`` writes a ``games_active_games`` row; after a restart
``games/utils/recovery.py`` hands each row to ``bot.game_recoverers[type]``
to re-bind its buttons. A type with rows but no recoverer comes back as a
dead message whose buttons fail, and the recovery sweep skips it without a
word (``test_unknown_game_type_is_skipped_not_crashed``).

The duels (Hot Potato, Pressure Cooker, Quickdraw) keep their own tables
through ``<module>.create_game`` and never reach this sweep; Risky Rolls
restores its rounds in its own ``cog_load`` and must stay off the shared
table, or it would acquire rows nothing recovers.

Born green on 2026-09-14: 17 created types, 16 recoverers, Photo exempt.
"""

from __future__ import annotations

import ast
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"
GAME_TYPE_ARG = 3

#: Types that open a row with no recoverer, and why that is safe. Shrink only.
EXEMPT = {
    # launch() archives the row with end_game() straight after creating it:
    # a history record for stats, never a live game a restart could strand.
    "photo": "ended in the same launch() that creates it",
}


def _created_and_recovered() -> tuple[dict[str, str], set[str], list[str]]:
    created: dict[str, str] = {}
    recovered: set[str] = set()
    dynamic: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "create_game"
            ):
                arg = next(
                    (kw.value for kw in node.keywords if kw.arg == "game_type"),
                    node.args[GAME_TYPE_ARG] if len(node.args) > GAME_TYPE_ARG else None,
                )
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    created.setdefault(arg.value, f"src/{rel}:{node.lineno}")
                else:
                    dynamic.append(f"src/{rel}:{node.lineno}")
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and ast.unparse(node.targets[0].value).endswith("game_recoverers")
                and isinstance(node.targets[0].slice, ast.Constant)
            ):
                recovered.add(node.targets[0].slice.value)
    return created, recovered, dynamic


def test_sweep_sees_the_party_games():
    created, recovered, _ = _created_and_recovered()
    assert len(created) >= 15 and len(recovered) >= 15


def test_every_created_game_type_has_a_recoverer():
    created, recovered, dynamic = _created_and_recovered()
    assert not dynamic, f"create_game with a non-literal game type: {dynamic}"
    stranded = sorted(
        f"{game_type} (first created at {where})"
        for game_type, where in created.items()
        if game_type not in recovered and game_type not in EXEMPT
    )
    assert not stranded, (
        "These open a games_active_games row but register no "
        "bot.game_recoverers entry, so a restart strands them:\n  "
        + "\n  ".join(stranded)
    )


def test_exemptions_only_shrink():
    created, recovered, _ = _created_and_recovered()
    stale = sorted(t for t in EXEMPT if t not in created or t in recovered)
    assert not stale, f"No longer needs an exemption — remove from EXEMPT: {stale}"


def test_risky_rolls_stays_off_the_shared_live_game_table():
    source = (SRC / "bot_modules/games/risky_roll/cog.py").read_text(encoding="utf-8")
    created, _, _ = _created_and_recovered()
    assert "create_game" not in source
    assert "risky_roll" not in created
