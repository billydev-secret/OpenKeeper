"""Posting change cards — the Discord half shared by the todo and bug boards.

What a card says and when the watermark moves are ``change_logic``'s decisions.
This is the glue both boards need to act on them: send one card into the
board's channel, classify how that went, and store each cursor the plan allows.
Kept in one place so the two boards cannot drift into different answers about
which failures are worth retrying.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Sequence

import discord

from bot_modules.core.layout import card
from bot_modules.shared.age_gate import is_age_gated_channel
from bot_modules.todo.layouts import build_change_card_layout
from bot_modules.todo.service import CursorColumn
from bot_modules.todo.change_logic import (
    SEND_GIVE_UP,
    SEND_OK,
    SEND_RETRY,
    CardImage,
    CardPlan,
    cursor_after,
    media_filename,
    survives_strip,
)

log = logging.getLogger(__name__)

#: Downloads one upload link's bytes — ``bot.http.get_from_cdn`` in the cog.
Fetch = Callable[[str], Awaitable[bytes]]


@dataclass
class Sent:
    """How one card went, and — when it posted — where, and whose images."""

    outcome: str
    message: discord.Message | None = None
    image_bug_ids: list[int] = field(default_factory=list)


async def _download(
    images: Sequence[CardImage], fetch: Fetch | None, *, budget: int, label: str
) -> list[tuple[CardImage, bytes]]:
    """Fetch each screenshot, skipping any that fails or would overflow the
    message's upload limit. A lost image costs its picture, never the card."""
    if fetch is None:
        return []
    got: list[tuple[CardImage, bytes]] = []
    total = 0
    for image in images:
        try:
            data = await fetch(image.url)
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            log.warning("%s card: screenshot %s could not be fetched", label, image.filename)
            continue
        if total + len(data) > budget:
            continue
        total += len(data)
        got.append((image, data))
    return got


async def send_change_card(
    guild: discord.Guild,
    channel_id: int,
    plan: CardPlan,
    *,
    label: str,
    fetch: Fetch | None = None,
) -> Sent:
    """Post one card into a board's channel; report how it went.

    The outcome is one of ``SEND_OK``/``SEND_RETRY``/``SEND_GIVE_UP``, and the
    difference between the last two is the whole point of splitting them:
    ``cursor_after`` leaves the watermark alone for a retry and banks it for a
    give-up. A 403 or a missing channel will still be a 403 next minute, so
    holding the cursor for it would widen the arrival window every tick until
    somebody noticed — a weekend of a locked channel becomes a wall of cards.
    """
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        # The channel was deleted, or is a type the board cannot live in.
        # Nothing about the next tick changes that.
        return Sent(SEND_GIVE_UP)
    images = (
        await _download(
            plan.images, fetch, budget=int(guild.filesize_limit), label=label
        )
        if plan.images
        else []
    )
    title, body = plan.title or "", plan.body or ""
    # A screenshot is member-submitted media, so it is spoilered wherever
    # Discord age-gates the channel (style guide § Media).
    spoiler = is_age_gated_channel(channel)
    try:
        try:
            if images:
                # A V2 message shows no attachment a component does not
                # reference, so the gallery names each upload.
                message = await channel.send(
                    view=card(
                        build_change_card_layout(
                            title,
                            body,
                            images=[(i.filename, i.bug_id) for i, _ in images],
                            spoiler=spoiler,
                        )
                    ),
                    files=[
                        discord.File(io.BytesIO(data), filename=image.filename)
                        for image, data in images
                    ],
                )
            else:
                message = await channel.send(
                    view=card(build_change_card_layout(title, body))
                )
        except discord.HTTPException as exc:
            # Too large after all: the card's text is what the log is for, so
            # it goes again without the pictures rather than retrying forever.
            if not images or exc.status != 413:
                raise
            images = []
            message = await channel.send(
                view=card(build_change_card_layout(title, body))
            )
    except (discord.Forbidden, discord.NotFound):
        log.warning(
            "%s change card refused in guild %s channel %s — giving up on"
            " this card (permissions or channel gone)",
            label, guild.id, channel_id,
        )
        return Sent(SEND_GIVE_UP)
    except discord.HTTPException:
        log.warning(
            "%s change card failed to post in guild %s channel %s; will"
            " retry on the next tick",
            label, guild.id, channel_id,
        )
        return Sent(SEND_RETRY)
    return Sent(
        SEND_OK,
        message=message,
        image_bug_ids=sorted({image.bug_id for image, _ in images}),
    )


async def deliver_cards(
    guild: discord.Guild,
    channel_id: int,
    plans: Iterable[tuple[CardPlan, CursorColumn]],
    *,
    save_cursor: Callable[[CursorColumn, float], None],
    label: str,
    fetch: Fetch | None = None,
    settle: Callable[[float, Sent], None] | None = None,
) -> bool:
    """Act on a tick's plans in order. True if any card actually posted.

    ``save_cursor(column, value)`` is blocking and runs in a worker thread. The
    caller re-sticks its board when this returns True — the card has to land
    first, or the board ends up above it.

    ``fetch`` downloads a plan's screenshots, and ``settle(cursor, sent)`` —
    blocking, in a worker thread, after each cursor it stores — is how the bug
    board drops the upload links that window covered and records the card its
    images landed on. The todo board passes neither.
    """
    posted = False
    for plan, column in plans:
        sent = (
            await send_change_card(
                guild, channel_id, plan, label=label, fetch=fetch
            )
            if plan.posts
            else Sent(SEND_OK)
        )
        posted = posted or (plan.posts and sent.outcome == SEND_OK)
        cursor = cursor_after(plan, outcome=sent.outcome)
        if cursor is not None:
            await asyncio.to_thread(save_cursor, column, cursor)
            if settle is not None:
                await asyncio.to_thread(settle, cursor, sent)
    return posted


#: Attempts at downloading a screenshot that stays on a card before it is
#: given up. A kept image that will not download is lost with the edit (see
#: :func:`_strip_layout_card`), so a transient CDN failure gets one more go
#: rather than costing a picture for good (ship review, 2026-10-01).
STRIP_FETCH_ATTEMPTS = 2


async def _fetch_with_retry(fetch: Fetch | None, url: str) -> bytes | None:
    """``fetch(url)``, retried once on a transient failure; ``None`` when
    there is no fetcher or every attempt failed."""
    if fetch is None:
        return None
    for attempt in range(STRIP_FETCH_ATTEMPTS):
        try:
            data = await fetch(url)
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            data = None
        if data is not None:
            return data
        if attempt + 1 < STRIP_FETCH_ATTEMPTS:
            await asyncio.sleep(1)
    return None


async def _strip_layout_card(
    message: discord.Message,
    bug_ids: Sequence[int],
    fetch: Fetch | None,
    *,
    label: str,
) -> None:
    """Take these bugs' screenshots out of a Components V2 card's gallery.

    A fetched V2 message reports no ``attachments`` (style guide § Media), so
    there is no list of uploads to filter and hand back the way an embed-era
    card allows. The gallery is rebuilt instead: every image that stays is
    downloaded from the card and uploaded again under its own name, and the
    edit replaces the message's files with exactly those — which is what
    takes a closed bug's picture off Discord rather than merely out of view.

    An image that should stay but will not download — after one retry —
    is lost with the ones being removed. The removal is the promise; a picture is never worth
    leaving a closed bug's screenshot up for.
    """
    view = discord.ui.LayoutView.from_message(message, timeout=None)
    gallery = next(
        (i for i in view.walk_children() if isinstance(i, discord.ui.MediaGallery)),
        None,
    )
    if gallery is None:
        return
    named = [(item, media_filename(item.media.url)) for item in gallery.items]
    kept = [(item, name) for item, name in named if survives_strip(name, bug_ids)]
    if len(kept) == len(named):
        return
    files: list[discord.File] = []
    items: list[discord.MediaGalleryItem] = []
    for item, name in kept:
        data = await _fetch_with_retry(fetch, item.media.url)
        if data is None:
            log.warning(
                "%s card %s: screenshot %s could not be carried over", label,
                message.id, name,
            )
            continue
        files.append(discord.File(io.BytesIO(data), filename=name))
        items.append(
            discord.MediaGalleryItem(
                f"attachment://{name}",
                description=item.description,
                spoiler=item.spoiler,
            )
        )
    if items:
        gallery.items = items
    else:
        view.remove_item(gallery)
    await message.edit(view=view, attachments=files)


async def strip_card_images(
    channel: discord.abc.GuildChannel | None,
    message_id: int,
    bug_ids: Sequence[int],
    *,
    label: str,
    fetch: Fetch | None = None,
) -> bool:
    """Edit these bugs' screenshots off a card; the rest of the card stays.

    True when there is nothing left to do — edited, or the card or channel is
    gone, or the bot can no longer touch it (a card it cannot edit now it will
    not be able to edit next minute either). False on a transient failure, so
    the loop tries again.

    Two shapes of card are in the channel. One posted before the Components
    V2 conversion is an embed with plain attachments, and keeps the edit it
    always had; a V2 card goes through ``_strip_layout_card``, which needs
    ``fetch`` to carry the surviving images over.
    """
    if not isinstance(channel, discord.TextChannel):
        return True
    try:
        message = await channel.fetch_message(message_id)
        if message.flags.components_v2:
            await _strip_layout_card(message, bug_ids, fetch, label=label)
        else:
            kept = [
                a for a in message.attachments if survives_strip(a.filename, bug_ids)
            ]
            if len(kept) != len(message.attachments):
                await message.edit(attachments=kept)
    except (discord.Forbidden, discord.NotFound):
        log.warning(
            "%s card %s: screenshots could not be removed (card gone or no"
            " access) — giving up",
            label, message_id,
        )
        return True
    except discord.HTTPException:
        log.warning("%s card %s: screenshot removal failed; will retry", label, message_id)
        return False
    return True
