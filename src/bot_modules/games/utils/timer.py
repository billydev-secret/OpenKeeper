import asyncio
import time
import logging
import discord

log = logging.getLogger(__name__)


def format_deadline(deadline: int) -> str:
    """Render a Discord dynamic relative timestamp that ticks down on the client."""
    return f"⏰ ends <t:{deadline}:R>"


def now_plus(seconds: int) -> int:
    """Return a Unix timestamp `seconds` from now."""
    return int(time.time()) + seconds


class GameTimer:
    """Countdown timer that ticks down live on Discord clients via <t:UNIX:R>.

    The bot only edits the message twice: once at start to install the dynamic
    timestamp, and once when the timer expires (color flash + callback).
    """

    def __init__(
        self,
        duration: int,
        message: discord.Message,
        callback,
        timer_field_index: int = 0,
        timer_item_id: int | None = None,
    ):
        self.duration = duration
        self.message = message
        self.callback = callback
        self.timer_field_index = timer_field_index
        #: Stable numeric id of the card's timer ``TextDisplay``, for a
        #: Components V2 board. Set instead of relying on
        #: ``timer_field_index``, which only means anything on an embed;
        #: ``None`` (the default) keeps every embed-shaped caller on
        #: ``_install_field`` untouched.
        self.timer_item_id = timer_item_id
        self.deadline: int = 0
        self._task: asyncio.Task | None = None
        self._cancelled = False
        self._skipped = False

    async def start(self):
        if self.duration <= 0:
            return
        self.deadline = now_plus(self.duration)
        if self.timer_item_id is not None:
            await self._install_item()
        else:
            await self._install_field()
        self._task = asyncio.create_task(self._run())

    async def _install_field(self):
        """Install the dynamic timestamp once. Skipped if the embed builder
        already rendered a `<t:...:R>` markup in the timer field."""
        try:
            if not self.message.embeds:
                return
            embed = self.message.embeds[0]
            if self.timer_field_index >= len(embed.fields):
                return
            field = embed.fields[self.timer_field_index]
            if field.value and "<t:" in field.value:
                return  # Already installed by the embed builder
            new_embed = embed.copy()
            new_embed.set_field_at(
                self.timer_field_index,
                name=field.name,
                value=format_deadline(self.deadline),
                inline=field.inline,
            )
            await self.message.edit(embed=new_embed)
        except Exception as e:  # noqa: BLE001 — runs before start() creates the countdown task; a cosmetic timestamp edit must never leave the game without its timer
            log.debug("Timer field install error: %s", e)

    async def _install_item(self):
        """Install the dynamic timestamp once, in a Components V2 card.

        The layout sibling of ``_install_field``. There is no ``embed.copy()``
        for a layout: the card's own ``TextDisplay`` *is* the message body,
        so this reads the message's current component tree back with
        ``LayoutView.from_message`` (the message's own state, not whatever
        the caller's in-memory view happens to hold — a restick may have
        moved the board since), finds the stable-id item with ``find_item``,
        and edits its ``content`` in place before re-sending the same view.
        Skipped, the same way, if the builder already rendered a live
        ``<t:...:R>`` into that item.
        """
        assert self.timer_item_id is not None
        try:
            if not self.message.components:
                return
            view = discord.ui.LayoutView.from_message(self.message, timeout=None)
            item = view.find_item(self.timer_item_id)
            if not isinstance(item, discord.ui.TextDisplay):
                return
            if item.content and "<t:" in item.content:
                return  # Already installed by the layout builder
            item.content = format_deadline(self.deadline)
            await self.message.edit(content=None, embed=None, attachments=[], view=view)
        # Broad on purpose: from_message parses a component tree a newer
        # Discord may have grown, and a failed install only leaves the card's
        # static deadline showing — never worth killing the timer task.
        except Exception as e:  # noqa: BLE001 — see the comment above: a failed install must never kill the timer task
            log.debug("Timer item install error: %s", e)

    async def _run(self):
        # Sleep until the deadline (or until cancelled/skipped)
        try:
            remaining = self.deadline - int(time.time())
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass  # Either skip() or cancel() — decide below

        if self._cancelled:
            return

        try:
            await self.callback()
        except Exception:
            log.exception("GameTimer callback error")

    def cancel(self):
        self._cancelled = True
        if self._task:
            self._task.cancel()

    def skip(self):
        """Immediately fire the callback by cancelling the wait."""
        self._skipped = True
        if self._task:
            self._task.cancel()

    @property
    def remaining(self) -> int:
        """Seconds left until deadline (clamped to 0)."""
        if self.deadline == 0:
            return self.duration
        return max(0, self.deadline - int(time.time()))
