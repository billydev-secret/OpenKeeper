"""``bot_modules.core.paths`` — runtime data dirs follow the on-disk layout.

The ``var/`` layout is adopted the instant ``base/var`` exists and not a
moment sooner: the production checkout runs on the flat layout until the
operator moves the directories (disaster_recovery_runbook.md § Where
everything is), so the fallback to ``base/name`` is load-bearing. The
``flat`` rows below are the ones that fail if it is removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot_modules.core.paths import VAR_DIR, data_dir


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        pytest.param("flat", Path("backups"), id="flat-layout-keeps-the-old-path"),
        pytest.param("var", Path("var") / "backups", id="var-layout-moves-under-var"),
        pytest.param(
            "var-is-a-file", Path("backups"), id="a-stray-var-file-is-not-the-layout"
        ),
    ],
)
def test_data_dir_follows_the_layout_on_disk(
    tmp_path: Path, layout: str, expected: Path
) -> None:
    if layout == "var":
        (tmp_path / VAR_DIR).mkdir()
    elif layout == "var-is-a-file":
        (tmp_path / VAR_DIR).write_text("", encoding="utf-8")
    assert data_dir("backups", base=tmp_path) == tmp_path / expected


def test_data_dir_never_creates_var(tmp_path: Path) -> None:
    """The move is the operator's press; the helper only observes it."""
    data_dir("guess_cache", base=tmp_path)
    assert not (tmp_path / VAR_DIR).exists()
    assert not (tmp_path / "guess_cache").exists()


def test_data_dir_keeps_a_relative_base_relative(tmp_path: Path, monkeypatch) -> None:
    """Production's DB path is the relative ``dungeonkeeper.db``, so ``base`` is
    ``.`` and the paths the bot records in the DB stay relative — both layouts."""
    monkeypatch.chdir(tmp_path)
    assert data_dir("backups", base=Path(".")) == Path("backups")
    (tmp_path / VAR_DIR).mkdir()
    assert data_dir("backups", base=Path(".")) == Path("var") / "backups"
