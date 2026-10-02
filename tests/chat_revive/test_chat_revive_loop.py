"""Tests for the Chat Revive monitor loop — fires, refuses, never embarrasses.

Economy-loop style: hand-rolled bot/guild/channel stubs, explicit ``now_ts``
injection, a real migrated DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core.background import FailureStreak
from bot_modules.core.db_utils import open_db
from bot_modules.chat_revive import loop as chat_revive_loop
from bot_modules.chat_revive.loop import consider_channel, run_tick
from bot_modules.chat_revive.service import (
    ChannelConfig,
    GuildConfig,
    add_question,
    save_channel_config,
    save_guild_config,
)
from tests.db_template import migrated_db

GID, CID, BOT_ID = 100, 200, 999


def _ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc).timestamp()


NOW = _ts(30, 19)  # 19:00 local (offset 0), a normally-lively evening hour


class FakeMessage(SimpleNamespace):
    pass


def _history_msg(ts: float, author_id: int) -> FakeMessage:
    return FakeMessage(
        author=SimpleNamespace(id=author_id),
        created_at=datetime.fromtimestamp(ts, tz=timezone.utc),
    )


class FakeChannel:
    """Just enough discord.TextChannel for the loop; isinstance-compatible."""

    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self, history_msgs: list[FakeMessage] | None = None) -> None:
        self.id = CID
        self.name = "general"
        self.slowmode_delay = 0
        self._history_msgs = history_msgs or []
        self.send = AsyncMock(return_value=SimpleNamespace(id=777))

    def is_nsfw(self) -> bool:
        return False

    def history(self, limit: int = 1):
        msgs = self._history_msgs[:limit]

        async def _aiter():
            for m in msgs:
                yield m

        return _aiter()


def _bot(channel: FakeChannel) -> SimpleNamespace:
    guild = SimpleNamespace(
        id=GID, get_channel=lambda cid: channel if cid == channel.id else None
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=BOT_ID),
        get_guild=lambda gid: guild if gid == GID else None,
        games_db=None,
        game_busy_checks={},
    )


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A fresh failure streak per test, on a clock the test can move."""
    clock = Clock()
    monkeypatch.setattr(
        chat_revive_loop,
        "_failures",
        FailureStreak(
            "chat revive",
            threshold=chat_revive_loop.MAX_SEND_FAILURES,
            retry_after=chat_revive_loop.PAUSED_PROBE_SECONDS,
            clock=clock,
        ),
    )
    return clock


def _forbidden() -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "no")


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


_mid = iter(range(1, 1_000_000))


def _seed_lively_history(conn, *, days: int = 29, last_gap: float = 3000.0) -> float:
    """A bursty evening channel: a 3-message burst (60s apart) every 30 minutes,
    18:00-21:59 daily, for ``days`` days. Between-burst gaps are ~28m, so band 9
    learns a ~1680s between-conversation lull threshold (vs. the old model's
    ~4-min inter-message trigger). A trailing message is then added ``last_gap``
    seconds before NOW, so the last human message is exactly that far back and
    the ``last_gap`` (3000s) silence clears the ~1680s threshold. Returns the
    last human timestamp."""

    def _ins(ts: float, author_id: int) -> None:
        conn.execute(
            "INSERT INTO processed_messages (guild_id, message_id, channel_id,"
            " user_id, created_at, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (GID, next(_mid), CID, author_id, ts, ts),
        )

    for day in range(1, days + 1):
        for hour in (18, 19, 20, 21):
            for half in (0, 30):
                for i in range(3):
                    _ins(_ts(day, hour, half) + i * 60, 1 + i % 5)
    _ins(NOW - last_gap, 1)
    return NOW - last_gap


def _enable(conn, mode: str = "post", **channel_overrides) -> ChannelConfig:
    save_guild_config(conn, GuildConfig(guild_id=GID, mode=mode, role_id=555))
    cfg = ChannelConfig(guild_id=GID, channel_id=CID, **channel_overrides)
    save_channel_config(conn, cfg)
    add_question(conn, GID, "Spark?", created_by=1, now_ts=NOW - 40 * 86400)
    return cfg


async def test_fires_on_genuine_lull_and_records(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    fired = await consider_channel(_bot(channel), db, cfg, NOW)
    assert fired
    channel.send.assert_awaited_once()
    text = channel.send.await_args.args[0]
    assert "Spark?" in text
    assert channel.send.await_args.kwargs["allowed_mentions"] is not None
    with open_db(db) as conn:
        ev = conn.execute("SELECT * FROM revive_events").fetchone()
        q = conn.execute("SELECT use_count FROM revive_questions").fetchone()
    assert ev["trigger_kind"] == "auto"
    assert ev["message_id"] == 777
    assert ev["pinged"] == 0  # ping_enabled defaults off
    assert q["use_count"] == 1


async def test_pings_when_enabled_and_scarce(db):
    with open_db(db) as conn:
        cfg = _enable(conn, ping_enabled=True)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    assert await consider_channel(_bot(channel), db, cfg, NOW)
    text = channel.send.await_args.args[0]
    assert "<@&555>" in text
    with open_db(db) as conn:
        assert conn.execute("SELECT pinged FROM revive_events").fetchone()[0] == 1


async def test_never_fires_twice_channel_rest(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    bot = _bot(channel)
    assert await consider_channel(bot, db, cfg, NOW)
    # Same tick evaluated again, and a tick 2 minutes later: both refuse.
    assert not await consider_channel(bot, db, cfg, NOW)
    assert not await consider_channel(bot, db, cfg, NOW + 120)
    channel.send.assert_awaited_once()


async def test_aborts_when_history_shows_newer_message(db):
    """Ingest lag: someone spoke seconds ago but the ledger hasn't caught up."""
    with open_db(db) as conn:
        cfg = _enable(conn)
        _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(NOW - 5, author_id=7)])
    assert not await consider_channel(_bot(channel), db, cfg, NOW)
    channel.send.assert_not_awaited()
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM revive_events").fetchone()[0] == 0


async def test_aborts_when_newest_message_is_our_own(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human + 1, author_id=BOT_ID)])
    assert not await consider_channel(_bot(channel), db, cfg, NOW)
    channel.send.assert_not_awaited()


async def test_send_failure_records_nothing(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    channel.send = AsyncMock(
        side_effect=discord.HTTPException(
            SimpleNamespace(status=403, reason="Forbidden"), "no"
        )
    )
    assert not await consider_channel(_bot(channel), db, cfg, NOW)
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM revive_events").fetchone()[0] == 0


@pytest.mark.parametrize(
    "stuck, reason",
    [
        pytest.param("no_question", "no_question", id="no-eligible-question"),
        pytest.param("send", "send_failed", id="send-refused"),
        pytest.param("no_question_spoke", None, id="no-question-but-someone-spoke"),
        pytest.param(None, None, id="a-revive-that-posts-pings-no-staff"),
    ],
)
async def test_an_unfilled_lull_pings_the_staff_revive_role(db, monkeypatch, stuck, reason):
    """docs/specs/staff_pings_spec.md: a confirmed lull the bot can't fill tells
    the moderators who opted in; one it fills tells nobody."""
    from bot_modules.shared import feature_roles as fr
    from bot_modules.moderation.settings import staff_ping_service

    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
        if stuck in ("no_question", "no_question_spoke"):
            conn.execute("DELETE FROM revive_questions")
    # The ledger lags Discord: someone spoke seconds ago, so the lull is over.
    newest = NOW - 5 if stuck == "no_question_spoke" else last_human
    channel = FakeChannel([_history_msg(newest, author_id=1)])
    if stuck == "send":
        channel.send = AsyncMock(side_effect=_forbidden())
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(staff_ping_service, "post_staff_ping", post)
    bot = _bot(channel)
    bot.ctx = SimpleNamespace()

    await consider_channel(bot, db, cfg, NOW)

    if reason is None:
        post.assert_not_awaited()
        return
    post.assert_awaited_once()
    _ctx, _guild, entry, text = post.await_args.args
    assert entry is fr.STAFF_REVIVE_PING
    assert text == staff_ping_service.revive_stuck_text(reason, CID)


@pytest.mark.parametrize(
    "posted, newest_offset, empty_bank, handed",
    [
        pytest.param(True, None, False, True, id="lull-pings-staff"),
        pytest.param(True, None, True, True, id="bank-empty-still-pings"),
        pytest.param(False, None, False, False, id="ping-had-nowhere-to-go"),
        pytest.param(True, -5, False, False, id="someone-just-spoke"),
    ],
)
async def test_ping_moderators_mode_hands_the_lull_to_staff(
    db, monkeypatch, posted, newest_offset, empty_bank, handed
):
    """docs/specs/chat_revive_spec.md § What a lull does: in ping-moderators mode the
    same confirmed lull posts nothing and pings @Staff · Chat Revive on its own
    cooldown lane; it is recorded only when the ping went out, and then
    rests the channel exactly as a posted revive would."""
    from bot_modules.shared import feature_roles as fr
    from bot_modules.moderation.settings import staff_ping_service

    with open_db(db) as conn:
        cfg = _enable(conn, mode="ping_staff", ping_enabled=True)
        last_human = _seed_lively_history(conn)
        if empty_bank:
            conn.execute("DELETE FROM revive_questions")
    newest = NOW + newest_offset if newest_offset is not None else last_human
    channel = FakeChannel([_history_msg(newest, author_id=1)])
    post = AsyncMock(return_value=posted)
    monkeypatch.setattr(staff_ping_service, "post_staff_ping", post)
    bot = _bot(channel)
    bot.ctx = SimpleNamespace()

    assert await consider_channel(bot, db, cfg, NOW) is handed

    channel.send.assert_not_awaited()
    if newest_offset is not None:
        post.assert_not_awaited()
    else:
        post.assert_awaited_once()
        _ctx, _guild, entry, text = post.await_args.args
        assert entry is fr.STAFF_REVIVE_PING
        assert text == staff_ping_service.revive_lull_text(CID)
        assert post.await_args.kwargs == {"lane": staff_ping_service.REVIVE_LULL_LANE}
    with open_db(db) as conn:
        events = conn.execute("SELECT * FROM revive_events").fetchall()
        used = conn.execute("SELECT COALESCE(SUM(use_count), 0) FROM revive_questions").fetchone()[0]
    assert used == 0
    if not handed:
        assert events == []
        return
    [ev] = events
    assert ev["trigger_kind"] == "staff"
    assert ev["question_id"] is None and ev["message_id"] is None
    assert ev["pinged"] == 0  # the member role is never tagged in this mode
    # The rest period holds the next ping back, as it would a post.
    post.reset_mock()
    assert not await consider_channel(bot, db, cfg, NOW + 120)
    post.assert_not_awaited()


async def test_ping_moderators_mode_pauses_an_unreadable_channel(db, monkeypatch):
    from bot_modules.moderation.settings import staff_ping_service

    with open_db(db) as conn:
        cfg = _enable(conn, mode="ping_staff")
        _seed_lively_history(conn)
    channel = FakeChannel()

    def _history(limit: int = 1):
        raise _forbidden()

    channel.history = _history  # type: ignore[method-assign]
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(staff_ping_service, "post_staff_ping", post)
    bot = _bot(channel)
    bot.ctx = SimpleNamespace()
    for _ in range(chat_revive_loop.MAX_SEND_FAILURES):
        assert not await consider_channel(bot, db, cfg, NOW)
    assert chat_revive_loop._failures.failing() == {(GID, CID)}
    post.assert_not_awaited()


@pytest.mark.parametrize("broken", ["send", "history"])
async def test_a_channel_that_keeps_failing_is_paused_then_probed(db, clock, broken):
    """An unpostable channel stays quiet, so the lull never ends — without a
    ceiling it was retried every tick, for good."""
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    if broken == "send":
        channel.send = AsyncMock(side_effect=_forbidden())
    else:
        def _history(limit: int = 1):
            raise _forbidden()

        channel.history = _history  # type: ignore[method-assign]
    bot = _bot(channel)
    for _ in range(chat_revive_loop.MAX_SEND_FAILURES):
        assert not await consider_channel(bot, db, cfg, NOW)
    assert chat_revive_loop._failures.failing() == {(GID, CID)}

    # Paused: nothing is even attempted.
    channel.send = AsyncMock(return_value=SimpleNamespace(id=777))
    channel.history = FakeChannel([_history_msg(last_human, author_id=1)]).history  # type: ignore[method-assign]
    assert not await consider_channel(bot, db, cfg, NOW)
    channel.send.assert_not_awaited()

    # An hour on, the probe goes through, works, and ends the pause.
    clock.now += chat_revive_loop.PAUSED_PROBE_SECONDS
    assert await consider_channel(bot, db, cfg, NOW)
    assert chat_revive_loop._failures.failing() == set()


async def test_refuses_when_guild_disabled(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        save_guild_config(conn, GuildConfig(guild_id=GID, mode="off"))
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    assert not await consider_channel(_bot(channel), db, cfg, NOW)


async def test_fires_even_when_game_busy(db):
    """The active-game busy check is disabled for now (silently blocked a
    genuine lull with no diagnostic trace) — decide from silence history alone."""
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    bot = _bot(channel)

    async def busy_check(channel_id: int) -> bool:
        return channel_id == CID

    bot.game_busy_checks = {"risky_roll": busy_check}
    assert await consider_channel(bot, db, cfg, NOW)


async def test_run_tick_measures_follow_up(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    bot = _bot(channel)
    assert await consider_channel(bot, db, cfg, NOW)
    # Three people answer within the half hour.
    with open_db(db) as conn:
        for uid, dt in [(11, 60), (12, 120), (13, 500)]:
            conn.execute(
                "INSERT INTO processed_messages (guild_id, message_id, channel_id,"
                " user_id, created_at, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (GID, next(_mid), CID, uid, NOW + dt, NOW + dt),
            )
    await run_tick(bot, db, NOW + 1900)
    with open_db(db) as conn:
        ev = conn.execute("SELECT * FROM revive_events").fetchone()
    assert ev["success"] == 1
    assert ev["follow_msgs"] == 3
    assert ev["follow_authors"] == 3


async def test_run_tick_isolates_channel_errors(db):
    """A channel the bot can't resolve doesn't break the sweep for others."""
    with open_db(db) as conn:
        _enable(conn)
        save_channel_config(
            conn, ChannelConfig(guild_id=GID, channel_id=CID + 1)  # unresolvable
        )
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    await run_tick(_bot(channel), db, NOW)
    channel.send.assert_awaited_once()
