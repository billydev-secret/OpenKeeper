"""A game's retained send goes out through ``retry_transient``, or says why not.

discord.py retries a 500, 502, 504 or 524 by itself (``discord/http.py:766``)
and **does not retry a 503** — that one falls through to ``:791`` and is raised
as ``DiscordServerError``. Discord returns 503 during ordinary gateway churn,
so a multi-round game loop meets one eventually. Clapback game ``959cd749``
is the one that was actually observed dying this way, and
``games/utils/send_retry.py`` is the fix that came out of it.

**Retained is the line this rule draws.** A fire-and-forget send that fails
loses one message. A send whose ``Message`` the loop *keeps* — to store its id
in ``games_active_games``, to bind a view to, to edit next phase — takes the
game with it: the row is written with no ``message_id``, the view leaks in
``bot.active_views``, and ``recovery.py`` will not resume it on the next
restart because it skips games whose anchor message is gone. The anchor id is
exactly what the failed send never wrote. The channel stays blocked until the
24-hour sweep.

**The file set is derived, not listed.** The rule covers every module under
``src/bot_modules`` that imports ``game_manager`` or defines
``on_game_resolved`` — 46 files. The check this replaces was a substring scan
hard-coded to one file (``games_clapback_cog.py``), which is why the standards
audit's row 1.10 read "2 of 22 done" while 41 of 47 retained sites across 22
files were still bare.

**``lambda:``, never ``functools.partial``.** Both defer the call correctly,
but ``tests/contracts/test_mention_default_audit.py`` walks the AST for ``.send`` calls
and cannot see through a ``partial`` — wrapping a send in one silently removes
it from that sweep's coverage. Measured: bare → found, ``lambda:`` → found,
``partial`` → blind. ``test_no_send_is_hidden_behind_a_partial`` below is what
keeps this contract from quietly disarming that one.

**What this cannot see.** It matches the retained shape only, so a
fire-and-forget send that mattered anyway is invisible. It cannot derive
*why* a site is safe: whether the send runs before or after the
state-creating ``create_game()`` is what separates ``games_ffa_cog.py``'s
launch (wrapped) from its banner launch (exempt), two calls in one file, and
that is program order, not syntax. It is blind to the caller — ``risky_roll``'s
send is locally caught and still had to be wrapped, because
``scheduled_games_service`` marks the schedule row done *before* awaiting the
launcher, so a swallowed failure permanently loses a one-time occurrence. It
recognises one retry spelling, a call named ``retry_transient``. And it cannot
weigh duplicate-post risk, which is the whole reason ``rushmore``'s vote card
is exempt rather than wrapped. ``ALLOWED`` carries a human reason per row and
this file checks the *structure* each reason claims, never the claim itself.
"""

from __future__ import annotations

import ast
from collections import Counter
from typing import NamedTuple
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src" / "bot_modules"
GAME_MANAGER = "game_manager"
RESOLVED_HOOK = "on_game_resolved"
SEND_ATTRS = frozenset({"send", "send_message"})
RETRY = "retry_transient"


class Site(NamedTuple):
    path: str
    func: str
    target: str
    line: int


class Exempt(NamedTuple):
    path: str
    func: str
    target: str
    #: How many sites in that function share the name. Only the three
    #: ``last_msg`` reveal sends in quiplash's ``_run_reveal_phase`` need it.
    times: int
    #: The structure the reason leans on, re-checked below: an ``except``
    #: clause around the send, a ``finally`` that runs regardless, or ``""``
    #: for a reason that is about live UI rather than about a handler.
    guard: str
    reason: str


#: Measured 2026-09-24, after the fix wave. Shrink-only: a new bare retained
#: send is wrapped, not added here. Adding a row is a claim that losing this
#: message cannot strand the game — and the reason has to say how.
ALLOWED: tuple[Exempt, ...] = (
    Exempt(
        "games/ama/cog.py", "approve", "question_msg", 1, "",
        "Screened-question approve. The view is not stopped until after the send "
        "succeeds, so the host pressing Approve again is a live retry path — and a "
        "question left at `pending` here is *correct*, because that is exactly the "
        "state the host's next Approve press acts on. Contrast the unfiltered post "
        "in `on_submit`, where `pending` is unreachable by any press and which is "
        "wrapped and rolled back instead.",
    ),
    Exempt(
        "games/ffa/cog.py", "on_submit", "reply_msg", 1, "except",
        "Anonymous reply modal; `except discord.HTTPException` tells the member to "
        "resubmit and returns before any payload write.",
    ),
    Exempt(
        "games/ffa/cog.py", "next_prompt", "new_msg", 1, "except",
        "Deliberately 'send first' by its own design comment; the handler tells the "
        "host to retry before `modify_payload` runs.",
    ),
    Exempt(
        "games/ffa/cog.py", "launch_banner", "msg", 1, "",
        "Banner-mode launch: `create_game()` runs AFTER this send, so a failure "
        "leaves no row and no active_views entry to leak. Contrast the embed launch "
        "in the same file, which creates the row first and is wrapped.",
    ),
    Exempt(
        "games/legitlibs/modes/classic.py", "_run_reveal_phase", "reveal_msg", 1, "finally",
        "Reveal send inside a try/finally whose finally calls `end_game` "
        "unconditionally, with contrib_ids resolved before the try. Only the "
        "rendered story and the 'Another One' offer are lost, never the payout.",
    ),
    Exempt(
        "games/legitlibs/modes/quiplash.py", "_run_reveal_phase", "last_msg", 3, "finally",
        "Three reveal sends — the empty-submissions notice, the single-submission "
        "reveal, and the cast-attribution card — all in the same try/finally as "
        "classic.py's, with paid_ids pre-resolved and `end_game` running regardless.",
    ),
    Exempt(
        "games/photo/cog.py", "launch", "msg", 1, "",
        "`create_game()` runs AFTER this send, so nothing is leaked; the scheduler's "
        "own `except Exception` absorbs it and the next occurrence is unaffected.",
    ),
    Exempt(
        "games/price/cog.py", "_run_round", "vote_msg", 1, "except",
        "Vote card already in `except Exception: advance_round + return` — the round "
        "skips its vote and the game moves on rather than stranding.",
    ),
    Exempt(
        "games/rushmore/cog.py", "_run_draft_loop", "ping_msg", 1, "except",
        "Draft turn ping in `except discord.HTTPException: pass`; the turn falls "
        "through to its own timeout whether or not the ping posted.",
    ),
    Exempt(
        "games/rushmore/cog.py", "_run_backfill", "msg", 1, "except",
        "Backfill invite in `except discord.HTTPException: return`; the caller goes "
        "straight to final boards with whatever picks already exist.",
    ),
    Exempt(
        "games/rushmore/cog.py", "_show_final_boards", "vote_msg", 1, "except",
        "Vote card whose `except Exception` deliberately does NOT retry — it falls to "
        "a no-winner recap and ends cleanly. Wrapping this one is the harmful move: "
        "a retry after a send that actually landed leaves two live, independently "
        "tallied vote cards for one round.",
    ),
    Exempt(
        "duels/musical_chairs/cog.py", "_repost_panel", "new_msg", 1, "except",
        "`_repost_panel` returns False on failure by documented contract, and its one "
        "caller falls back to editing the still-live old panel in place.",
    ),
    Exempt(
        "games/start_ping_service.py", "_game_lobby_ping", "message", 1, "except",
        "One-shot lobby advertisement, claimed before the send and wrapped in its own "
        "`except Exception: log + return`; the lobby and its board are unaffected.",
    ),
    Exempt(
        "games/scheduled_service.py", "_process_due", "announcement", 1, "except",
        "One-shot launch announcement; the game is already launched and boarded "
        "before this line, and the send has its own `except Exception: log + continue`.",
    ),
)


def drives_a_game(tree: ast.AST) -> bool:
    """A module runs a game if it imports the manager or defines the hook.

    Derived rather than listed so a new game cog is covered the day it lands.
    ``from ...game_manager import end_game`` puts the name in ``module``;
    ``from ...utils import game_manager`` puts it in ``names``. Both are the
    same dependency, and checking only one is how a scan goes quietly blind to
    half the files it is meant to cover.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and GAME_MANAGER in node.module:
                return True
            if any(GAME_MANAGER in a.name for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(GAME_MANAGER in a.name for a in node.names):
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == RESOLVED_HOOK:
                return True
    return False


def _awaited_send(value: ast.expr | None) -> bool:
    """``await <anything>.send(...)`` — but not ``await retry_transient(...)``."""
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return False
    fn = value.value.func
    return isinstance(fn, ast.Attribute) and fn.attr in SEND_ATTRS


#: A handler only absorbs a 503 if it names something ``DiscordServerError``
#: actually inherits from. Its MRO is DiscordServerError -> HTTPException ->
#: DiscordException -> Exception, so ``except discord.Forbidden`` — a sibling,
#: not an ancestor — catches nothing here. That was the defect behind the
#: thirteen widened handlers this contract shipped with, and pinning an
#: exemption to a Forbidden-only handler would re-introduce it quietly.
CATCHES_503 = ("HTTPException", "DiscordServerError", "DiscordException", "Exception")


def _absorbs_a_503(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare except
        return True
    return any(name in ast.unparse(handler.type) for name in CATCHES_503)


def _enclosing(tree: ast.AST) -> list[tuple[int, int, str]]:
    return [
        (n.lineno, n.end_lineno or n.lineno, n.name)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def retained_bare_sends(source: str, rel: str) -> list[Site]:
    """Every ``x = await <chan>.send(...)`` in a game-driving module."""
    tree = ast.parse(source)
    if not drives_a_game(tree):
        return []
    funcs = _enclosing(tree)
    out: list[Site] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if not _awaited_send(node.value):
            continue
        inner = [name for lo, hi, name in funcs if lo <= node.lineno <= hi]
        out.append(Site(rel, inner[-1] if inner else "<module>", ast.unparse(target), node.lineno))
    return out


def _game_modules() -> list[tuple[str, str]]:
    return [
        (p.relative_to(SRC).as_posix(), p.read_text(encoding="utf-8"))
        for p in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def _all_sites() -> list[Site]:
    out: list[Site] = []
    for rel, source in _game_modules():
        out.extend(retained_bare_sends(source, rel))
    return out


def _key(s: Site | Exempt) -> tuple[str, str, str]:
    return (s.path, s.func, s.target)


def _allowed_counter() -> Counter[tuple[str, str, str]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for row in ALLOWED:
        counts[_key(row)] += row.times
    return counts


def test_every_retained_game_send_goes_out_through_the_retry():
    found = Counter(_key(s) for s in _all_sites())
    extra = found - _allowed_counter()
    assert not extra, (
        "These keep the Message they send — its id goes in games_active_games, or a "
        "view binds to it — and a 503 walks straight past them, leaving a game row "
        "with no anchor that recovery.py will refuse to resume:\n  "
        + "\n  ".join(f"{p} :: {fn} :: {t}" for (p, fn, t) in sorted(extra))
        + "\nWrap it: `x = await retry_transient(lambda: chan.send(...), label=\"...\")`. "
        "Use `lambda:` and never `functools.partial` — a partial makes the send "
        "invisible to tests/contracts/test_mention_default_audit.py. If losing this message "
        "genuinely cannot strand the game, add an ALLOWED row saying how."
    )


def test_allowed_only_shrinks():
    """A wrapped or deleted site left pinned makes the list lie about what is left."""
    found = Counter(_key(s) for s in _all_sites())
    stale = _allowed_counter() - found
    assert not stale, (
        "Wrapped, renamed or gone — remove from ALLOWED: "
        + ", ".join(f"{p} :: {fn} :: {t}" for (p, fn, t) in sorted(stale))
    )


def test_each_exemption_still_has_the_guard_its_reason_claims():
    """The half of an exemption that can rot without anyone noticing.

    Every ``except``/``finally`` row above is exempt *because* of a handler
    somebody wrote. Delete or narrow that handler and the reason becomes false
    while the row sits there passing. This re-derives the structure — never the
    claim, which no test can check.

    For an ``except`` row it is not enough that *a* handler is there: it has to
    be one a 503 lands in. Narrowing one to ``except discord.Forbidden`` — the
    exact shape this contract shipped thirteen fixes for — fails here, because
    ``DiscordServerError`` is that class's sibling and not its ancestor.
    """
    by_path = dict(_game_modules())
    for row in ALLOWED:
        if not row.guard:
            continue
        tree = ast.parse(by_path[row.path])
        lines = [
            s.line
            for s in retained_bare_sends(by_path[row.path], row.path)
            if _key(s) == _key(row)
        ]
        assert lines, f"{row.path}::{row.target} not found — ALLOWED is stale"
        for line in lines:
            wrapping = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Try)
                and any(s.lineno <= line <= (s.end_lineno or s.lineno) for s in node.body)
                and (
                    any(_absorbs_a_503(h) for h in node.handlers)
                    if row.guard == "except"
                    else node.finalbody
                )
            ]
            assert wrapping, (
                f"{row.path}:{line} ({row.target}) is exempt on the strength of a "
                f"`{row.guard}` that is no longer there. Either restore it, or wrap "
                f"the send in retry_transient and drop the ALLOWED row.\n"
                f"  The reason on file: {row.reason}"
            )


#: Calls that undo game state. A handler that runs one of these is doing the
#: cleanup the game depends on, so it has to be reachable by the failure it is
#: guarding against. Deliberately a named list rather than "any call": a
#: handler that only logs and returns is not making a promise it can break.
CLEANUP_CALLS = frozenset({
    "end_game", "abandon", "modify_payload", "update_game_state",
    "_db_set_state", "_cleanup", "rollback", "pop", "discard",
})


def _cleans_up(body: list[ast.stmt]) -> list[str]:
    found = []
    for stmt in body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in CLEANUP_CALLS:
                found.append(name)
    return found


def test_cleanup_after_a_retried_send_can_catch_the_exhausted_retry():
    """The defect that made the thirteen widened handlers necessary.

    Wrapping a send in ``retry_transient`` changes what comes out of it. A
    handler written for ``except discord.Forbidden`` used to see every failure
    worth cleaning up after; once the send retries, the failure that survives
    three attempts is a ``DiscordServerError``, which is ``Forbidden``'s
    *sibling* — both inherit ``HTTPException`` — and lands nowhere.

    So the cleanup silently stopped running: ``end_game`` never called, the
    view never popped, the row left with no anchor. The handler still looked
    right, and nothing failed. This is the check that says otherwise.

    Note what it does not ask. A handler that only logs and returns is fine
    Forbidden-only — it promises nothing. The rule bites exactly when a
    handler undoes state, because that is when not running is a bug.
    """
    offenders: list[str] = []
    for rel, source in _game_modules():
        tree = ast.parse(source)
        if not drives_a_game(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not node.handlers:
                continue
            retried = any(
                isinstance(n, ast.Call)
                and (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")) == RETRY
                for stmt in node.body
                for n in ast.walk(stmt)
            )
            if not retried:
                continue
            if any(_absorbs_a_503(h) for h in node.handlers):
                continue
            for handler in node.handlers:
                calls = _cleans_up(handler.body)
                if calls:
                    offenders.append(
                        f"{rel}:{handler.lineno} `except "
                        f"{ast.unparse(handler.type) if handler.type else 'bare'}` "
                        f"-> {sorted(set(calls))}"
                    )
    assert not offenders, (
        "These clean up after a send that retries, but cannot catch the failure a "
        "retry actually ends in. retry_transient re-raises the last error, and for a "
        "503 that is DiscordServerError — a sibling of Forbidden, not a subclass — so "
        "this cleanup never runs and the game is left exactly as stranded as if no "
        "handler existed:\n  " + "\n  ".join(offenders)
        + "\nWiden it: `except (discord.Forbidden, discord.DiscordServerError):`"
    )


def test_no_send_is_hidden_behind_a_partial():
    """``functools.partial`` defers the call and blinds the mention audit.

    ``tests/contracts/test_mention_default_audit.py`` walks for ``.send`` calls to prove
    every one passes ``allowed_mentions``. It sees through ``lambda:`` and not
    through ``partial``, so the natural-looking way to satisfy *this* contract
    is also the way to silently disarm *that* one. Measured 2026-09-24: bare →
    found, ``lambda:`` → found, ``partial`` → blind.
    """
    offenders: list[str] = []
    for rel, source in _game_modules():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "partial" or not node.args:
                continue
            inner = node.args[0]
            if isinstance(inner, ast.Attribute) and inner.attr in SEND_ATTRS:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "A send behind functools.partial is invisible to "
        f"tests/contracts/test_mention_default_audit.py: {offenders}. Use `lambda:` instead."
    )


def test_the_scan_finds_the_files_and_the_shape_it_is_supposed_to_find():
    """A guard on the guard: a silently-empty scan would pass everything."""
    modules = _game_modules()
    driving = [rel for rel, src in modules if drives_a_game(ast.parse(src))]
    assert len(driving) > 40, f"only {len(driving)} game modules found — has the derivation broken?"
    assert "games/clapback/cog.py" in driving, "the cog this rule came from fell out of the scan"
    assert "games/board_sticky_service.py" in driving, "the shared board fallback fell out"

    cases = [
        ("from bot_modules.games.utils import game_manager\nasync def f():\n msg = await channel.send(x)\n",
         1, "a bare retained send in a game file"),
        ("from bot_modules.games.utils import game_manager\nasync def f():\n await channel.send(x)\n",
         0, "fire-and-forget is not retained"),
        ("async def f():\n msg = await channel.send(x)\n",
         0, "not a game file"),
        ("from bot_modules.games.utils import game_manager\nasync def f():\n msg = await retry_transient(lambda: channel.send(x))\n",
         0, "already wrapped"),
        ("async def on_game_resolved():\n pass\nasync def f():\n v.message = await channel.send(x)\n",
         1, "attribute target, game file derived from the hook"),
        ("from bot_modules.games.utils.game_manager import end_game\nasync def f():\n msg = await interaction.followup.send(x)\n",
         1, "followup.send counts, and the other import spelling is seen"),
    ]
    for source, want, why in cases:
        got = len(retained_bare_sends(source, "probe.py"))
        assert got == want, f"detector {why}: wanted {want}, got {got}"
