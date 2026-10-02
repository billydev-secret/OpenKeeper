"""staff_ping_service — the one answer to "is this staff ping's delivery set up"."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.shared import feature_roles as fr
from bot_modules.moderation.settings import staff_ping_service as svc
from bot_modules.xp.promotion_review_service import CHANNEL_KEY

GUILD = 42


def test_nothing_is_delivered_on_a_fresh_guild(sync_db_path):
    with open_db(sync_db_path) as conn:
        assert svc.delivered_keys(conn, GUILD) == set()


def test_promotion_reviews_deliver_once_the_review_channel_is_set(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, CHANNEL_KEY, "555", GUILD)
        assert svc.delivered_keys(conn, GUILD) == {fr.PROMOTION_REVIEW_PING.key}


def test_a_running_bump_tracker_offers_no_staff_ping(sync_db_path):
    """Staff · Bump was retired 2026-09-30: it repeated the Bump Tracker's own
    reminder into the staff channel, so the same sites were announced twice."""
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.CHANNEL_KEY, "777", GUILD)
        set_config_value(conn, "ticket_notify_on_create", "0", GUILD)
        conn.execute(
            "INSERT INTO bump_tracker_config (guild_id, channel_id, role_id, enabled)"
            " VALUES (?, 9, 0, 1)",
            (GUILD,),
        )
        assert svc.delivered_keys(conn, GUILD) == set()
    assert not any("bump" in e.key for e in svc.CHANNEL_PINGS)


def test_every_channel_ping_is_staff_and_names_its_cooldown():
    for entry in svc.CHANNEL_PINGS:
        assert entry in fr.STAFF_ROLES, entry.key
        assert svc.COOLDOWN_SECONDS[entry.key] >= 0, entry.key


@pytest.mark.parametrize(
    "notify, delivered",
    [
        pytest.param(None, True, id="notify-default-on"),
        pytest.param("1", True, id="notify-on"),
        pytest.param("0", False, id="notify-off-takes-the-ping-too"),
    ],
)
def test_the_ticket_ping_follows_the_notify_switch(sync_db_path, notify, delivered):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.CHANNEL_KEY, "777", GUILD)
        if notify is not None:
            set_config_value(conn, "ticket_notify_on_create", notify, GUILD)
        got = fr.STAFF_TICKET_PING.key in svc.delivered_keys(conn, GUILD)
    assert got is delivered


def test_the_dashboard_sets_every_staff_ping_but_promotion_reviews():
    assert set(svc.CHANNEL_PINGS) == set(fr.STAFF_ROLES) - {fr.PROMOTION_REVIEW_PING}


def test_the_chores_ping_is_delivered_only_with_an_active_chore(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.CHANNEL_KEY, "777", GUILD)
        assert fr.STAFF_CHORES_PING.key not in svc.delivered_keys(conn, GUILD)
        conn.execute(
            "INSERT INTO todo_recurring (guild_id, task, status, created_at) VALUES (?, 'Sweep', 'paused', 0)",
            (GUILD,),
        )
        assert fr.STAFF_CHORES_PING.key not in svc.delivered_keys(conn, GUILD)
        conn.execute(
            "INSERT INTO todo_recurring (guild_id, task, status, created_at) VALUES (?, 'Mop', 'active', 0)",
            (GUILD,),
        )
        assert fr.STAFF_CHORES_PING.key in svc.delivered_keys(conn, GUILD)


def test_spawned_chore_names_counts_only_fresh_instances():
    from bot_modules.todo.recurring_service import RecurringTask, SpawnResult

    due = [RecurringTask(id=i, guild_id=g, task=t) for i, g, t in
           [(1, 7, "Sweep"), (2, 7, "Mop"), (3, 7, "Dust"), (4, 8, "Bins")]]
    spawned = [
        SpawnResult(recurring_id=1, guild_id=7, status="spawned", todo_id=11),
        SpawnResult(recurring_id=2, guild_id=7, status="skipped_pending"),
        SpawnResult(recurring_id=3, guild_id=7, status="spawned", todo_id=None, missed_todo_id=5),
        SpawnResult(recurring_id=4, guild_id=8, status="spawned", todo_id=14),
        SpawnResult(recurring_id=99, guild_id=8, status="spawned", todo_id=15),
    ]
    assert svc.spawned_chore_names(due, spawned) == {7: ["Sweep"], 8: ["Bins"]}


@pytest.mark.parametrize(
    "names, text",
    [
        pytest.param(["Sweep"], "New chore on the todo board: **Sweep**.", id="one"),
        pytest.param(["A", "B"], "New chores on the todo board: **A**, **B**.", id="two"),
        pytest.param(["*x*"], "New chore on the todo board: **\\*x\\***.", id="escaped"),
        pytest.param(
            [str(i) for i in range(12)],
            "New chores on the todo board: "
            + ", ".join(f"**{i}**" for i in range(10)) + ", 2 more.",
            id="capped",
        ),
    ],
)
def test_chores_due_text(names, text):
    assert svc.chores_due_text(names) == text


@pytest.mark.parametrize(
    "mode, channel_on, delivered",
    [
        pytest.param("post", True, True, id="posting-with-a-channel"),
        # Ping-moderators mode is running too: it's the whole point of the row.
        pytest.param("ping_staff", True, True, id="ping-moderators-with-a-channel"),
        pytest.param("off", True, False, id="guild-switched-off"),
        pytest.param("post", False, False, id="no-enabled-channel"),
        pytest.param("ping_staff", False, False, id="ping-moderators-no-channel"),
    ],
)
def test_the_revive_ping_is_delivered_only_while_chat_revive_runs(
    sync_db_path, mode, channel_on, delivered
):
    from bot_modules.chat_revive.service import (
        ChannelConfig,
        GuildConfig,
        save_channel_config,
        save_guild_config,
    )

    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.CHANNEL_KEY, "777", GUILD)
        save_guild_config(conn, GuildConfig(guild_id=GUILD, mode=mode))
        save_channel_config(
            conn, ChannelConfig(guild_id=GUILD, channel_id=5, enabled=channel_on)
        )
        got = fr.STAFF_REVIVE_PING.key in svc.delivered_keys(conn, GUILD)
    assert got is delivered


@pytest.mark.parametrize(
    "reason, needle",
    [
        pytest.param(svc.REVIVE_NO_QUESTION, "no question left", id="no-question"),
        pytest.param(svc.REVIVE_SEND_FAILED, "couldn't post", id="send-failed"),
    ],
)
def test_revive_stuck_text(reason, needle):
    text = svc.revive_stuck_text(reason, 9)
    assert text.startswith("<#9> has gone quiet") and needle in text


def test_revive_lull_text_names_the_channel_and_nothing_else():
    """Ping-moderators mode: just the lull (Ben, 2026-09-30), no question."""
    assert svc.revive_lull_text(9) == "<#9> has gone quiet — a good moment to start something."


# ── Greeting Watch ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "staff_channel, enabled, channels, delivered",
    [
        pytest.param("777", "1", "10,11", True, id="on-with-a-watched-channel"),
        pytest.param("", "1", "10", False, id="no-staff-channel"),
        pytest.param("777", "0", "10", False, id="switched-off"),
        pytest.param("777", "1", "", False, id="nothing-watched"),
        pytest.param("777", None, None, False, id="never-set-up"),
    ],
)
def test_the_greeting_ping_is_delivered_only_while_greeting_watch_runs(
    sync_db_path, staff_channel, enabled, channels, delivered
):
    with open_db(sync_db_path) as conn:
        if staff_channel:
            set_config_value(conn, svc.CHANNEL_KEY, staff_channel, GUILD)
        if enabled is not None:
            set_config_value(conn, "greeting_watch_enabled", enabled, GUILD)
            set_config_value(conn, "greeting_watch_channel_ids", channels, GUILD)
        got = fr.STAFF_GREETING_PING.key in svc.delivered_keys(conn, GUILD)
    assert got is delivered


def test_greeting_unanswered_text():
    text = svc.greeting_unanswered_text("Robin", 9, 5, "https://discord.com/channels/1/9/2")
    assert text == (
        "Robin said hello in <#9> about 5 min ago and nobody has answered. "
        "https://discord.com/channels/1/9/2"
    )


# ── QOTD ─────────────────────────────────────────────────────────────────

QOTD = fr.STAFF_QOTD_PING
#: 2026-06-30 19:30 UTC; the tests' guild runs on UTC (offset 0).
EVENING = 1_782_847_800.0


@pytest.fixture(autouse=True)
def _fresh_qotd_marks(monkeypatch):
    monkeypatch.setattr(svc, "_qotd_warned", {})
    monkeypatch.setattr(svc, "_qotd_retry_at", {})


@pytest.mark.parametrize(
    "raw, hour",
    [
        pytest.param("18", 18, id="hour"),
        pytest.param(" 0 ", 0, id="midnight-trimmed"),
        pytest.param("23", 23, id="last-hour"),
        pytest.param("24", None, id="out-of-range"),
        pytest.param("-1", None, id="negative"),
        pytest.param("", None, id="blank-is-off"),
        pytest.param("abc", None, id="garbage"),
    ],
)
def test_parse_deadline_hour(raw, hour):
    assert svc.parse_deadline_hour(raw) == hour


@pytest.mark.parametrize(
    "local_hour, deadline, posted, late",
    [
        pytest.param(19, 18, False, True, id="past-deadline-none-posted"),
        pytest.param(18, 18, False, True, id="on-the-hour"),
        pytest.param(17, 18, False, False, id="before-deadline"),
        pytest.param(19, 18, True, False, id="already-posted"),
        pytest.param(19, None, False, False, id="dial-off"),
    ],
)
def test_qotd_overdue(local_hour, deadline, posted, late):
    assert svc.qotd_overdue(local_hour, deadline, posted) is late


SPICY = fr.STAFF_SPICY_QOTD_PING
#: 18:00 UTC on EVENING's day, the deadline the tests set.
DUE_18 = 1_782_842_400


def test_the_qotd_ping_is_delivered_only_with_a_deadline(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.CHANNEL_KEY, "777", GUILD)
        assert QOTD.key not in svc.delivered_keys(conn, GUILD)
        set_config_value(conn, svc.QOTD_DEADLINE_KEY, "18", GUILD)
        assert QOTD.key in svc.delivered_keys(conn, GUILD)


@pytest.mark.parametrize(
    "deadline, spicy_role, delivered",
    [
        pytest.param("18", "4242", True, id="deadline-and-role"),
        pytest.param("", "4242", False, id="no-deadline"),
        pytest.param("18", "", False, id="no-spicy-role"),
        pytest.param("18", "0", False, id="spicy-role-none"),
    ],
)
def test_the_spicy_qotd_ping_needs_the_deadline_and_the_spicy_role(
    sync_db_path, deadline, spicy_role, delivered
):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.CHANNEL_KEY, "777", GUILD)
        set_config_value(conn, svc.QOTD_DEADLINE_KEY, deadline, GUILD)
        set_config_value(conn, svc.SPICY_ROLE_KEY, spicy_role, GUILD)
        assert (SPICY.key in svc.delivered_keys(conn, GUILD)) is delivered


def test_the_spicy_role_does_not_fall_back_to_the_legacy_guild(sync_db_path):
    """Economy settings are guild-scoped; a guild-0 row configures nobody."""
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.CHANNEL_KEY, "777", GUILD)
        set_config_value(conn, svc.QOTD_DEADLINE_KEY, "18", GUILD)
        set_config_value(conn, svc.SPICY_ROLE_KEY, "4242", 0)
        assert SPICY.key not in svc.delivered_keys(conn, GUILD)


def _qotd_posted(conn, day, kind="qotd"):
    conn.execute(
        "INSERT INTO econ_qotd (guild_id, channel_id, message_id, question, posted_by,"
        " local_day, created_at, kind) VALUES (?, 1, 2, 'Q?', 3, ?, 0, ?)",
        (GUILD, day, kind),
    )


@pytest.mark.parametrize(
    "deadline, posted_day, due",
    [
        pytest.param("18", None, True, id="late"),
        pytest.param("20", None, False, id="not-yet"),
        pytest.param("18", "2026-06-30", False, id="posted-today"),
        pytest.param("18", "2026-06-29", True, id="only-yesterdays"),
        pytest.param("", None, False, id="off"),
    ],
)
def test_overdue_qotds(sync_db_path, deadline, posted_day, due):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.QOTD_DEADLINE_KEY, deadline, GUILD)
        if posted_day:
            _qotd_posted(conn, posted_day)
        got = svc.overdue_qotds(conn, EVENING)
    assert got == ([(GUILD, "qotd", "2026-06-30", DUE_18)] if due else [])


@pytest.mark.parametrize(
    "spicy_role, posted_kinds, late_kinds",
    [
        pytest.param("", [], ["qotd"], id="no-spicy-qotd-in-this-guild"),
        pytest.param("4242", [], ["qotd", "spicy"], id="both-late"),
        pytest.param("4242", ["qotd"], ["spicy"], id="regular-doesnt-cover-spicy"),
        pytest.param("4242", ["spicy"], ["qotd"], id="spicy-doesnt-cover-regular"),
        pytest.param("4242", ["qotd", "spicy"], [], id="both-posted"),
    ],
)
def test_the_two_kinds_are_checked_apart(sync_db_path, spicy_role, posted_kinds, late_kinds):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.QOTD_DEADLINE_KEY, "18", GUILD)
        set_config_value(conn, svc.SPICY_ROLE_KEY, spicy_role, GUILD)
        for kind in posted_kinds:
            _qotd_posted(conn, "2026-06-30", kind)
        got = svc.overdue_qotds(conn, EVENING)
    assert [kind for _, kind, _, _ in got] == late_kinds


@pytest.mark.parametrize(
    "kind, what",
    [
        pytest.param("qotd", "No question of the day", id="regular"),
        pytest.param("spicy", "No spicy question of the day", id="spicy"),
    ],
)
def test_qotd_late_text_shows_the_deadline_as_local_time(kind, what):
    text = svc.qotd_late_text(kind, DUE_18)
    assert text.startswith(what)
    assert f"<t:{DUE_18}:t>" in text
    assert "18:00" not in text


@pytest.mark.parametrize("posts", [True, False])
async def test_the_qotd_ping_goes_out_once_a_day_and_only_marks_on_success(
    sync_db_path, monkeypatch, posts
):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.QOTD_DEADLINE_KEY, "18", GUILD)
    post = AsyncMock(return_value=posts)
    monkeypatch.setattr(svc, "post_staff_ping", post)
    ctx = SimpleNamespace(open_db=lambda: open_db(sync_db_path))
    bot = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(id=gid))

    await svc.ping_overdue_qotd(ctx, bot, EVENING)
    await svc.ping_overdue_qotd(ctx, bot, EVENING + 60)
    assert post.await_count == 1  # posted: marked; not posted: backing off
    await svc.ping_overdue_qotd(ctx, bot, EVENING + svc.QOTD_RETRY_SECONDS)

    # Posted: marked for the day, so later ticks stay quiet. Not posted
    # (nobody opted in yet): unmarked, so it tries again after the backoff.
    assert post.await_count == (1 if posts else 2)
    assert post.await_args.args[2] is QOTD
    assert post.await_args.args[3] == svc.qotd_late_text("qotd", DUE_18)


async def test_each_kind_is_marked_on_its_own(sync_db_path, monkeypatch):
    """The spicy ping failing (nobody opted in) must not stop the regular one
    being marked, nor the regular one posting mark the spicy one done."""
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.QOTD_DEADLINE_KEY, "18", GUILD)
        set_config_value(conn, svc.SPICY_ROLE_KEY, "4242", GUILD)
    post = AsyncMock(side_effect=lambda ctx, guild, entry, text: entry is QOTD)
    monkeypatch.setattr(svc, "post_staff_ping", post)
    ctx = SimpleNamespace(open_db=lambda: open_db(sync_db_path))
    bot = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(id=gid))

    await svc.ping_overdue_qotd(ctx, bot, EVENING)
    assert [c.args[2] for c in post.await_args_list] == [QOTD, SPICY]
    assert post.await_args_list[1].args[3] == svc.qotd_late_text("spicy", DUE_18)
    await svc.ping_overdue_qotd(ctx, bot, EVENING + svc.QOTD_RETRY_SECONDS)
    assert [c.args[2] for c in post.await_args_list] == [QOTD, SPICY, SPICY]


def test_ticket_opened_text_names_no_member():
    assert svc.ticket_opened_text(12, 9) == "Ticket **#12** was opened: <#9>."


@pytest.mark.parametrize(
    "last, now, ok",
    [
        pytest.param(None, 5.0, True, id="never-pinged"),
        pytest.param(0.0, 599.0, False, id="inside-cooldown"),
        pytest.param(0.0, 600.0, True, id="cooldown-over"),
    ],
)
def test_cooled_down(last, now, ok):
    assert svc.cooled_down(last, now, 600) is ok


# ── post_staff_ping ──────────────────────────────────────────────────────

TICKET = fr.STAFF_TICKET_PING
REVIVE = fr.STAFF_REVIVE_PING


@pytest.fixture(autouse=True)
def _fresh_cooldowns(monkeypatch):
    monkeypatch.setattr(svc, "_last_post", {})


class _Channel(discord.abc.Messageable):
    def __init__(self):
        self.send = AsyncMock()

    async def _get_channel(self):  # pragma: no cover - Messageable plumbing
        return self


def _guild(*, channel=True, role=True, holders=(False,)):
    """``holders`` lists who holds role 5, one ``bot`` flag each."""
    ch = _Channel()
    held = SimpleNamespace(members=[SimpleNamespace(bot=b) for b in holders])
    guild = SimpleNamespace(
        id=GUILD,
        get_channel=lambda cid: ch if channel and cid == 777 else None,
        get_role=lambda rid: held if role and rid == 5 else None,
    )
    return guild, ch


def _configure(path, *, channel="777", role="5", key=None, job_running=True):
    with open_db(path) as conn:
        if channel:
            set_config_value(conn, svc.CHANNEL_KEY, channel, GUILD)
        if role:
            set_config_value(conn, key or TICKET.key, role, GUILD)
        if not job_running:
            set_config_value(conn, "ticket_notify_on_create", "0", GUILD)
    return SimpleNamespace(open_db=lambda: open_db(path))


async def test_post_staff_ping_mentions_the_role_in_the_staff_channel(sync_db_path):
    ctx = _configure(sync_db_path)
    guild, ch = _guild()
    assert await svc.post_staff_ping(ctx, guild, TICKET, "Go look.", now=0) is True
    kwargs = ch.send.await_args.kwargs
    assert kwargs["content"] == "<@&5> Go look."
    assert kwargs["allowed_mentions"].everyone is False


@pytest.mark.parametrize(
    "setup, guild_kw",
    [
        pytest.param({"channel": ""}, {}, id="channel-unset"),
        pytest.param({"role": ""}, {}, id="role-unset"),
        pytest.param({}, {"channel": False}, id="channel-deleted"),
        pytest.param({}, {"role": False}, id="role-deleted"),
        pytest.param({}, {"holders": ()}, id="nobody-opted-in"),
        pytest.param({}, {"holders": (True,)}, id="only-a-bot-holds-it"),
        pytest.param({"job_running": False}, {}, id="job-not-running"),
    ],
)
async def test_post_staff_ping_skips_when_not_configured(sync_db_path, setup, guild_kw):
    ctx = _configure(sync_db_path, **setup)
    guild, ch = _guild(**guild_kw)
    assert await svc.post_staff_ping(ctx, guild, TICKET, "x", now=0) is False
    ch.send.assert_not_awaited()


async def test_post_staff_ping_respects_the_cooldown(sync_db_path, monkeypatch):
    monkeypatch.setitem(svc.COOLDOWN_SECONDS, TICKET.key, 600)
    ctx = _configure(sync_db_path)
    guild, ch = _guild()
    cooldown = svc.COOLDOWN_SECONDS[TICKET.key]
    assert await svc.post_staff_ping(ctx, guild, TICKET, "x", now=0) is True
    assert await svc.post_staff_ping(ctx, guild, TICKET, "x", now=cooldown - 1) is False
    assert await svc.post_staff_ping(ctx, guild, TICKET, "x", now=cooldown) is True
    assert ch.send.await_count == 2


def _configure_revive(path):
    """Staff · Chat Revive configured, with Chat Revive running in a channel."""
    from bot_modules.chat_revive.service import (
        ChannelConfig,
        GuildConfig,
        save_channel_config,
        save_guild_config,
    )

    ctx = _configure(path, key=REVIVE.key)
    with open_db(path) as conn:
        save_guild_config(conn, GuildConfig(guild_id=GUILD, mode="ping_staff"))
        save_channel_config(conn, ChannelConfig(guild_id=GUILD, channel_id=5))
    return ctx


@pytest.mark.parametrize(
    "first_lane, second_lane",
    [
        pytest.param(svc.REVIVE_LULL_LANE, None, id="lull-ping-then-cant-fill-ping"),
        pytest.param(None, svc.REVIVE_LULL_LANE, id="cant-fill-ping-then-lull-ping"),
    ],
)
async def test_a_lane_keeps_its_own_clock(sync_db_path, first_lane, second_lane):
    """Review finding (2026-09-30): Chat Revive's lull pings once shared the
    subject's 3-hour clock, so switching back to Post questions could hold
    the first can't-fill ping back for hours. Neither clock blocks the other."""
    ctx = _configure_revive(sync_db_path)
    guild, ch = _guild()
    assert await svc.post_staff_ping(ctx, guild, REVIVE, "a", now=0, lane=first_lane) is True
    assert await svc.post_staff_ping(ctx, guild, REVIVE, "b", now=1, lane=second_lane) is True
    assert ch.send.await_count == 2


async def test_the_lull_lane_is_a_backstop_against_a_ping_every_tick(sync_db_path):
    """Review finding (2026-09-30): a lull ping posts before its lull is
    recorded, so a failed record left the lull standing and, with no
    cooldown, re-pinged every two-minute tick. The lane holds it for 30
    minutes — and no longer than that, so the revive dials still pace it."""
    ctx = _configure_revive(sync_db_path)
    guild, ch = _guild()
    lane = svc.REVIVE_LULL_LANE
    assert lane.cooldown == 30 * 60
    assert await svc.post_staff_ping(ctx, guild, REVIVE, "x", now=0, lane=lane) is True
    assert await svc.post_staff_ping(ctx, guild, REVIVE, "x", now=120, lane=lane) is False
    assert await svc.post_staff_ping(ctx, guild, REVIVE, "x", now=lane.cooldown, lane=lane) is True
    assert ch.send.await_count == 2


async def test_two_tickets_opened_minutes_apart_both_ping(sync_db_path):
    # Every ticket is its own job; a cooldown would swallow the second one.
    ctx = _configure(sync_db_path)
    guild, ch = _guild()
    assert await svc.post_staff_ping(ctx, guild, TICKET, "a", now=0) is True
    assert await svc.post_staff_ping(ctx, guild, TICKET, "b", now=300) is True
    assert ch.send.await_count == 2


async def test_a_database_error_reads_as_a_skipped_ping(sync_db_path):
    ctx = _configure(sync_db_path)
    guild, ch = _guild()
    ctx.open_db = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
    assert await svc.post_staff_ping(ctx, guild, TICKET, "x", now=0) is False
    ch.send.assert_not_awaited()


async def test_a_refused_post_does_not_start_the_cooldown(sync_db_path):
    ctx = _configure(sync_db_path)
    guild, ch = _guild()
    ch.send.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    assert await svc.post_staff_ping(ctx, guild, TICKET, "x", now=0) is False
    ch.send.side_effect = None
    assert await svc.post_staff_ping(ctx, guild, TICKET, "x", now=1) is True


async def test_post_staff_ping_refuses_a_ping_with_its_own_channel():
    with pytest.raises(ValueError):
        await svc.post_staff_ping(SimpleNamespace(), SimpleNamespace(id=GUILD),
                                  fr.PROMOTION_REVIEW_PING, "x")


async def test_every_ticket_pings_no_cooldown(sync_db_path):
    ctx = _configure(sync_db_path, key=fr.STAFF_TICKET_PING.key)
    guild, ch = _guild()
    assert await svc.ping_ticket_opened(ctx, guild, ticket_id=1, channel_id=9) is True
    assert await svc.ping_ticket_opened(ctx, guild, ticket_id=2, channel_id=9) is True
    assert ch.send.await_args.kwargs["content"] == "<@&5> Ticket **#2** was opened: <#9>."


async def test_the_ticket_ping_is_silent_with_notify_off(sync_db_path):
    ctx = _configure(sync_db_path, key=fr.STAFF_TICKET_PING.key)
    with open_db(sync_db_path) as conn:
        set_config_value(conn, "ticket_notify_on_create", "0", GUILD)
    guild, ch = _guild()
    assert await svc.ping_ticket_opened(ctx, guild, ticket_id=1, channel_id=9) is False
    ch.send.assert_not_awaited()
