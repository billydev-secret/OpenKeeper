"""Needle reply DMs, the member's opt-in, and who may close a thread.

Logic and store layer only; the cog is glue over these.
"""
from __future__ import annotations

import re
import sqlite3
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.needle import cog as needle_cog
from bot_modules.needle.cog import (
    _delete_channel,
    _ensure_tables,
    _get_channel_config,
    _upsert_channel,
)
from bot_modules.needle.logic import (
    REPLY_DM_BUTTON_TEMPLATE,
    build_reply_digest_text,
    can_manage_thread,
    dm_button_may_write,
    mute_button_label,
    reply_dm_custom_id,
    resolve_starter_id,
    should_dm_starter,
    starter_deleted_fate,
    toggled,
)
from bot_modules.needle.store import (
    is_reply_opted_in,
    reply_dms_offered,
    set_reply_optin,
)

GUILD, CHANNEL, THREAD = 1, 2, 3
STARTER, REPLIER, BOT = 100, 200, 999


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_tables(c)
    yield c
    c.close()


# ── who may close / rename ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("user_id", "owner_id", "starter_id", "manage", "expected"),
    [
        # The bug: Needle opens the thread, so the bot is the owner and the
        # poster was refused. The starter must be allowed.
        pytest.param(STARTER, BOT, STARTER, False, True, id="starter-of-bot-owned-thread"),
        pytest.param(REPLIER, BOT, STARTER, False, False, id="someone-else"),
        pytest.param(REPLIER, BOT, STARTER, True, True, id="moderator"),
        pytest.param(STARTER, STARTER, 0, False, True, id="hand-opened-thread-owner"),
        pytest.param(0, 0, 0, False, False, id="unknown-ids-never-match"),
    ],
)
def test_can_manage_thread(user_id, owner_id, starter_id, manage, expected):
    assert can_manage_thread(
        user_id=user_id, owner_id=owner_id,
        starter_author_id=starter_id, has_manage_threads=manage,
    ) is expected


@pytest.mark.parametrize(
    ("starter_author", "owner_id", "expected"),
    [
        pytest.param(STARTER, BOT, STARTER, id="starter-message-author-wins"),
        # Starter message gone, owner is the bot: nobody but a mod qualifies.
        pytest.param(None, BOT, 0, id="bot-owner-never-taken-for-starter"),
        pytest.param(None, STARTER, STARTER, id="member-owner-stands-in"),
        pytest.param(None, 0, 0, id="nothing-known"),
    ],
)
def test_resolve_starter_id(starter_author, owner_id, expected):
    assert resolve_starter_id(
        starter_author_id=starter_author, owner_id=owner_id, bot_id=BOT,
    ) == expected


_thread_n = iter(range(900_000, 10**9))


def _thread_needing_a_fetch(*, owner_id: int) -> tuple[Any, Any]:
    """A thread with no cached starter message, so ``_starter_author_id``
    must hit ``parent.fetch_message`` — the path a transient error can poison."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock()
    thread = cast(Any, SimpleNamespace(
        id=next(_thread_n),
        starter_message=None, parent=channel, owner_id=owner_id,
        guild=SimpleNamespace(me=SimpleNamespace(id=BOT)),
    ))
    return channel, thread


@pytest.mark.asyncio
async def test_a_transient_fetch_error_is_not_cached_as_unknown(monkeypatch):
    """A rate limit or a 5xx on the starter-message fetch is not proof the
    message is gone. Before the fix this cached author_id 0 forever (since the
    thread's owner is the bot Needle itself), permanently silencing reply DMs
    and locking the real poster out of /close and /title for that thread."""
    monkeypatch.setattr(needle_cog, "_STARTER_CACHE", {})
    channel, thread = _thread_needing_a_fetch(owner_id=BOT)
    channel.fetch_message.side_effect = discord.HTTPException(MagicMock(status=503), "service unavailable")
    assert await needle_cog._starter_author_id(thread) == 0
    assert thread.id not in needle_cog._STARTER_CACHE

    channel.fetch_message.side_effect = None
    channel.fetch_message.return_value = SimpleNamespace(author=SimpleNamespace(id=STARTER))
    assert await needle_cog._starter_author_id(thread) == STARTER
    assert needle_cog._STARTER_CACHE[thread.id] == STARTER


@pytest.mark.asyncio
async def test_a_genuinely_deleted_starter_message_is_cached_as_unknown(monkeypatch):
    monkeypatch.setattr(needle_cog, "_STARTER_CACHE", {})
    channel, thread = _thread_needing_a_fetch(owner_id=BOT)
    channel.fetch_message.side_effect = discord.NotFound(MagicMock(status=404), "Unknown Message")
    assert await needle_cog._starter_author_id(thread) == 0
    assert needle_cog._STARTER_CACHE[thread.id] == 0


def test_starter_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(needle_cog, "_STARTER_CACHE", {})
    monkeypatch.setattr(needle_cog, "_STARTER_CACHE_MAX", 3)
    for tid in range(5):
        needle_cog._remember_starter(tid, tid)
    assert list(needle_cog._STARTER_CACHE) == [2, 3, 4]


# ── should the starter be DMed ───────────────────────────────────────────────


_BASE = dict(
    notify_enabled=True, starter_author_id=STARTER, replier_id=REPLIER,
    replier_is_bot=False, opted_in=True, no_contact=False,
)


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        pytest.param({}, True, id="happy-path"),
        pytest.param({"notify_enabled": False}, False, id="channel-dial-off"),
        pytest.param({"starter_author_id": 0}, False, id="starter-unknown"),
        pytest.param({"replier_id": STARTER}, False, id="starter-replying-to-self"),
        pytest.param({"replier_is_bot": True}, False, id="bot-reply"),
        # Opt-IN since migration 244: a member who never chose is not DMed,
        # even with the channel's dial on. Reverting to opt-out fails this row.
        pytest.param({"opted_in": False}, False, id="starter-not-opted-in"),
        pytest.param({"no_contact": True}, False, id="no-contact-pair"),
        # Opting in never outranks a no-contact pair.
        pytest.param({"opted_in": True, "no_contact": True}, False, id="opted-in-no-contact"),
    ],
)
def test_should_dm_starter(over, expected):
    assert should_dm_starter(**{**_BASE, **over}) is expected


def _digest(names, count):
    return build_reply_digest_text(
        replier_names=names, reply_count=count, thread_name="**bold** _title_",
        guild_name="Keep", guild_id=GUILD, thread_id=THREAD, first_reply_id=55,
    )


def test_a_single_reply_reads_as_the_one_per_reply_dm_did():
    text = _digest(["Alice"], 1)
    assert text.startswith("**Alice** replied to your thread")
    assert "**bold**" not in text
    assert "\\*\\*bold\\*\\*" in text
    assert "replies)" not in text
    assert f"Reply: https://discord.com/channels/{GUILD}/{THREAD}/55" in text


@pytest.mark.parametrize(
    ("names", "count", "who"),
    [
        pytest.param(["Alice"], 4, "**Alice** replied", id="one-member-many-replies"),
        pytest.param(["Alice", "Bo"], 2, "**Alice** and **Bo** replied", id="two-members"),
        pytest.param(
            ["A", "B", "C", "D", "E"], 7, "**A**, **B** and 3 others replied", id="crowd"
        ),
    ],
)
def test_a_batch_names_repliers_counts_replies_and_links_the_first(names, count, who):
    text = _digest(names, count)
    assert text.startswith(who)
    assert f"({count} replies)" in text
    assert f"First new reply: https://discord.com/channels/{GUILD}/{THREAD}/55" in text


@pytest.mark.parametrize(
    ("behavior", "can_delete", "read", "others", "fate"),
    [
        pytest.param("nothing", True, True, False, "nothing", id="nothing"),
        pytest.param("archive", True, True, False, "archive", id="archive"),
        pytest.param("delete", True, True, True, "delete", id="delete"),
        pytest.param("delete", False, True, False, "archive", id="delete-without-perm"),
        pytest.param("archive_if_empty", True, True, False, "delete", id="empty-thread"),
        pytest.param("archive_if_empty", True, True, True, "archive", id="someone-replied"),
        pytest.param("archive_if_empty", False, True, False, "archive", id="empty-no-perm"),
        # The bug: a failed history read came back as an empty list and read as
        # "nobody replied", deleting the thread with members' replies in it.
        pytest.param("archive_if_empty", True, False, False, "archive", id="history-unread"),
    ],
)
def test_starter_deleted_fate(behavior, can_delete, read, others, fate):
    assert starter_deleted_fate(
        behavior=behavior, can_delete=can_delete, history_read=read, others_posted=others
    ) == fate


# ── the DM button ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["mute", "unmute"])
def test_custom_id_round_trips_through_the_template(action):
    cid = reply_dm_custom_id(action, guild_id=GUILD, channel_id=CHANNEL, thread_id=THREAD)
    assert len(cid) <= 100
    m = re.fullmatch(REPLY_DM_BUTTON_TEMPLATE, cid)
    assert m is not None
    assert (m["action"], int(m["guild_id"]), int(m["channel_id"]), int(m["thread_id"])) == (
        action, GUILD, CHANNEL, THREAD
    )


def test_a_real_snowflake_id_fits_discords_custom_id_limit():
    big = 1469491362444480666
    assert len(reply_dm_custom_id("unmute", guild_id=big, channel_id=big, thread_id=big)) <= 100


def test_button_offers_the_undo_after_a_press():
    assert toggled("mute") == "unmute" and toggled("unmute") == "mute"
    assert mute_button_label("mute") == "🔕 Turn off reply DMs"
    assert mute_button_label("unmute") == "🔔 Turn reply DMs back on"


def test_the_button_no_longer_claims_to_be_per_thread():
    """The press flips the member's server-wide switch, so a label that says
    "this thread" would promise something it doesn't do."""
    assert "thread" not in mute_button_label("mute")
    assert "thread" not in mute_button_label("unmute")


@pytest.mark.parametrize(
    ("action", "is_member", "allowed"),
    [
        pytest.param("mute", True, True, id="member-turns-off"),
        pytest.param("unmute", True, True, id="member-turns-on"),
        # Off only deletes, so even a departed member may press it.
        pytest.param("mute", False, True, id="departed-turns-off"),
        # The bug: an erased member re-created an opt-in row from an old DM.
        pytest.param("unmute", False, False, id="departed-cannot-turn-on"),
    ],
)
def test_dm_button_may_write(action, is_member, allowed):
    assert dm_button_may_write(action, is_member=is_member) is allowed


def test_a_button_on_a_dm_sent_before_the_opt_in_still_dispatches():
    """DMs sent since 2026-09-12 carry the per-thread custom id. The template
    must keep matching it, or those buttons go dead at the restart."""
    legacy = "needle_dm:mute:1469491362444480666:1550999999999999652:1550999999999999999"
    assert re.fullmatch(REPLY_DM_BUTTON_TEMPLATE, legacy) is not None


# ── store ────────────────────────────────────────────────────────────────────


def test_nobody_is_opted_in_by_default(conn):
    assert not is_reply_opted_in(conn, guild_id=GUILD, user_id=STARTER)


def test_opt_in_then_out(conn):
    kw = dict(guild_id=GUILD, user_id=STARTER)
    set_reply_optin(conn, on=True, **kw)
    set_reply_optin(conn, on=True, **kw)  # a double press is harmless
    assert is_reply_opted_in(conn, **kw)
    set_reply_optin(conn, on=False, **kw)
    assert not is_reply_opted_in(conn, **kw)
    # Off stores nothing.
    assert conn.execute("SELECT COUNT(*) FROM needle_reply_optins").fetchone()[0] == 0
    set_reply_optin(conn, on=False, **kw)


def test_opt_in_is_per_member_and_per_guild(conn):
    set_reply_optin(conn, guild_id=GUILD, user_id=STARTER, on=True)
    assert not is_reply_opted_in(conn, guild_id=GUILD, user_id=REPLIER)
    assert not is_reply_opted_in(conn, guild_id=GUILD + 1, user_id=STARTER)


def _upsert(conn, **over):
    kwargs = dict(
        guild_id=GUILD, channel_id=CHANNEL, title_type="first_fifty", custom_title="",
        include_bots=False, slowmode=0, delete_behavior="archive_if_empty",
        reply_type="default", custom_reply="", default_reactions="",
    )
    kwargs.update(over)
    _upsert_channel(conn, **kwargs)  # type: ignore[arg-type]
    return _get_channel_config(conn, GUILD, CHANNEL)


def test_reply_dms_ship_off_and_round_trip(conn):
    cfg = _upsert(conn)
    assert cfg is not None and cfg.notify_op_on_reply is False
    cfg = _upsert(conn, notify_op_on_reply=True)
    assert cfg is not None and cfg.notify_op_on_reply is True


def test_removing_a_channel_keeps_members_opt_ins(conn):
    """The opt-in is the member's, for the whole server — not the channel's."""
    _upsert(conn)
    set_reply_optin(conn, guild_id=GUILD, user_id=STARTER, on=True)
    assert _delete_channel(conn, GUILD, CHANNEL) == 1
    assert is_reply_opted_in(conn, guild_id=GUILD, user_id=STARTER)


@pytest.mark.parametrize(
    ("channels", "offered"),
    [
        pytest.param([], False, id="no-channels"),
        pytest.param([False], False, id="dial-off"),
        pytest.param([False, True], True, id="one-channel-on"),
    ],
)
def test_reply_dms_are_offered_only_where_a_dial_is_on(conn, channels, offered):
    for i, on in enumerate(channels):
        _upsert(conn, channel_id=CHANNEL + i, notify_op_on_reply=on)
    assert reply_dms_offered(conn, GUILD) is offered
    assert reply_dms_offered(conn, GUILD + 1) is False
