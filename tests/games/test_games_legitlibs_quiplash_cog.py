"""Cog-layer tests for the Quiplash mode module.

Only the glue that isn't already covered by ``quiplash_logic.py``'s own
tests lives here — see ``tests/games/test_games_legitlibs_quiplash_logic.py`` for
the pure-logic coverage (submission collection, payout roster, reveal
shuffle, etc.).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.games.legitlibs.modes.quiplash import _build_cast_lines
from bot_modules.games.utils.names import cached_names


def _guild_with_member(uid: int, display_name: str) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.display_name = display_name
    guild = MagicMock(spec=discord.Guild)
    guild.get_member = lambda mid: member if mid == uid else None
    return guild


@pytest.mark.parametrize(
    ("display_name", "expected_first_line", "raw_survives_escaping"),
    [
        pytest.param(
            "[Claim your gift](https://evil.example)",
            "**#1** — \\[Claim your gift](https://evil.example)",
            True,
            id="masked-link-nickname",
        ),
        pytest.param(
            "__spooky__ *ghost*",
            "**#1** — \\_\\_spooky\\_\\_ \\*ghost\\*",
            True,
            id="bold-italic-underline-nickname",
        ),
        pytest.param(
            "Ordinary Name",
            "**#1** — Ordinary Name",
            False,
            id="plain-nickname-untouched",
        ),
    ],
)
def test_build_cast_lines_escapes_a_markdown_nickname(
    display_name, expected_first_line, raw_survives_escaping
):
    """The "Who Wrote What" cast reveal lands in an embed description, so a
    nickname's own markdown must not reach it raw — the reader would see
    bot-authored formatting (or a clickable masked link) that the writer
    typed, not the bot.

    ``resolve_name()`` (``game_manager``) is cache-only and unescaped by
    design (other callers need the raw string), so escaping is
    ``_build_cast_lines``'s own job, matching ``classic.py``'s parallel
    ``resolve_names()`` which already escapes before its field value.
    """
    uid = 555
    guild = _guild_with_member(uid, display_name)

    lines = _build_cast_lines(cached_names(guild), [str(uid)])

    assert lines == [expected_first_line]
    if raw_survives_escaping:
        # A markdown name is only *prefixed* with backslashes, so its raw
        # text still appears as a substring of the escaped line — a naive
        # `raw not in escaped` assertion would pass even if escaping were
        # dropped. Confirm the escaped form actually differs from the raw
        # rendering (the exact-match assert above is the real guard).
        assert lines[0] != f"**#1** — {display_name}"


def test_build_cast_lines_numbers_and_orders_multiple_players():
    """Layout must stay untouched by the escaping fix: same numbering,
    separator and ordering as before.
    """
    guild = MagicMock(spec=discord.Guild)
    names = {111: "Alice", 222: "Bob", 333: "Charlie"}
    guild.get_member = lambda mid: MagicMock(display_name=names[mid])

    lines = _build_cast_lines(cached_names(guild), ["111", "222", "333"])

    assert lines == [
        "**#1** — Alice",
        "**#2** — Bob",
        "**#3** — Charlie",
    ]


def test_build_cast_lines_falls_back_to_user_id_off_guild_cache():
    """An id the guild cache doesn't know about (departed member, fake test
    id outside the shortcut range) still renders — as ``User <id>``, the
    plain form every game card uses for a member nobody can name, never a
    bare number.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.get_member = lambda mid: None

    lines = _build_cast_lines(cached_names(guild), ["999999"])

    assert lines == ["**#1** — User 999999"]
