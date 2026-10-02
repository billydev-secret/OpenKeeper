"""The commit-msg hook that rejects attribution trailers.

Its partner, ``test_commit_trailer_contract.py``, reads history and therefore
skips wherever history is absent — the remote runner has no ``.git`` and CI
checks out shallow. This file has no such gap: it feeds the checker strings
directly, so the rule stays covered in every tier.
"""

from __future__ import annotations

import sys

import pytest
from tests.paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_commit_msg import main, offending_lines  # noqa: E402

CLEAN = """Scope: a descriptive summary

Why this change was needed, and the edge case it handles.
"""


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>",
        "Claude-Session: https://claude.ai/code/session_01Fztk",
        "co-authored-by: claude sonnet 5 <noreply@anthropic.com>",
        "  Claude-Session: https://example.invalid",
    ],
)
def test_a_trailer_is_caught(trailer: str) -> None:
    assert offending_lines(CLEAN + "\n" + trailer)


def test_a_clean_message_passes() -> None:
    assert offending_lines(CLEAN) == []


def test_both_trailers_are_reported_not_just_the_first() -> None:
    """A message usually carries the pair, and fixing one at a time is a loop."""
    message = (
        CLEAN
        + "\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
        + "\nClaude-Session: https://claude.ai/code/session_01Fztk"
    )
    assert len(offending_lines(message)) == 2


def test_prose_about_the_rule_is_not_a_trailer() -> None:
    """This repo's own commits explain the guard; they must not trip it.

    The commit that added this checker has to name what it forbids, and a body
    quoting ``Co-Authored-By`` mid-sentence is not a trailer. Matching anywhere
    in the line instead of at its start would make the rule unable to describe
    itself.
    """
    message = (
        "Standards: reject attribution trailers at commit time\n"
        "\n"
        "A `Co-Authored-By: Claude` line names someone who cannot be reached,\n"
        "and `Claude-Session:` publishes a URL only its author can open.\n"
    )
    assert offending_lines(message) == []


def test_a_commented_line_is_ignored() -> None:
    """git strips ``#`` lines before the message is stored.

    ``git commit -v`` puts the whole staged diff below such a comment, so a
    commit that *adds* this checker carries its own FORBIDDEN list down there.
    Reading those would make the hook reject the commit that installs it.
    """
    assert offending_lines(CLEAN + "\n# Co-Authored-By: Claude <x@y.z>\n") == []


def test_main_returns_one_and_names_the_line(tmp_path, capsys) -> None:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(CLEAN + "\nClaude-Session: https://example.invalid\n", encoding="utf-8")

    assert main(["check_commit_msg.py", str(path)]) == 1
    err = capsys.readouterr().err
    assert "Claude-Session" in err
    assert "line 5" in err


def test_main_returns_zero_on_a_clean_message(tmp_path) -> None:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(CLEAN, encoding="utf-8")
    assert main(["check_commit_msg.py", str(path)]) == 0


def test_main_reports_usage_rather_than_crashing(tmp_path, capsys) -> None:
    """Exit 2, not 1: a broken invocation is not a rejected message.

    A hook that cannot run must not read as "this commit is fine" either, so
    the code is non-zero — but distinct, so a misconfigured hook is
    distinguishable from a caught trailer in the output.
    """
    assert main(["check_commit_msg.py"]) == 2
    assert "usage" in capsys.readouterr().err

    missing = tmp_path / "nope"
    assert main(["check_commit_msg.py", str(missing)]) == 2
