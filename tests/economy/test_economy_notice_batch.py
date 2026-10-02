"""Batched economy notes: what is queued, and the one DM a batch becomes."""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.dm.batch.logic import PendingDM
from bot_modules.dm.batch.store import claim_due
from bot_modules.economy.notice_batch import (
    MAX_LINES,
    NOTICE_BATCH_KIND,
    queue_notice,
    render_notices,
)


def _row(i: int, line: str, *, actor: int = 0, named: bool = True) -> PendingDM:
    return PendingDM(
        id=i, kind=NOTICE_BATCH_KIND, guild_id=1, user_id=9, group_key=0,
        actor_id=actor, ref_id=0, payload={"line": line, "named": int(named)},
        created_at=0,
    )


def _names(uid: int) -> str:
    return {5: "Ann", 6: "@everyone"}.get(uid, f"User {uid}")


def test_notes_for_one_recipient_share_a_batch(sync_db_path):
    with open_db(sync_db_path) as conn:
        queue_notice(conn, guild_id=1, recipient_id=9, line="sent you 5 coins.", actor_id=5)
        queue_notice(conn, guild_id=1, recipient_id=9, line="gifted you **Color**!", actor_id=6)
        queue_notice(
            conn, guild_id=1, recipient_id=9, line="A bounty was cancelled.",
            actor_id=7, named=False,
        )
        queue_notice(conn, guild_id=1, recipient_id=8, line="sent you 1 coin.", actor_id=5)
        batches = claim_due(conn, now=10**12)
    by_recipient = {b[0].user_id: b for b in batches}
    assert sorted(by_recipient) == [8, 9]
    assert [r.actor_id for r in by_recipient[9]] == [5, 6, 7]
    assert {r.kind for r in by_recipient[9]} == {NOTICE_BATCH_KIND}


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param(
            [_row(1, "sent you 🪙 5 coins.", actor=5)],
            "Ann sent you 🪙 5 coins.",
            id="one-note-reads-as-the-old-dm",
        ),
        pytest.param(
            [_row(1, "sent you 🪙 5 coins.", actor=5),
             _row(2, "A bounty you chipped into was cancelled.", actor=7, named=False)],
            "Ann sent you 🪙 5 coins.\nA bounty you chipped into was cancelled.",
            id="unnamed-note-never-names-its-actor",
        ),
        pytest.param(
            [_row(1, "sent you 🪙 5 coins.", actor=6)],
            "@​everyone sent you 🪙 5 coins.",
            id="a-name-cannot-ping-through-the-bank-fallback",
        ),
        pytest.param([_row(1, "")], "", id="empty-line-dropped"),
    ],
)
def test_render_notices(rows, expected):
    assert render_notices(rows, _names) == expected


def test_render_notices_caps_a_long_batch():
    rows = [_row(i, f"sent you {i} coins.", actor=5) for i in range(MAX_LINES + 3)]
    lines = render_notices(rows, _names).splitlines()
    assert len(lines) == MAX_LINES
    assert lines[-1] == "…and 4 more."
