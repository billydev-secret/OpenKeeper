"""Contract: four ruff rules the repo follows in prose are pinned to a ceiling.

CLAUDE.md and ``docs/design_guide.md`` ask for ``logging.exception`` inside an
``except``, timezone-aware datetimes, closures that bind their loop variable,
and a blind ``except`` that at least logs. Nothing enforced any of it: as of
2026-09-20 ``pyproject.toml`` carries no ruff ``select`` at all, so ruff runs
its default ``E4/E7/E9/F`` set and every one of these rules is off.

The obvious fix — adding them to ``select`` — is the wrong one here.
``scripts/gate.py`` runs ``ruff check .`` over the **whole repo**, unscoped, on
every commit in every session. Selecting ``BLE001`` would therefore fail the
next commit of every concurrent session, for violations in files they never
opened. A pinned ceiling puts the rule under the full suite and CI instead,
the tier CLAUDE.md already gives pyright and the browser sweep, and leaves
every other session's gate byte-identical.

The pin is a ratchet, not a target. It may only fall, and a commit that fixes
sites lowers it in the same commit — ``test_baselines_are_not_stale`` fails
otherwise, so a fix cannot quietly bank headroom for a later regression. A rule
that reaches zero is also held there against ``# noqa``, so the last site
cannot be silenced instead of fixed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT

#: Rule → the number of violations present when the ratchet was installed.
#: Counts are repo-wide (``src/``, ``tests/`` and ``scripts/``), because that
#: is the scope ``gate.py`` lints and a dated ``datetime.now()`` in a test is
#: as wrong as one in a service.
BASELINES = {
    # Logging an exception with log.error() drops the traceback, which is the
    # half that says where it came from. Cleared 2026-09-20: at zero this is a
    # born-green guard, not a ceiling.
    "TRY400": 0,
    # A naive datetime compares wrong against an aware one and silently shifts
    # by the host's offset. Ruff offers no automated fix: each site needed
    # someone to decide which clock was meant — an absolute instant (UTC), a
    # guild-local wall clock (this repo's fixed ``tz_offset_hours``, never UTC),
    # or a test's frozen reference. Cleared 2026-09-20; born-green guards now.
    "DTZ001": 0,
    "DTZ005": 0,
    "DTZ007": 0,
    # A closure defined in a loop that reads the loop variable sees its final
    # value, not the one from its own iteration. This is a live bug class, not
    # a style rule: clearing it in 2026-09-20 turned up three games whose vote
    # view could be advanced by the previous round's stale callback. At zero
    # this is a born-green guard, not a ceiling.
    "B023": 0,
    # A bare ``except Exception`` that neither logs nor re-raises turns a
    # failure into silence — the shape behind several of this repo's
    # longest-lived bugs. Ruff exempts a handler that calls
    # ``logging.exception``, so clearing TRY400 took seven of these with it.
    # Cleared 2026-09-27 (standards audit 1.4): each of the 163 was narrowed,
    # made to log, or kept broad with a ``# noqa: BLE001 — <why>``. Unlike the
    # rules above, a reasoned suppression here is the rule working — a loop
    # guard *should* be broad — so it sits in ``REASONED`` below instead of
    # being held to zero suppressions.
    "BLE001": 0,
}

#: Zero-pinned rules whose ``# noqa`` is legitimate when it says why. Each
#: suppression must carry a reason after the code — see
#: ``test_reasoned_suppressions_say_why``.
REASONED = frozenset({"BLE001"})


def _counts(rules: list[str], *, ignore_noqa: bool = False) -> dict[str, int]:
    """Violations per rule, counted the way ``gate.py`` invokes ruff.

    One process for all the codes asked for: ruff walks the tree once, and the
    test costs about as much as the gate's own ruff step.

    ``ignore_noqa`` counts sites a ``# noqa`` comment has silenced. That is the
    honest number for a rule pinned at zero — see
    ``test_zero_pinned_rules_cannot_be_suppressed``.
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--select", ",".join(sorted(rules)),
            *(["--ignore-noqa"] if ignore_noqa else []),
            "--output-format", "json",
            ".",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # ruff exits 1 when it finds violations, which is the normal case here;
    # only a crash (2) means the invocation itself was wrong.
    assert proc.returncode in (0, 1), (
        f"ruff failed to run (exit {proc.returncode}):\n{proc.stderr}"
    )
    found = dict.fromkeys(rules, 0)
    for item in json.loads(proc.stdout or "[]"):
        code = item.get("code")
        if code in found:
            found[code] += 1
    return found


#: Rules already driven to zero. These get the stricter count below.
ZERO_PINNED = sorted(r for r, n in BASELINES.items() if n == 0 and r not in REASONED)


@pytest.fixture(scope="module")
def counts() -> dict[str, int]:
    return _counts(sorted(BASELINES))


@pytest.fixture(scope="module")
def unsuppressed_counts() -> dict[str, int]:
    return _counts(ZERO_PINNED, ignore_noqa=True) if ZERO_PINNED else {}


@pytest.mark.parametrize("rule", sorted(BASELINES))
def test_no_new_violations(rule: str, counts: dict[str, int]) -> None:
    allowed = BASELINES[rule]
    actual = counts[rule]
    assert actual <= allowed, (
        f"{rule}: {actual} violations, baseline {allowed}. "
        f"Fix the new one rather than raising the pin — run "
        f"`ruff check --select {rule} .` to see them all."
    )


@pytest.mark.parametrize("rule", ZERO_PINNED)
def test_zero_pinned_rules_cannot_be_suppressed(
    rule: str, unsuppressed_counts: dict[str, int]
) -> None:
    """A rule at zero stays at zero even counting ``# noqa``-silenced sites.

    Without this, the cheapest way past the ceiling is a comment rather than a
    fix — write a naive ``datetime.now()``, tag it ``# noqa: DTZ005``, and the
    pin never moves. A rule still working down a backlog (BLE001) keeps its
    documented suppressions; a rule that reached zero has no backlog left, so
    it has nothing left to legitimately suppress.
    """
    actual = unsuppressed_counts[rule]
    assert actual == 0, (
        f"{rule}: {actual} site(s) exist once `# noqa` comments are ignored, "
        f"though the enforced count is 0. Fix them rather than silencing them — "
        f"`ruff check --select {rule} --ignore-noqa .` lists them."
    )


@pytest.mark.parametrize("rule", sorted(BASELINES))
def test_baselines_are_not_stale(rule: str, counts: dict[str, int]) -> None:
    allowed = BASELINES[rule]
    actual = counts[rule]
    assert actual >= allowed, (
        f"{rule}: down to {actual} from a baseline of {allowed}. "
        f"Lower BASELINES[{rule!r}] to {actual} in the same commit that fixed "
        f"them, so the headroom cannot be spent on a later regression."
    )


_SCANNED = ("src", "tests", "scripts")


def _bare_suppressions(rule: str) -> list[str]:
    """``# noqa: <rule>`` comments with nothing after the code."""
    bare = re.compile(rf"#\s*noqa:\s*[A-Z0-9, ]*\b{rule}\b[A-Z0-9, ]*\s*$")
    found = []
    for top in _SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if bare.search(line):
                    found.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    return found


@pytest.mark.parametrize("rule", sorted(REASONED))
def test_reasoned_suppressions_say_why(rule: str) -> None:
    """A kept-broad site states why breadth is right there, on the line.

    ``# noqa: BLE001`` alone is the silence the rule exists to stop, moved
    into a comment. The reason is what a later reader weighs before narrowing
    it — ``— a loop guard; one bad row must not stop the sweep``.
    """
    bare = _bare_suppressions(rule)
    assert not bare, (
        f"`# noqa: {rule}` with no reason — add `— <why breadth is right "
        f"here>` after the code, or narrow the except:\n  " + "\n  ".join(bare)
    )


def test_the_bare_suppression_scan_matches_what_it_should():
    bare = re.compile(r"#\s*noqa:\s*[A-Z0-9, ]*\bBLE001\b[A-Z0-9, ]*\s*$")
    assert bare.search("except Exception:  # noqa: BLE001")
    assert bare.search("except Exception:  # noqa: BLE001, E722  ")
    assert not bare.search("except Exception:  # noqa: BLE001 — a loop guard")
    assert not bare.search("except Exception:  # noqa: BLE001 - keep the loop alive")
