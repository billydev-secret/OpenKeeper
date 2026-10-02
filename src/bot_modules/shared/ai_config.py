"""Runtime configuration for AI commands (system prompts).

Stores the system prompts in the existing ``config`` table under well-known
keys. Each AI prompt has a hardcoded default which is returned when no override
has been written, keeping fresh installations working out of the box.

There is deliberately no *model* setting here. Exactly one local model is
served at a time, chosen by the model-source settings (``llm_model_path`` /
``llm_hf_repo`` / ``llm_hf_file``) on the same panel and loaded at startup;
``ollama_client.chat`` takes no model argument, so a per-command model dial
would be a preference nothing enforces.

Prompts are **bot-wide**: the dashboard panel is primary-guild-only and writes
them at ``guild_id=0``, which every guild's readers resolve through
``get_config_value``'s legacy fallback.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import (
    delete_config_value,
    get_config_value,
    open_db,
    set_config_value as _db_set_config_value,
)

# ── Prompt registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptInfo:
    key: str
    label: str
    description: str
    default_factory: Callable[[], str]


def _default_watch_check() -> str:
    from bot_modules.moderation.ai_service import _WATCH_CHECK_SYSTEM
    return _WATCH_CHECK_SYSTEM


def _default_wellness() -> str:
    from bot_modules.wellness.ai import _ENCOURAGEMENT_SYSTEM
    return _ENCOURAGEMENT_SYSTEM


_PROMPTS: list[PromptInfo] = [
    PromptInfo(
        key="ai_prompt_watch_check",
        label="Watch — live rule check",
        description="System prompt for the /watch relay (single-message rule check).",
        default_factory=_default_watch_check,
    ),
    PromptInfo(
        key="ai_prompt_wellness_encouragement",
        label="Wellness encouragement",
        description="System prompt for the weekly wellness encouragement note.",
        default_factory=_default_wellness,
    ),
]

_PROMPTS_BY_KEY: dict[str, PromptInfo] = {p.key: p for p in _PROMPTS}


def list_prompts() -> list[PromptInfo]:
    return list(_PROMPTS)


# ── Read helpers ───────────────────────────────────────────────────────


def get_prompt(conn: sqlite3.Connection, key: str, guild_id: int = 0) -> str:
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "", guild_id)
    return raw if raw else info.default_factory()


def get_prompt_with_source(
    conn: sqlite3.Connection, key: str, guild_id: int = 0
) -> tuple[str, bool]:
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "", guild_id)
    if raw:
        return raw, True
    return info.default_factory(), False


def get_prompt_from_path(db_path: Path, key: str, guild_id: int = 0) -> str:
    try:
        with open_db(db_path) as conn:
            return get_prompt(conn, key, guild_id)
    except (sqlite3.Error, OSError, KeyError):
        info = _PROMPTS_BY_KEY.get(key)
        return info.default_factory() if info else ""


# ── Write helpers ──────────────────────────────────────────────────────


def set_config(conn: sqlite3.Connection, key: str, value: str, guild_id: int = 0) -> None:
    _db_set_config_value(conn, key, value, guild_id)


def set_prompt(conn: sqlite3.Connection, key: str, value: str, guild_id: int = 0) -> None:
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    set_config(conn, key, value, guild_id)


def reset_prompt(conn: sqlite3.Connection, key: str, guild_id: int = 0) -> None:
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    delete_config_value(conn, key, guild_id)
