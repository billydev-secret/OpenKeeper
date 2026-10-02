"""Every render site of a member-naming embed builder passes a real resolver.

House rule (CLAUDE.md, ``docs/embed_style_guide.md``): a member named inside an
embed is resolved through ``services/name_resolver.build_name_fn``, never
written as ``<@id>`` — an embed mention is resolved by the *reading* client from
its own cache, so it shows as a bare number to anyone who hasn't seen that
member. Every builder therefore takes a ``name_fn`` that *defaults to*
``mention`` so an un-wired caller still renders, which is exactly why the
wiring needs its own guard: forgetting the resolver at a call site would bring
the bare-number bug back silently. This table walks each cog's AST and fails
on any call to a naming builder that does not pass the resolver.

Features whose guard already lives beside their logic tests (clapback, MLT,
WYR, session recap, /games game-status, Guess Who, casino) are not repeated
here; this file holds the rows that had nowhere else to go when their
builders gained a ``name_fn`` on 2026-09-02. A new naming builder adds a
``pytest.param`` row, not a new test function.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

import bot_modules.games.compliment.cog as compliment_cog
import bot_modules.games.mfk.cog as mfk_cog
import bot_modules.games.ama.cog as ama_cog
import bot_modules.games.nhie.cog as nhie_cog
import bot_modules.games.ttl.cog as ttl_cog
import bot_modules.duels.musical_chairs.cog as musical_chairs_cog
import bot_modules.duels.musical_chairs.layouts as musical_chairs_layouts
import bot_modules.games.compliment.layouts as compliment_layouts
import bot_modules.games.mfk.layouts as mfk_layouts
import bot_modules.games.ama.layouts as ama_layouts
import bot_modules.games.nhie.layouts as nhie_layouts
import bot_modules.games.ttl.layouts as ttl_layouts
from tests.paths import REPO_ROOT


def _calls_to(source: str, names: set[str]):
    """Every ``Call`` node in ``source`` whose callee is one of ``names``,
    whether called bare (``build_x(...)``) or through a module
    (``embeds.build_x(...)``)."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if callee in names:
            yield callee, node


def _source_of(module) -> str:
    return pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("cog_module", "embeds_module", "kwarg"),
    [
        pytest.param(compliment_cog, compliment_layouts, "name_fn", id="compliment"),
        pytest.param(mfk_cog, mfk_layouts, "name_fn", id="mfk"),
        # Two Truths' recap took two resolvers in the embed era (a name and a
        # mention one); its card takes the one ``name_fn`` and pings from the
        # card body under an allow-list (2026-09-30).
        pytest.param(ttl_cog, ttl_layouts, "name_fn", id="ttl"),
        # AMA's board and NHIE's round/recap cards had a ``name_fn`` that
        # defaulted to ``unknown``, so an un-wired caller rendered quietly
        # (2026-10-01): it is required now, and each cog builds it.
        pytest.param(ama_cog, ama_layouts, "name_fn", id="ama"),
        pytest.param(nhie_cog, nhie_layouts, "name_fn", id="nhie"),
        pytest.param(
            musical_chairs_cog, musical_chairs_layouts, "name_fn", id="musical-chairs"
        ),
    ],
)
def test_every_render_site_passes_a_resolver(cog_module, embeds_module, kwarg):
    needs = {
        name
        for name, fn in inspect.getmembers(embeds_module, inspect.isfunction)
        if kwarg in inspect.signature(fn).parameters
    }
    assert needs, f"{embeds_module.__name__} has no builder taking {kwarg}"
    fname = pathlib.Path(inspect.getfile(cog_module)).name
    missed = [
        f"{fname}:{node.lineno} {callee}()"
        for callee, node in _calls_to(_source_of(cog_module), needs)
        if not any(kw.arg == kwarg for kw in node.keywords)
    ]
    assert not missed, f"render sites with no {kwarg}: " + ", ".join(missed)


@pytest.mark.parametrize(
    "module_path",
    [
        pytest.param("bot_modules/survivor/cog.py", id="survivor-cog"),
        pytest.param("bot_modules/survivor/tasks.py", id="survivor-reckoning"),
        pytest.param("bot_modules/survivor/views.py", id="survivor-panel"),
        pytest.param("web_server/routes/survivor.py", id="survivor-preview"),
    ],
)
def test_survivor_render_sites_resolve_names(module_path):
    """Survivor's builders take their resolver *positionally* (``name_of``),
    so the kwarg table above can't see them. Until 2026-09-11 all four sites
    passed a hand-rolled ``lambda uid: f"soul {uid}"`` instead — a literal
    bare id in the post, the exact failure this rule exists to prevent
    (todo #203). Each site must build a real resolver.
    """
    src = (
        REPO_ROOT / "src" / module_path
    ).read_text(encoding="utf-8")
    assert "build_name_fn(" in src, f"{module_path} builds no name resolver"
    assert "soul {" not in src, f"{module_path} formats a bare member id"
