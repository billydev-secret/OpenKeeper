"""The bug list's Discord surface: ``/bug`` and the bug board.

A bug is a ``todos`` row on the bug list (migration 225), so this is the todo
cog's shape with everything that does not apply cut away — no chores, no shop
orders, no sign-off, approval or confession queues. See docs/specs/todo_spec.md § The bug list.

The one real difference is who may write. ``/todo`` is moderators only. The bug
list can be opened to every member from the Bug List panel on the dashboard,
and then filing is rate-limited per member (``todo_service.bug_filing_refusal``).
Closing a bug stays moderator-only either way.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import open_db
from bot_modules.core.layout import ExpiringLayoutView, bind, card
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.todo.service import (
    bug_cards_to_strip,
    bug_filing_mode,
    bug_filing_refusal,
    bug_screenshots_open,
    clear_board,
    complete_todo,
    create_todo,
    CursorColumn,
    expire_bug_screenshots,
    forget_bug_cards,
    get_board,
    guilds_with_board,
    guilds_with_change_cards,
    last_bug_filed_at,
    LIST_BUG,
    pending_count,
    pending_todos,
    save_board,
    save_cursor_column,
    screenshot_entries,
    screenshot_refusal,
    SCREENSHOT_MAX,
    settle_bug_screenshots,
    TASK_MAX_LEN,
)
from bot_modules.todo.change_cards import Sent, deliver_cards, strip_card_images
from bot_modules.todo.change_logic import (
    CHANGE_FETCH,
    SEND_OK,
    plan_bug_cards,
    strips_by_card,
)
from bot_modules.todo.board_logic import (
    MAX_BOARD_ROWS,
    board_signature,
    render_footer,
    render_rows,
)
from bot_modules.todo.layouts import (
    BUG_ADD_ID,
    BUG_COMPLETE_ID,
    BUG_PICK_ID,
    build_bug_board_layout,
    build_complete_picker_layout,
    build_completed_layout,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

#: The bug board loop's cadence — the todo board's, for the same reasons.
_LOOP_INTERVAL = 60.0

#: How long the Complete picker listens — the todo board's, and under the 15
#: minutes an ephemeral message stays editable for the same reason.
PICKER_TIMEOUT = 180.0

_CLOSE_REFUSAL = "❌ Only moderators can close bugs."

_SCREENSHOTS_CLOSED = (
    "❌ Screenshots aren't being taken on this server's bug list — file it"
    " without one."
)


def _filed_reply(bug_id: int, text: str, shots: int) -> str:
    reply = f"🐛 Bug #{bug_id} filed: {text}"
    if shots:
        noun = "screenshot" if shots == 1 else "screenshots"
        reply += f"\n📎 Your {noun} will be posted with it in the bug channel."
    return reply


class BugAddModal(discord.ui.Modal, title="File a Bug"):
    """The bug board's File button — what broke, plus optional details."""

    task = discord.ui.TextInput(
        label="Bug",
        placeholder="What's broken?",
        max_length=TASK_MAX_LEN,
        required=True,
    )
    notes = discord.ui.TextInput(
        label="Details (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="What you did, what you expected, what happened instead",
        max_length=1000,
        required=False,
    )

    def __init__(self, *, screenshots: bool = False) -> None:
        super().__init__()
        #: Only built where there is a bug card to carry the images — see
        #: ``todo_service.bug_screenshots_open``. Absent, the modal is unchanged.
        self.upload: discord.ui.FileUpload | None = None
        if screenshots:
            self.upload = discord.ui.FileUpload(
                required=False, max_values=SCREENSHOT_MAX
            )
            self.add_item(
                discord.ui.Label(
                    text="Screenshots (optional)",
                    description="Posted with the bug in the bug channel;"
                    " removed when it's fixed.",
                    component=self.upload,
                )
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        text = str(self.task.value).strip()
        if not text:
            await interaction.response.send_message(
                "❌ Bug cannot be empty.", ephemeral=True
            )
            return
        # Checked again at submit, not only at the click: the modal can sit open
        # for minutes, across an admin closing filing or a second filing from
        # the member's /bug that started the cooldown.
        refusal = await cog.filing_refusal(interaction)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        guild_id = interaction.guild.id
        # The bug is filed whatever the screenshots say: the member has typed it
        # out, and losing that to a bad upload would be the worse outcome.
        uploads = list(self.upload.values) if self.upload is not None else []
        problem = await cog.screenshot_problem(guild_id, uploads) if uploads else None
        if problem:
            uploads = []
        bug_id = await cog.add_bug(
            guild_id,
            interaction.user.id,
            text,
            description=str(self.notes.value).strip() or None,
            screenshots=uploads,
        )
        reply = _filed_reply(bug_id, text, len(uploads))
        if problem:
            reply += f"\n{problem} The bug was filed without them."
        await interaction.response.send_message(reply, ephemeral=True)
        await cog.refresh_board(guild_id)


class BugCompleteView(ExpiringLayoutView):
    """The ephemeral picker listing the open bugs a mod can close."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__(timeout=PICKER_TIMEOUT)
        for item in build_complete_picker_layout(rows, bugs=True):
            self.add_item(item)
        self.select = next(
            i for i in self.walk_children() if isinstance(i, discord.ui.Select)
        )
        bind(self, BUG_PICK_ID, self.picked)

    async def picked(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        # The picker outlives the click that opened it, so the gate is asked
        # again here rather than trusted from the button.
        if not cog.is_mod(interaction):
            await interaction.response.send_message(_CLOSE_REFUSAL, ephemeral=True)
            return
        guild_id = interaction.guild.id
        done = await cog.complete_bugs(
            guild_id, interaction.user.id, [int(v) for v in self.select.values]
        )
        # Stopped before the outcome replaces it — see ``TodoCompleteView``.
        self.stop()
        await interaction.response.edit_message(
            view=card(build_completed_layout(done, bugs=True))
        )
        await cog.refresh_board(guild_id)


class BugBoardView(discord.ui.LayoutView):
    """The bug board: its card and its persistent buttons — static custom_ids,
    like the todo board, and built bare for the copy ``cog_load`` registers."""

    def __init__(self, items: list[discord.ui.Item] | None = None) -> None:
        super().__init__(timeout=None)
        if items is None:
            items = build_bug_board_layout(render_rows(()), render_footer(0, noun="bug"))
        for item in items:
            self.add_item(item)
        bind(self, BUG_ADD_ID, self.add)
        bind(self, BUG_COMPLETE_ID, self.complete)

    async def add(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        refusal = await cog.filing_refusal(interaction)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.send_modal(
            BugAddModal(screenshots=await cog.screenshots_open(interaction.guild.id))
        )

    async def complete(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        if not cog.is_mod(interaction):
            await interaction.response.send_message(_CLOSE_REFUSAL, ephemeral=True)
            return
        guild_id = interaction.guild.id

        def _load() -> list[dict]:
            with cog.bot.ctx.open_db() as conn:
                return [
                    dict(r)
                    for r in pending_todos(conn, guild_id, limit=25, list_key=LIST_BUG)
                ]

        rows = await asyncio.to_thread(_load)
        if not rows:
            await interaction.response.send_message(
                "No open bugs — the list is already clear. ✨", ephemeral=True
            )
            return
        view = BugCompleteView(rows)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


def _resolve_cog(interaction: discord.Interaction) -> "BugCog | None":
    """Resolve the cog at click time — the board outlives cog reloads."""
    bot = cast("Bot", interaction.client)
    return cast("BugCog | None", bot.get_cog("BugCog"))


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "❌ The bug list isn't available right now.", ephemeral=True
    )


class BugCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.board = StickyPanel(
            "bug board",
            bot,
            load_ids=self._read_ids,
            save_ids=self._write_ids,
            build=self.build_panel,
            refresh_on_update=True,
        )
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_view(BugBoardView())

    async def cog_unload(self) -> None:
        self.board.cancel_all()

    # ── slash command ────────────────────────────────────────────────────

    @app_commands.command(name="bug", description="File a bug on the server's bug list.")
    @app_commands.describe(
        bug="What's broken? Say what you did and what happened.",
        screenshot="An image of the problem, posted with the bug in the bug channel.",
    )
    async def bug(
        self,
        interaction: discord.Interaction,
        bug: str,
        screenshot: discord.Attachment | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        text = bug.strip()
        if not text:
            await interaction.response.send_message(
                "❌ Bug cannot be empty.", ephemeral=True
            )
            return
        if len(text) > TASK_MAX_LEN:
            await interaction.response.send_message(
                f"❌ A bug must be {TASK_MAX_LEN} characters or fewer.", ephemeral=True
            )
            return
        refusal = await self.filing_refusal(interaction)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        guild_id = interaction.guild.id
        # A slash option can't be hidden per server, so where screenshots are
        # off the option is refused here, before anything is filed — the text
        # is still in the member's command box to send again without it.
        uploads = [screenshot] if screenshot is not None else []
        problem = await self.screenshot_problem(guild_id, uploads) if uploads else None
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        bug_id = await self.add_bug(
            guild_id, interaction.user.id, text, screenshots=uploads
        )
        await interaction.response.send_message(
            _filed_reply(bug_id, text, len(uploads)), ephemeral=True
        )
        await self.refresh_board(guild_id)

    # ── gates and writes ─────────────────────────────────────────────────

    def is_mod(self, interaction: discord.Interaction) -> bool:
        """The todo list's moderator tier — ``AppContext.is_mod``, as ``/todo`` uses."""
        return isinstance(interaction.user, discord.Member) and bool(
            self.bot.ctx.is_mod(interaction)
        )

    async def filing_refusal(self, interaction: discord.Interaction) -> str | None:
        """Why this member may not file right now, or None if they may.

        Moderators skip the read entirely: nothing stored can refuse them.
        """
        if interaction.guild is None:
            return "❌ Server only."
        if self.is_mod(interaction):
            return None
        guild_id, user_id = interaction.guild.id, interaction.user.id

        def _read() -> tuple[str, float | None]:
            with self.bot.ctx.open_db() as conn:
                return (
                    bug_filing_mode(conn, guild_id),
                    last_bug_filed_at(conn, guild_id, user_id),
                )

        mode, last = await asyncio.to_thread(_read)
        return bug_filing_refusal(
            is_mod=False, mode=mode, last_filed_at=last, now=time.time()
        )

    async def screenshots_open(self, guild_id: int) -> bool:
        def _read() -> bool:
            with self.bot.ctx.open_db() as conn:
                return bug_screenshots_open(conn, guild_id)

        return await asyncio.to_thread(_read)

    async def screenshot_problem(
        self, guild_id: int, uploads: list[discord.Attachment]
    ) -> str | None:
        """Why these uploads can't ride a bug card, or None if they can."""
        if not await self.screenshots_open(guild_id):
            return _SCREENSHOTS_CLOSED
        return screenshot_refusal([(a.content_type, a.size) for a in uploads])

    async def add_bug(
        self,
        guild_id: int,
        user_id: int,
        text: str,
        *,
        description: str | None = None,
        screenshots: list[discord.Attachment] | None = None,
    ) -> int:
        """File a bug. Shared by ``/bug`` and the board's File button.

        ``screenshots`` must already have passed ``screenshot_problem``; only
        their upload links are held, until the next bug card posts them.

        Does not repaint: callers answer the interaction first (see
        ``TodoCog.add_todo`` for the rate-limit hazard that rule avoids).
        """
        entries = screenshot_entries([(a.url, a.filename) for a in screenshots or ()])

        def _create() -> int:
            with self.bot.ctx.open_db() as conn:
                return create_todo(
                    conn,
                    guild_id,
                    user_id,
                    text,
                    description=description,
                    screenshots=entries,
                    list_key=LIST_BUG,
                )

        return await asyncio.to_thread(_create)

    async def complete_bugs(
        self, guild_id: int, user_id: int, bug_ids: list[int]
    ) -> int:
        """Close the picked bugs; returns how many this call actually closed.

        Scoped to the bug list in the service, so a todo id smuggled into the
        picker's values closes nothing.
        """

        def _complete() -> int:
            with self.bot.ctx.open_db() as conn:
                return sum(
                    1
                    for bug_id in bug_ids
                    if complete_todo(conn, bug_id, guild_id, user_id, list_key=LIST_BUG)
                )

        return await asyncio.to_thread(_complete)

    # ── board plumbing ───────────────────────────────────────────────────

    def _read_ids(self, guild_id: int) -> tuple[int, int]:
        with self.bot.ctx.open_db() as conn:
            board = get_board(conn, guild_id, list_key=LIST_BUG)
        return board.channel_id, board.message_id

    def _write_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.bot.ctx.open_db() as conn:
            if channel_id and message_id:
                save_board(conn, guild_id, channel_id, message_id, list_key=LIST_BUG)
            else:
                clear_board(conn, guild_id, list_key=LIST_BUG)

    def _read_rows(self, guild_id: int) -> tuple[list[dict], int]:
        with self.bot.ctx.open_db() as conn:
            # One screenful plus a sentinel, and the true total for the footer —
            # the same read the todo board's task section makes.
            rows = [
                dict(r)
                for r in pending_todos(
                    conn, guild_id, limit=MAX_BOARD_ROWS + 1, list_key=LIST_BUG
                )
            ]
            total = pending_count(conn, guild_id, list_key=LIST_BUG)
        return rows, total

    async def build_panel(self, guild: discord.Guild) -> PanelContent:
        rows, total = await asyncio.to_thread(self._read_rows, guild.id)
        return PanelContent(
            layout=BugBoardView(
                build_bug_board_layout(
                    render_rows(rows, total=total), render_footer(total, noun="bug")
                )
            ),
            signature=board_signature(rows, total),
        )

    # ── change cards ─────────────────────────────────────────────────────
    #
    # The todo board's change cards, on the bug board's own switch. Arrivals
    # only: no noon digest (that card is about chores) and no name lookup, since
    # a bug card never names its filer — see ``change_logic.bug_line``.

    def _read_change_data(self, guild_id: int) -> dict:
        """Bugs that arrived since the cursor. Blocking; one connection.

        The clock is taken inside the read, for the reason
        ``TodoCog._read_change_data`` gives, and the arrival query is skipped
        while the cursor is NULL because that tick only banks the clock.
        """
        with self.bot.ctx.open_db() as conn:
            board = get_board(conn, guild_id, list_key=LIST_BUG)
            now = time.time()
            cursor = board.change_cursor_at
            bugs = (
                [
                    dict(r)
                    for r in pending_todos(
                        conn,
                        guild_id,
                        limit=CHANGE_FETCH,
                        since_ts=cursor,
                        list_key=LIST_BUG,
                    )
                ]
                if cursor is not None
                else []
            )
        return {
            "now": now,
            "channel_id": board.channel_id,
            "cursor_at": cursor,
            "bugs": bugs,
        }

    async def post_change_cards(
        self, guild: discord.Guild, *, announce: bool = True
    ) -> bool:
        """One guild's bug-card pass. True if a card was posted.

        Posted and then the board re-stuck beneath it, exactly as
        ``TodoCog.post_change_cards`` does and for the same reasons.
        """
        data = await asyncio.to_thread(self._read_change_data, guild.id)
        channel_id = int(data["channel_id"] or 0)
        if not channel_id:
            return False
        posted = await deliver_cards(
            guild,
            channel_id,
            plan_bug_cards(data=data, announce=announce),
            save_cursor=lambda column, value: self._save_cursor(
                guild.id, column, value
            ),
            label="bugs",
            fetch=self.bot.http.get_from_cdn,
            settle=lambda cursor, sent: self._settle_screenshots(
                guild.id, channel_id, cursor, sent
            ),
        )
        if posted:
            self.board.schedule_restick(guild.id)
        return posted

    def _settle_screenshots(
        self, guild_id: int, channel_id: int, cursor: float, sent: Sent
    ) -> None:
        with self.bot.ctx.open_db() as conn:
            posted = sent.outcome == SEND_OK and sent.message is not None
            settle_bug_screenshots(
                conn,
                guild_id,
                upto_ts=cursor,
                posted_bug_ids=sent.image_bug_ids if posted else (),
                channel_id=channel_id,
                message_id=sent.message.id if posted and sent.message else 0,
            )

    async def strip_closed_screenshots(self) -> None:
        """Edit closed (or erased filers') bugs' screenshots off their cards.

        Every guild in one read, and whatever the change-card switch says now:
        images already posted come down when their bug closes even if the
        switch was turned off in between.
        """

        def _read() -> list[dict]:
            with self.bot.ctx.open_db() as conn:
                expire_bug_screenshots(conn, now=time.time())
                return [dict(r) for r in bug_cards_to_strip(conn)]

        rows = await asyncio.to_thread(_read)
        for (guild_id, channel_id, message_id), bug_ids in strips_by_card(rows).items():
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild is not None else None
            if not await strip_card_images(
                channel, message_id, bug_ids,
                label="bugs", fetch=self.bot.http.get_from_cdn,
            ):
                continue
            await asyncio.to_thread(self._forget_cards, bug_ids)

    def _forget_cards(self, bug_ids: list[int]) -> None:
        with self.bot.ctx.open_db() as conn:
            forget_bug_cards(conn, bug_ids)

    def _save_cursor(
        self, guild_id: int, column: CursorColumn, value: float
    ) -> None:
        with self.bot.ctx.open_db() as conn:
            save_cursor_column(conn, guild_id, column, value, list_key=LIST_BUG)

    async def place_board(
        self, guild: discord.Guild, target: discord.TextChannel
    ) -> discord.Message | None:
        return await self.board.place(guild, target)

    async def unpost_board(self, guild: discord.Guild) -> bool:
        return await self.board.unpost(guild)

    async def refresh_board(self, guild_id: int) -> bool:
        return await self.board.refresh(guild_id)

    @commands.Cog.listener("on_message")
    async def _restick_board(self, message: discord.Message) -> None:
        await self.board.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_board_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        await self.board.on_channel_delete(channel)


def _work_lists(ctx) -> tuple[set[int], set[int]]:
    """``(guilds_with_a_bug_board, guilds_with_bug_cards_on)``, in one read."""
    with open_db(ctx.db_path) as conn:
        return (
            set(guilds_with_board(conn, list_key=LIST_BUG)),
            set(guilds_with_change_cards(conn, list_key=LIST_BUG)),
        )


async def bug_board_loop(bot: Bot) -> None:
    """Keep the bug boards honest: publish the board set, retry failed edits,
    and post change cards where the bug board's switch is on.

    Every write to the bug list (``/bug``, the board, the dashboard) repaints
    the board itself, and nothing spawns bugs on a schedule, so a quiet tick
    only drains retries. The first pass repaints every board once, which heals
    one that drifted while the bot was down.
    """
    await bot.wait_until_ready()
    first_pass = True
    while not bot.is_closed():
        try:
            boards, card_guilds = await asyncio.to_thread(_work_lists, bot.ctx)
            booting = first_pass
            cog = cast("BugCog | None", bot.get_cog("BugCog"))
            if cog is not None:
                cog.board.set_known_guilds(boards)
                work = cog.board.take_retries() & boards
                if first_pass:
                    work |= boards
                    first_pass = False
                for guild_id in sorted(work):
                    try:
                        await cog.refresh_board(guild_id)
                    except Exception:
                        log.exception("bug board refresh failed for %s", guild_id)
                # After the repaints, and silent on boot — both for the reasons
                # ``todo_board_loop`` gives.
                for guild_id in sorted(card_guilds & boards):
                    guild = bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    try:
                        await cog.post_change_cards(guild, announce=not booting)
                    except Exception:
                        log.exception("bug change cards failed for %s", guild_id)
                try:
                    await cog.strip_closed_screenshots()
                except Exception:
                    log.exception("bug screenshot removal failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bug board loop tick failed")
        await asyncio.sleep(_LOOP_INTERVAL)


async def setup(bot: Bot) -> None:
    await bot.add_cog(BugCog(bot))
