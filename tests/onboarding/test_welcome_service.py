"""Tests for onboarding/welcome/service.py — the greeter arrival line.

The arrival line used to be a hard-coded ``@here - {mention} has arrived``
f-string in events_cog with no dashboard control. It is now one per-guild
template: the copy *and* the ping live in the same box, and an empty box means
"post nothing".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.onboarding.welcome.service import (
    DEFAULT_ARRIVAL_MESSAGE,
    render_arrival_message,
)


def _member(*, mention: str = "<@500>", display_name: str = "Newbie",
            guild_name: str = "The Meadow") -> MagicMock:
    member = MagicMock()
    member.mention = mention
    member.display_name = display_name
    member.guild = MagicMock()
    member.guild.name = guild_name
    return member


def test_default_template_keeps_the_historical_line():
    """Guilds that never touch the dial keep exactly what they had."""
    assert render_arrival_message(DEFAULT_ARRIVAL_MESSAGE, _member()) == (
        "@here - <@500> has arrived"
    )


@pytest.mark.parametrize(
    "template,expected",
    [
        ("{member} is here", "<@500> is here"),
        ("{mention} is here", "<@500> is here"),          # birthday-panel alias
        ("{member_name} joined {server}", "Newbie joined The Meadow"),
        ("Say hi to {member_name}!", "Say hi to Newbie!"),
        ("  padded  ", "padded"),
    ],
)
def test_placeholders_and_trimming(template, expected):
    assert render_arrival_message(template, _member()) == expected


@pytest.mark.parametrize("template", ["", "   ", "\n\t ", None])
def test_blank_template_is_the_off_switch(template):
    """A cleared box posts nothing rather than an empty message."""
    assert render_arrival_message(template, _member()) == ""


@pytest.mark.parametrize(
    "display_name,forbidden",
    [
        ("@everyone", "@everyone"),
        ("@here", "@here"),
        ("<@&123456789012345678>", "<@&123456789012345678>"),
    ],
)
def test_a_hostile_display_name_cannot_ping(display_name, forbidden):
    """The arrival line is plain content, so a name is not a safe splice.

    The line already needs Mention Everyone for its ``@here``; a newcomer who
    renames themselves ``@everyone`` must not ride that permission. Only the
    member-controlled half is escaped — the admin's own ``@here`` survives.
    """
    out = render_arrival_message(
        "@here - {member_name} has arrived", _member(display_name=display_name)
    )
    assert out.startswith("@here - ")          # the admin's own ping survives
    name_part = out[len("@here - "):]
    assert forbidden not in name_part          # the member's does not
    # Neutralised, not deleted — the name still reads in the channel.
    assert discord.utils.escape_mentions(display_name) in name_part


def test_a_guild_can_drop_the_here_ping():
    """The whole point of the dial: no forced @here."""
    out = render_arrival_message("{member_name} has arrived", _member())
    assert "@here" not in out
    assert out == "Newbie has arrived"


@pytest.mark.parametrize("which", ["welcome", "leave"])
def test_member_placeholder_in_an_embed_prints_the_name(which):
    """Welcome/leave render inside an embed, where <@id> never pings and shows
    as a bare number to a reader who hasn't cached the newcomer."""
    from bot_modules.onboarding.welcome.service import build_leave_embed, build_welcome_embed

    member = _member(display_name="New_bie")
    member.guild.member_count = 3
    member.guild.icon = None
    build = build_welcome_embed if which == "welcome" else build_leave_embed
    embed = build(member, "Hi {member} ({member_name})")
    # Both placeholders print the *escaped* name. Until 2026-09-24 this line
    # asserted ``(New_bie)`` — raw — and so pinned the defect the test below
    # describes rather than catching it.
    assert embed.description == "Hi New\\_bie (New\\_bie)"
    assert "<@" not in embed.description


@pytest.mark.parametrize("which", ["welcome", "leave"])
@pytest.mark.parametrize(
    "display_name",
    [
        "[Claim your gift](https://evil.example)",
        "**Moderator**",
        "||hidden||",
        "`code`",
    ],
    ids=["masked-link", "bold", "spoiler", "code-span"],
)
def test_member_name_cannot_smuggle_markdown_into_the_card(which, display_name):
    """A display name is member-controlled text landing in ``description=``.

    An embed description renders the whole markdown set, masked links
    included — the one surface where ``[text](url)`` becomes clickable, which
    a message ``content=`` does not do. So a member who joins as
    ``[Claim your gift](https://evil.example)`` was handed a link rendered
    inside a card the server's own bot appears to have written, and a member
    named ``**Moderator**`` was handed the formatting to match.

    This is the ``{member_name}`` half. ``{member}`` was escaped from the
    start; the two placeholders both print the name here, so the asymmetry was
    invisible to a reader and to every test — including the one above, which
    asserted it.

    It is pinned per-guild rather than hypothetically: the shipped default
    leave template is ``{member_name} has left the server.``

    The masked-link row is the reason this asserts an exact string rather than
    "the raw name is absent". ``escape_markdown`` neutralises that name by
    prepending one backslash to the ``[`` — ``\\[Claim your gift](...)`` — which
    is enough, because Discord forms a masked link only from a literal ``[``.
    The raw name is still a *substring* of that output, so a ``not in`` check
    would fail on the one input that matters most while the protection was
    working.
    """
    from bot_modules.onboarding.welcome.service import (
        DEFAULT_LEAVE_MESSAGE,
        build_leave_embed,
        build_welcome_embed,
    )

    assert "{member_name}" in DEFAULT_LEAVE_MESSAGE  # the default is the vector

    member = _member(display_name=display_name)
    member.guild.member_count = 3
    member.guild.icon = None
    build = build_welcome_embed if which == "welcome" else build_leave_embed
    embed = build(member, DEFAULT_LEAVE_MESSAGE)

    assert embed.description != f"{display_name} has left the server.", (
        "the member-controlled name reached an embed description unescaped, "
        "where Discord renders it as markdown"
    )
    assert embed.description == discord.utils.escape_markdown(
        display_name
    ) + " has left the server."
