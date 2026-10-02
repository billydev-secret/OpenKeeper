"""How a game card names a member.

Two resolvers reach a game's builders, and both miss on a member the bot
has not cached: ``game_manager.resolve_name`` hands back the bare id as a
string, and a ``name_fn`` from ``shared.name_resolver.build_name_fn`` hands
back ``<@id>``, which the *reading* client renders from its own cache — a
raw number for anyone who has not seen that member either. A card shows
neither: a member nobody can name reads ``User <id>``
(``docs/components_v2_style_guide.md`` § Mentions and names), the same
plain form a no-contact pair degrades to on purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from bot_modules.shared.name_resolver import GuildNames, build_name_fn, mention


def unknown(user_id: Any) -> str:
    return f"User {user_id}"


async def build_names(bot: Any, guild: Any, user_ids: Iterable[Any]) -> GuildNames:
    """A ``build_name_fn`` resolver for ``user_ids``: the live member cache,
    then ``known_users`` — which still names a member who has left. Build it
    once per render or game, in async code, and pass it down; a name it cannot
    find is left for :func:`resolved_name` / :func:`plain_name` to turn into
    ``User <id>``."""
    ids = []
    for raw in user_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not ids or guild is None:
        # Nobody to look up, or no guild to key ``known_users`` by.
        return cached_names(guild)
    return await build_name_fn(
        guild=guild,
        db_path=bot.ctx.db_path,
        guild_id=getattr(guild, "id", 0),
        user_ids=ids,
    )


def cached_names(guild: Any) -> GuildNames:
    """The resolver before anything has been prefetched: the live member
    cache only. What a view starts with until its first async refresh."""
    return GuildNames(guild=guild)


def plain_name(names: GuildNames, user_id: Any) -> str:
    """The unescaped display name for a select label or a field the builder
    escapes itself, or ``User <id>`` for a true miss."""
    try:
        found = names.plain(int(user_id))
    except (TypeError, ValueError):
        found = None
    return found or unknown(user_id)


async def host_display(bot: Any, guild: Any, host_id: Any) -> str:
    """The host's plain name for a recovered board, or ``Host`` with no guild."""
    if guild is None:
        return "Host"
    return plain_name(await build_names(bot, guild, [host_id]), host_id)


def resolved_name(name_fn: Callable[[int], str], user_id: int) -> str:
    """``name_fn(user_id)`` — already escaped — with the resolver's ``<@id>``
    miss replaced by ``User <id>``."""
    name = name_fn(user_id)
    return unknown(user_id) if name == mention(user_id) else name
