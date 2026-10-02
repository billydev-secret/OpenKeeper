"""Every no-contact gate in the code belongs to a surface the spec names.

``docs/specs/no_contact_spec.md`` § Gated surfaces is the list a mod reads to know
what the no-contact list actually stops. Nothing held it to the code: a gate
could be added without a row (the Rematch ping and Whisper's guess feedback
both were), and a row could outlive the gate it describes after a refactor.
``SURFACES`` below is the binding between the two:

1. **No unregistered gate.** Every function in ``src/`` that references a gate
   (``is_no_contact*``, ``no_contact_partners*``, ``no_contact_pairs_among*``,
   ``check_and_record``) — called, or handed to ``asyncio.to_thread`` — is
   listed under a surface, or sits inside a function that is.
2. **No dead registration.** Every listed function still reaches a gate:
   directly, or through a helper that does (``_blocked_pair``,
   ``effective_blocked``, ...). A refactor that drops the gate from a
   surface's entry point fails here even though the helper survives.
3. **Registry and spec agree.** The surfaces are exactly the table's rows,
   and each row's text names every module its functions live in.

What it cannot see: a new surface that puts two members in contact and never
consults the list at all. That is the design rule in CLAUDE.md, and the
reviewer's to catch — this test keeps the gates that exist honest.

Standards status: internal design row 1.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_REPO = REPO_ROOT
_SRC = _REPO / "src"
_BOT = "bot_modules/"
_SPEC = _REPO / "docs" / "specs" / "no_contact_spec.md"

GATES = frozenset({
    "is_no_contact",
    "is_no_contact_conn",
    "no_contact_partners",
    "no_contact_partners_conn",
    "no_contact_pairs_among",
    "no_contact_pairs_among_conn",
    "check_and_record",
})

# Surface (the spec table's first column) -> "path under src/bot_modules::qualname".
# List each entry point a member's action runs through, plus any helper that
# references a gate directly — rule 1 needs the helper, rule 2 the entry points.
SURFACES: dict[str, tuple[str, ...]] = {
    "Whisper": (
        "whisper/cog.py::WhisperCog._send_impl",
        "whisper/cog.py::WhisperReplyModal.on_submit",
        "whisper/cog.py::_deliver_guess_batch",
    ),
    "AMA": (
        "games/ama/cog.py::AskQuestionModal.on_submit",
        "games/ama/cog.py::ReplyModal.on_submit",
        "games/ama/cog.py::ScreenedQuestionView.approve",
        "games/ama/cog.py::AMAView._begin_ask",
    ),
    "Confessions": (
        "confessions/cog.py::ReplyModal.on_submit",
        "confessions/cog.py::ConfessionsCog._on_message_typed_reply",
    ),
    "Needle (Auto-Thread)": (
        "needle/cog.py::NeedleCog._notify_starter",
        # The batched reply DM re-checks at send time (2026-09-28).
        "dm/batch/service.py::take_due",
    ),
    "Guess Who": (
        # The reveal (solve and expiry) names the pair; a no-contact pair degrades to ids.
        "games/guess/cog.py::_reveal_names_for",
        "games/guess/cog.py::GuessSelectView._on_select",
        "games/guess/cog.py::GameView._guess_callback",
        "games/guess/cog.py::CropEditorView._on_post",
    ),
    "Pen Pals": (
        "pen_pals/cog.py::_is_blocked_pair",
        "pen_pals/cog.py::_do_pair",
        "pen_pals/cog.py::_do_round",
        "pen_pals/cog.py::_find_instant_match",
        "pen_pals/cog.py::_force_pair_status",
        "pen_pals/cog.py::_reply_reminder_due",
        "pen_pals/cog.py::_notify_expiry",
    ),
    "Voice Master": (
        "voice/master/service.py::effective_blocked",
        "voice/master/cog.py::VoiceMasterCog._handle_hub_join",
        "voice/master/voice_master_commands.py::_sync_lock_member_overwrites",
        "voice/master/voice_master_commands.py::_sync_hidden_member_overwrites",
        "voice/master/voice_master_commands.py::_teardown_spectator_overwrites",
        "voice/master/voice_master_commands.py::post_knock_request",
        "voice/master/voice_master_commands.py::_apply_invite",
        "voice/master/voice_master_commands.py::_KnockResponseView._resolve",
    ),
    "DM requests": (
        "dm/perms/cog.py::DmPermsCog._is_mutual",
        "dm/perms/cog.py::DmPermsCog._precheck_dm_request",
        "dm/perms/service.py::load_member_connections",
        "dm/perms/cog.py::DmSettingsView.reload",
        "dm/perms/cog.py::open_dm_settings",
    ),
    "Risky Rolls": (
        "games/risky_roll/views.py::_blocked_pairs_in",
        "games/risky_roll/views.py::_blocked_pairs_in_ids",
        "games/risky_roll/views.py::_room_pings",
        "games/risky_roll/views.py::RiskyRollView.roll_button",
        # Close Round moved to the host panel (2026-10-01): the gate is in
        # close_round now; a pre-move board's close_button only points there.
        "games/risky_roll/views.py::close_round",
        "games/risky_roll/views.py::auto_close_round",
        "games/risky_roll/views.py::SixtyNineQuestionModal.on_submit",
        "games/risky_roll/views.py::_post_fallback_question",
        "games/risky_roll/views.py::_chase_posted",
    ),
    "Economy transfers": (
        "economy/economy_cog.py::EconomyCog.finalize_pay",
        "economy/economy_cog.py::EconomyCog.finalize_gift",
    ),
    "Community Bounty": (
        "economy/bounty_service.py::should_notify_winner",
        "economy/bounty_service.py::notifiable_contributors",
        "economy/bounty_service.py::award_pair_no_contact",
        "economy/bounty_views.py::_handle_award",
        "economy/bounty_views.py::_handle_cancel",
        "economy/bounty_views.py::_card_name_fn",
    ),
    "Spin the Compliment": ("games/compliment/cog.py::ComplimentView.close_generate",),
    "Marry-Fornicate-Kiss": ("games/mfk/cog.py::MFKView.close_assign",),
    "Clapback": (
        "games/clapback/cog.py::ClapbackCog._forbidden_pairs",
        "games/clapback/cog.py::ClapbackJoinView.start_game",
        "games/clapback/cog.py::ClapbackSubmitView.join_now",
        "games/clapback/cog.py::ClapbackCog.auto_start",
        "games/clapback/cog.py::ClapbackCog._run_game",
        "games/clapback/cog.py::ClapbackCog.mid_game_join",
    ),
    "Most Likely To": ("games/mlt/cog.py::MLTVoteView._vote_select_callback",),
    "Truth or Dare (traditional)": (
        "games/traditional/cog.py::TraditionalHostView._pick_target",
    ),
    "Duel challenge": (
        "duels/base_game.py::BaseGame._blocked_pair",
        "duels/base_duel.py::BaseDuel._base_challenge",
    ),
    "Group-game lobby join": (
        "duels/base_game.py::BaseGame._blocked_with_any",
        "duels/base_game.py::BaseGame._handle_lobby_join",
        "duels/base_game.py::BaseGame._handle_rematch",
    ),
    "Name the Loser": ("duels/base_game.py::BaseGame._refuse_rename_across_pair",),
    "Meadow Mahjong": ("games/mahjong/mahjong_service.py::MahjongService._tx_join",),
}


# ── the scan ─────────────────────────────────────────────────────────


def _names_in(node: ast.AST) -> set[str]:
    """Every bare name and attribute name *read* anywhere under ``node``."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Store):
            out.add(n.attr)
    return out


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    """``qualname -> def`` for every function, nested ones included."""
    out: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    out[qual] = child
                walk(child, qual + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _short(site: str) -> str:
    """``path::Class.method`` -> ``method``."""
    return site.split("::", 1)[1].rsplit(".", 1)[-1]


class Scan:
    """Which functions reference a gate, and which reach one through a helper."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.funcs: dict[str, ast.AST] = {}
        for path, text in sources.items():
            for qual, node in _functions(ast.parse(text)).items():
                self.funcs[f"{path}::{qual}"] = node
        self.names = {site: _names_in(node) for site, node in self.funcs.items()}
        # A site references the gate directly when its own body does, not
        # only through a nested def (the nested def is its own site).
        self.direct = {
            site for site, node in self.funcs.items() if GATES & self._own_names(node)
        }
        self.reaching = self._close_over_helpers()

    @staticmethod
    def _own_names(node: ast.AST) -> set[str]:
        out: set[str] = set()
        stack = list(ast.iter_child_nodes(node))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                if isinstance(n, ast.Lambda):
                    stack.extend(ast.iter_child_nodes(n))
                continue
            if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Store):
                out.add(n.id)
            elif isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Store):
                out.add(n.attr)
            stack.extend(ast.iter_child_nodes(n))
        return out

    def _close_over_helpers(self) -> set[str]:
        """Sites whose body, nested defs included, reaches a gate.

        A helper passes reach on by its short name only when that name is
        defined once in the whole tree — ``on_submit`` or ``reload`` would
        otherwise make every caller of any namesake count as gated.
        """
        short_count: dict[str, int] = {}
        for site in self.funcs:
            short_count[_short(site)] = short_count.get(_short(site), 0) + 1
        reaching = {s for s, names in self.names.items() if GATES & names}
        while True:
            helpers = {_short(s) for s in reaching if short_count[_short(s)] == 1}
            grown = {
                s
                for s, names in self.names.items()
                if s not in reaching and helpers & (names - {_short(s)})
            }
            if not grown:
                return reaching
            reaching |= grown


def _registered(site: str, registry: dict[str, tuple[str, ...]]) -> bool:
    """A site is covered when it, or a function it is nested in, is listed."""
    listed = {s for sites in registry.values() for s in sites}
    path, qual = site.split("::")
    parts = qual.split(".")
    return any(f"{path}::{'.'.join(parts[:i])}" in listed for i in range(1, len(parts) + 1))


def _tree_sources() -> dict[str, str]:
    out = {}
    for p in sorted(_SRC.rglob("*.py")):
        rel = p.relative_to(_SRC).as_posix()
        if "no_contact" in p.name or "/no_contact/" in rel:
            continue  # the list's own service, cog and routes manage it,
            # whether as services/no_contact_*.py or a no_contact/ package
        out[rel.removeprefix(_BOT)] = p.read_text(encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def scan() -> Scan:
    return Scan(_tree_sources())


def _spec_rows() -> dict[str, str]:
    text = _SPEC.read_text(encoding="utf-8")
    section = text.split("## Gated surfaces", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) < 3 or cells[0] in ("Surface", "") or set(cells[0]) <= {"-"}:
            continue
        rows[re.sub(r"\*\*", "", cells[0])] = line
    return rows


# ── the three rules ──────────────────────────────────────────────────


def test_the_scan_sees_the_gates(scan):
    """A broken scanner would pass every rule below by finding nothing."""
    assert len(scan.direct) >= 30
    assert "whisper/cog.py::WhisperCog._send_impl" in scan.direct, (
        "a gate handed to asyncio.to_thread is a reference, not a call — the "
        "scan must count it"
    )


def test_every_gate_belongs_to_a_registered_surface(scan):
    unregistered = sorted(s for s in scan.direct if not _registered(s, SURFACES))
    assert not unregistered, (
        "These functions consult the no-contact list but no surface lists them. "
        "Add each to SURFACES under the surface it gates — and if that surface "
        "is new, give it a row in docs/specs/no_contact_spec.md § Gated surfaces:\n  "
        + "\n  ".join(unregistered)
    )


def test_every_registered_function_still_reaches_a_gate(scan):
    missing = [s for sites in SURFACES.values() for s in sites if s not in scan.funcs]
    assert not missing, f"registered but not defined (renamed or deleted?): {missing}"
    dead = [s for sites in SURFACES.values() for s in sites if s not in scan.reaching]
    assert not dead, (
        "These surface entry points no longer reach the no-contact list, "
        "directly or through a helper. Either the gate was lost in a refactor "
        "(restore it) or the surface changed (update SURFACES and the spec row):\n  "
        + "\n  ".join(dead)
    )


def test_the_registry_is_the_spec_table():
    rows = _spec_rows()
    assert set(SURFACES) == set(rows), (
        f"only in SURFACES: {sorted(set(SURFACES) - set(rows))}; "
        f"only in the spec table: {sorted(set(rows) - set(SURFACES))}"
    )


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_each_spec_row_names_its_modules(surface):
    row = _spec_rows()[surface]
    stems = {Path(site.split("::")[0]).stem for site in SURFACES[surface]}
    unnamed = sorted(stem for stem in stems if stem not in row)
    assert not unnamed, f"the {surface!r} row's Where cell doesn't mention {unnamed}"


# ── the scan's own rules, on synthetic code ──────────────────────────


_HELPER_MOD = """
import asyncio
from svc import no_contact_service

def _kept_apart(a, b):
    return no_contact_service.is_no_contact(a, b)

async def threaded(a, b):
    return await asyncio.to_thread(no_contact_service.no_contact_partners, a, b)

def via_helper(a, b):
    if _kept_apart(a, b):
        return

def on_submit(a):
    return a

def ungated(a, b):
    return on_submit(a)

class View:
    def outer(self):
        def _tx(conn):
            return no_contact_service.is_no_contact_conn(conn, 1, 2)
        return _tx
"""
_OTHER_MOD = """
def on_submit(a):
    return no_contact_service.check_and_record(a)
"""


@pytest.mark.parametrize(
    "site, direct, reaching",
    [
        pytest.param("m.py::_kept_apart", True, True, id="a-direct-call"),
        pytest.param("m.py::threaded", True, True, id="handed-to-to_thread"),
        pytest.param("m.py::via_helper", False, True, id="through-a-unique-helper"),
        pytest.param("m.py::ungated", False, False, id="an-ambiguous-name-carries-nothing"),
        pytest.param("m.py::View.outer._tx", True, True, id="the-nested-def-is-the-site"),
        pytest.param("m.py::View.outer", False, True, id="its-enclosing-method-reaches"),
    ],
)
def test_the_scan(site, direct, reaching):
    s = Scan({"m.py": _HELPER_MOD, "o.py": _OTHER_MOD})
    assert (site in s.direct) is direct
    assert (site in s.reaching) is reaching


def test_a_nested_site_is_covered_by_its_listed_parent():
    registry = {"X": ("m.py::View.outer",)}
    assert _registered("m.py::View.outer._tx", registry)
    assert not _registered("m.py::View.other", registry)
