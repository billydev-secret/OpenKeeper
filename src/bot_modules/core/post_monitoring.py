from __future__ import annotations

import logging

import discord

from bot_modules.core.utils import format_user_for_log
from bot_modules.shared.age_gate import is_age_gated_channel
from bot_modules.core.channel_rollup import thread_parent_id
from bot_modules.core.gif_guard_logic import is_guarded_channel, message_has_gif

#: What counts as an image for XP and auto-react. Discord sometimes serves an
#: attachment without a ``content_type``, hence the extension fallback.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")

#: What spoiler enforcement treats as an image — deliberately a strict subset
#: of :data:`IMAGE_EXTENSIONS`, and deliberately without the ``content_type``
#: fallback :func:`attachment_is_image` applies.
#:
#: This gate deletes members' posts, so widening it is a behaviour change and
#: not a tidy-up: every format added here becomes newly deletable.
#: ``test_spoiler_extensions_are_a_strict_subset`` pins the relationship.
SPOILER_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


#: What a member sees for five seconds after GIF Guard removes their post.
GIF_GUARD_NOTICE = "Beep Boop - friendly bot helper: GIFs aren't allowed in this channel."


def _channel_label(channel) -> str:
    name = getattr(channel, "name", None)
    return f"#{name} ({channel.id})" if name else str(channel.id)


def attachment_is_image(attachment: discord.Attachment) -> bool:
    """True for attachments that are images, regardless of size.

    :func:`enforce_spoiler_requirement` deliberately does *not* use this — see
    ``SPOILER_IMAGE_EXTENSIONS``."""
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def message_has_qualifying_image(message: discord.Message) -> bool:
    return any(attachment_is_image(attachment) for attachment in message.attachments)


async def enforce_spoiler_requirement(
    message: discord.Message,
    *,
    bypass_role_ids: frozenset[int] | set[int],
    log: logging.Logger,
) -> bool:
    """Delete unspoilered images in channels Discord marks NSFW.

    The scope is Discord's own setting and nothing else: a channel counts when
    it, or its category, is marked age-restricted (``is_age_gated_channel``).
    There is no bot-side list. A channel whose flag can't be read is left
    alone, because this gate deletes members' posts.

    Every image posted there without Discord's spoiler blur goes. Only the
    filename and the spoiler flag are read — the picture itself is never
    downloaded or judged, so there is no verdict to be wrong and nothing to
    record. The content-aware version this replaced deleted by model score and
    blocked photos in SFW channels; see docs/specs/post_monitoring_spec.md.
    """
    if not is_age_gated_channel(message.channel, default=False):
        return False

    if not isinstance(message.author, discord.Member):
        return False

    if any(role.id in bypass_role_ids for role in message.author.roles):
        return False

    if not message.attachments:
        return False

    for attachment in message.attachments:
        # Not attachment_is_image: this gate is narrower on purpose, because
        # everything it matches becomes deletable. See SPOILER_IMAGE_EXTENSIONS.
        if not attachment.filename.lower().endswith(SPOILER_IMAGE_EXTENSIONS):
            continue
        if attachment.is_spoiler():
            continue

        try:
            log.info(
                "Deleting spoilerless image from %s: %s",
                message.author,
                message.content,
            )
            await message.delete()
            await message.channel.send(
                "Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.",
                delete_after=5,
            )
        except discord.Forbidden:
            log.warning(
                "Missing permission to delete spoilerless image in channel %s from user %s",
                _channel_label(message.channel),
                format_user_for_log(message.author),
            )
        except discord.HTTPException:
            log.exception(
                "Failed to enforce spoiler requirement in channel %s",
                _channel_label(message.channel),
            )
        return True

    return False


async def enforce_gif_guard(
    message: discord.Message,
    *,
    guarded_channel_ids: frozenset[int] | set[int],
    bypass_role_ids: frozenset[int] | set[int],
    log: logging.Logger,
) -> bool:
    """Delete a GIF posted in a channel an admin listed on the GIF Guard panel.

    Runs on a new message and again on its edits: a GIF link from a host
    ``gif_guard_logic`` doesn't name only shows up as a GIF once Discord's
    preview arrives, which comes in as an edit. Threads follow their parent
    channel. An empty list guards nothing. Bypass roles are the spoiler
    gate's, from the Global panel. Webhooks, bots and any author that doesn't
    resolve as a guild member are always skipped — the on_message pipeline
    already filters bots before it ever calls this, but on_message_edit calls
    straight in, so the check has to live here too. Returns True when the
    message was a GIF this gate acted on.
    """
    if not is_guarded_channel(
        message.channel.id, thread_parent_id(message.channel), guarded_channel_ids
    ):
        return False

    if not isinstance(message.author, discord.Member) or message.author.bot:
        return False

    if any(role.id in bypass_role_ids for role in message.author.roles):
        return False

    if not message_has_gif(
        message.content,
        [
            (a.filename, a.content_type, bool(a.flags.animated))
            for a in message.attachments
        ],
        [(e.type, e.url) for e in message.embeds],
    ):
        return False

    try:
        log.info(
            "GIF Guard: deleting a GIF from %s in %s",
            format_user_for_log(message.author),
            _channel_label(message.channel),
        )
        await message.delete()
        await message.channel.send(GIF_GUARD_NOTICE, delete_after=5)
    except discord.NotFound:
        # Already gone — the member deleted it, or a second event for the same
        # message raced this one. Either way there is nothing left to do.
        pass
    except discord.Forbidden:
        log.warning(
            "GIF Guard: missing permission to delete a GIF in channel %s from user %s",
            _channel_label(message.channel),
            format_user_for_log(message.author),
        )
    except discord.HTTPException:
        log.exception(
            "GIF Guard: failed to delete a GIF in channel %s",
            _channel_label(message.channel),
        )
    return True
