"""Contract: card builders are pure — nothing in ``embeds.py``/``layouts.py`` awaits.

A builder takes what it renders (names through ``name_fn``, the accent as a
passed color) and returns an embed. Fetching inside one hides a network or
database call in what every caller and test treats as a pure function, and
makes the builder impossible to cover with a plain ``case()`` row in
``test_embed_accent_contract.py``. Resolve in the cog or service, then build.

``layouts.py`` — the Components V2 counterpart, where a builder returns a
``LayoutView`` instead of an ``Embed`` — is swept by the same rule and for
the same reason. A V2 card is *more* tempting to fetch inside, because it
holds its own buttons and a button handler is async; the handler may await,
the builder may not. ``*_layouts.py`` (the naming a feature with no package
of its own uses — ``docs/components_v2_style_guide.md`` § Code conventions,
e.g. ``economy_drops_layouts.py`` beside ``economy_drops_loop.py``, or
``economy/auction_layouts.py`` for a module inside a shared package with no
subpackage of its own) is swept the same way, since it is the same module by
another name.

Born green: 174 ``build_*`` functions across 31 ``embeds.py`` files, none
async, on 2026-09-14; the ``layouts.py`` arm added with the Components V2
mechanics on 2026-09-20, with no such module in the tree yet; the
``*_layouts.py`` arm widened with the Coin Drops port (2026-09-28), the
first feature in this tree with no package of its own — it also brings the
already-landed ``economy/quest_layouts.py``, ``economy/approval_layouts.py``
and ``economy/shop_layouts.py`` under the sweep for the first time; the
auctions port (2026-09-28) adds ``economy/auction_layouts.py``; the Bounty
Board port (2026-09-28) adds ``economy/bounty_layouts.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"


def _embeds_modules() -> list[Path]:
    return sorted(
        {*SRC.rglob("embeds.py"), *SRC.rglob("layouts.py"), *SRC.rglob("*_layouts.py")}
    )


def test_sweep_sees_the_embed_modules():
    """A moved tree would make the purity check below pass vacuously."""
    assert len(_embeds_modules()) >= 25


def test_embed_modules_never_await():
    offenders = []
    for path in _embeds_modules():
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                offenders.append(f"src/{rel}:{node.lineno} async def {node.name}")
            elif isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                offenders.append(f"src/{rel}:{node.lineno} {type(node).__name__}")
    assert not offenders, (
        "embeds.py builders must be pure: resolve names, accents and data in the "
        "caller and pass them in. Offenders:\n  " + "\n  ".join(offenders)
    )
