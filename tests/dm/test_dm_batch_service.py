"""Batched DMs — the queue, the send-time no-contact recheck and the flush."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.dm.batch import service
from bot_modules.dm.batch.logic import WINDOW_SECONDS
from bot_modules.dm.batch.store import (
    claim_due,
    drop_group,
    enqueue,
    pending_count,
    retract_refs,
)
from bot_modules.no_contact.service import add_pair

GUILD, STARTER, REPLIER, OTHER = 5, 100, 200, 300
T0 = 1_000_000


def q(conn, *, actor=REPLIER, ref=1, group=10, user=STARTER, at=T0, kind="k"):
    enqueue(
        conn, kind=kind, guild_id=GUILD, user_id=user, group_key=group,
        actor_id=actor, ref_id=ref, payload={"channel_id": 7}, now=at,
    )


def test_a_batch_inside_its_window_is_left_whole(sync_db_path):
    with open_db(sync_db_path) as conn:
        q(conn, at=T0)
        assert claim_due(conn, now=T0 + WINDOW_SECONDS - 1) == []
        assert pending_count(conn) == 1


def test_a_due_batch_takes_its_later_rows_and_is_deleted(sync_db_path):
    with open_db(sync_db_path) as conn:
        q(conn, ref=1, at=T0)
        q(conn, ref=2, actor=OTHER, at=T0 + WINDOW_SECONDS - 5)  # joined late
        q(conn, ref=3, group=11, at=T0 + 60)  # another thread, not yet due
        batches = claim_due(conn, now=T0 + WINDOW_SECONDS)
        assert [[r.ref_id for r in b] for b in batches] == [[1, 2]]
        assert batches[0][0].payload == {"channel_id": 7}
        assert pending_count(conn) == 1
        # Claimed means gone: the next pass never sends it twice.
        assert claim_due(conn, now=T0 + WINDOW_SECONDS) == []


def test_a_deleted_message_retracts_only_its_row(sync_db_path):
    with open_db(sync_db_path) as conn:
        q(conn, ref=1)
        q(conn, ref=2)
        assert retract_refs(conn, GUILD, [1, 0]) == 1
        batches = claim_due(conn, now=T0 + WINDOW_SECONDS)
    assert [r.ref_id for r in batches[0]] == [2]


def test_drop_group_clears_a_thread_for_every_recipient(sync_db_path):
    with open_db(sync_db_path) as conn:
        q(conn, group=10)
        q(conn, group=10, user=OTHER, actor=REPLIER)
        q(conn, group=11)
        assert drop_group(conn, kind="k", guild_id=GUILD, group_key=10) == 2
        assert pending_count(conn) == 1


@pytest.mark.parametrize("created_by", [STARTER, REPLIER])
def test_a_pair_added_while_waiting_drops_that_actor_at_send(sync_db_path, created_by):
    with open_db(sync_db_path) as conn:
        q(conn, ref=1, actor=REPLIER)
        q(conn, ref=2, actor=OTHER)
    add_pair(sync_db_path, GUILD, STARTER, REPLIER, created_by=created_by)
    with open_db(sync_db_path) as conn:
        batches = service.take_due(conn, now=T0 + WINDOW_SECONDS)
    assert [[r.actor_id for r in b] for b in batches] == [[OTHER]]


def test_a_batch_emptied_by_no_contact_sends_nothing(sync_db_path):
    with open_db(sync_db_path) as conn:
        q(conn, actor=REPLIER)
    add_pair(sync_db_path, GUILD, STARTER, REPLIER, created_by=STARTER)
    with open_db(sync_db_path) as conn:
        assert service.take_due(conn, now=T0 + WINDOW_SECONDS) == []
        assert pending_count(conn) == 0



async def _noop_handler(*_a):
    return None


def test_a_kind_registered_without_recheck_keeps_its_named_rows(sync_db_path):
    """Whisper's actor is the member a guess named, not a sender: its handler
    degrades that name itself, so take_due must not drop the row. A kind that
    does re-check, flushed in the same pass, still loses its blocked row."""
    service.register_handler("named", _noop_handler, recheck_actor=False)
    try:
        with open_db(sync_db_path) as conn:
            q(conn, kind="named", actor=REPLIER, ref=1)
            q(conn, kind="k", actor=REPLIER, ref=2)
        add_pair(sync_db_path, GUILD, STARTER, REPLIER, created_by=STARTER)
        with open_db(sync_db_path) as conn:
            batches = service.take_due(conn, now=T0 + WINDOW_SECONDS)
        assert [[r.kind for r in b] for b in batches] == [["named"]]
    finally:
        service.unregister_handler("named")
    with open_db(sync_db_path) as conn:
        q(conn, kind="named", actor=REPLIER, ref=3)
        # Unregistered, the kind is back to the default re-check.
        assert service.take_due(conn, now=T0 + WINDOW_SECONDS) == []

def _bot(db_path, *, member_present=True):
    member = SimpleNamespace(id=STARTER)
    guild = MagicMock()
    guild.get_member.return_value = member if member_present else None
    bot = MagicMock()
    bot.ctx.open_db = lambda: open_db(db_path)
    bot.get_guild.return_value = guild
    return bot, guild, member


@pytest.fixture
def handler():
    h = AsyncMock()
    service.register_handler("k", h)
    yield h
    service.unregister_handler("k")


async def test_flush_hands_each_due_batch_to_its_kind(sync_db_path, handler):
    with open_db(sync_db_path) as conn:
        q(conn, ref=1)
        q(conn, ref=2)
    bot, guild, member = _bot(sync_db_path)
    assert await service.flush_due(bot, now=T0 + WINDOW_SECONDS) == 1
    args = handler.await_args.args
    assert args[1] is guild and args[2] is member
    assert [r.ref_id for r in args[3]] == [1, 2]


async def test_flush_skips_a_recipient_who_left(sync_db_path, handler):
    with open_db(sync_db_path) as conn:
        q(conn)
    bot, _, _ = _bot(sync_db_path, member_present=False)
    assert await service.flush_due(bot, now=T0 + WINDOW_SECONDS) == 0
    handler.assert_not_awaited()


async def test_flush_drops_a_kind_with_no_handler(sync_db_path):
    with open_db(sync_db_path) as conn:
        q(conn, kind="unregistered")
    bot, _, _ = _bot(sync_db_path)
    assert await service.flush_due(bot, now=T0 + WINDOW_SECONDS) == 0
    with open_db(sync_db_path) as conn:
        assert pending_count(conn) == 0


async def test_one_failing_handler_does_not_stop_the_rest(sync_db_path, handler):
    handler.side_effect = [RuntimeError("boom"), None]
    with open_db(sync_db_path) as conn:
        q(conn, group=10)
        q(conn, group=11)
    bot, _, _ = _bot(sync_db_path)
    assert await service.flush_due(bot, now=T0 + WINDOW_SECONDS) == 1
    assert handler.await_count == 2
