"""Tests for confession service identity pool helpers."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.shared.anon_audit_service import (
    DEFAULT_RETENTION_DAYS,
    EVENT_CONFESSION_POSTED,
    FEATURE_CONFESSIONS,
    RETENTION_FOREVER,
    SECONDS_PER_DAY,
    insert_event,
    list_events,
    purge_expired,
    set_retention_days,
)
from bot_modules.confessions.service import (
    MAX_DISCORD_MESSAGE_LENGTH,
    MAX_EMBED_DESCRIPTION_LENGTH,
    PENDING_TTL_SECONDS,
    GuildConfig,
    _ANON_ADJECTIVES,
    _ANON_ANIMALS,
    _ANON_CIRCLES,
    _COLOR_POOL_SIZE,
    _NAME_POOL_SIZE,
    _OP_CIRCLE,
    anon_circle_from_index,
    anon_name_from_index,
    build_anon_reply,
    build_confession_embed,
    get_ephemeral_anon_identity,
    get_or_assign_anon_identity,
    enqueue_confession,
    get_config,
    get_pending_confession,
    pending_confession_count,
    pending_confessions,
    pop_pool_index,
    purge_expired_pending,
    purge_old_thread_posts,
    resolve_pending_confession,
    upsert_config,
)


def test_anon_name_from_index_first():
    assert anon_name_from_index(0) == f"{_ANON_ADJECTIVES[0]} {_ANON_ANIMALS[0]}"


def test_anon_name_from_index_last():
    last = len(_ANON_ADJECTIVES) * len(_ANON_ANIMALS) - 1
    assert anon_name_from_index(last) == f"{_ANON_ADJECTIVES[-1]} {_ANON_ANIMALS[-1]}"


def test_anon_name_from_index_second_row():
    n_animals = len(_ANON_ANIMALS)
    idx = n_animals + 3  # second adjective, fourth animal
    assert anon_name_from_index(idx) == f"{_ANON_ADJECTIVES[1]} {_ANON_ANIMALS[3]}"


def test_anon_circle_from_index_all():
    for i, circle in enumerate(_ANON_CIRCLES):
        assert anon_circle_from_index(i) == circle


def test_pop_pool_index_no_repeats_color(sync_db_path: Path):
    """Color pool yields all unique indices before repeating."""
    pool_size = _COLOR_POOL_SIZE
    seen: set[int] = set()
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size):
            idx = pop_pool_index(conn, guild_id=1, root_message_id=100, pool_type="color", pool_size=pool_size)
            assert idx not in seen, f"Duplicate index {idx} before pool exhausted"
            seen.add(idx)
    assert seen == set(range(pool_size))


def test_pop_pool_index_refills_after_exhaustion(sync_db_path: Path):
    """After pool is exhausted the next call returns a valid index."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size):
            pop_pool_index(conn, guild_id=1, root_message_id=200, pool_type="color", pool_size=pool_size)
        extra = pop_pool_index(conn, guild_id=1, root_message_id=200, pool_type="color", pool_size=pool_size)
    assert 0 <= extra < pool_size


def test_pop_pool_index_increments_cycle(sync_db_path: Path):
    """cycle column increments when pool refills."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size + 1):
            pop_pool_index(conn, guild_id=1, root_message_id=300, pool_type="color", pool_size=pool_size)
        row = conn.execute(
            "SELECT cycle FROM confession_pools WHERE guild_id=1 AND root_message_id=300 AND pool_type='color'"
        ).fetchone()
    assert row["cycle"] == 1


def test_pop_pool_index_separate_threads_independent(sync_db_path: Path):
    """Pools for different root_message_ids are fully independent."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        idx_a = pop_pool_index(conn, guild_id=1, root_message_id=400, pool_type="color", pool_size=pool_size)
        idx_b = pop_pool_index(conn, guild_id=1, root_message_id=500, pool_type="color", pool_size=pool_size)
    assert 0 <= idx_a < pool_size
    assert 0 <= idx_b < pool_size


def test_pop_pool_index_name_and_color_are_independent(sync_db_path: Path):
    """name and color pools for the same thread are stored as separate DB rows."""
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        n = pop_pool_index(conn, guild_id=1, root_message_id=600, pool_type="name", pool_size=_NAME_POOL_SIZE)
        c = pop_pool_index(conn, guild_id=1, root_message_id=600, pool_type="color", pool_size=_COLOR_POOL_SIZE)
        rows = conn.execute(
            "SELECT pool_type FROM confession_pools WHERE guild_id=1 AND root_message_id=600"
        ).fetchall()
    pool_types = {r["pool_type"] for r in rows}
    assert pool_types == {"name", "color"}
    assert 0 <= n < _NAME_POOL_SIZE
    assert 0 <= c < _COLOR_POOL_SIZE


def test_persistent_identity_stable(sync_db_path: Path):
    """Same user same thread always returns same (name_idx, emoji_idx)."""
    a = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1000, user_id=42)
    b = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1000, user_id=42)
    assert a == b


def test_persistent_identity_unique_per_user(sync_db_path: Path):
    """Two different users in the same thread get different name and color indices."""
    a = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1001, user_id=10)
    b = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1001, user_id=11)
    assert a[0] != b[0], "name_index should differ"
    assert a[1] != b[1], "emoji_index should differ"


def test_persistent_identity_valid_range(sync_db_path: Path):
    name_idx, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1002, user_id=99)
    assert 0 <= name_idx < _NAME_POOL_SIZE
    assert 0 <= emoji_idx < _COLOR_POOL_SIZE


def test_persistent_identity_writes_to_db(sync_db_path: Path):
    get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1003, user_id=77)
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT name_index, emoji_index FROM confession_emoji_assignments "
            "WHERE guild_id=1 AND root_message_id=1003 AND user_id=77"
        ).fetchone()
    assert row is not None
    assert row["name_index"] >= 0
    assert row["emoji_index"] >= 0


def test_persistent_identity_legacy_backfill(sync_db_path: Path):
    """Existing rows with name_index=-1 get a valid name_index on next call."""
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO confession_emoji_assignments (guild_id, root_message_id, user_id, emoji_index, name_index) "
            "VALUES (1, 9999, 55, 3, -1)"
        )
        conn.commit()
    name_idx, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=9999, user_id=55)
    assert name_idx >= 0
    assert emoji_idx == 3  # original emoji preserved


def test_ephemeral_identity_does_not_write_to_assignments(sync_db_path: Path):
    get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2000)
    with sqlite3.connect(str(sync_db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM confession_emoji_assignments WHERE guild_id=1 AND root_message_id=2000"
        ).fetchall()
    assert len(rows) == 0


def test_ephemeral_identity_valid_range(sync_db_path: Path):
    name_idx, emoji_idx = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2001)
    assert 0 <= name_idx < _NAME_POOL_SIZE
    assert 0 <= emoji_idx < _COLOR_POOL_SIZE


def test_ephemeral_identity_advances_pool(sync_db_path: Path):
    """Two consecutive ephemeral calls to the same thread return different indices."""
    a = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2002)
    b = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2002)
    assert a != b


def test_ephemeral_and_persistent_share_pool(sync_db_path: Path):
    """Ephemeral and persistent calls compete for the same color pool (no reuse within a cycle)."""
    pool_size = _COLOR_POOL_SIZE
    seen_colors: set[int] = set()
    root = 3000
    for user_id in range(pool_size // 2):
        _, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=root, user_id=user_id)
        seen_colors.add(emoji_idx)
    for _ in range(pool_size - pool_size // 2):
        _, emoji_idx = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=root)
        seen_colors.add(emoji_idx)
    assert seen_colors == set(range(pool_size))


def test_build_anon_reply_op():
    result = build_anon_reply("hello world", is_op=True)
    assert result.startswith(f"{_OP_CIRCLE} [OP]\n")
    assert "hello world" in result


def test_build_anon_reply_non_op():
    result = build_anon_reply("secret stuff", is_op=False, circle="🔴", anon_name="Brave Owl")
    assert result.startswith("🔴 Brave Owl\n")
    assert "secret stuff" in result


@pytest.mark.parametrize(
    "long_content",
    [
        pytest.param("x" * (MAX_DISCORD_MESSAGE_LENGTH + 100), id="plain-overflow"),
        # Worst case for the escape-then-fit bug: escape_markdown backslash-
        # prefixes every `*`, so this doubles in length. The raw content is
        # well under MAX_DISCORD_MESSAGE_LENGTH, but the *escaped* string
        # blows past it — the case a naive slice of the raw text would miss
        # entirely and ship over-cap.
        pytest.param("*" * MAX_DISCORD_MESSAGE_LENGTH, id="markdown-heavy-overflow"),
    ],
)
def test_build_anon_reply_truncates(long_content):
    result = build_anon_reply(long_content, is_op=False, circle="🔵", anon_name="Tiny Fox")
    assert len(result) <= MAX_DISCORD_MESSAGE_LENGTH, (
        f"message content= must fit Discord's {MAX_DISCORD_MESSAGE_LENGTH}-char cap"
    )
    # A naive slice of the already-escaped text can cut between a backslash
    # and the character it protects, leaving that backslash as the last
    # thing a member reads. trim_escaped drops it instead.
    assert not result.rstrip("…").endswith("\\")


def test_build_anon_reply_truncates_escapes_not_just_slices():
    """A future "simplification" that truncates the raw text and escapes
    afterward (or slices the escaped text with a plain `[:n]`) must fail
    here: it would either ship literal bold/italic markdown to every reader,
    or ship a dangling backslash. Guards both at once.
    """
    long_content = "*" * MAX_DISCORD_MESSAGE_LENGTH
    result = build_anon_reply(long_content, is_op=False, circle="🔵", anon_name="Tiny Fox")
    body = result.split("\n", 1)[1]
    # The asterisks really were escaped: every one that survived the trim is
    # backslash-prefixed, so none of them can pair up into Discord bold/italic.
    assert "\\*" in body
    assert "**" not in body


def test_build_confession_embed_trims_without_splitting_an_escape_pair():
    """The public card had the same plain slice the reply above was fixed for.

    ``safe`` is escaped *before* the cut, so ``safe[:4095]`` can land between
    a backslash and the character it protects — and the orphan then escapes
    the ``…`` appended immediately after it, so the marker vanishes from the
    one card the whole channel reads.
    """
    embed = build_confession_embed("*" * 5000, color=discord.Color.blurple())
    body = embed.description
    assert len(body) <= MAX_EMBED_DESCRIPTION_LENGTH
    assert "\\*" in body  # really escaped, not merely truncated
    assert not body.rstrip("…").endswith("\\")


def test_build_anon_reply_defangs_everyone():
    result = build_anon_reply("@everyone listen up", is_op=False, circle="🟢", anon_name="Quiet Yak")
    assert "@everyone" not in result
    assert "@​everyone" in result


# ── Audit trail vs operational TTL ────────────────────────────────────
#
# The confessions audit panel reads `anon_audit_log`, not `confession_threads`.
# These two lifetimes must stay independent: the operational table is purged at
# a fourteen-day TTL so thread identity and reply routing stay bounded, and before
# this split that TTL silently truncated the moderation record to a rolling
# week. Since the Discord mod-log channel became optional, the audit row is the
# only durable trace of who is behind an anonymous post.


def test_operational_purge_leaves_audit_trail_intact(sync_db_path: Path):
    guild_id, message_id = 4242, 777
    old = int(time.time()) - (30 * 86400)
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO confession_threads (guild_id, message_id, channel_id,"
            " root_message_id, original_author_id, notify_original_author, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, message_id, 100, message_id, 42, 1, old),
        )
        insert_event(
            conn,
            guild_id=guild_id,
            feature=FEATURE_CONFESSIONS,
            event=EVENT_CONFESSION_POSTED,
            actor_id=42,
            message_id=message_id,
            created_at=old,
        )

    removed = purge_old_thread_posts(sync_db_path)

    assert removed == 1
    with open_db(sync_db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM confession_threads WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0] == 0
        # The moderator-facing record outlives it.
        surviving = list_events(conn, guild_id, feature=FEATURE_CONFESSIONS)
        assert [e.actor_id for e in surviving] == [42]


def test_audit_row_expires_on_its_own_retention_window(sync_db_path: Path):
    """Confessions rows are purged by the anon-audit window like any other."""
    guild_id = 4242
    stale = time.time() - (DEFAULT_RETENTION_DAYS + 1) * SECONDS_PER_DAY
    fresh = time.time()
    with open_db(sync_db_path) as conn:
        for ts in (stale, fresh):
            insert_event(
                conn,
                guild_id=guild_id,
                feature=FEATURE_CONFESSIONS,
                event=EVENT_CONFESSION_POSTED,
                actor_id=42,
                created_at=ts,
            )

    purge_expired(sync_db_path)

    with open_db(sync_db_path) as conn:
        remaining = list_events(conn, guild_id, feature=FEATURE_CONFESSIONS)
    assert len(remaining) == 1
    assert remaining[0].created_at == pytest.approx(fresh)


def test_retention_off_keeps_confessions_forever(sync_db_path: Path):
    """`0` disables purging — the guild's opt-out of a bounded audit window."""
    guild_id = 4242
    ancient = time.time() - (10 * 365 * SECONDS_PER_DAY)
    with open_db(sync_db_path) as conn:
        set_retention_days(conn, guild_id, RETENTION_FOREVER)
        insert_event(
            conn,
            guild_id=guild_id,
            feature=FEATURE_CONFESSIONS,
            event=EVENT_CONFESSION_POSTED,
            actor_id=42,
            created_at=ancient,
        )

    purge_expired(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert len(list_events(conn, guild_id, feature=FEATURE_CONFESSIONS)) == 1


# ── config round-trip ──────────────────────────────────────────────────


def test_config_round_trips_without_an_attachment_dial(sync_db_path: Path):
    """Confessions are submitted through a Discord modal, which has no
    attachment path at all — so there is no attachment cap to store. The
    column survives on live servers; nothing loads or writes it, and the
    upsert still round-trips every setting that is real."""
    from bot_modules.confessions import service as cs

    assert not hasattr(cs.GuildConfig, "max_attachments")
    with open_db(sync_db_path) as conn:
        cs._create_tables(conn)
    cs.upsert_config(
        sync_db_path,
        cs.GuildConfig(
            guild_id=77, dest_channel_id=11, log_channel_id=22,
            cooldown_seconds=90, max_chars=500, per_day_limit=3,
            replies_enabled=False, blocked_user_ids=[5],
        ),
    )
    loaded = cs.get_config(sync_db_path, 77)
    assert loaded is not None
    assert (loaded.cooldown_seconds, loaded.max_chars, loaded.per_day_limit) == (90, 500, 3)
    assert loaded.replies_enabled is False
    assert loaded.blocked_set() == {5}
    assert not hasattr(loaded, "max_attachments")


# ── mod-approve mode: the approval queue ──────────────────────────────
#
# `confession_pending` is the only table in the feature that holds a
# confession's text beside its author's real id, so the tests below are as
# much about what it *stops* holding as what it stores.

GUILD = 4242


def _queue(db_path: Path, content="I ate the last biscuit", *, author_id=99, notify=1):
    return enqueue_confession(
        db_path,
        guild_id=GUILD,
        author_id=author_id,
        content=content,
        notify_original_author=notify,
    )


def test_require_approval_round_trips_through_the_config(sync_db_path: Path):
    upsert_config(
        sync_db_path,
        GuildConfig(guild_id=GUILD, dest_channel_id=1, log_channel_id=0,
                    require_approval=True),
    )
    cfg = get_config(sync_db_path, GUILD)
    assert cfg is not None and cfg.require_approval is True


def test_approval_is_off_for_a_guild_that_never_set_it(sync_db_path: Path):
    """The dial ships dark: an existing guild keeps posting straight through."""
    upsert_config(
        sync_db_path,
        GuildConfig(guild_id=GUILD, dest_channel_id=1, log_channel_id=0),
    )
    cfg = get_config(sync_db_path, GUILD)
    assert cfg is not None and cfg.require_approval is False


def test_a_queued_confession_is_listed_for_its_guild(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    with open_db(sync_db_path) as conn:
        rows = pending_confessions(conn, GUILD)
        assert [r["id"] for r in rows] == [pending_id]
        assert rows[0]["content"] == "I ate the last biscuit"
        assert pending_confession_count(conn, GUILD) == 1


def test_the_queue_never_hands_back_an_author_id(sync_db_path: Path):
    """The privacy seam, at the layer that decides it.

    The board and the picker are moderator surfaces; naming a confession's
    author is admin-only, on the audit panel. Nothing on the approval path is
    given an id it could print, so no renderer downstream can leak one by
    accident.
    """
    pending_id = _queue(sync_db_path, author_id=1234567890)
    with open_db(sync_db_path) as conn:
        listed = pending_confessions(conn, GUILD)
        one = get_pending_confession(conn, GUILD, pending_id)
    assert "author_id" not in listed[0]
    assert one is not None and "author_id" not in one


def test_the_queue_is_scoped_to_one_guild(sync_db_path: Path):
    _queue(sync_db_path)
    enqueue_confession(
        sync_db_path, guild_id=GUILD + 1, author_id=7, content="elsewhere",
        notify_original_author=0,
    )
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 1
        assert [r["content"] for r in pending_confessions(conn, GUILD)] == [
            "I ate the last biscuit"
        ]


def test_the_queue_is_worked_oldest_first(sync_db_path: Path):
    first = _queue(sync_db_path, "first")
    second = _queue(sync_db_path, "second")
    with open_db(sync_db_path) as conn:
        assert [r["id"] for r in pending_confessions(conn, GUILD)] == [first, second]


def test_a_confession_from_another_guild_cannot_be_opened(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert get_pending_confession(conn, GUILD + 1, pending_id) is None


def test_resolving_returns_the_author_and_the_body(sync_db_path: Path):
    """The one call that *does* return an author id — it goes straight to the
    poster or the rejection DM and is never rendered."""
    pending_id = _queue(sync_db_path, author_id=555, notify=1)
    row = resolve_pending_confession(sync_db_path, GUILD, pending_id)
    assert row is not None
    assert row["author_id"] == 555
    assert row["content"] == "I ate the last biscuit"
    assert row["notify_original_author"] == 1


def test_resolving_deletes_the_row(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    resolve_pending_confession(sync_db_path, GUILD, pending_id)
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 0


def test_a_confession_can_only_be_resolved_once(sync_db_path: Path):
    """Two mods pressing Approve together must not post the same confession
    twice. The claim is the delete, so the loser gets nothing to act on."""
    pending_id = _queue(sync_db_path)
    assert resolve_pending_confession(sync_db_path, GUILD, pending_id) is not None
    assert resolve_pending_confession(sync_db_path, GUILD, pending_id) is None


def test_resolving_is_guild_scoped(sync_db_path: Path):
    pending_id = _queue(sync_db_path)
    assert resolve_pending_confession(sync_db_path, GUILD + 1, pending_id) is None
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 1


def test_an_unreviewed_confession_is_swept_after_a_week(sync_db_path: Path):
    """Not housekeeping: the privacy notice promises the confession-to-author
    link self-destructs in seven days, and a queue nobody works must not
    become the exception."""
    pending_id = _queue(sync_db_path, author_id=555)
    with open_db(sync_db_path) as conn:
        conn.execute(
            "UPDATE confession_pending SET created_at = ? WHERE id = ?",
            (int(time.time()) - PENDING_TTL_SECONDS - 60, pending_id),
        )

    dropped = purge_expired_pending(sync_db_path)

    assert [(r["id"], r["author_id"], r["guild_id"]) for r in dropped] == [
        (pending_id, 555, GUILD)
    ]
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 0


def test_the_sweep_leaves_a_confession_inside_the_window(sync_db_path: Path):
    _queue(sync_db_path)
    assert purge_expired_pending(sync_db_path) == []
    with open_db(sync_db_path) as conn:
        assert pending_confession_count(conn, GUILD) == 1


def test_the_sweep_reports_nothing_when_the_queue_is_empty(sync_db_path: Path):
    assert purge_expired_pending(sync_db_path) == []


# ── the alias map has a clock now (anon-tail-69, migration 211) ──

from bot_modules.confessions.service import (  # noqa: E402
    THREAD_METADATA_TTL_SECONDS,
    clear_anon_identities,
    upsert_thread_post,
)


def _alias_rows(db_path: Path) -> list[tuple[int, int]]:
    with open_db(db_path) as conn:
        return [
            (int(r["root_message_id"]), int(r["user_id"]))
            for r in conn.execute(
                "SELECT root_message_id, user_id FROM confession_emoji_assignments ORDER BY 1, 2"
            )
        ]


def test_an_alias_is_stamped_when_assigned(sync_db_path: Path):
    before = int(time.time())
    get_or_assign_anon_identity(sync_db_path, GUILD, 500, 7)
    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT created_at FROM confession_emoji_assignments WHERE root_message_id = 500"
        ).fetchone()
    assert int(row["created_at"]) >= before


def test_the_ttl_sweep_takes_stale_aliases_with_the_threads(sync_db_path: Path):
    """445 thread-less alias rows had piled up in prod, each still naming a
    member beside a pseudonym: the sweep never reached the table because it
    had no timestamp to reach it by."""
    get_or_assign_anon_identity(sync_db_path, GUILD, 500, 7)   # fresh: stays
    get_or_assign_anon_identity(sync_db_path, GUILD, 600, 8)   # old: goes
    upsert_thread_post(sync_db_path, GUILD, 601, 10, 600, 8)
    stale = int(time.time()) - THREAD_METADATA_TTL_SECONDS - 60
    with open_db(sync_db_path) as conn:
        conn.execute(
            "UPDATE confession_emoji_assignments SET created_at = ? WHERE root_message_id = 600", (stale,)
        )
        conn.execute("UPDATE confession_threads SET created_at = ? WHERE root_message_id = 600", (stale,))

    assert purge_old_thread_posts(sync_db_path) == 2  # one thread row, one alias row
    assert _alias_rows(sync_db_path) == [(500, 7)]


def test_an_unstamped_alias_is_never_swept(sync_db_path: Path):
    """A row still at 0 is one we know nothing about, not an old one.

    Migration 211 backfills every pre-existing row, but the sweep must not
    lean on that: purging a zero-stamp alias hands the member a new name and
    colour mid-thread (the first cut of 211 did exactly that to every alias
    on a thread still inside its week)."""
    get_or_assign_anon_identity(sync_db_path, GUILD, 500, 7)
    upsert_thread_post(sync_db_path, GUILD, 500, 10, 500, 7)
    with open_db(sync_db_path) as conn:
        conn.execute("UPDATE confession_emoji_assignments SET created_at = 0")
    assert purge_old_thread_posts(sync_db_path) == 0
    assert _alias_rows(sync_db_path) == [(500, 7)]


def test_clear_anon_identities_drops_only_the_named_roots(sync_db_path: Path):
    get_or_assign_anon_identity(sync_db_path, GUILD, 500, 7)
    get_or_assign_anon_identity(sync_db_path, GUILD, 500, 8)
    get_or_assign_anon_identity(sync_db_path, GUILD, 600, 7)
    get_or_assign_anon_identity(sync_db_path, GUILD + 1, 500, 7)  # another guild, same root id

    assert clear_anon_identities(sync_db_path, GUILD, [500]) == 2
    assert _alias_rows(sync_db_path) == [(500, 7), (600, 7)]
    with open_db(sync_db_path) as conn:
        pools = conn.execute(
            "SELECT COUNT(*) FROM confession_pools WHERE guild_id = ? AND root_message_id = 500", (GUILD,)
        ).fetchone()[0]
    assert pools == 0
    assert clear_anon_identities(sync_db_path, GUILD, []) == 0


# ── typed-reply DMs (migration 222) ──────────────────────────────────

from bot_modules.confessions.service import (  # noqa: E402
    get_thread_info,
    get_thread_info_by_discord_thread,
    update_discord_thread_id,
)


def test_thread_ttl_is_fourteen_days_and_is_the_end_of_service():
    """Replies to a confession older than this notify nobody — accepted
    2026-09-12. Changing it changes a figure manual.html states to members."""
    assert THREAD_METADATA_TTL_SECONDS == 14 * 24 * 60 * 60
    assert PENDING_TTL_SECONDS == 7 * 24 * 60 * 60  # pending holds text; stays a week


def test_a_confession_is_found_by_its_discord_thread(sync_db_path: Path):
    upsert_thread_post(sync_db_path, GUILD, 500, 10, 500, 42, 1)
    upsert_thread_post(sync_db_path, GUILD, 501, 20, 500, 43, 0)  # a reply row
    update_discord_thread_id(sync_db_path, GUILD, 500, 20)

    assert get_thread_info_by_discord_thread(sync_db_path, GUILD, 20) == (500, 42, 1)
    assert get_thread_info_by_discord_thread(sync_db_path, GUILD + 1, 20) is None
    assert get_thread_info_by_discord_thread(sync_db_path, GUILD, 21) is None
    assert get_thread_info_by_discord_thread(sync_db_path, GUILD, 0) is None


def test_a_stamped_reply_row_is_never_taken_for_the_root(sync_db_path: Path):
    upsert_thread_post(sync_db_path, GUILD, 501, 20, 500, 43, 1)
    with open_db(sync_db_path) as conn:
        conn.execute("UPDATE confession_threads SET discord_thread_id = 20 WHERE message_id = 501")
    assert get_thread_info_by_discord_thread(sync_db_path, GUILD, 20) is None


def test_after_the_ttl_a_reply_resolves_to_nobody(sync_db_path: Path):
    upsert_thread_post(sync_db_path, GUILD, 500, 10, 500, 42, 1)
    update_discord_thread_id(sync_db_path, GUILD, 500, 20)
    with open_db(sync_db_path) as conn:
        conn.execute(
            "UPDATE confession_threads SET created_at = ?",
            (int(time.time()) - THREAD_METADATA_TTL_SECONDS - 60,),
        )
    purge_old_thread_posts(sync_db_path)
    assert get_thread_info_by_discord_thread(sync_db_path, GUILD, 20) is None
    assert get_thread_info(sync_db_path, GUILD, 500) is None


def test_reply_dials_round_trip_with_their_defaults(sync_db_path: Path):
    upsert_config(sync_db_path, GuildConfig(guild_id=GUILD, dest_channel_id=1, log_channel_id=0))
    cfg = get_config(sync_db_path, GUILD)
    assert cfg is not None
    assert (cfg.notify_op_on_reply, cfg.notify_op_on_typed_reply) == (True, False)

    cfg.notify_op_on_reply, cfg.notify_op_on_typed_reply = False, True
    upsert_config(sync_db_path, cfg)
    cfg = get_config(sync_db_path, GUILD)
    assert cfg is not None
    assert (cfg.notify_op_on_reply, cfg.notify_op_on_typed_reply) == (False, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["confession", "reply"])
async def test_log_cards_name_the_author_never_mention_them(which):
    """The admin log names the author on purpose — as a name plus id, never a
    <@id>, which renders as a bare number to an admin who hasn't cached them."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot_modules.confessions.service import log_confession, log_reply

    channel = SimpleNamespace(send=AsyncMock())
    author = SimpleNamespace(id=4242, display_name="Anon*Author", mention="<@4242>")
    if which == "confession":
        await log_confession(
            log_channel=channel, author=author, guild_id=1,
            dest_channel_id=2, dest_message_id=3, content="hi",
        )
    else:
        await log_reply(
            log_channel=channel, author=author, guild_id=1,
            parent_channel_id=2, parent_message_id=3,
            reply_channel_id=4, reply_message_id=5, content="hi",
        )
    embed = channel.send.await_args.kwargs["embed"]
    field = next(f for f in embed.fields if f.name == "Author")
    assert field.value == "Anon\\*Author (`4242`)"


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["confession", "reply"])
async def test_log_cards_escape_content_before_fitting_to_the_field_cap(which):
    """The Content field renders markdown exactly like the Author field above
    it, so member-authored text must be escaped there too (services-misc
    re-audit) -- and escaped *before* the 1024-char field cap is applied,
    since escape_markdown can double the string and Discord measures the
    escaped length."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot_modules.core.branding import FIELD_VALUE_LIMIT
    from bot_modules.confessions.service import log_confession, log_reply

    channel = SimpleNamespace(send=AsyncMock())
    author = SimpleNamespace(id=4242, display_name="Author", mention="<@4242>")
    raw = "**URGENT** " + ("*" * 1200)
    if which == "confession":
        await log_confession(
            log_channel=channel, author=author, guild_id=1,
            dest_channel_id=2, dest_message_id=3, content=raw,
        )
    else:
        await log_reply(
            log_channel=channel, author=author, guild_id=1,
            parent_channel_id=2, parent_message_id=3,
            reply_channel_id=4, reply_message_id=5, content=raw,
        )
    embed = channel.send.await_args.kwargs["embed"]
    field = next(f for f in embed.fields if f.name == "Content")
    # Escaped, not raw markdown.
    assert field.value.startswith("\\*\\*URGENT\\*\\*")
    # Fitted to the escaped length, not the pre-escape 1024-char slice that
    # would have overshot the field cap once every `*` doubled.
    assert len(field.value) <= FIELD_VALUE_LIMIT


# ---------------------------------------------------------------------------
# The rate-limit clock's retention (bug #222, item 4)
# ---------------------------------------------------------------------------


def test_the_sweep_takes_a_stale_rate_limit_row(sync_db_path: Path):
    """`last_confess_at` says this member posted an anonymous confession at a
    given moment — the one thing that correlates them against a confession
    still visible in the channel — and the row was kept indefinitely because
    it holds no text. It now ages out with the rest of the store."""
    stale = int(time.time()) - int(THREAD_METADATA_TTL_SECONDS) - 60
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO confession_rate_limits "
            "(guild_id, author_id, last_confess_at, last_reply_at, day_key, day_count) "
            "VALUES (4242, 7, ?, 0, '2026-01-01', 3)",
            (stale,),
        )

    assert purge_old_thread_posts(sync_db_path) == 1
    with open_db(sync_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM confession_rate_limits").fetchone()[0] == 0


def test_the_sweep_leaves_a_rate_limit_row_still_doing_its_job(sync_db_path: Path):
    """The cooldown and the daily cap are what the row is for, so a recent one
    stays — sweeping it would hand the member a fresh allowance."""
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO confession_rate_limits "
            "(guild_id, author_id, last_confess_at, last_reply_at, day_key, day_count) "
            "VALUES (4242, 7, ?, 0, '2026-09-17', 3)",
            (int(time.time()),),
        )

    assert purge_old_thread_posts(sync_db_path) == 0
    with open_db(sync_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM confession_rate_limits").fetchone()[0] == 1


def test_a_reply_only_member_is_measured_on_the_reply_clock(sync_db_path: Path):
    """Somebody who has only ever replied has `last_confess_at` at 0, so a
    sweep keyed on that column alone would take a live row."""
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO confession_rate_limits "
            "(guild_id, author_id, last_confess_at, last_reply_at, day_key, day_count) "
            "VALUES (4242, 7, 0, ?, '2026-09-17', 0)",
            (int(time.time()),),
        )

    assert purge_old_thread_posts(sync_db_path) == 0
    with open_db(sync_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM confession_rate_limits").fetchone()[0] == 1
