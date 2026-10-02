"""Fallback-chain tests for embed-safe display-name resolution.

A ``<@id>`` inside an embed is resolved client-side only, so it renders as a
bare numeric id for any viewer who hasn't cached that user. ``resolve_name_from``
replaces it with real text; these tests pin each rung of the chain it walks:

    live member cache -> known_users -> <@id>

The live cache goes first because ``intents.members`` is on, making it complete
and nickname-fresh for present members, while a ``known_users`` row is only as
fresh as that user's last recorded activity. The table then covers the case the
cache structurally cannot: members who have left the guild.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.shared.message_store import (
    get_known_user_names_bulk,
    init_known_users_table,
    upsert_known_user,
)
from bot_modules.shared.name_resolver import (
    build_name_fn,
    mention,
    mentioned_user_ids,
    named_or_anonymous,
    resolve_mentions,
    resolve_mentions_from,
    resolve_name_from,
)
from tests.fakes import FakeChannel, FakeGuild, FakeMember, FakeRole

GUILD_ID = 999


def _guild(members: dict[int, str] | None = None) -> FakeGuild:
    """A stand-in for the gateway-maintained member cache, keyed id -> name."""
    return FakeGuild(
        id=GUILD_ID,
        members={
            uid: FakeMember(id=uid, display_name=name)
            for uid, name in (members or {}).items()
        },
    )


# ── The chain, rung by rung ──────────────────────────────────────────────────


def test_member_cache_hit_wins():
    guild = _guild({7: "Nickname"})
    assert resolve_name_from(7, guild=guild, table_names={7: "StaleName"}) == "Nickname"


def test_member_cache_beats_a_stale_table_row():
    """The ordering decision, stated as a test.

    known_users only updates on activity, so a member who renamed and hasn't
    posted since has a stale row. The live cache must win.
    """
    guild = _guild({7: "NewName"})
    assert resolve_name_from(7, guild=guild, table_names={7: "OldName"}) == "NewName"


def test_table_covers_a_departed_member():
    """No member object — left the guild — but we still have a name on file."""
    guild = _guild({})
    assert resolve_name_from(7, guild=guild, table_names={7: "Departed"}) == "Departed"


def test_unknown_user_falls_back_to_mention():
    guild = _guild({})
    assert resolve_name_from(7, guild=guild, table_names={}) == "<@7>"


def test_no_guild_at_all_still_uses_the_table():
    """DM / uncached-guild contexts pass guild=None."""
    assert resolve_name_from(7, guild=None, table_names={7: "FromTable"}) == "FromTable"


def test_no_guild_and_no_table_is_a_mention():
    assert resolve_name_from(7, guild=None) == "<@7>"


@pytest.mark.parametrize(
    "blank",
    ["", "   ", "\t"],
    ids=["empty", "spaces", "tab"],
)
def test_blank_names_are_treated_as_misses(blank):
    """A whitespace-only name must not render as an invisible label."""
    guild = _guild({7: blank})
    assert resolve_name_from(7, guild=guild, table_names={7: blank}) == "<@7>"


def test_blank_member_name_falls_through_to_the_table():
    guild = _guild({7: "  "})
    assert resolve_name_from(7, guild=guild, table_names={7: "Real"}) == "Real"


# ── Markdown safety ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("*Star*", r"\*Star\*"),
        ("_Under_", r"\_Under\_"),
        ("**Bold**", r"\*\*Bold\*\*"),
        ("~~Strike~~", r"\~\~Strike\~\~"),
        ("`code`", r"\`code\`"),
    ],
    ids=["asterisk", "underscore", "bold", "strike", "backtick"],
)
def test_markdown_in_a_name_is_escaped(raw, expected):
    """An unescaped name would otherwise reformat the surrounding embed copy."""
    guild = _guild({7: raw})
    assert resolve_name_from(7, guild=guild) == expected


def test_markdown_escaped_on_the_table_rung_too():
    assert resolve_name_from(7, guild=None, table_names={7: "*x*"}) == r"\*x\*"


# ── The DB-backed prefetch ───────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "names.db"
    with open_db(path) as conn:
        init_known_users_table(conn)
        upsert_known_user(
            conn, GUILD_ID, 100, "left_user", "Departed Dan", ts=1.0,
            current_member=False,
        )
        upsert_known_user(
            conn, GUILD_ID, 101, "name_only", "", ts=1.0,
        )
        upsert_known_user(
            conn, GUILD_ID, 102, "other_guild", "Wrong Guild", ts=1.0,
        )
    return path


def test_bulk_lookup_falls_back_to_username(db_path):
    """A row with a blank display_name still yields the username."""
    with open_db(db_path) as conn:
        got = get_known_user_names_bulk(conn, GUILD_ID, [100, 101])
    assert got == {100: "Departed Dan", 101: "name_only"}


def test_bulk_lookup_is_guild_scoped(db_path):
    with open_db(db_path) as conn:
        assert get_known_user_names_bulk(conn, GUILD_ID + 1, [102]) == {}


def test_bulk_lookup_empty_input_makes_no_query(db_path):
    with open_db(db_path) as conn:
        assert get_known_user_names_bulk(conn, GUILD_ID, []) == {}


@pytest.mark.asyncio
async def test_build_name_fn_resolves_present_and_departed(db_path):
    """End to end: a present member from cache, a departed one from the table."""
    guild = _guild({5: "Present Pat"})
    name_fn = await build_name_fn(
        guild=guild, db_path=db_path, guild_id=GUILD_ID, user_ids=[5, 100],
    )
    assert name_fn(5) == "Present Pat"
    assert name_fn(100) == "Departed Dan"


@pytest.mark.asyncio
async def test_build_name_fn_uses_username_when_display_name_is_blank(db_path):
    """The username rung is escaped like any other — hence ``name\\_only``.

    Usernames commonly contain underscores, so this rung is exactly where an
    unescaped name would silently italicise the rest of an embed line.
    """
    guild = _guild({})
    name_fn = await build_name_fn(
        guild=guild, db_path=db_path, guild_id=GUILD_ID, user_ids=[101],
    )
    assert name_fn(101) == r"name\_only"


@pytest.mark.asyncio
async def test_build_name_fn_mentions_a_wholly_unknown_id(db_path):
    guild = _guild({})
    name_fn = await build_name_fn(
        guild=guild, db_path=db_path, guild_id=GUILD_ID, user_ids=[404],
    )
    assert name_fn(404) == "<@404>"


@pytest.mark.asyncio
async def test_build_name_fn_skips_the_query_when_cache_covers_everyone(db_path):
    """Present members cost no DB read at all — the cache answers first."""
    guild = _guild({5: "Pat", 6: "Sam"})
    name_fn = await build_name_fn(
        guild=guild, db_path=db_path, guild_id=GUILD_ID, user_ids=[5, 6],
    )
    # A bogus path would raise if the prefetch had tried to open the DB.
    name_fn_no_db = await build_name_fn(
        guild=guild, db_path=db_path.parent / "missing" / "nope.db",
        guild_id=GUILD_ID, user_ids=[5, 6],
    )
    assert name_fn(5) == "Pat"
    assert name_fn_no_db(6) == "Sam"


# ── Mentions inside free text ─────────────────────────────────────────────
#
# A sponsored QOTD is member-written and can carry its own mentions. It is
# drawn into an image (where nothing resolves) and shown in embeds (where a
# mention is a bare id to most readers), so every kind becomes plain text.


def _text_guild() -> FakeGuild:
    return FakeGuild(
        id=GUILD_ID,
        members={5: FakeMember(id=5, display_name="Pat_Q")},
        roles={
            7: FakeRole(id=7, name="Mods"),
            GUILD_ID: FakeRole(id=GUILD_ID, name="@everyone"),
        },
        channels={8: FakeChannel(id=8, name="general")},
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("In honor of <@5>'s ping", r"In honor of @Pat\_Q's ping", id="user"),
        pytest.param("<@!5>", r"@Pat\_Q", id="nickname-form-user"),
        pytest.param("<@100>", "@Departed Dan", id="departed-user-from-table"),
        pytest.param("<@404>", "@unknown member", id="unknown-user"),
        pytest.param("<@&7>", "@Mods", id="role"),
        pytest.param(f"<@&{GUILD_ID}>", "@everyone", id="everyone-role-no-double-at"),
        pytest.param("<@&404>", "@unknown role", id="unknown-role"),
        pytest.param("<#8>", "#general", id="channel"),
        pytest.param("<#404>", "#unknown channel", id="unknown-channel"),
        pytest.param("no mentions *here*", "no mentions *here*", id="author-markdown-kept"),
        pytest.param("<:wave:123> <@5>", r"<:wave:123> @Pat\_Q", id="custom-emoji-untouched"),
    ],
)
def test_mentions_in_text_resolve(text, expected):
    got = resolve_mentions_from(
        text, guild=_text_guild(), table_names={100: "Departed Dan"}
    )
    assert got == expected


def test_unescaped_mode_is_for_images_and_the_dashboard():
    """A backslash would be drawn on the card literally."""
    assert resolve_mentions_from("<@5>", guild=_text_guild(), markdown=False) == "@Pat_Q"


def test_no_guild_hides_every_id():
    got = resolve_mentions_from("<@5> <@&7> <#8>", guild=None)
    assert got == "@unknown member @unknown role #unknown channel"


def test_only_user_mentions_feed_the_prefetch():
    assert mentioned_user_ids("<@5> <@!6> <@&7> <#8>") == [5, 6]


def test_an_unwired_builder_leaves_the_text_as_written():
    """The ``mention`` default knows no guild — its output stays pre-resolver."""
    assert resolve_mentions("hi <@5>", mention) == "hi <@5>"


@pytest.mark.asyncio
async def test_build_name_fn_resolves_text_with_the_prefetch(db_path):
    text = "Remember <@100>? Ask <@5>."
    names = await build_name_fn(
        guild=_guild({5: "Pat"}), db_path=db_path, guild_id=GUILD_ID,
        user_ids=mentioned_user_ids(text),
    )
    assert resolve_mentions(text, names) == "Remember @Departed Dan? Ask @Pat."


# ── named_or_anonymous ────────────────────────────────────────────────
#
# A user id of 0 is not an unknown member but the *absence* of one: an erasure
# detaching a still-running purchase from whoever bought it. Resolving it
# would render the literal <@0>.


def test_a_real_id_is_resolved_normally():
    assert named_or_anonymous(5, lambda uid: f"user-{uid}") == "user-5"


def test_a_detached_purchase_names_no_one():
    assert named_or_anonymous(0, lambda uid: f"user-{uid}") == "a member"


def test_the_resolver_is_never_called_for_a_detached_purchase():
    """Otherwise the embed reads '<@0>', which links to nothing."""
    calls = []

    def _resolver(uid):
        calls.append(uid)
        return f"user-{uid}"

    assert named_or_anonymous(0, _resolver) == "a member"
    assert calls == []


def test_the_fallback_wording_is_the_callers_to_choose():
    assert named_or_anonymous(0, str, fallback="someone") == "someone"
