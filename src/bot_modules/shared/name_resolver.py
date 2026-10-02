"""Shared display-name resolution for embeds — no Discord API calls.

A ``<@id>`` mention is only resolved into a name by the *reading* client, from
its own cache. Discord's servers do nothing to it, so inside an embed it
degrades to a bare numeric id for any viewer who hasn't seen that user before.
Anywhere a name is meant to simply render, resolve it here and emit plain text.

The chain, in order:

1. ``guild.get_member(uid).display_name`` — ``intents.members`` is enabled, so
   this cache is complete for present members and updates the moment somebody
   changes their nickname.
2. ``known_users.display_name``, then ``known_users.username`` — persistent, and
   the only source covering members who have **left** the guild.
3. ``<@id>`` — last resort for a user neither source knows.

The live cache leads because a ``known_users`` row is only as fresh as that
user's last recorded activity: someone who renamed and hasn't spoken since keeps
a stale name on file indefinitely. The table then covers exactly what the cache
structurally cannot.

:func:`resolve_name_from` is the pure, synchronous core, safe to call from an
embed builder. :func:`build_name_fn` does the async prefetch and hands back a
``NameFn`` closed over the results — and only queries for ids the member cache
misses, so a roster of present members costs no I/O at all.

Member-written text (a sponsored QOTD, say) can carry its own ``<@id>``,
``<@&id>`` and ``<#id>`` mentions. :func:`resolve_mentions_from` rewrites every
one to ``@name`` / ``@role`` / ``#channel`` — channels included, because the
same text is also drawn into images, where nothing resolves at all. Feed
:func:`mentioned_user_ids` to the prefetch so departed members are named too.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bot_modules.core.db_utils import open_db
from bot_modules.shared.message_store import get_known_user_names_bulk

if TYPE_CHECKING:
    import discord

# A NameFn turns a user id into embed-ready text: an escaped display name, or a
# raw <@id> mention for a user we genuinely cannot name.
NameFn = Callable[[int], str]


def mention(user_id: int) -> str:
    """The last-resort rendering for a user we can't name.

    Also the default ``NameFn`` for embed builders whose caller doesn't inject
    a resolver, which keeps their pre-resolver output unchanged.
    """
    return f"<@{user_id}>"


def named_or_anonymous(user_id: int, name_fn: NameFn, *, fallback: str = "a member") -> str:
    """``name_fn(user_id)``, or ``fallback`` when there is no user to name.

    A user id of 0 is not an unknown member — it is the absence of one, which
    is how an erasure detaches a still-running purchase from the person who
    bought it (``economy_theme_service.anonymise_live_theme``). Feeding that 0
    to a resolver would render the literal ``<@0>``, so every embed that can
    outlive its buyer routes through here instead of repeating the check.
    """
    return name_fn(user_id) if user_id else fallback


def resolve_name_from(
    user_id: int,
    *,
    guild: "discord.Guild | None",
    table_names: Mapping[int, str] | None = None,
) -> str:
    """Resolve one user id to embed-ready text.

    The chain is live member cache -> ``known_users`` (``table_names``) ->
    ``<@id>``. ``table_names`` is a prefetched mapping rather than a live
    connection so this stays synchronous and safe to call from an embed
    builder; see :func:`build_name_fn` for the async prefetch.

    The result is markdown-escaped, since a display name containing ``*`` or
    ``_`` would otherwise mangle the surrounding embed copy.
    """
    import discord  # local: keeps module import cheap for non-Discord callers

    name = _known_name(user_id, guild=guild, table_names=table_names)
    return discord.utils.escape_markdown(name) if name else mention(user_id)


def _known_name(
    user_id: int,
    *,
    guild: "discord.Guild | None",
    table_names: Mapping[int, str] | None,
) -> str | None:
    """The unescaped name the chain finds for ``user_id``, or None."""
    member = guild.get_member(user_id) if guild is not None else None
    for candidate in (
        member.display_name if member is not None else None,
        table_names.get(user_id) if table_names else None,
    ):
        name = (candidate or "").strip()
        if name:
            return name
    return None


# <@id> and <@!id> (user), <@&id> (role), <#id> (channel). The kind is group 1.
_MENTION_RE = re.compile(r"<(@!?|@&|#)(\d+)>")


def mentioned_user_ids(text: str) -> list[int]:
    """The user ids mentioned in ``text`` — what to hand ``build_name_fn``."""
    return [
        int(match.group(2))
        for match in _MENTION_RE.finditer(text or "")
        if match.group(1) in ("@", "@!")
    ]


def resolve_mentions_from(
    text: str,
    *,
    guild: "discord.Guild | None",
    table_names: Mapping[int, str] | None = None,
    markdown: bool = True,
) -> str:
    """Rewrite every user, role and channel mention in ``text`` as plain text.

    Users walk the same chain as :func:`resolve_name_from`; roles and channels
    come from the guild cache. Something neither source knows becomes
    ``@unknown member`` / ``@unknown role`` / ``#unknown channel`` rather than
    a raw id, since the id is exactly what this exists to hide.

    ``markdown`` escapes the substituted names for an embed. Pass False when
    the text is drawn into an image or shown on the dashboard, where the
    escaping backslashes would be printed literally. The rest of ``text`` is
    never touched — its markdown is the author's.
    """
    import discord  # local: keeps module import cheap for non-Discord callers

    def _substitute(match: re.Match[str]) -> str:
        kind, object_id = match.group(1), int(match.group(2))
        if kind == "#":
            channel = guild.get_channel_or_thread(object_id) if guild else None
            prefix, unknown = "#", "unknown channel"
            name = channel.name if channel is not None else None
        elif kind == "@&":
            role = guild.get_role(object_id) if guild else None
            prefix, unknown = "@", "unknown role"
            # @everyone's role is literally named "@everyone".
            name = role.name.lstrip("@") if role is not None else None
        else:
            prefix, unknown = "@", "unknown member"
            name = _known_name(object_id, guild=guild, table_names=table_names)
        name = (name or "").strip()
        if not name:
            return prefix + unknown
        return prefix + (discord.utils.escape_markdown(name) if markdown else name)

    return _MENTION_RE.sub(_substitute, text or "")


@dataclass(frozen=True)
class GuildNames:
    """The ``NameFn`` :func:`build_name_fn` returns.

    Calling it names one user id, exactly as before. It also carries the guild
    and the prefetched names, so a builder handed only a ``name_fn`` can still
    rewrite the mentions inside free text — see :func:`resolve_mentions`.
    """

    guild: "discord.Guild | None"
    table_names: Mapping[int, str] = field(default_factory=dict)

    def __call__(self, user_id: int) -> str:
        return resolve_name_from(user_id, guild=self.guild, table_names=self.table_names)

    def plain(self, user_id: int) -> str | None:
        """The unescaped name for a select label or a plain-text field, or
        ``None`` for a user neither the cache nor the table knows."""
        return _known_name(user_id, guild=self.guild, table_names=self.table_names)

    def text(self, text: str, *, markdown: bool = True) -> str:
        return resolve_mentions_from(
            text, guild=self.guild, table_names=self.table_names, markdown=markdown
        )


def resolve_mentions(text: str, name_fn: NameFn, *, markdown: bool = True) -> str:
    """``resolve_mentions_from`` for a builder that holds only a ``name_fn``.

    A resolver from :func:`build_name_fn` rewrites the text. A plain function
    — the ``mention`` default of an un-wired caller — knows no guild, so the
    text passes through unchanged, matching that default's pre-resolver output.
    """
    if isinstance(name_fn, GuildNames):
        return name_fn.text(text, markdown=markdown)
    return text


async def build_name_fn(
    *,
    guild: "discord.Guild | None",
    db_path: Path,
    guild_id: int,
    user_ids: list[int],
) -> GuildNames:
    """Prefetch names for ``user_ids`` and return a sync resolver over them.

    Only ids the live member cache can't answer are looked up in the database,
    so a guild full of present members costs no query at all. The single
    batched read runs in a worker thread to keep sqlite off the event loop.
    """
    misses = sorted(
        {
            uid for uid in user_ids
            if uid and (guild is None or guild.get_member(uid) is None)
        }
    )

    table_names: dict[int, str] = {}
    if misses:
        def _lookup() -> dict[int, str]:
            with open_db(db_path) as conn:
                return get_known_user_names_bulk(conn, guild_id, misses)

        table_names = await asyncio.to_thread(_lookup)

    return GuildNames(guild=guild, table_names=table_names)
