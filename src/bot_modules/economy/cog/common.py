"""Constants and plain helpers shared by the economy cog, its mixins and views."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, NamedTuple

import discord

from bot_modules.core.paths import data_dir
from bot_modules.economy.shop_items import ItemView
from bot_modules.economy.quest_views import (
    can_manage_economy,
)
from bot_modules.economy.service import (
    EconSettings,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger("dungeonkeeper.economy")


_DISABLED_MSG = "The economy isn't enabled on this server yet."


# Shared by both bring-your-own upload paths (emoji modal, /bank role icon)
# when the member's rental is a curated catalog icon rather than a custom one.
_CATALOG_LOCKED_MSG = (
    "❌ You're renting a curated catalog icon — to upload your own instead, "
    "pick **Custom** from the shop's icon picker first (/bank shop)."
)


_NO_ROLE_ICONS_MSG = "❌ This server doesn't support role icons right now."


# Transfers above this need an explicit confirm step (spec §5, "over 100").
_PAY_CONFIRM_THRESHOLD = 100


_MAX_ROLE_NAME_LEN = 32


_MAX_MEMO_LEN = 100


_MAX_ICON_BYTES = 256 * 1024


# Emoji upload types Discord accepts, mapped to whether they're animated.
_EMOJI_CONTENT_TYPES = {
    "image/png": False, "image/jpeg": False,
    "image/webp": False, "image/gif": True,
}


# The custom-name perk renames the member's personal role AND sets their
# server nickname to match — "call yourself anything" has to actually change
# the name people see, not just a cosmetic role label. The nickname is
# best-effort (a bot can't outrank the hierarchy or rename the guild owner),
# so the confirmation spells out which parts landed.
_NICK_FORBIDDEN = (
    "I couldn't set your server nickname to match — my role needs to sit "
    "above yours with the Manage Nicknames permission (and Discord never "
    "lets a bot rename the server owner)."
)


_NICK_FAILED = "Your server nickname didn't take, though — try again in a moment."


def _custom_name_confirmation(text: str, *, nick_ok: bool, nick_reason: str = "") -> str:
    """The ephemeral reply after a member sets their custom name."""
    if nick_ok:
        return (
            f"Your name is now **{text}** — it's your server nickname and your "
            "personal role name."
        )
    base = f"Your role name is now **{text}**."
    return f"{base} {nick_reason}" if nick_reason else base


def _icon_store_path(db_path, guild_id: int, user_id: int):
    """Managed on-disk path for an uploaded personal-role icon (per guild/member)."""
    directory = data_dir("econ_role_icons", base=db_path.parent) / str(guild_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user_id}.png"


# The pasted form of a custom emoji: <:name:id> (or <a:name:id> when animated).
_CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_~]+):(\d+)>$")


def _resolve_guild_emoji(guild: discord.Guild, raw: str) -> discord.Emoji | None:
    """Resolve member input to one of *this guild's* custom emojis.

    Accepts the pasted form ``<:name:id>`` (matched by id) or a typed
    ``:name:`` / bare name. Unicode emojis and custom emojis from other
    servers resolve to ``None`` — role icons only take images this server
    has already approved as emojis.

    The name match tries the server's exact casing first, then falls back to
    a case-insensitive one, matching how Discord resolves ``:name:`` typing
    (the same rule ``economy_emoji_service.validate_emoji_name`` applies when
    it reserves a name). Without the fallback every mixed-case emoji —
    ``:monkaS:``, ``:peepoHappy:`` — was unreachable by the short name a
    member actually types, while the pasted form still worked because it
    matches by id. Exact wins first so a server holding both ``:pepe:`` and
    ``:PEPE:`` still resolves each one precisely; if input matches neither
    exactly the fallback takes the lowest id — the oldest emoji — because
    Discord doesn't enforce name uniqueness and ``guild.emojis`` arrives in
    gateway-payload order, so an unordered pick would hand the member a
    different icon after a restart or a re-upload.
    """
    raw = raw.strip()
    m = _CUSTOM_EMOJI_RE.match(raw)
    if m:
        return discord.utils.get(guild.emojis, id=int(m.group(3)))
    name = raw.strip(":").strip()
    if not name:
        return None
    exact = discord.utils.get(guild.emojis, name=name)
    if exact is not None:
        return exact
    folded = name.casefold()
    return min(
        (e for e in guild.emojis if e.name.casefold() == folded),
        key=lambda e: e.id,
        default=None,
    )


# Trigger-quest cache staleness bound: a dashboard edit takes effect on the
# next message after at most this many seconds.
_TRIGGER_CACHE_TTL = 60.0


class _ShopContext(NamedTuple):
    """Everything /bank shop renders from, read on a single connection."""

    settings: EconSettings
    owned: set[str]
    # The subset of ``owned`` the staff comp is covering (empty for everyone
    # else), so the shop can label those rows without a second read.
    comped: set[str]
    balance: int
    icon_range: tuple[int, int, int] | None
    # (min, max, count) over the guild's rentable palette colours, or None when
    # it has none — which is also the shop's switch for hiding the Palette row.
    color_range: tuple[int, int, int] | None
    refundable: list[dict]
    shields_held: int
    shield_price: int
    # Admin-defined custom items this viewer should see, and which of them
    # they already hold — empty in a guild that has defined none, which is
    # also the switch that hides the Server Store section and its button.
    items: list[ItemView]
    owned_item_ids: set[int]


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".avif")


def _has_image_attachment(message: discord.Message) -> bool:
    """True when any attachment is an image.

    Content-type first; filename extension as the fallback for the uploads
    Discord serves without one (some mobile clients).
    """
    for att in message.attachments:
        ctype = att.content_type or ""
        if ctype.startswith("image/"):
            return True
        if not ctype and att.filename.lower().endswith(_IMAGE_EXTENSIONS):
            return True
    return False


def _can_grant(user: discord.Member, settings: EconSettings) -> bool:
    """True for server admins or holders of the configured manager role.

    Delegates to the canonical gate in ``quest_views`` so the grant command
    and the sign-off buttons enforce one rule.
    """
    return can_manage_economy(user, settings)


def _clean_memo(memo: str | None) -> str | None:
    """Collapse a pay memo to a single trimmed line, or None if it's empty.

    Newlines would break the one-line-per-row wallet and ledger renders, so
    they collapse to spaces. The Range on the command caps length client-side;
    the truncation here is the server-side guard.
    """
    if not memo:
        return None
    cleaned = " ".join(memo.split())
    if not cleaned:
        return None
    return cleaned[:_MAX_MEMO_LEN]


def _can_post_publicly(interaction: discord.Interaction) -> bool:
    """Whether the bot may post a public pay receipt where this was invoked.

    Checked up front so the usual "no permission" case is decided before the
    sender is told anything, rather than surfacing as a half-finished payment.
    The followup is still wrapped in a try/except: thread permissions don't
    resolve cleanly through ``permissions_for``, and a channel can change
    under a slow interaction either way.
    """
    guild, channel = interaction.guild, interaction.channel
    if guild is None or guild.me is None:
        return False
    if not isinstance(channel, (discord.abc.GuildChannel, discord.Thread)):
        return False
    perms = channel.permissions_for(guild.me)
    return perms.send_messages and perms.embed_links
