"""``games/utils/names`` — the shared resolver the party games name players
through: the live cache, then ``known_users`` (a member who has left), then
``User <id>`` for a true miss."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bot_modules.games.utils import names as game_names
from bot_modules.shared.message_store import init_known_users_table
from bot_modules.shared.name_resolver import GuildNames, mention


class _Guild:
    id = 5

    def __init__(self, members: dict[int, str]):
        self._m = {k: SimpleNamespace(display_name=v) for k, v in members.items()}

    def get_member(self, uid):
        return self._m.get(uid)


def _bot(db_path):
    return SimpleNamespace(ctx=SimpleNamespace(db_path=db_path))


def test_plain_name_is_unescaped_and_a_true_miss_reads_user_id():
    resolver = GuildNames(guild=_Guild({1: "Ann_"}), table_names={2: "Gone*"})
    assert game_names.plain_name(resolver, 1) == "Ann_"
    assert game_names.plain_name(resolver, 2) == "Gone*"
    assert game_names.plain_name(resolver, 3) == "User 3"
    assert game_names.plain_name(resolver, "oops") == "User oops"


def test_resolved_name_turns_the_resolvers_mention_miss_into_user_id():
    resolver = GuildNames(guild=_Guild({}), table_names={})
    assert resolver(9) == mention(9)
    assert game_names.resolved_name(resolver, 9) == "User 9"


async def test_build_names_reads_known_users_for_a_member_the_cache_misses(tmp_path: Path):
    from bot_modules.core.db_utils import open_db  # noqa: PLC0415

    db_path = tmp_path / "t.db"
    with open_db(db_path) as conn:
        init_known_users_table(conn)
        conn.execute(
            "INSERT INTO known_users (guild_id, user_id, username, display_name) VALUES (5, 2, 'gone', 'Gone Girl')"
        )
        conn.commit()
    resolver = await game_names.build_names(_bot(db_path), _Guild({1: "Ann"}), [1, 2, 3, "bad"])
    assert game_names.plain_name(resolver, 1) == "Ann"
    assert game_names.plain_name(resolver, 2) == "Gone Girl"
    assert game_names.plain_name(resolver, 3) == "User 3"


async def test_build_names_with_nobody_to_name_touches_no_database():
    class _NoCtx:
        @property
        def ctx(self):
            raise AssertionError("no ids, no db")

    resolver = await game_names.build_names(_NoCtx(), _Guild({}), [])
    assert isinstance(resolver, GuildNames)


async def test_build_names_with_no_guild_is_cache_only_and_touches_no_database():
    class _NoCtx:
        @property
        def ctx(self):
            raise AssertionError("no guild, no db")

    resolver = await game_names.build_names(_NoCtx(), None, [1, 2])
    assert game_names.plain_name(resolver, 1) == "User 1"
