"""Where the bot's runtime data lives on disk.

The bot and its scripts write eight directories of runtime data beside the
database: DB snapshots (``backups/``), GGUF weights (``models/``), the Guess
originals cache (``guess_cache/``) and five per-guild upload stores
(``econ_icon_catalog/``, ``econ_role_icons/``, ``econ_emoji/``,
``quote_borders/``, ``swatches/``). They are moving under one ``var/``
directory so the repo root holds source and config only.

The move itself is an operator step taken with the bot stopped
(``docs/disaster_recovery_runbook.md`` § Where everything is), not something
a merge does, so every writer resolves its directory through
:func:`data_dir`, which follows whichever layout is on disk at the time.
"""

from __future__ import annotations

from pathlib import Path

#: The directory under the DB's parent that gathers every runtime data dir.
VAR_DIR = "var"


def data_dir(name: str, *, base: Path) -> Path:
    """The directory holding runtime data ``name`` for the checkout at ``base``.

    ``base`` is the database's parent directory — the repo root in production
    (where the DB path is the relative ``dungeonkeeper.db``, so the result
    stays relative too) and a tmp directory in tests.

    Returns ``base/var/name`` when ``base/var`` exists, else ``base/name``.
    The fallback is a **deliberate anomaly**: the old flat layout is what the
    production checkout has until the operator creates ``var/`` and moves the
    directories into it in one press, and this helper must keep every writer
    on the old paths until that moment. Nothing creates ``var/`` by itself.
    ``tests/core/test_paths.py`` fails if the fallback is removed.
    """
    var = base / VAR_DIR
    if var.is_dir():
        return var / name
    return base / name
