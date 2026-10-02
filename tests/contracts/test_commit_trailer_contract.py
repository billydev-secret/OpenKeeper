"""Contract: no commit since 2026-09-13 carries an AI attribution trailer.

CLAUDE.md § Commits says it in four words — "**No** ``Co-Authored-By`` /
``Claude-Session`` trailers" — and until now nothing checked. The rule is not
cosmetic: a trailer names a co-author who cannot be contacted, holds no
copyright and will not answer a question about the change, and
``Claude-Session:`` publishes a URL that resolves for nobody but the person who
ran it. ``git log`` is the repo's only durable record of intent, so a line in
it that is false stays false.

A commit message is also the one artefact that cannot be fixed later. Editing
it rewrites history, and this repo shares its object store across five
worktrees — so the cheap moment is the moment it is written, which is what the
``commit-msg`` tier is for. This test is the backstop underneath it: it catches
a ``--no-verify`` bypass and a machine that never installed the hook.

**Why a cutoff rather than zero.** 333 commits of 2,841 carry
``Co-Authored-By: Claude`` and 121 carry ``Claude-Session:`` — all of them
before the rule was written down. Rewriting 333 messages would relocate every
commit on five branches to fix a line nobody reads, so they are grandfathered.
The pin is the boundary, and the boundary is real: the last offender was
authored at 2026-09-13T10:38 and the next commit of any kind at 21:38, so the
cutoff sits in an eleven-hour gap rather than splitting a working session. Every
commit after it was already clean when this landed — a born-green guard, not a
ceiling to work down.

**Author date, not committer date.** A rebase rewrites the committer date to
now, which would sweep a grandfathered commit forward across the cutoff and
fail this test for a commit nobody edited. The author date survives a rebase.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT

#: In the gap between the last trailer (``e4511370``, authored
#: 2026-09-13T10:38:35-07:00) and the next commit (``e0fa3763``, 21:38:26).
CUTOFF = datetime.fromisoformat("2026-09-13T18:00:00-07:00")

#: Matched case-insensitively against the whole message. ``Claude-Session`` is
#: listed separately because a message can carry it without the co-author line.
FORBIDDEN = ("co-authored-by: claude", "claude-session:")

_FIELD = "\x1f"
_RECORD = "\x1e"


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        pytest.skip(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@pytest.fixture(scope="module")
def commits() -> list[tuple[str, datetime, str, str]]:
    """``(sha, author_date, subject, message)`` for every commit behind HEAD.

    Skips where there is no history to read, rather than passing vacuously:

    * the scoped gate dispatches pytest to a Windows runner that syncs
      ``src/``, ``tests/`` and ``scripts/`` into a workspace **inside** a
      separate, stale clone. ``git`` there answers from that outer clone — a
      real repository whose history is months old and has nothing to do with
      the code under test — so "is this a work tree?" is the wrong question.
      The right one is whether the tree git reports *is* this one, which the
      top-level check below asks;
    * CI checks out with ``actions/checkout``'s default ``fetch-depth: 1``, so
      ``git log`` there sees exactly one commit.

    A skip is visible in the run; a vacuous pass is not. Enforcement lives
    where the commits are actually made — the working checkouts, which have
    full history — and in the ``commit-msg`` hook.
    """
    if _git("rev-parse", "--is-inside-work-tree").strip() != "true":
        pytest.skip("not a git work tree")

    toplevel = _git("rev-parse", "--show-toplevel").strip()
    if not toplevel or Path(toplevel).resolve() != ROOT:
        pytest.skip(
            f"git answers for {toplevel!r}, not this tree ({ROOT}) — the "
            f"history behind that repository is not the history of this code."
        )

    if _git("rev-parse", "--is-shallow-repository").strip() == "true":
        pytest.skip(
            "shallow clone — `git log` cannot see the history this guards. "
            "Set fetch-depth: 0 on the checkout to run it here."
        )

    raw = _git("log", f"--format=%H{_FIELD}%aI{_FIELD}%s{_FIELD}%B{_RECORD}", "HEAD")
    out: list[tuple[str, datetime, str, str]] = []
    for record in raw.split(_RECORD):
        record = record.strip("\n")
        if not record:
            continue
        sha, authored, subject, message = record.split(_FIELD, 3)
        out.append((sha, datetime.fromisoformat(authored), subject, message))
    return out


def test_the_cutoff_still_describes_this_history(commits) -> None:
    """The grandfather boundary means nothing if no commit falls after it.

    History here has been rewritten before (2026-09-13), and a rewrite that
    moved every author date forward would leave the test below scanning an
    empty set and reporting success. This is the canary for that.
    """
    after = [c for c in commits if c[1] > CUTOFF]
    assert after, (
        f"no commit is authored after {CUTOFF.isoformat()}, so the guard below "
        f"checks nothing. History was rewritten, or HEAD is detached somewhere "
        f"old — re-derive the cutoff before trusting a green run."
    )


def test_no_attribution_trailer_since_the_cutoff(commits) -> None:
    offenders = [
        (sha[:8], authored.date().isoformat(), subject, trailer)
        for sha, authored, subject, message in commits
        if authored > CUTOFF
        for trailer in FORBIDDEN
        if trailer in message.lower()
    ]
    assert not offenders, (
        "commit messages carry an attribution trailer CLAUDE.md forbids:\n"
        + "\n".join(f"  {s} {d} {subj!r} — {t!r}" for s, d, subj, t in offenders)
        + "\n\nThe message is the only thing wrong; the change itself is fine. "
        "Fix the most recent with `git commit --amend`, or an older one with "
        "an interactive rebase — but check with Ben first if it is already "
        "pushed or already merged into another session's branch."
    )
