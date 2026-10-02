"""Welcome and leave message formatting."""

from __future__ import annotations

import discord

DEFAULT_WELCOME_MESSAGE = "Glad to have you here! Feel free to introduce yourself."
DEFAULT_LEAVE_MESSAGE = "{member_name} has left the server."

# The one-line arrival ping the bot drops in greeter chat when a newcomer lands
# without an intake card. Editable per guild on the Welcome panel: the default
# keeps the historical forced ``@here``, and clearing the field turns the line
# off entirely (one dial instead of separate copy/ping/enable toggles).
DEFAULT_ARRIVAL_MESSAGE = "@here - {member} has arrived"

# Placeholders the arrival line understands (a subset of the welcome/leave set —
# no embed, no bio lookups, just who arrived).
ARRIVAL_PLACEHOLDER_HELP = (
    "`{member}` — mention  •  `{member_name}` — display name  •  "
    "`{server}` — server name"
)

# Placeholders available in both welcome and leave templates
PLACEHOLDER_HELP = (
    "`{member}` — member's name  •  `{member_name}` — display name  •  "
    "`{member_id}` — user ID  •  `{server}` — server name  •  "
    "`{member_count}` — member count  •  `{bios_channel}` — bios channel mention  •  "
    "`{bio_link}` — direct link to the bios trigger button  •  "
    "`{member_bio_link}` — jump URL to this member's own bio post (empty if they have no bio; "
    "auto-resurrects an archived bio for returning members)  •  "
    "`{server_guide}` — mention of the configured server-guide channel (empty if unset)"
)


def _resolve(
    template: str,
    member: discord.Member,
    *,
    bio_link: str = "",
    bios_channel_mention: str = "",
    member_bio_link: str = "",
    server_guide_mention: str = "",
) -> str:
    guild = member.guild
    # These templates render inside an embed, where a <@id> neither pings nor
    # reliably resolves — a reader whose client hasn't cached a brand-new
    # member sees a bare number. {member} therefore prints the name; the
    # welcome's separate "mention the member" option is what pings.
    #
    # Both name placeholders are escaped, and {member_name} was not until
    # 2026-09-24. The result lands in an embed ``description=``, which renders
    # the full markdown set including masked links — so a member joining as
    # ``[Claim your gift](https://evil.example)`` got a clickable link in what
    # reads as a bot-authored greeting. The default leave template is
    # ``{member_name} has left the server.``, so this fired on every departure
    # in every guild with a leave channel, not in some opt-in corner.
    escaped_name = discord.utils.escape_markdown(member.display_name)
    return (
        template.replace("{member}", escaped_name)
        .replace("{member_name}", escaped_name)
        .replace("{member_id}", str(member.id))
        .replace("{server}", guild.name)
        .replace("{member_count}", str(guild.member_count or 0))
        .replace("{bio_link}", bio_link)
        .replace("{bios_channel}", bios_channel_mention)
        .replace("{member_bio_link}", member_bio_link)
        .replace("{server_guide}", server_guide_mention)
    )


def build_welcome_embed(
    member: discord.Member,
    message_template: str,
    *,
    bio_link: str = "",
    bios_channel_mention: str = "",  # embed-mention-ok: bios_channel_mention
    member_bio_link: str = "",
    server_guide_mention: str = "",  # embed-mention-ok: server_guide_mention
    color: discord.Color | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        description=_resolve(
            message_template,
            member,
            bio_link=bio_link,
            bios_channel_mention=bios_channel_mention,
            member_bio_link=member_bio_link,
            server_guide_mention=server_guide_mention,
        ),
        color=color or discord.Color.blurple(),
    )
    embed.set_author(
        name=f"Welcome, {member.display_name}!", icon_url=member.display_avatar.url
    )
    if member.guild.icon:
        embed.set_thumbnail(url=member.guild.icon.url)
    member_count = member.guild.member_count or 0
    embed.set_footer(text=f"Member #{member_count} • {member.guild.name}")
    return embed


def build_leave_embed(
    member: discord.Member,
    message_template: str,
    *,
    bio_link: str = "",
    bios_channel_mention: str = "",  # embed-mention-ok: bios_channel_mention
    member_bio_link: str = "",
    server_guide_mention: str = "",  # embed-mention-ok: server_guide_mention
    color: discord.Color | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        description=_resolve(
            message_template,
            member,
            bio_link=bio_link,
            bios_channel_mention=bios_channel_mention,
            member_bio_link=member_bio_link,
            server_guide_mention=server_guide_mention,
        ),
        color=color or discord.Color.blurple(),
    )
    embed.set_author(
        name=f"{member.display_name} left", icon_url=member.display_avatar.url
    )
    member_count = member.guild.member_count or 0
    embed.set_footer(text=f"{member.guild.name} • {member_count} members remaining")
    return embed


def render_arrival_message(template: str, member: discord.Member) -> str:
    """Render the greeter-chat arrival line, or "" when the guild turned it off.

    A blank/whitespace-only template is the off switch — the caller sends
    nothing rather than posting an empty message. ``{mention}`` is accepted as
    an alias for ``{member}`` so an admin who copies the wording from the
    birthday panel still gets a ping.

    Unlike the welcome/leave templates this one is sent as **plain message
    content**, not an embed description, so every mention in it is live. The
    template itself is admin-authored and keeps its ``@here``; the display name
    spliced in by ``{member_name}`` is not — a newcomer calling themselves
    ``@everyone`` would otherwise ping the whole server through the very
    permission the ``@here`` needs, and one calling themselves ``**Mod**``
    would otherwise forge emphasis onto the line. Only the member-controlled
    substitution is escaped, mention-wise and markdown-wise.
    """
    text = (template or "").strip()
    if not text:
        return ""
    member_name = discord.utils.escape_markdown(
        discord.utils.escape_mentions(member.display_name)
    )
    rendered = (
        text.replace("{member}", member.mention)
        .replace("{mention}", member.mention)
        .replace("{member_name}", member_name)
        .replace("{server}", member.guild.name)
    ).strip()
    return rendered


def server_guide_mention_for(channel_id: int) -> str:
    """Return ``<#channel_id>`` mention or empty string when unset."""
    return f"<#{channel_id}>" if channel_id else ""
