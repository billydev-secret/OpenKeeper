"""Headless Discord-message deletion — the engine behind ``/mydata`` and the
dashboard's Member Data → Delete Messages tab.

**Nothing in this module opens the database.** That is the guarantee
``_run_deletion`` carried when it lived in the cog, and it is asserted in
tests: clearing a member's Discord messages must never touch the server-side
records — XP, activity and profile all survive, and the modes differ only in
*which* Discord messages go.

A run reports through a :class:`DeletionProgress` sink rather than owning a
surface, because the two callers report very differently: the slash command
edits one ephemeral message (falling back to a DM when the interaction token
expires), while the dashboard polls a counter held in memory. Edit-rate
throttling belongs to whichever sink actually edits a Discord message, not
here — a web poller has no edits to throttle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import discord

from bot_modules.privacy.logic import (
    MODE_ALL,
    chunk_for_bulk_delete,
    group_messages_by_channel,
    is_forum_thread,
    message_matches_mode,
    partition_by_bulk_delete_window,
)
from bot_modules.shared.discord_scan import find_user_messages

log = logging.getLogger("dungeonkeeper.privacy")


@dataclass(frozen=True)
class DeletionResult:
    """What one run did. ``found == 0`` means the scan matched nothing."""

    mode: str
    found: int
    deleted: int = 0
    failed: int = 0
    replaced: int = 0

    @property
    def empty(self) -> bool:
        return self.found == 0


class DeletionProgress(Protocol):
    """Where a run reports to. Either method may be slow; neither may raise."""

    async def scanning(self, done: int, total: int, found: int) -> None: ...

    async def deleting(self, done: int, total: int) -> None: ...


async def delete_discord_messages(
    guild: discord.Guild,
    user_id: int,
    msg_rows: list[tuple[int, int]],
    on_progress=None,
) -> tuple[int, int, int]:
    """Delete Discord messages for *user_id*. Returns (deleted, failed, replaced).

    Forum thread OPs (message_id == channel_id) are kept as threads: the original
    message is deleted and the bot posts a [deleted] tombstone so the thread and
    its replies survive under the bot's name.

    on_progress: optional async callable(deleted, failed, replaced) called after each channel.
    """
    by_channel = group_messages_by_channel(msg_rows)

    deleted = 0
    failed = 0
    replaced = 0

    for channel_id, message_ids in by_channel.items():
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                # Channel deleted on Discord — its messages are gone with it.
                log.info(
                    "Channel %d no longer exists — counting %d messages as already gone",
                    channel_id,
                    len(message_ids),
                )
                deleted += len(message_ids)
                if on_progress:
                    await on_progress(deleted, failed, replaced)
                continue
            except (discord.Forbidden, discord.HTTPException) as exc:
                # Bot lost access or transient error — try direct deletion as a fallback.
                log.warning("Cannot resolve channel %d (%s) — attempting direct deletion", channel_id, exc)
                partial = discord.PartialMessageable(
                    state=guild._state, id=channel_id, guild_id=guild.id  # type: ignore[arg-type]
                )
                for mid in message_ids:
                    try:
                        await partial.get_partial_message(mid).delete()
                        deleted += 1
                    except discord.NotFound:
                        deleted += 1
                    except (discord.Forbidden, discord.HTTPException) as del_exc:
                        log.warning("Direct delete failed for message %d: %s", mid, del_exc)
                        failed += 1
                    if on_progress:
                        await on_progress(deleted, failed, replaced)
                    await asyncio.sleep(0.5)
                continue

        if not isinstance(
            channel,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread),
        ):
            # Unsupported channel type — count as gone
            deleted += len(message_ids)
            if on_progress:
                await on_progress(deleted, failed, replaced)
            continue

        # Unarchive threads so we can delete from them (and so forum-OP re-post
        # works — Discord rejects sends to archived threads with code 50083).
        # Re-archive at the end of the per-channel block.
        was_archived = isinstance(channel, discord.Thread) and channel.archived
        if was_archived:
            try:
                await channel.edit(archived=False)  # type: ignore[union-attr]
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Cannot unarchive thread %d: %s", channel_id, exc)
                failed += len(message_ids)
                if on_progress:
                    await on_progress(deleted, failed, replaced)
                continue

        # Forum thread OPs: message_id == channel_id (Discord snowflake parity).
        # Delete the OP and re-post its content under the bot so the thread —
        # and any replies from other members — survives.
        if is_forum_thread(channel) and channel_id in message_ids:
            try:
                op = await channel.fetch_message(channel_id)  # type: ignore[union-attr]
                await op.delete()
                await channel.send("[deleted]")  # type: ignore[union-attr]
                replaced += 1
            except discord.NotFound:
                replaced += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Forum OP re-post failed in thread %d: %s", channel_id, exc)
                failed += 1
            message_ids = [mid for mid in message_ids if mid != channel_id]
            await asyncio.sleep(0.5)

        if not message_ids:
            if was_archived:
                try:
                    await channel.edit(archived=True)  # type: ignore[union-attr]
                except (discord.Forbidden, discord.HTTPException):
                    pass
            continue

        recent, old = partition_by_bulk_delete_window(message_ids)

        for chunk in chunk_for_bulk_delete(recent):
            batch = [discord.Object(id=mid) for mid in chunk]
            try:
                await channel.delete_messages(batch)  # type: ignore[union-attr]
                deleted += len(batch)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Bulk delete failed in channel %d: %s", channel_id, exc)
                failed += len(batch)
            if on_progress:
                await on_progress(deleted, failed, replaced)
            await asyncio.sleep(1)

        for mid in old:
            try:
                await channel.get_partial_message(mid).delete()  # type: ignore[union-attr]
                deleted += 1
            except discord.NotFound:
                deleted += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Delete failed for message %d: %s", mid, exc)
                failed += 1
            if on_progress:
                await on_progress(deleted, failed, replaced)
            await asyncio.sleep(0.5)

        if was_archived:
            try:
                await channel.edit(archived=True)  # type: ignore[union-attr]
            except (discord.Forbidden, discord.HTTPException):
                pass

    return deleted, failed, replaced


#: Both progress sinks take three counters and await. Named once so the two
#: optional locals in :func:`run_deletion` can say so without repeating it.
_ProgressHook = Callable[[int, int, int], Awaitable[None]]


async def run_deletion(
    guild: discord.Guild,
    user_id: int,
    *,
    mode: str = MODE_ALL,
    progress: DeletionProgress | None = None,
) -> DeletionResult:
    """Scan *guild* for *user_id*'s messages and delete the ones *mode* selects.

    Returns without deleting anything when the scan finds nothing, so a caller
    can render "nothing to delete" from ``result.empty`` rather than inferring
    it from three zero counters.
    """
    # Each hook is optional and defined only when a caller passed a sink. The
    # closures take their own names rather than rebinding these: an ``async
    # def`` reusing the name declares the type, which both obscures the
    # annotation and makes the ``None`` unassignable.
    scan_progress: _ProgressHook | None = None
    delete_progress: _ProgressHook | None = None

    if progress is not None:
        sink = progress  # bound once, so the closures capture a non-optional

        async def _on_scan(done: int, total: int, found: int) -> None:
            await sink.scanning(done, total, found)

        scan_progress = _on_scan

    msg_rows = await find_user_messages(
        guild,
        user_id,
        on_progress=scan_progress,
        # Filter during the scan: the returned ids carry no attachment detail,
        # so a mode that selects on content has to decide while the Message is
        # still in hand.
        predicate=(
            None if mode == MODE_ALL else lambda m: message_matches_mode(m, mode)
        ),
    )
    total = len(msg_rows)
    if total == 0:
        return DeletionResult(mode=mode, found=0)

    if progress is not None:
        sink = progress

        async def _on_delete(deleted: int, failed: int, replaced: int) -> None:
            await sink.deleting(deleted + failed + replaced, total)

        delete_progress = _on_delete

    deleted, failed, replaced = await delete_discord_messages(
        guild, user_id, msg_rows, on_progress=delete_progress
    )
    return DeletionResult(
        mode=mode,
        found=total,
        deleted=deleted,
        failed=failed,
        replaced=replaced,
    )
