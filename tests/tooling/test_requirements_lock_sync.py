"""A dependency added to pyproject.toml but never compiled into its lock.

pyproject.toml holds the human-edited direct dependencies — `[project]
dependencies` for the runtime set, `[dependency-groups]` for dev, mcp and
local-llm. requirements/*.lock is what CI, the remote runner and prod actually
install. Editing a list without re-running `uv pip compile` means the new
package is real in your head, absent from every machine, and the failure
surfaces as an ImportError on the box you least want it on.

Only the direction that breaks prod is checked: every direct dependency must
appear in its lock. The locks legitimately hold far more (transitive pins).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

_ROOT = REPO_ROOT
_PYPROJECT = _ROOT / "pyproject.toml"

# lock -> the pyproject lists it must pin. dev.lock is compiled from the
# project *plus* its group, so it must carry the runtime set too; mcp and
# local-llm are compiled from their group alone.
LOCKS = {
    "requirements/base.lock": ("project",),
    "requirements/dev.lock": ("project", "dev"),
    "requirements/mcp.lock": ("mcp",),
    "requirements/local-llm.lock": ("local-llm",),
}


def _canon(name: str) -> str:
    """PEP 503 normalisation — `discord.py` and `discord-py` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _name(spec: str) -> str:
    """`uvicorn[standard]>=0.29.0 ; marker` -> `uvicorn`."""
    return _canon(re.split(r"[<>=!~\[;\s]", spec.strip(), maxsplit=1)[0])


def _declared(pyproject: dict, source: str) -> set[str]:
    if source == "project":
        specs = pyproject["project"]["dependencies"]
    else:
        specs = pyproject["dependency-groups"][source]
    return {_name(s) for s in specs if isinstance(s, str)}


def _locked(path: Path) -> set[str]:
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(_name(line))
    return out


def _load() -> dict:
    if not _PYPROJECT.exists():
        pytest.skip("pyproject.toml is not synced here")
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("lock", "sources"), LOCKS.items(), ids=list(LOCKS))
def test_every_direct_dependency_is_in_its_lock(lock, sources):
    pyproject = _load()
    lock_path = _ROOT / lock
    if not lock_path.exists():
        pytest.skip(f"{lock} is not synced to this runner")
    wanted = set().union(*(_declared(pyproject, s) for s in sources))
    missing = sorted(wanted - _locked(lock_path))
    assert not missing, (
        f"pyproject.toml names {missing} but {lock} does not pin them — "
        f"recompile it (the commands are in pyproject.toml's [project] comment)"
    )


def test_the_mcp_lock_carries_none_of_the_bot():
    """The MCP server runs in its own venv at /opt/dk-mcp and must not drag the
    bot's dependencies in: its lock is compiled from the mcp group alone. A
    compile that included the project would pin discord.py there."""
    lock_path = _ROOT / "requirements/mcp.lock"
    if not lock_path.exists():
        pytest.skip("requirements/mcp.lock is not synced to this runner")
    # Some runtime deps (httpx, pydantic…) are legitimately shared through mcp
    # itself, so only unmistakably bot-side packages are checked.
    leaked = {"discord-py", "wavelink", "mediapipe", "fastapi"} & _locked(lock_path)
    assert not leaked, f"requirements/mcp.lock pins bot dependencies: {sorted(leaked)}"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("discord.py>=2.6.0", "discord-py"),
        ("uvicorn[standard]>=0.29.0", "uvicorn"),
        ("huggingface_hub>=0.24.0", "huggingface-hub"),
        ("nudenet", "nudenet"),
        ("colorama==0.4.6 ; sys_platform == 'win32'", "colorama"),
    ],
)
def test_the_parser_handles_the_shapes_these_files_actually_use(spec, expected):
    """Guards the normalisation, not the repo: a parser that returned empty
    sets would make the check above pass on a genuinely broken lock."""
    assert _name(spec) == expected
