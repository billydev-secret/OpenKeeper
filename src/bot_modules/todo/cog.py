from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import get_tz_offset_hours, open_db, open_db_immediate
from bot_modules.core.layout import ExpiringLayoutView, bind, card
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.confessions.approval_views import open_confessions_picker
from bot_modules.economy.approval_views import open_approvals_picker
from bot_modules.economy.quest_views import open_signoff_picker
from bot_modules.confessions.service import (
    pending_confession_count,
    pending_confessions,
)
from bot_modules.economy.approvals_service import (
    pending_approval_count,
    pending_approvals,
)
from bot_modules.economy.quests_service import (
    pending_signoff_count,
    pending_signoff_rows,
)
from bot_modules.economy.service import load_econ_settings
from bot_modules.shared import feature_roles as fr
from bot_modules.moderation.settings import staff_ping_service
from bot_modules.todo.recurring_service import (
    chore_board_rows,
    due_recurring,
    spawn_due,
)
from bot_modules.shared.name_resolver import build_name_fn
from bot_modules.todo.service import (
    chore_changes_since,
    clear_board,
    complete_todo,
    create_todo,
    CursorColumn,
    get_board,
    guilds_with_board,
    guilds_with_change_cards,
    pending_count,
    pending_todos,
    save_board,
    save_cursor_column,
    TASK_MAX_LEN,
)
from bot_modules.todo.change_cards import deliver_cards
from bot_modules.todo.change_logic import (
    CHANGE_FETCH,
    local_noon_before,
    plan_cards,
    subject_ids,
)
from bot_modules.todo.board_logic import (
    MAX_APPROVAL_ROWS,
    MAX_BOARD_ROWS,
    MAX_CONFESSION_ROWS,
    MAX_SIGNOFF_ROWS,
    board_content_signature,
    board_sections,
    completable_options,
    nothing_to_tick_message,
    render_board_footer,
)
from bot_modules.todo.layouts import (
    TODO_ADD_ID,
    TODO_APPROVALS_ID,
    TODO_COMPLETE_ID,
    TODO_CONFESSIONS_ID,
    TODO_PICK_ID,
    TODO_SIGNOFFS_ID,
    build_complete_picker_layout,
    build_completed_layout,
    build_todo_board_layout,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

#: How often the background loop spawns due recurring tasks and repaints boards.
_LOOP_INTERVAL = 60.0

#: How many chores the board reads. It renders a bounded slice and reports the
#: rest as a count, and the footer's "N of M done" has to count the overflow
#: too — so unlike the task list this fetches a real slice rather than one
#: sentinel row past the window. Active recurring definitions are a handful per
#: guild; a server past this many has a dashboard problem, not a board problem.
_CHORE_FETCH = 50

#: How long the Complete picker listens. Ephemeral, so it must stay under the
#: 15 minutes Discord accepts edits for, or the grey-out edit fails silently.
PICKER_TIMEOUT = 180.0


class TodoAddModal(discord.ui.Modal, title="Add a Task"):
    """The board's Add button — headline plus optional notes."""

    task = discord.ui.TextInput(
        label="Task",
        placeholder="What needs doing?",
        max_length=TASK_MAX_LEN,
        required=True,
    )
    notes = discord.ui.TextInput(
        label="Notes (optional)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        text = str(self.task.value).strip()
        if not text:
            await interaction.response.send_message(
                "❌ Task cannot be empty.", ephemeral=True
            )
            return
        guild_id = interaction.guild.id
        todo_id = await cog.add_todo(
            guild_id,
            interaction.user.id,
            text,
            description=str(self.notes.value).strip() or None,
        )
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {text}", ephemeral=True
        )
        await cog.refresh_board(guild_id)


class TodoCompleteView(ExpiringLayoutView):
    """The ephemeral picker listing the pending tasks a mod can tick off."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__(timeout=PICKER_TIMEOUT)
        for item in build_complete_picker_layout(rows):
            self.add_item(item)
        self.select = next(
            i for i in self.walk_children() if isinstance(i, discord.ui.Select)
        )
        bind(self, TODO_PICK_ID, self.picked)

    async def picked(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        ids = [int(v) for v in self.select.values]

        def _complete() -> int:
            done = 0
            with cog.bot.ctx.open_db() as conn:
                for todo_id in ids:
                    if complete_todo(conn, todo_id, guild_id, user_id):
                        done += 1
            return done

        done = await asyncio.to_thread(_complete)
        # The outcome replaces this view on the message, so its timer must not
        # outlive it and edit the greyed-out picker back over the receipt.
        self.stop()
        await interaction.response.edit_message(
            view=card(build_completed_layout(done))
        )
        await cog.refresh_board(guild_id)


class TodoBoardView(discord.ui.LayoutView):
    """The board: its card and its persistent buttons.

    Carries no per-message state, so it's a static-custom_id view (the
    ShopPanelView pattern) re-registered in ``cog_load`` rather than a
    DynamicItem — one board per guild, and the ids never vary. The instance
    registered at cog load is built with no ``items``: a persistent view
    dispatches on ``custom_id`` alone, so the text only matters on the copy
    that is actually posted.
    """

    def __init__(self, items: list[discord.ui.Item] | None = None) -> None:
        super().__init__(timeout=None)
        if items is None:
            items = build_todo_board_layout((), render_board_footer((), 0))
        for item in items:
            self.add_item(item)
        bind(self, TODO_ADD_ID, self.add)
        bind(self, TODO_COMPLETE_ID, self.complete)
        bind(self, TODO_SIGNOFFS_ID, self.signoffs)
        bind(self, TODO_APPROVALS_ID, self.approvals)
        bind(self, TODO_CONFESSIONS_ID, self.confessions)

    async def add(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None:
            await _unavailable(interaction)
            return
        if not await _require_mod(interaction, cog.bot.ctx):
            return
        await interaction.response.send_modal(TodoAddModal())

    async def complete(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        if not await _require_mod(interaction, cog.bot.ctx):
            return
        guild_id = interaction.guild.id

        def _load() -> tuple[list[dict], str]:
            """Everything tickable, in the order the board shows it.

            One button over both sections: with a single board there is no
            longer a "which list was it on?" for a mod to answer first, and the
            thing being completed is a todo row either way.
            """
            with cog.bot.ctx.open_db() as conn:
                # The same slice the board renders — reading fewer than it
                # shows lets an "already clear" message appear over visibly
                # open rows. The picker caps at Discord's 25.
                chores = [
                    dict(r) for r in chore_board_rows(conn, guild_id, limit=_CHORE_FETCH)
                ]
                tasks = [
                    dict(r)
                    for r in pending_todos(
                        conn, guild_id, limit=25, exclude_chores=True
                    )
                ]
            options = [dict(o) for o in completable_options(chores, tasks)]
            # The chore-aware wording only earns its place when chores are the
            # reason there is nothing to offer; with tasks outstanding the list
            # is never empty anyway.
            empty = (
                nothing_to_tick_message(chores)
                if chores
                else "Nothing pending — the list is already clear. ✨"
            )
            return options, empty

        rows, empty_message = await asyncio.to_thread(_load)
        if not rows:
            await interaction.response.send_message(empty_message, ephemeral=True)
            return
        view = TodoCompleteView(rows)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def signoffs(self, interaction: discord.Interaction) -> None:
        """Review the quest claims waiting on a mod.

        Deliberately *not* behind ``_require_mod``: approving pays real
        currency, so the economy's own manager gate applies, and it lives with
        the rest of the sign-off flow in ``economy/quest_views.py`` — this
        button is only the door onto the board.
        """
        if _resolve_cog(interaction) is None:
            await _unavailable(interaction)
            return
        await open_signoff_picker(interaction)

    async def approvals(self, interaction: discord.Interaction) -> None:
        """Review the paid requests waiting on a mod — themes, questions, pins.

        Same deal as Sign-Offs: deliberately *not* behind ``_require_mod``,
        because every decision behind it moves currency and the economy's own
        manager gate applies. This button is only the door onto the board.
        """
        if _resolve_cog(interaction) is None:
            await _unavailable(interaction)
            return
        await open_approvals_picker(interaction)

    async def confessions(self, interaction: discord.Interaction) -> None:
        """Review the confessions mod-approve mode is holding.

        The fifth and last button the board can carry — Discord allows five per
        action row, and this fills the row exactly.

        Unlike Sign-Offs and Approvals this one *is* the board's own moderator
        tier, applied inside the picker: no currency moves behind it, so there
        is no narrower economy gate to defer to. The picker gates itself rather
        than relying on this button, because the ephemeral card it opens
        outlives the click.
        """
        if _resolve_cog(interaction) is None:
            await _unavailable(interaction)
            return
        await open_confessions_picker(interaction)


def _resolve_cog(interaction: discord.Interaction) -> "TodoCog | None":
    """Resolve the cog at click time — the board outlives cog reloads."""
    bot = cast("Bot", interaction.client)
    return cast("TodoCog | None", bot.get_cog("TodoCog"))


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "❌ The todo list isn't available right now.", ephemeral=True
    )


async def _require_mod(
    interaction: discord.Interaction,
    ctx: AppContext,
    action: str = "manage the todo list",
) -> bool:
    """The one moderator gate for every Discord surface of the todo list —
    `/todo` and both board buttons.

    Delegates to ``AppContext.is_mod``: Discord's administrator/manage_guild
    short-circuit, then the guild's configured ``mod_role_ids``/
    ``admin_role_ids``. That is the bot's house definition of a moderator, and
    ``web_server.auth.resolve_guild_perms`` resolves the dashboard's
    ``moderator`` tier the same way — so a mod refused here is refused there,
    and a mod who can tick a chore off on the dashboard can tick it off in the
    channel. This gate used to read the games cogs'
    ``has_mod_or_admin_permissions`` (administrator/manage_guild/manage_channels
    and nothing else), which refused real moderators the dashboard let in.
    """
    user = interaction.user
    if not isinstance(user, discord.Member) or not ctx.is_mod(interaction):
        await interaction.response.send_message(
            f"❌ Only moderators can {action}.", ephemeral=True
        )
        return False
    return True


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.board = StickyPanel(
            "todo board",
            bot,
            load_ids=self._read_ids,
            save_ids=self._write_ids,
            build=self.build_panel,
            refresh_on_update=True,
        )
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_view(TodoBoardView())

    async def cog_unload(self) -> None:
        self.board.cancel_all()

    # ── slash command ────────────────────────────────────────────────────

    @app_commands.command(name="todo", description="Add a task to the server todo list.")
    @app_commands.describe(task="The task to add.")
    async def todo(self, interaction: discord.Interaction, task: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        # The todo list is a mod worklist, curated from the dashboard — only
        # moderators may add to it (the web endpoints are mod-gated too).
        if not await _require_mod(interaction, self.bot.ctx, "add to the todo list"):
            return
        task = task.strip()
        if not task:
            await interaction.response.send_message("❌ Task cannot be empty.", ephemeral=True)
            return
        if len(task) > TASK_MAX_LEN:
            await interaction.response.send_message(
                f"❌ Task must be {TASK_MAX_LEN} characters or fewer.", ephemeral=True
            )
            return
        guild_id = interaction.guild.id
        todo_id = await self.add_todo(guild_id, interaction.user.id, task)
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )
        await self.refresh_board(guild_id)

    async def add_todo(
        self,
        guild_id: int,
        user_id: int,
        task: str,
        *,
        description: str | None = None,
    ) -> int:
        """Create a task. Shared by `/todo` and the board's Add button.

        Deliberately does *not* repaint the board: callers must answer the
        interaction first. A repaint is a REST edit, and under per-channel rate
        limiting discord.py sleeps until reset — long enough to burn the
        three-second window and show "This interaction failed" for a task that
        was in fact saved.
        """

        def _create() -> int:
            with self.bot.ctx.open_db() as conn:
                return create_todo(
                    conn, guild_id, user_id, task, description=description
                )

        return await asyncio.to_thread(_create)

    # ── board plumbing ───────────────────────────────────────────────────
    #
    # The sticky machinery (locks, debounce, id cache, delete-and-repost) lives
    # in core.sticky.StickyPanel; this cog only says where the ids are stored
    # and what the panel should look like.

    def _read_ids(self, guild_id: int) -> tuple[int, int]:
        with self.bot.ctx.open_db() as conn:
            board = get_board(conn, guild_id)
        return board.channel_id, board.message_id

    def _write_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.bot.ctx.open_db() as conn:
            if channel_id and message_id:
                save_board(conn, guild_id, channel_id, message_id)
            else:
                clear_board(conn, guild_id)

    def _read_rows(self, guild_id: int) -> dict:
        with self.bot.ctx.open_db() as conn:
            # Pending sign-offs are read straight from the claims table rather
            # than mirrored into todos: nothing to keep in sync, and the
            # Complete button can never offer one (a claim is approved, not
            # ticked off). One row past the visible window, same sentinel
            # trick the task list uses, plus the true total for the footer.
            signoffs = [
                dict(r)
                for r in pending_signoff_rows(
                    conn, guild_id, limit=MAX_SIGNOFF_ROWS + 1
                )
            ]
            # Both only matter when something is waiting: a guild with no
            # claims — the normal state, and every guild with the economy off
            # — pays nothing for the section it isn't rendering.
            signoff_total = pending_signoff_count(conn, guild_id) if signoffs else 0
            # The three paid queues, read the same way and for the same
            # reason: a submission is not a todo row, so nothing is mirrored
            # and resolving it anywhere takes it off the board.
            approvals = pending_approvals(
                conn, guild_id, limit=MAX_APPROVAL_ROWS + 1
            )
            approval_total = (
                pending_approval_count(conn, guild_id) if approvals else 0
            )
            # Confessions held by mod-approve mode, read the same way again.
            # A guild with the mode off never has a row here, so the section
            # and its two queries cost nothing until somebody turns it on.
            confessions = pending_confessions(
                conn, guild_id, limit=MAX_CONFESSION_ROWS + 1
            )
            confession_total = (
                pending_confession_count(conn, guild_id) if confessions else 0
            )
            currency_emoji = (
                load_econ_settings(conn, guild_id).currency_emoji
                if (signoffs or approvals)
                else ""
            )
            chores = [
                dict(r) for r in chore_board_rows(conn, guild_id, limit=_CHORE_FETCH)
            ]
            # One screenful plus a sentinel: enough to render and to know the
            # list overflows, without hauling every pending row. Chore-spawned
            # rows are excluded — the chores section above shows them, with
            # more state than a task line can carry.
            rows = [
                dict(r)
                for r in pending_todos(
                    conn, guild_id, limit=MAX_BOARD_ROWS + 1, exclude_chores=True
                )
            ]
            total = pending_count(conn, guild_id, exclude_chores=True)
        return {
            "signoffs": signoffs,
            "signoff_total": signoff_total,
            "approvals": approvals,
            "approval_total": approval_total,
            "confessions": confessions,
            "confession_total": confession_total,
            "currency_emoji": currency_emoji,
            "chores": chores,
            "rows": rows,
            "total": total,
        }

    async def build_panel(self, guild: discord.Guild) -> PanelContent:
        data = await asyncio.to_thread(self._read_rows, guild.id)
        chores, rows = data["chores"], data["rows"]
        signoffs, total = data["signoffs"], data["total"]
        signoff_total = data["signoff_total"]
        approvals, approval_total = data["approvals"], data["approval_total"]
        confessions = data["confessions"]
        confession_total = data["confession_total"]
        for chore in chores:
            chore["completed_by_name"] = self._display_name(
                guild, chore.get("completed_by")
            )
        for row in rows:
            row["buyer_name"] = self._display_name(guild, row.get("buyer_id"))
        for claim in signoffs:
            claim["claimant_name"] = self._display_name(guild, claim.get("user_id"))
        for request in approvals:
            request["requester_name"] = self._display_name(
                guild, request.get("user_id")
            )
        sections = board_sections(
            chores,
            rows,
            signoff_rows=signoffs,
            signoff_total=signoff_total,
            approval_rows=approvals,
            approval_total=approval_total,
            # No name-resolution pass above for these, unlike every other
            # section: pending_confessions returns no author id at all, on
            # purpose. See confessions/approval_views.py.
            confession_rows=confessions,
            confession_total=confession_total,
            currency_emoji=data["currency_emoji"],
            task_total=total,
        )
        footer = render_board_footer(
            chores, total, signoff_total, approval_total, confession_total
        )
        return PanelContent(
            layout=TodoBoardView(build_todo_board_layout(sections, footer)),
            signature=board_content_signature(
                chores,
                rows,
                total,
                signoff_rows=signoffs,
                signoff_total=signoff_total,
                approval_rows=approvals,
                approval_total=approval_total,
                confession_rows=confessions,
                confession_total=confession_total,
            ),
        )

    def _display_name(self, guild: discord.Guild, user_id) -> str:
        """Who ticked it, as the mods know them. Never a raw id on the board."""
        if not user_id:
            return ""
        member = guild.get_member(int(user_id))
        if member is None:
            return "someone"
        return discord.utils.escape_markdown(member.display_name)

    # ── change cards ─────────────────────────────────────────────────────
    #
    # The board is edited in place, and an edit does not mark a channel unread,
    # so a change to it is invisible until a mod goes and looks. These post a
    # real message above the board when something new lands — the channel then
    # shows as modified, and the channel is also the history (todo #210).

    def _read_change_data(self, guild_id: int) -> dict:
        """Everything one guild's change cards need. Blocking; one connection.

        Reads the *pending* queries with an age filter rather than the raw
        tables, so anything a mod handled inside the window is already gone —
        see ``pending_todos``. The whole read is skipped when the cursor is
        NULL, because that tick has nothing to announce by definition and only
        needs the clock banked.

        The tick's clock is taken **here**, pinned to this transaction's
        snapshot, and handed back for the planners to store as the cursor. It
        cannot be read later in ``post_change_cards``: between this read and
        that point sits an awaited name lookup, and a row landing in that gap
        would be invisible to these queries yet still older than the cursor —
        announced by nobody, ever. Taken against the snapshot instead, the worst
        case flips to the harmless side: a row racing the first statement is
        either seen now or still newer than the cursor and seen next tick.
        """
        with open_db(self.bot.ctx.db_path) as conn:
            board = get_board(conn, guild_id)
            now = time.time()
            cursor = board.change_cursor_at
            tasks: list[dict] = []
            signoffs: list[dict] = []
            approvals: list[dict] = []
            confessions = 0
            if cursor is not None:
                tasks = [
                    dict(r)
                    for r in pending_todos(
                        conn,
                        guild_id,
                        limit=CHANGE_FETCH,
                        exclude_chores=True,
                        since_ts=cursor,
                    )
                ]
                signoffs = [
                    dict(r)
                    for r in pending_signoff_rows(
                        conn, guild_id, CHANGE_FETCH, since_ts=cursor
                    )
                ]
                approvals = [
                    dict(r)
                    for r in pending_approvals(
                        conn, guild_id, limit=CHANGE_FETCH, since_ts=cursor
                    )
                ]
                confessions = pending_confession_count(
                    conn, guild_id, since_ts=cursor
                )
            tz_offset_hours = get_tz_offset_hours(conn, guild_id)
            spawned = missed = 0
            # Only on the tick the digest can actually fire. The planner would
            # throw these counts away on the other 1,439 minutes of the day, and
            # this is the one query in the pass that has no cursor to make it
            # cheap. Same condition as ``plan_digest_card``'s, by construction:
            # a NULL banks silently and needs no counts, and a digest already
            # past today's noon has nothing left to report.
            if (
                board.digest_at is not None
                and board.digest_at < local_noon_before(now, tz_offset_hours)
            ):
                spawned, missed = chore_changes_since(conn, guild_id, board.digest_at)
            return {
                "now": now,
                "channel_id": board.channel_id,
                "cursor_at": cursor,
                "digest_at": board.digest_at,
                "tz_offset_hours": tz_offset_hours,
                "tasks": tasks,
                "signoffs": signoffs,
                "approvals": approvals,
                "confessions": confessions,
                "spawned": spawned,
                "missed": missed,
            }

    async def post_change_cards(
        self, guild: discord.Guild, *, announce: bool = True
    ) -> bool:
        """One guild's change-card pass. True if anything was posted.

        ``announce=False`` banks the clock and posts nothing — what the loop's
        boot pass wants, so a restart is not treated as news.

        The card is posted **and then the board is re-stuck beneath it**, which
        is the whole point: Billy asked for a card *above* the panel. The board
        does not restick on its own here — it is a ``restick_on_bot=False``
        panel, so the bot's own card never reaches ``on_message`` — so the
        reposition is explicit.

        No card/restick loop is possible. A card is emitted only when the poll
        finds new *rows*, and reposting the board creates no rows, so the board
        moving can never cause another card. The card is also deliberately not
        recorded as a sticky placement: ``_delayed_restick`` yields to any
        message ``was_placed`` recognises, so registering it would pin the board
        permanently *above* the cards instead of below them.

        What to say, who to name and when the watermark may move are all
        ``change_logic``'s — see ``plan_cards`` and ``cursor_after``. Sending and
        storing are ``change_cards.deliver_cards``, shared with the bug board.
        What is left here is a batched name lookup and the restick.
        """
        data = await asyncio.to_thread(self._read_change_data, guild.id)
        channel_id = int(data["channel_id"] or 0)
        if not channel_id:
            return False

        name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=guild.id,
            user_ids=subject_ids(data, announce=announce),
        )

        posted = await deliver_cards(
            guild,
            channel_id,
            plan_cards(data=data, name_fn=name_fn, announce=announce),
            save_cursor=lambda column, value: self._save_cursor(
                guild.id, column, value
            ),
            label="todo",
        )
        if posted:
            # Debounced, so a card and a member's message in the same quiet
            # period cost one repost — and so this goes through the same
            # at-the-bottom and yield-to-another-panel guards chat does.
            self.board.schedule_restick(guild.id)
        return posted

    def _save_cursor(
        self, guild_id: int, column: CursorColumn, value: float
    ) -> None:
        with self.bot.ctx.open_db() as conn:
            save_cursor_column(conn, guild_id, column, value)

    # Named pass-throughs: the routes, the loop and the tests all speak
    # "board", not "panel".

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
        """Clear the board's ids if its channel was deleted."""
        await self.board.on_channel_delete(channel)


async def repaint_board(bot, guild_id: int) -> None:
    """Repaint a guild's sticky board after an out-of-band mutation. Never raises.

    The single implementation: the quest sign-off path wraps this rather than
    keeping its own copy, so the two cannot drift into different defensive
    shapes and different log messages for the same failure.

    ``todo_board_loop`` deliberately does **not** poll for changes — it repaints
    only what it just spawned plus failed retries, because "every user-facing
    mutation repaints the board itself" (see its docstring). Automatic chore
    sign-off is such a mutation, and it happens in `events_cog` and in the game
    launch paths, neither of which holds the cog. Without this the QOTD chore
    would sit on the board looking outstanding until the next daily spawn — the
    row correct in the database, the surface a mod actually reads stale for
    most of a day, which to them is indistinguishable from the feature not
    working.
    """
    # ``bot`` may be annotated as the bare Client on some callers (the quest
    # expiry sweep holds one); only a commands.Bot carries cogs, which the
    # runtime one always is.
    get_cog = getattr(bot, "get_cog", None)
    cog = get_cog("TodoCog") if get_cog is not None else None
    refresh = getattr(cog, "refresh_board", None)
    if refresh is None:
        return
    try:
        await refresh(int(guild_id))
    except Exception:
        log.exception("todo board repaint failed for %s", guild_id)


def _tick(ctx) -> tuple[set[int], set[int], set[int], dict[int, list[str]]]:
    """One scheduler pass: spawn what is due, report what needs repainting.

    Returns ``(guilds_with_a_board, guilds_that_changed, guilds_posting_cards,
    new_chores)``, the last being each guild's freshly spawned chore names for
    the staff chores ping.
    Runs in a worker thread — all of it is blocking sqlite.

    The third set is the change-card poll's work list, gathered here because
    this function already holds a connection. It is empty unless somebody has
    ticked the dial on, which is what stops the feature costing anything while
    it is dark.

    "Changed" covers a **reset that only wrote a row off**. A day where the
    chore was not done spawns a fresh row *and* marks the old one missed, and
    even in the edge case where the spawn is what fails, the write-off alone
    has moved the board — so keying the repaint on "did a row spawn" would
    leave the miss invisible until something else happened to repaint.
    """
    now = time.time()
    # Probe on a plain (deferred) transaction first. spawn_due is a
    # read-then-write, so it needs BEGIN IMMEDIATE to keep skip-if-pending
    # honest against a racing "Run now" — but taking the global write lock
    # every 60s when nothing is due would make every other writer queue behind
    # a no-op. Escalate only on a hit; the escalated transaction re-reads under
    # the lock, so the race the lock guards is still covered.
    with open_db(ctx.db_path) as conn:
        due = bool(due_recurring(conn, now))
        boards = set(guilds_with_board(conn))
        cards = set(guilds_with_change_cards(conn))
    if not due:
        return boards, set(), cards, {}

    with open_db_immediate(ctx.db_path) as conn:
        # Re-read under the lock, the same set spawn_due is about to walk.
        due_now = due_recurring(conn, now)
        spawned = spawn_due(
            conn,
            now_ts=now,
            offset_hours_for=lambda gid: get_tz_offset_hours(conn, gid),
        )
        boards = set(guilds_with_board(conn))
    changed = {
        r.guild_id
        for r in spawned
        if r.todo_id is not None or r.missed_todo_id is not None
    }
    return boards, changed, cards, staff_ping_service.spawned_chore_names(due_now, spawned)


async def todo_board_loop(bot: Bot) -> None:
    """Spawn due recurring tasks, then repaint the boards that changed.

    Every user-facing mutation (`/todo`, the board buttons, the dashboard)
    repaints the board itself, so on a quiet tick there is nothing to do. The
    two things this loop owes a repaint are tasks it just spawned and edits a
    previous tick failed to apply — anything else would be N pointless DB reads
    and signature comparisons per minute, forever.
    """
    await bot.wait_until_ready()
    ctx = bot.ctx
    # Repaint every board once on boot. Normally the loop only touches guilds
    # where something spawned, but a board posted by a previous release can be
    # carrying a view this one no longer registers — after the 180 merge the
    # surviving message is the old chore board's, whose ✅ Mark Done button now
    # answers "This interaction failed" until something repaints it. An edit
    # replaces the view along with the content, so one pass on boot closes that
    # window instead of waiting for the next chat in the channel. It also heals
    # any board that drifted while the bot was down.
    first_pass = True
    while not bot.is_closed():
        try:
            boards, changed, card_guilds, new_chores = await asyncio.to_thread(_tick, ctx)
            booting = first_pass
            cog = cast("TodoCog | None", bot.get_cog("TodoCog"))
            if cog is not None:
                # Publish the board set so the on_message listener can reject
                # boardless guilds without touching the DB.
                cog.board.set_known_guilds(boards)
                # Repaint sequentially: these are edits against N different
                # channels and a 60s cadence gives no reason to burst them.
                # take_retries() is drained exactly once — leaving it undrained
                # is how pen pals lost its failed edits (F5 in
                # docs/reviews/2026-08-06-sticky-panel-machinery.md).
                work = (changed | cog.board.take_retries()) & boards
                if first_pass:
                    work |= boards
                    first_pass = False
                for guild_id in sorted(work):
                    try:
                        await cog.refresh_board(guild_id)
                    except Exception:
                        log.exception("todo board refresh failed for %s", guild_id)
                # Then the change cards, for the guilds that asked for them.
                # After the repaints, so a card never lands above a board still
                # showing the state before the thing the card is announcing.
                #
                # The boot pass announces nothing, it only re-banks the clock.
                # The cursor is a wall-clock watermark and downtime is a window
                # nobody was watching, so announcing it would make every deploy
                # post a card about items that had been sitting on the board the
                # whole time — and a long outage would post a wall of them. Same
                # judgement as the NULL-cursor guard in ``plan_arrival_card``.
                for guild_id in sorted(card_guilds & boards):
                    guild = bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    try:
                        await cog.post_change_cards(guild, announce=not booting)
                    except Exception:
                        log.exception("todo change cards failed for %s", guild_id)
            # The staff chores ping (docs/specs/staff_pings_spec.md). Independent of
            # the board: a guild with chores but no board still has the job.
            for guild_id, names in sorted(new_chores.items()):
                guild = bot.get_guild(guild_id)
                if guild is not None:
                    await staff_ping_service.post_staff_ping(
                        ctx, guild, fr.STAFF_CHORES_PING,
                        staff_ping_service.chores_due_text(names),
                    )
            # The staff QOTD ping rides this loop's minute tick too.
            await staff_ping_service.ping_overdue_qotd(ctx, bot, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("todo board loop tick failed")
        await asyncio.sleep(_LOOP_INTERVAL)


async def setup(bot: Bot) -> None:
    await bot.add_cog(TodoCog(bot))
