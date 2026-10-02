"""The repo root, for tests that read source, docs or scripts by path.

A test that works it out itself (`Path(__file__).resolve().parents[1]`) is
right only while it sits directly in `tests/`; moving it into a feature folder
silently points it one level too high, and a scan of a directory that isn't
there usually finds nothing and passes. This module never moves, so importing
the constant keeps a test's paths correct wherever the test lives.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
