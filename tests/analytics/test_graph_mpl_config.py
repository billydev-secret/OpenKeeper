"""matplotlib's config dir must land inside the repo, not under $HOME.

The unit runs ProtectHome=read-only, so the default ~/.config/matplotlib is
unwritable: matplotlib warns and rebuilds its font cache into a fresh temp dir
on every boot. The graph module redirects MPLCONFIGDIR before importing
matplotlib.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT



@pytest.mark.parametrize(
    "module",
    [
        "bot_modules.analytics.activity_graphs",
    ],
)
def test_importing_a_graph_module_redirects_mplconfigdir(module):
    __import__(module)

    raw = os.environ.get("MPLCONFIGDIR", "")
    assert raw, "MPLCONFIGDIR was not set by importing a graph module"

    configured = Path(raw).resolve()
    assert configured == (REPO_ROOT / ".cache" / "matplotlib").resolve()


def test_mplconfigdir_is_not_under_home():
    """The whole point: $HOME is read-only in production."""
    configured = Path(os.environ["MPLCONFIGDIR"]).resolve()
    home = Path.home().resolve()

    assert not configured.is_relative_to(home / ".config"), (
        f"MPLCONFIGDIR resolved under the read-only home config dir: {configured}"
    )


def test_configured_dir_is_writable():
    """matplotlib creates the dir itself, but the parent must be writable."""
    configured = Path(os.environ["MPLCONFIGDIR"])
    configured.mkdir(parents=True, exist_ok=True)

    probe = configured / ".write-probe"
    probe.write_text("ok", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "ok"
    probe.unlink()
