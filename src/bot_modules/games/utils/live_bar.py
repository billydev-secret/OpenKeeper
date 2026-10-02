import asyncio
import time
import logging
import discord

from bot_modules.core.meters import fill, mono

log = logging.getLogger(__name__)

BAR_WIDTH = 14


def build_bar(count: int, total: int, width: int = BAR_WIDTH) -> tuple[str, str]:
    """Returns (bar_string, percentage_string) for a live vote meter.

    The bar comes back already wrapped in a code span. It has to: these bars
    sit next to each other as one field per option, and a bare ``▰▱`` run
    renders proportionally, so a 0% bar is visibly *longer* than a 50% one
    even though both are exactly ``width`` characters. See
    ``bot_modules.core.meters`` for the full explanation.
    """
    pct = f"{round(count / total * 100)}%" if total else "0%"
    return mono(fill(count, total, width)), pct


class LiveBarUpdater:
    """Rate-limits embed edits to once per 3 seconds to avoid Discord API rate limits."""

    def __init__(self, min_interval: float = 3.0):
        self._last_update: float = 0.0
        self._pending: bool = False
        self._min_interval = min_interval
        self._lock = asyncio.Lock()

    async def _claim(self) -> bool:
        """Wait out the rate limit and claim the next slot.

        False when another update is already queued for that slot — this one
        is dropped, since it would draw the same bars a moment later.
        """
        async with self._lock:
            now = time.monotonic()
            gap = now - self._last_update
            if gap >= self._min_interval:
                self._last_update = now
                self._pending = False
                return True
            if self._pending:
                return False  # Another update is already queued; drop this one
            self._pending = True

        # Wait outside the lock, so the votes arriving meanwhile can see
        # _pending and drop themselves rather than queue up behind us.
        await asyncio.sleep(self._min_interval - gap)
        async with self._lock:
            self._last_update = time.monotonic()
            self._pending = False
        return True

    async def schedule_update(self, message: discord.Message | None, build_embed_fn):
        """
        Call build_embed_fn() to get the new embed and edit the message.
        Rate-limited to once per min_interval seconds. Accepts None (no-op)
        so callers can pass interaction.message without narrowing.
        """
        if message is None:
            return
        if not await self._claim():
            return
        try:
            embed = build_embed_fn()
            await message.edit(embed=embed)
        except Exception as e:  # noqa: BLE001 — build_embed_fn is caller-supplied per-game render logic; a render must never break the caller (see schedule_render)
            log.debug("LiveBarUpdater edit error: %s", e)

    async def schedule_update_layout(
        self, message: discord.Message | None, view: discord.ui.LayoutView
    ) -> None:
        """The Components V2 sibling of :meth:`schedule_update`.

        A layout card has no fresh object a builder hands back the way
        ``build_embed_fn()`` does — the view *is* the message body — so the
        caller rebuilds *view*'s items in place (its own ``_rebuild``, or
        equivalent) before calling this, and this only re-sends the same
        instance. ``content=None, embed=None, attachments=[]`` turns an
        edit that used to carry an embed into a bare Components V2 one; on a
        message that was already a layout these are no-ops.
        """
        if message is None:
            return
        if not await self._claim():
            return
        try:
            await message.edit(content=None, embed=None, attachments=[], view=view)
        except discord.HTTPException as e:
            log.debug("LiveBarUpdater layout edit error: %s", e)

    async def schedule_render(self, render_fn):
        """Rate-limited redraw where the *caller* owns the edit.

        The same gate as :meth:`schedule_update`, for a board whose current
        location the caller has to look up rather than hold: a live board on
        the "keep it at the bottom" dial, where a restick may have reposted
        the board since the caller captured a message to edit (see
        ``game_board_sticky_service.redraw_board``).
        """
        if not await self._claim():
            return
        try:
            await render_fn()
        except Exception as e:  # noqa: BLE001 — render_fn is caller-owned; a render must never break the caller
            log.debug("LiveBarUpdater render error: %s", e)
