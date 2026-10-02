"""Pure decision logic for the games-config admin cog.

The cog handles per-server settings for the games cluster: which channels
allow games, where audit logs go, plus read-only views into the active
game in a channel. The Discord glue
(interaction objects, permission checks, db calls) lives in the cog;
this module is the row-and-string transforms it delegates to so they
can be tested without spinning up Discord.

``rows`` (in :func:`channel_ids_from_rows`) is generally a sequence of
mapping-like objects (sqlite3.Row or plain dict). Tests use dicts so we
don't need an open DB.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from bot_modules.shared.name_resolver import NameFn, mention


def describe_active_game(
    row: Mapping[str, Any] | None, name_fn: NameFn = mention
) -> tuple[str, str]:
    """Return ``(title, description)`` for the ``/games game-status`` embed.

    The cog hands us the row from ``games_active_games`` (or ``None``
    when no game is running). We build both the title and the body
    text here so the cog's branch is just an embed construction.

    The host is mod-facing, so it renders as ``Name (`id`)``: the name
    through ``name_fn`` (a ``<@id>`` inside an embed is digits to any
    client that hasn't cached the member) with the id kept alongside so
    a moderator retains something copyable.
    """
    if not row:
        return (
            "No Active Game",
            "There's no game running in this channel.",
        )
    return (
        "Active Game",
        (
            f"**Type:** {row['game_type']}\n"
            f"**State:** {row['state']}\n"
            f"**Host:** {name_fn(int(row['host_id']))} (`{row['host_id']}`)\n"
            f"**Game ID:** `{row['game_id']}`"
        ),
    )


def describe_force_end(game_type: str) -> str:
    """Body text for the ``/games game-end`` success embed."""
    return f"The **{game_type}** game has been ended by an admin/mod."


def has_admin_permissions(perms: Any) -> bool:
    """Return True if the given guild_permissions grant admin.

    Pulled out so the predicate test doesn't need a real Interaction.
    """
    return bool(getattr(perms, "administrator", False))


def channel_ids_from_rows(rows: Iterable[Sequence[Any]]) -> list[int]:
    """Project a sequence of channel-id rows down to a plain int list.

    Convenience wrapper used by callers that need the IDs as a list
    (e.g. for set membership) rather than a rendered string.
    """
    return [int(row[0]) for row in rows]
