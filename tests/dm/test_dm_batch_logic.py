"""Batched DMs — the pure decisions (grouping, the window, the name list)."""
from __future__ import annotations

import pytest

from bot_modules.dm.batch.logic import (
    PendingDM,
    decode_payload,
    drop_blocked,
    encode_payload,
    group_batches,
    join_names,
    unique_actors,
)


def row(id_: int, *, user=1, group=10, actor=100, created=0, kind="k", guild=5) -> PendingDM:
    return PendingDM(
        id=id_, kind=kind, guild_id=guild, user_id=user, group_key=group,
        actor_id=actor, ref_id=1000 + id_, payload={}, created_at=created,
    )


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        pytest.param([], "Someone", id="none"),
        pytest.param(["A"], "**A**", id="one"),
        pytest.param(["A", "B"], "**A** and **B**", id="two"),
        pytest.param(["A", "B", "C"], "**A**, **B** and **C**", id="three-all-named"),
        pytest.param(["A", "B", "C", "D", "E"], "**A**, **B** and 3 others", id="five"),
    ],
)
def test_join_names(names, expected):
    assert join_names(names) == expected


def test_group_batches_splits_by_recipient_kind_and_group():
    rows = [
        row(1, created=5),
        row(2, created=1, user=2),
        row(3, created=9),
        row(4, created=3, group=11),
        row(5, created=4, kind="other"),
    ]
    batches = group_batches(rows)
    assert [[r.id for r in b] for b in batches] == [[2], [4], [5], [1, 3]]


def test_drop_blocked_keeps_actorless_rows_and_other_actors():
    rows = [row(1, actor=100), row(2, actor=200), row(3, actor=0)]
    kept = drop_blocked(rows, {(5, 1, 100)})
    assert [r.id for r in kept] == [2, 3]


def test_unique_actors_in_first_seen_order():
    rows = [row(1, actor=200), row(2, actor=100), row(3, actor=200), row(4, actor=0)]
    assert unique_actors(rows) == [200, 100]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(encode_payload({"channel_id": 7}), {"channel_id": 7}, id="round-trip"),
        pytest.param("", {}, id="empty"),
        pytest.param("not json", {}, id="garbage"),
        pytest.param("[1, 2]", {}, id="not-an-object"),
    ],
)
def test_decode_payload(raw, expected):
    assert decode_payload(raw) == expected
