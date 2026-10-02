"""Mention-injection guard for the Needle auto-thread welcome message.

``_apply_variables`` substitutes a member-controlled display name into the
welcome template that the bot then *sends and pins*. A nickname like
``<@&roleId>`` or ``@everyone`` must not survive as a live ping. These tests
exercise the substitution helper directly (logic layer) — no Discord needed.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bot_modules.needle import cog as needle_cog
from bot_modules.needle.cog import (
    _TEXT_ONLY_NOTICE_AT,
    NeedleChannelConfig,
    NeedleGlobalConfig,
    _apply_variables,
    _bot_can_manage_messages,
    _claim_text_only_notice,
    _ensure_tables,
    _get_channel_config,
    _release_text_only_notice,
    _upsert_channel,
)
from bot_modules.needle.logic import (
    DEFAULT_AUTO_ARCHIVE_MINUTES,
    TEXT_ONLY_NOTICE_COOLDOWN_SECONDS,
    auto_archive_minutes,
    build_text_only_notice,
    removal_blocked,
    should_post_text_only_notice,
    starter_post_verdict,
    thread_create_options,
)


def _msg(display: str, channel_id: int = 555) -> Any:
    author = SimpleNamespace(display_name=display, name=display)
    return cast(Any, SimpleNamespace(author=author, channel=SimpleNamespace(id=channel_id)))


def _thread(mention: str = "<#999>") -> Any:
    return cast(Any, SimpleNamespace(mention=mention))


def test_apply_variables_neutralizes_role_mention_in_nickname():
    out = _apply_variables(
        "Thread created by $USER in $CHANNEL",
        message=_msg("<@&123456789012345678>"),
        thread=_thread(),
    )
    assert "<@&123456789012345678>" not in out
    assert "​" in out  # zero-width break inserted by escape_mentions


def test_apply_variables_neutralizes_everyone_in_nickname():
    out = _apply_variables(
        "Welcome $USER",
        message=_msg("@everyone"),
        thread=_thread(),
    )
    assert "@everyone" not in out


def test_apply_variables_keeps_channel_and_thread_refs():
    out = _apply_variables(
        "$USER in $CHANNEL — $THREAD",
        message=_msg("Alice", channel_id=42),
        thread=_thread("<#777>"),
    )
    assert "Alice" in out
    assert "<#42>" in out
    assert "<#777>" in out


# ── Auto-reactions survive; the status machine does not ──────────────────────
#
# Needle's reactions used to be two different things wearing the same clothes:
# a decorative `default_reactions` list, and a three-marker status machine the
# bot wrote, read back and swapped (🔵 open / ✅ archived / 🔒 locked). The
# machine was removed 2026-09-06 (migration 215) so that a reaction Needle adds
# never means anything — these tests pin both halves of that.


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_tables(c)
    yield c
    c.close()


def _upsert(conn, **over):
    kwargs = dict(
        guild_id=1, channel_id=2, title_type="first_fifty", custom_title="",
        include_bots=False, slowmode=0, delete_behavior="archive_if_empty",
        reply_type="default", custom_reply="", default_reactions="",
    )
    kwargs.update(over)
    _upsert_channel(conn, **kwargs)  # type: ignore[arg-type]
    return _get_channel_config(conn, kwargs["guild_id"], kwargs["channel_id"])


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("👍,👎", "👍,👎"),
        ("  👍 , 👎  ", "👍,👎"),   # padding trimmed
        ("👍,,👎", "👍,👎"),        # empty slots dropped
        ("", ""),                    # blank means no reactions
        (" , , ", ""),               # only-separators collapses to blank
    ],
)
def test_default_reactions_round_trip(conn, stored, expected):
    cfg = _upsert(conn, default_reactions=stored)
    assert cfg is not None
    assert cfg.default_reactions == expected


def test_channel_config_carries_no_status_fields(conn):
    """A stored channel has no status-reaction state left to read."""
    cfg = _upsert(conn, default_reactions="🔵")
    assert cfg is not None
    assert not hasattr(cfg, "status_reactions")
    assert not hasattr(cfg, "archive_immediately")
    # 🔵 as a *decorative* reaction is fine — it is only the machine that went.
    assert cfg.default_reactions == "🔵"


def test_needle_channels_table_has_no_status_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(needle_channels)")}
    assert "default_reactions" in cols
    assert "status_reactions" not in cols
    assert "archive_immediately" not in cols


def test_dataclasses_expose_only_the_surviving_dials():
    assert not {"status_reactions", "archive_immediately"} & set(
        NeedleChannelConfig.__dataclass_fields__
    )
    # The guild-wide config is now the reply template and nothing else — the
    # three emoji keys went with the machine.
    assert set(NeedleGlobalConfig.__dataclass_fields__) == {"default_reply"}


def test_status_reaction_handlers_are_gone():
    """Regression guard: the listeners that made a reaction load-bearing."""
    for gone in ("_on_thread_update", "_handle_thread_reply"):
        assert not hasattr(needle_cog.NeedleCog, gone), f"{gone} came back"


# ── Require an upload on every post (migration 246) ──────────────────────────
#
# Ben, 2026-09-29: members were typing ordinary chat into auto-threaded
# channels, and every line got its own thread. With the channel's dial on, a
# post with no attachment is removed before a thread exists. The dial ships
# off, and off must be exactly the old behaviour.

@pytest.mark.parametrize(
    ("require", "has_attachment", "is_bot", "is_mod", "expected"),
    [
        pytest.param(True, False, False, False, "remove", id="text-only-member-post-is-removed"),
        pytest.param(True, True, False, False, "thread", id="upload-with-or-without-caption-stays"),
        pytest.param(True, False, False, True, "thread", id="moderator-is-exempt"),
        pytest.param(True, False, True, False, "thread", id="bot-or-webhook-is-exempt"),
        pytest.param(True, False, True, True, "thread", id="both-exemptions"),
        pytest.param(False, False, False, False, "thread", id="dial-off-is-the-old-behaviour"),
        pytest.param(False, True, False, False, "thread", id="dial-off-with-upload"),
    ],
)
def test_starter_post_verdict(require, has_attachment, is_bot, is_mod, expected):
    assert (
        starter_post_verdict(
            require_attachment=require,
            has_attachment=has_attachment,
            author_is_bot=is_bot,
            author_can_manage_messages=is_mod,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("last", "now", "expected"),
    [
        pytest.param(None, 1000.0, True, id="never-told"),
        pytest.param(990.0, 1000.0, False, id="told-ten-seconds-ago"),
        pytest.param(
            1000.0 - TEXT_ONLY_NOTICE_COOLDOWN_SECONDS, 1000.0, True, id="cooldown-just-over"
        ),
    ],
)
def test_text_only_notice_cooldown(last, now, expected):
    assert should_post_text_only_notice(now=now, last_notice_at=last) is expected


def test_text_only_notice_names_only_the_poster():
    text = build_text_only_notice("<@42>")
    assert text.startswith("<@42> ")
    assert text.count("<@") == 1
    assert "thread" in text
    assert "caption" in text


def test_claiming_a_notice_is_per_member_and_per_channel():
    _TEXT_ONLY_NOTICE_AT.clear()
    try:
        assert _claim_text_only_notice(10, 1, 100.0) is True
        # The same member, a moment later: removed again, not told again.
        assert _claim_text_only_notice(10, 1, 105.0) is False
        # Someone else, and the same member elsewhere, are told.
        assert _claim_text_only_notice(10, 2, 105.0) is True
        assert _claim_text_only_notice(11, 1, 105.0) is True
        assert (
            _claim_text_only_notice(10, 1, 100.0 + TEXT_ONLY_NOTICE_COOLDOWN_SECONDS)
            is True
        )
    finally:
        _TEXT_ONLY_NOTICE_AT.clear()


def test_a_note_that_never_went_out_does_not_count_as_told():
    """Review finding, 2026-09-29: the cooldown was recorded before the send,
    so a failed send silenced the member's next note as well."""
    _TEXT_ONLY_NOTICE_AT.clear()
    try:
        assert _claim_text_only_notice(10, 1, 100.0) is True
        _release_text_only_notice(10, 1)
        assert _claim_text_only_notice(10, 1, 101.0) is True
        # Releasing a claim that was never made is harmless.
        _release_text_only_notice(10, 99)
    finally:
        _TEXT_ONLY_NOTICE_AT.clear()


@pytest.mark.parametrize(
    ("require", "can_manage", "expected"),
    [
        pytest.param(True, False, True, id="ticked-and-bot-cannot-delete"),
        pytest.param(True, True, False, id="ticked-and-bot-can-delete"),
        pytest.param(True, None, False, id="unknown-is-not-a-fault"),
        pytest.param(False, False, False, id="unticked-needs-no-permission"),
        pytest.param(False, None, False, id="unticked-and-unknown"),
    ],
)
def test_removal_blocked(require, can_manage, expected):
    assert (
        removal_blocked(require_attachment=require, bot_can_manage_messages=can_manage)
        is expected
    )


def _guild_with_channel(*, manage_messages: bool, me: object | None = "bot") -> Any:
    channel = SimpleNamespace(
        id=2,
        permissions_for=lambda _member: SimpleNamespace(manage_messages=manage_messages),
    )
    return SimpleNamespace(
        me=me, get_channel=lambda cid: channel if cid == 2 else None
    )


@pytest.mark.parametrize(
    ("guild", "channel_id", "expected"),
    [
        pytest.param(None, 2, None, id="bot-down"),
        pytest.param(_guild_with_channel(manage_messages=True), 3, None, id="channel-not-cached"),
        pytest.param(
            _guild_with_channel(manage_messages=True, me=None), 2, None, id="bot-member-missing"
        ),
        pytest.param(_guild_with_channel(manage_messages=True), 2, True, id="can-delete"),
        pytest.param(_guild_with_channel(manage_messages=False), 2, False, id="cannot-delete"),
    ],
)
def test_bot_can_manage_messages_reads_unknown_as_none(guild, channel_id, expected):
    assert _bot_can_manage_messages(guild, channel_id) is expected


def test_notice_memory_is_bounded():
    _TEXT_ONLY_NOTICE_AT.clear()
    try:
        for user_id in range(needle_cog._TEXT_ONLY_NOTICE_MAX + 25):
            _claim_text_only_notice(10, user_id, float(user_id))
        assert len(_TEXT_ONLY_NOTICE_AT) == needle_cog._TEXT_ONLY_NOTICE_MAX
        assert (10, 0) not in _TEXT_ONLY_NOTICE_AT  # oldest went first
    finally:
        _TEXT_ONLY_NOTICE_AT.clear()


def test_require_attachment_ships_off_and_round_trips(conn):
    cfg = _upsert(conn)
    assert cfg is not None
    assert cfg.require_attachment is False
    cfg = _upsert(conn, require_attachment=True)
    assert cfg is not None
    assert cfg.require_attachment is True
    # Saving the card again without the box ticked turns it back off.
    cfg = _upsert(conn, require_attachment=False)
    assert cfg is not None
    assert cfg.require_attachment is False


# ── Threads archive after: one of Discord's four durations, else one day ─────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(60, 60, id="1-hour"),
        pytest.param(1440, 1440, id="1-day"),
        pytest.param(4320, 4320, id="3-days"),
        pytest.param(10080, 10080, id="1-week"),
        pytest.param(0, 1440, id="zero"),
        pytest.param(90, 1440, id="not-a-discord-choice"),
        pytest.param(None, 1440, id="none"),
        pytest.param("1440", 1440, id="string-not-int"),
        pytest.param(-1440, 1440, id="negative"),
        pytest.param(True, 1440, id="bool-is-not-a-duration"),
    ],
)
def test_auto_archive_minutes_normalises(value, expected):
    assert auto_archive_minutes(value) == expected
    assert DEFAULT_AUTO_ARCHIVE_MINUTES == 1440


@pytest.mark.parametrize(
    ("archive_minutes", "slowmode", "expected"),
    [
        pytest.param(60, 0, {"auto_archive_duration": 60, "slowmode_delay": None}, id="1-hour-no-slowmode"),
        pytest.param(1440, 30, {"auto_archive_duration": 1440, "slowmode_delay": 30}, id="1-day-slowmode"),
        pytest.param(4320, 0, {"auto_archive_duration": 4320, "slowmode_delay": None}, id="3-days"),
        pytest.param(10080, 5, {"auto_archive_duration": 10080, "slowmode_delay": 5}, id="1-week"),
        pytest.param(90, 0, {"auto_archive_duration": 1440, "slowmode_delay": None}, id="bad-row-value-is-one-day"),
    ],
)
def test_thread_create_options_send_the_dial_to_discord(archive_minutes, slowmode, expected):
    """What the cog unpacks into create_thread is the channel's dial, clamped."""
    assert thread_create_options(archive_minutes=archive_minutes, slowmode=slowmode) == expected


def test_auto_archive_ships_at_one_day_and_round_trips(conn):
    cfg = _upsert(conn)
    assert cfg is not None
    assert cfg.auto_archive_minutes == 1440
    cfg = _upsert(conn, auto_archive_minutes=10080)
    assert cfg is not None
    assert cfg.auto_archive_minutes == 10080
    # A value Discord would reject is stored as one day, never as itself.
    cfg = _upsert(conn, auto_archive_minutes=90)
    assert cfg is not None
    assert cfg.auto_archive_minutes == 1440


def test_a_row_from_before_the_column_reads_as_off():
    """The cog can boot against a table the migration hasn't reached yet."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE needle_channels (guild_id INTEGER, channel_id INTEGER, "
        "title_type TEXT, custom_title TEXT, include_bots INTEGER, slowmode INTEGER, "
        "delete_behavior TEXT, reply_type TEXT, custom_reply TEXT, default_reactions TEXT)"
    )
    c.execute(
        "INSERT INTO needle_channels VALUES (1, 2, 'first_fifty', '', 0, 0, "
        "'archive_if_empty', 'default', '', '')"
    )
    cfg = _get_channel_config(c, 1, 2)
    c.close()
    assert cfg is not None
    assert cfg.require_attachment is False
    assert cfg.auto_archive_minutes == 1440
