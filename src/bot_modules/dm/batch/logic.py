"""Pure decisions for batched DMs.

A notification that can wait ten minutes is queued instead of sent. Queued
rows that share a *batch key* — the recipient, the feature (``kind``) and the
thing it is about (``group_key``, e.g. a thread) — go out together as one DM
once the oldest of them has waited the full window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: How long the first notification in a batch waits for company.
WINDOW_SECONDS = 600

#: Names spelled out in a batch summary before the rest become "N others".
MAX_NAMED = 3


@dataclass(frozen=True)
class PendingDM:
    """One queued notification — one row of ``dm_batch_pending``."""

    id: int
    kind: str
    guild_id: int
    user_id: int  # the recipient
    group_key: int
    actor_id: int  # the member whose action it reports; 0 = none
    ref_id: int  # the message it is about; 0 = none
    payload: dict[str, Any]
    created_at: int

    @property
    def batch_key(self) -> tuple[str, int, int, int]:
        return (self.kind, self.guild_id, self.user_id, self.group_key)


def encode_payload(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)


def decode_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def group_batches(rows: list[PendingDM]) -> list[list[PendingDM]]:
    """Rows grouped by batch key, each batch oldest first, batches in the order
    their oldest row arrived."""
    batches: dict[tuple[str, int, int, int], list[PendingDM]] = {}
    for row in sorted(rows, key=lambda r: (r.created_at, r.id)):
        batches.setdefault(row.batch_key, []).append(row)
    return list(batches.values())


def drop_blocked(
    rows: list[PendingDM], blocked: set[tuple[int, int, int]]
) -> list[PendingDM]:
    """Rows whose actor is (now) on the recipient's no-contact list are dropped.

    ``blocked`` holds ``(guild_id, recipient, actor)`` triples that are
    no-contact. Checked again at send time because a pair can be added while a
    row waits; the enqueueing feature has already checked once.
    """
    return [
        r for r in rows
        if not r.actor_id or (r.guild_id, r.user_id, r.actor_id) not in blocked
    ]


def unique_actors(rows: list[PendingDM]) -> list[int]:
    """The batch's actors, each once, in the order they first acted."""
    seen: dict[int, None] = {}
    for r in rows:
        if r.actor_id:
            seen.setdefault(r.actor_id, None)
    return list(seen)


def join_names(names: list[str], max_named: int = MAX_NAMED) -> str:
    """``**A**``, ``**A** and **B**``, ``**A**, **B** and **C**``, or
    ``**A**, **B** and 3 others``. Names arrive already escaped."""
    bold = [f"**{n}**" for n in names]
    if not bold:
        return "Someone"
    if len(bold) == 1:
        return bold[0]
    if len(bold) <= max_named:
        return f"{', '.join(bold[:-1])} and {bold[-1]}"
    shown = bold[: max_named - 1]
    rest = len(bold) - len(shown)
    return f"{', '.join(shown)} and {rest} others"
