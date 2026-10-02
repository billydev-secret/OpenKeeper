"""``/mydata`` — the member's own data panel: what this server holds, and
which of it they can delete themselves.

Two sections, one ephemeral panel. **Your Discord messages** clears the
member's messages from Discord and touches nothing server-side: the bot's
records (XP, activity, profile) are always retained for moderation. **Data
this server holds** deletes the bot's own records for one feature at a time,
from ``privacy.logic.MEMBER_ERASABLE_STORES``.

The genuine whole-member hard-erasure path is
``privacy_service.purge_user_data``, still deliberately not wired to any
command — it, the export and the anonymise path all live on the dashboard's
Moderation → Member Data page.

Two commands were folded in here on 2026-09-17 (bug #222, item 3):

* ``/delete_me``, which was this file's only command and did the messages half.
* ``/whisper forget-me``, which was one feature carrying its own erasure
  command. That is the shape this panel exists to stop: the whisper deletes
  were wired to the member's command and to nothing else, so an *operator*
  erasure reached no whisper at all while the member could clear the lot
  themselves. The deletes now live in ``whisper_repo.forget_member``, which
  this panel and ``purge_user_data`` both call.

``/delete_user`` went the same way earlier, for the same reason: an admin job
managed by a command moves to the web and takes its command with it, and one
deletion engine with two surfaces is what we want, not two commands. Note the
narrowing that came with it — the command was open to moderators, the page is
admin-only. The scan-and-delete engine both surfaces drive is
``privacy.deletion_service``; everything below is the Discord adapter.

A feature that needs member-facing erasure adds an ``ErasableStore`` and gets
a button. ``tests/privacy/test_privacy_member_data_panel.py`` fails the suite if it
ships a command instead, or if a store the register calls member-erasable has
no button here.

The departure auto-anonymise lives here too, because it drives the same
engine and must share ``_active_deletions`` with the two surfaces above: a
leave listener queues the member, and a worker loop runs one queued departure
at a time through ``departure_erasure_service``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pathlib import Path

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.shared.dm_branding import (
    brand_dm_embed,
    guild_display_name,
    guild_icon_url,
    send_branded_dm,
)

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.utils import ExpiringView, disable_all_items
from discord.ext import commands, tasks

from bot_modules.privacy.logic import (
    EVERYTHING_CONFIRM_LABEL,
    EVERYTHING_LABEL,
    EVERYTHING_SUBJECT,
    MEMBER_ERASABLE_STORES,
    MODE_ALL,
    MODE_MEDIA,
    MODE_TEXT,
    FINAL_CONFIRM_LABEL,
    ErasableStore,
    confirm_button_label,
    mode_noun,
    render_confirm_prompt,
    render_deletion_summary,
    render_empty_summary,
    render_everything_confirm,
    render_everything_summary,
    render_final_confirm,
    render_panel,
    render_progress_bar,
    render_scan_status,
    should_throttle,
)
from bot_modules.privacy.deletion_service import DeletionResult, run_deletion
from bot_modules.breaks import service as breaks_service
from bot_modules.breaks.store import get_break
from bot_modules.privacy import departure_erasure_service as departure
from bot_modules.privacy import self_erasure_service

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.privacy")

# ---------------------------------------------------------------------------
# Confirmation view
# ---------------------------------------------------------------------------

class _ConfirmDeleteView(discord.ui.View):
    """One confirm step. ``final=False`` is the first of the panel's two.

    A first step leaves the press unanswered and hands its interaction to the
    caller on :attr:`interaction`, which answers it by swapping in the second
    step. Answering here ("Working on it…") would spend the response the
    second step needs, and tell the member the deletion had started before
    they had confirmed it.
    """

    def __init__(
        self,
        actor_id: int,
        *,
        confirm_label: str = "Yes, delete my messages",
        final: bool = True,
    ) -> None:
        super().__init__(timeout=60)
        self.actor_id = actor_id
        self.final = final
        self.confirmed: bool | None = None
        self.interaction: discord.Interaction | None = None
        # The label is the last thing read before an irreversible click, so it
        # names the actual scope — a broader claim than "messages" would be a lie.
        self.confirm.label = confirm_label

    @discord.ui.button(label="Yes, delete my messages", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("❌ This isn't your confirmation.", ephemeral=True)
            return
        self.confirmed = True
        self.interaction = interaction
        if self.final:
            disable_all_items(self)
            await interaction.response.edit_message(
                content="Working on it — this may take a moment…", view=self
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("❌ This isn't your confirmation.", ephemeral=True)
            return
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


# ---------------------------------------------------------------------------
# Deletion logic
# ---------------------------------------------------------------------------


# Progress card colors: in-flight vs. finished.
# The progress cards take the guild's branding accent (#76); this is only the
# fallback for a context with no resolvable guild. _DONE_COLOR stays green on
# purpose — there the color is semantic ("complete"), not decoration.
_PROGRESS_FALLBACK = discord.Color.blurple()
_DONE_COLOR = discord.Color.green()

_PROGRESS_TITLE = "Deleting your messages"


def _scan_embed(
    done: int, total: int, found: int, color: "discord.Color | None" = None
) -> discord.Embed:
    """The in-progress scan card — status line plus a channel progress bar."""
    return discord.Embed(
        title=_PROGRESS_TITLE,
        description=f"{render_scan_status(done, total, found)}\n{render_progress_bar(done, total)}",
        color=color or _PROGRESS_FALLBACK,
    )


def _delete_embed(
    done: int, total: int, color: "discord.Color | None" = None
) -> discord.Embed:
    """The in-progress delete card — a message progress bar."""
    return discord.Embed(
        title=_PROGRESS_TITLE,
        description=f"Deleting…\n{render_progress_bar(done, total)}",
        color=color or _PROGRESS_FALLBACK,
    )


def _summary_embed(description: str) -> discord.Embed:
    """The final result card."""
    return discord.Embed(
        title="Deletion Complete",
        description=description,
        color=_DONE_COLOR,
    )


class _ProgressReporter:
    """Delivers deletion progress to a single, continuously-edited message.

    While the slash-command interaction token is alive (~15 min) it edits the
    original ephemeral response. A full-server scan can outlive that token; the
    first edit that fails flips us to a DM, and every later update — scan,
    delete, and the final summary — edits that *same* DM message. The member
    sees one card that updates in place, not a fresh message (and a fresh push
    notification) every couple of seconds.

    One reporter must span the whole run: sharing the DM handle across all three
    phases is what keeps it to a single notification.
    """

    def __init__(
        self,
        interaction: discord.Interaction,
        db_path: Path,
        guild: discord.Guild | None,
    ) -> None:
        self._interaction = interaction
        # Kept for the DM fallback below, which brands the card for the
        # server whose data is being deleted.
        self._db_path = db_path
        self._guild = guild
        # Sticky: once the token is gone the ephemeral message is unreachable,
        # so we never try to edit it again.
        self._fell_back = False
        self._dm_message: discord.Message | None = None

    async def update(self, embed: discord.Embed) -> None:
        if not self._fell_back:
            try:
                # content=None clears the "Working on it…" text the confirm view
                # left behind, so only the embed shows.
                await self._interaction.edit_original_response(
                    content=None, embed=embed, view=None
                )
                return
            except (discord.HTTPException, discord.NotFound):
                self._fell_back = True
        await self._update_dm(embed)

    async def _update_dm(self, embed: discord.Embed) -> None:
        if self._dm_message is not None:
            # The card that replaces a branded one has to be branded too.
            # ``send_branded_dm`` below stamps the *first* DM only; every
            # update after it is an edit carrying a fresh, unstamped embed, so
            # the attribution vanished seconds after it appeared and stayed
            # gone for the rest of the run — including the final summary. On
            # the erasure surface of all places, "which server is deleting my
            # data" is the whole question.
            #
            # A copy, because ``brand_dm_embed`` mutates: stamping the
            # caller's embed would double the footer if this edit fails and
            # the send below re-brands the same object. Attribution only
            # (``keep_color=True``) — the progress embeds already carry the
            # run's accent, so there is no accent to resolve and no database
            # read on a path that runs every couple of seconds.
            branded = brand_dm_embed(
                embed.copy(),
                guild_name=guild_display_name(self._guild),
                guild_icon_url=guild_icon_url(self._guild),
                keep_color=True,
            )
            try:
                await self._dm_message.edit(content=None, embed=branded)
                return
            except (discord.HTTPException, discord.NotFound):
                # The DM was lost (deleted?) — fall through and send a new one.
                self._dm_message = None
        # DMs closed — nowhere left to report. Deletion still completes.
        self._dm_message = await send_branded_dm(
            self._interaction.user,
            db_path=self._db_path,
            guild=self._guild,
            embed=embed,
            keep_color=True,
            bypass_break=True,  # progress on the member's own erasure
        )


class _EmbedProgress:
    """Renders a run's progress into the member's single card.

    Edit throttling lives here rather than in the runner: Discord rate-limits
    message edits, and the dashboard's poller — the other consumer of the same
    run — has no edits to throttle.
    """

    def __init__(
        self, reporter: "_ProgressReporter", accent: "discord.Color | None"
    ) -> None:
        self._reporter = reporter
        self._accent = accent
        self._last_scan = 0.0
        self._last_delete = 0.0

    async def scanning(self, done: int, total: int, found: int) -> None:
        now = asyncio.get_running_loop().time()
        if should_throttle(self._last_scan, now, done=done, total=total, interval=2.0):
            return
        self._last_scan = now
        await self._reporter.update(_scan_embed(done, total, found, self._accent))

    async def deleting(self, done: int, total: int) -> None:
        now = asyncio.get_running_loop().time()
        if should_throttle(
            self._last_delete, now, done=done, total=total, interval=1.5
        ):
            return
        self._last_delete = now
        await self._reporter.update(_delete_embed(done, total, self._accent))


async def _run_deletion(
    ctx: AppContext,
    guild: discord.Guild,
    user_id: int,
    original_interaction: discord.Interaction,
    *,
    mode: str = MODE_ALL,
    accent: "discord.Color | None" = None,
) -> None:
    """Run a deletion for the member and report it on their own card.

    The scan-and-delete engine is ``privacy.deletion_service`` — shared with the
    dashboard's Member Data page, which reports the same run into a polled
    counter instead. This function is the Discord adapter: it owns the card.

    Every mode only clears the member's *Discord* messages; nothing on the
    server side is touched. XP, activity and profile are retained for
    moderation — the modes differ only in which Discord messages
    (all / images / text) are removed.

    ``accent`` arrives already resolved from the command layer rather than being
    looked up here: reading the branding config would open the DB, and the
    guarantee asserted in tests is that a deletion run never touches
    server-side data at all.
    """
    # One reporter for the whole run — it holds the single message everything
    # edits, so scan, delete, and summary share one card and one notification.
    reporter = _ProgressReporter(original_interaction, ctx.db_path, guild)

    result = await run_deletion(
        guild, user_id, mode=mode, progress=_EmbedProgress(reporter, accent)
    )

    if result.empty:
        await reporter.update(_summary_embed(render_empty_summary(mode=mode)))
        return

    await reporter.update(
        _summary_embed(
            render_deletion_summary(
                deleted=result.deleted,
                failed=result.failed,
                replaced=result.replaced,
                mode=mode,
            )
        )
    )


# ---------------------------------------------------------------------------
# Bot-side erasure: the panel's lower half
# ---------------------------------------------------------------------------
#
# The button list is built from ``MEMBER_ERASABLE_STORES`` and each entry
# carries its own ``eraser`` — the same deletion function an operator erasure
# runs from ``purge_user_data``, so the member's own button and the dashboard's
# Erase tab can never drift into deleting different things, the failure that
# made bug #222's whisper item a bug rather than a wording fix.


def _erase_store(db_path: Path, store: ErasableStore, *, guild_id: int, user_id: int) -> None:
    """Run one store's erasure for the member (blocking; called in a thread)."""
    with open_db(db_path) as conn:
        # The whisper deletes lean on ON DELETE CASCADE to take each whisper's
        # guesses, replies and reports with it.
        conn.execute("PRAGMA foreign_keys = ON")
        store.eraser(conn, guild_id=guild_id, user_id=user_id)


#: The media step (``store.clear_media``) raised. Nothing claims success:
#: it runs first and raises before any row goes, so a retry finds everything
#: intact.
_STORE_FAILED = (
    "❌ Something went wrong and nothing was deleted. Please try again in a "
    "minute; if it keeps failing, open a ticket and a mod will help."
)

#: The eraser itself raised, *after* the media step already succeeded — a
#: Discord post or a cached file can be gone for good even though the row
#: that recorded it is not. Neither can be rolled back, so this must not
#: claim "nothing was deleted" the way ``_STORE_FAILED`` does.
_STORE_ROWS_FAILED = (
    "❌ Something went wrong partway — a post or file may already be gone, but "
    "nothing else was deleted. Please try again in a minute; if it keeps "
    "failing, open a ticket and a mod will help."
)


class _MyDataPanelView(ExpiringView):
    """The ``/mydata`` panel: message deletion above, bot-side stores below.

    Both halves route through the same confirm (:class:`_ConfirmDeleteView`)
    that ``/delete_me`` used, and since 2026-09-27 through a second, final one
    after it — see :meth:`_confirm`.

    The store buttons are built from :data:`MEMBER_ERASABLE_STORES` rather than
    written out, which is what makes the panel the cheap place to add a
    feature's erasure and a command the expensive one.

    :class:`ExpiringView` because the panel is read before it is pressed: a
    member who opens it, reads what each button covers and then decides would
    otherwise meet Discord's generic "This interaction failed" on buttons
    still rendering as live. On the one surface that tells them this is how
    they control their data, expiring has to look like expiring.
    """

    def __init__(
        self,
        cog: "PrivacyCog",
        *,
        actor_id: int,
        guild: discord.Guild,
        accent: "discord.Color | None",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.actor_id = actor_id
        self.guild = guild
        self.accent = accent

        for mode in (MODE_ALL, MODE_MEDIA, MODE_TEXT):
            button: discord.ui.Button = discord.ui.Button(
                label=_MODE_BUTTON_LABELS[mode],
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            button.callback = self._message_callback(mode)  # type: ignore[method-assign]
            self.add_item(button)

        for index, store in enumerate(MEMBER_ERASABLE_STORES):
            store_button: discord.ui.Button = discord.ui.Button(
                label=store.label,
                style=discord.ButtonStyle.danger,
                # Rows 1+ so a growing store list never crowds the message
                # buttons off the first row (Discord allows five per row).
                row=1 + index // 5,
            )
            store_button.callback = self._store_callback(store)  # type: ignore[method-assign]
            self.add_item(store_button)

        # On a row of its own below the stores: it is not one of them (its
        # eraser is the departure recipe, not a purge function), and it is
        # the one button on the panel that reaches past this panel's list.
        everything: discord.ui.Button = discord.ui.Button(
            label=EVERYTHING_LABEL,
            style=discord.ButtonStyle.danger,
            row=1 + (len(MEMBER_ERASABLE_STORES) + 4) // 5,
        )
        everything.callback = self._everything_callback()  # type: ignore[method-assign]
        self.add_item(everything)

    async def _is_actor(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id:
            return True
        await interaction.response.send_message(
            "❌ This isn't your panel.", ephemeral=True
        )
        return False

    async def _confirm(
        self,
        interaction: discord.Interaction,
        *,
        prompt: str,
        confirm_label: str,
        subject: str,
    ) -> bool:
        """Swap the panel for two confirm steps; True once both are pressed.

        The first carries the button's own copy — what goes, what survives.
        The second (:func:`render_final_confirm`) is the "just in case" Billy
        asked for on 2026-09-27, and is the same for every button so it reads
        as a last check rather than as new information.
        """
        # Stop the panel's own timer before its view is replaced on the
        # message: discord.py keeps a replaced view's timer running, and when
        # it fired ``ExpiringView.on_timeout`` would edit the greyed-out panel
        # back over the live confirm step.
        self.stop()
        first = _ConfirmDeleteView(self.actor_id, confirm_label=confirm_label, final=False)
        await interaction.response.edit_message(content=prompt, view=first)
        await first.wait()
        if first.confirmed is not True or first.interaction is None:
            return False
        second = _ConfirmDeleteView(self.actor_id, confirm_label=FINAL_CONFIRM_LABEL)
        await first.interaction.response.edit_message(
            content=render_final_confirm(subject), view=second
        )
        await second.wait()
        return second.confirmed is True

    def _message_callback(self, mode: str):
        async def callback(interaction: discord.Interaction) -> None:
            if not await self._is_actor(interaction):
                return
            if self.actor_id in self.cog._active_deletions:
                await interaction.response.send_message(
                    "❌ A deletion is already running for your account — "
                    "please wait for it to finish.",
                    ephemeral=True,
                )
                return
            # Claimed before the confirm wait, not after: the confirm view can
            # sit open for up to five minutes, and a second /mydata panel (or
            # a dashboard admin run) must be refused for that whole window —
            # not just once the member has actually clicked through. Matches
            # /delete_me's original ordering and docs/specs/privacy_spec.md's "while
            # a deletion for user X is running" guarantee.
            self.cog._active_deletions.add(self.actor_id)
            try:
                if not await self._confirm(
                    interaction,
                    prompt=render_confirm_prompt(mode=mode),
                    confirm_label=confirm_button_label(mode),
                    subject=f"Your {mode_noun(mode)}",
                ):
                    return
                await _run_deletion(
                    self.cog.bot.ctx,
                    self.guild,
                    self.actor_id,
                    interaction,
                    mode=mode,
                    accent=self.accent,
                )
            finally:
                self.cog._active_deletions.discard(self.actor_id)

        return callback

    def _store_callback(self, store: ErasableStore):
        async def callback(interaction: discord.Interaction) -> None:
            if not await self._is_actor(interaction):
                return
            if not await self._confirm(
                interaction,
                prompt=store.confirm,
                confirm_label=store.confirm_label,
                subject=store.subject,
            ):
                return
            db_path = self.cog.bot.ctx.db_path
            if store.clear_media is not None:
                try:
                    await store.clear_media(
                        db_path,
                        guild_id=self.guild.id,
                        user_id=self.actor_id,
                        delete_post=self.cog.delete_guess_post,
                    )
                except Exception:  # noqa: BLE001 - the member must never read "deleted" after a failure
                    log.exception(
                        "My Data: %s media clear failed for user %d", store.key, self.actor_id
                    )
                    await interaction.edit_original_response(content=_STORE_FAILED, view=None)
                    return
            try:
                await asyncio.to_thread(
                    _erase_store,
                    db_path,
                    store,
                    guild_id=self.guild.id,
                    user_id=self.actor_id,
                )
            except Exception:  # noqa: BLE001 - the member must never read "deleted" after a failure
                log.exception("My Data: %s erasure failed for user %d", store.key, self.actor_id)
                content = _STORE_ROWS_FAILED if store.clear_media is not None else _STORE_FAILED
                await interaction.edit_original_response(content=content, view=None)
                return
            await interaction.edit_original_response(content=store.done, view=None)

        return callback

    def _everything_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            if not await self._is_actor(interaction):
                return
            if self.actor_id in self.cog._active_deletions:
                await interaction.response.send_message(
                    "❌ A deletion is already running for your account — "
                    "please wait for it to finish.",
                    ephemeral=True,
                )
                return
            # Claimed across both confirms and the whole run, as the message
            # buttons do: the run starts with the same message sweep, and the
            # departure worker skips a member held here.
            self.cog._active_deletions.add(self.actor_id)
            try:
                if not await self._confirm(
                    interaction,
                    prompt=render_everything_confirm(),
                    confirm_label=EVERYTHING_CONFIRM_LABEL,
                    subject=EVERYTHING_SUBJECT,
                ):
                    return
                await self.cog.erase_everything(interaction, self.guild, accent=self.accent)
            finally:
                self.cog._active_deletions.discard(self.actor_id)

        return callback


#: Button labels for the three message modes. The mode's own
#: ``confirm_button_label`` is the danger-click wording; these are the calmer
#: labels on the panel itself, before anything is confirmed.
_MODE_BUTTON_LABELS = {
    MODE_ALL: "All my messages",
    MODE_MEDIA: "Images & files",
    MODE_TEXT: "Text only",
}


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

#: How long a finished dashboard run stays readable. The panel polls every few
#: seconds and stops at ``done``, so an hour is far past any real reader.
_WEB_RUN_TTL_SECONDS = 3600.0
#: Backstop for a burst of runs inside that hour; oldest finished go first.
_WEB_RUN_KEEP = 50


@dataclass
class _WebRun:
    """Live progress for a deletion started from the dashboard.

    Held in memory on the cog — see the bridge comment in :class:`PrivacyCog`
    for why this never becomes a table.
    """

    mode: str
    phase: str = "scanning"  # scanning → deleting → done | failed
    done: int = 0
    total: int = 0
    found: int = 0
    result: DeletionResult | None = None
    task: "asyncio.Task[None] | None" = None
    #: ``time.monotonic()`` when the run stopped, or None while it is live.
    #: Only finished runs are eligible for eviction — see ``_prune_web_runs``.
    finished_at: float | None = None


class _StateProgress:
    """Progress sink for a web-driven run: records counts, edits nothing.

    No throttling — there is no Discord message being edited and so no rate
    limit to respect. The panel decides how often it asks.
    """

    def __init__(self, state: _WebRun) -> None:
        self._state = state

    async def scanning(self, done: int, total: int, found: int) -> None:
        self._state.phase = "scanning"
        self._state.done = done
        self._state.total = total
        self._state.found = found

    async def deleting(self, done: int, total: int) -> None:
        self._state.phase = "deleting"
        self._state.done = done
        self._state.total = total


class PrivacyCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._active_deletions: set[int] = set()
        self._web_runs: dict[tuple[int, int], _WebRun] = {}
        super().__init__()

    async def cog_load(self) -> None:
        self._departure_loop.start()

    async def cog_unload(self) -> None:
        self._departure_loop.cancel()

    async def open_panel(self, interaction: discord.Interaction) -> None:
        """Open the member's own data panel, ephemerally, as the reply.

        Reached from the Server Features hub's 📜 My Data row and from
        ``/info``'s My Data button — the ``/mydata`` command was folded into
        the hub on 2026-09-26 (bug-list row 259). This stays the one
        member-facing erasure surface; only the door moved.

        The old ``/delete_me mode:`` picker became the three message buttons
        on the panel, because a flat choice list had to mix "delete from
        Discord" with "delete from the bot" once the whisper erasure moved in —
        two different acts reading as one menu.
        """
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This only works in a server.", ephemeral=True
            )
            return

        view = _MyDataPanelView(
            self,
            actor_id=interaction.user.id,
            guild=interaction.guild,
            # Resolved here rather than mid-run: a deletion run must not touch
            # server-side data, and reading the branding config opens the DB.
            accent=await safe_resolve_accent(
                self.bot.ctx, interaction.guild, log_label="privacy"
            ),
        )
        await interaction.response.send_message(
            render_panel(), view=view, ephemeral=True
        )
        # The handle ExpiringView edits when the five minutes are up; valid
        # for the whole window even though the panel is ephemeral.
        view.message = await interaction.original_response()

    # ── dashboard bridge ────────────────────────────────────────────────
    #
    # Moderation → Member Data → Delete Messages drives the same engine as
    # ``/mydata``, but a web caller has no Discord message to edit and no
    # request that can stay open for a full-server scan. So the run is started
    # as a task, reports into ``_web_runs``, and the panel polls it.
    #
    # ``_web_runs`` is in memory and stays that way: a deletion run is
    # guaranteed not to touch server-side data (asserted in
    # tests/privacy/test_privacy_modes.py), and a progress table would break that
    # guarantee to store something nobody needs after the run ends. The cost is
    # that a restart mid-run loses the progress card — the deletion itself is
    # already partly done either way, and re-running is safe.

    def _prune_web_runs(self) -> None:
        """Drop finished runs the panel can no longer be waiting on.

        Without this the dict is append-only for the life of the process: one
        small entry per member ever cleared from the dashboard, never removed.
        A live run is never evicted at any age — only ``finished_at`` entries
        are eligible, so a long full-server scan cannot be pruned out from
        under the panel polling it.

        The age cut is what the panel actually needs: it polls every few
        seconds and stops at ``done``, so an hour is far past any real reader.
        The count cap is the backstop for a burst inside that hour, and evicts
        the oldest finished run first.
        """
        now = time.monotonic()
        finished = sorted(
            (s.finished_at or 0.0, key)
            for key, s in self._web_runs.items()
            if s.finished_at is not None
        )
        survivors: list[tuple[int, int]] = []
        for at, key in finished:  # oldest first
            if now - at > _WEB_RUN_TTL_SECONDS:
                self._web_runs.pop(key, None)
            else:
                survivors.append(key)
        for key in survivors[: max(0, len(survivors) - _WEB_RUN_KEEP)]:
            self._web_runs.pop(key, None)

    def _web_state(self, guild_id: int, user_id: int) -> _WebRun | None:
        return self._web_runs.get((guild_id, user_id))

    async def start_message_deletion(
        self, guild_id: int, user_id: int, *, mode: str = MODE_ALL
    ) -> dict[str, Any]:
        """Begin a deletion and return immediately. Poll :meth:`deletion_status`.

        Shares ``_active_deletions`` with ``/mydata``, so a member clearing
        their own messages and an admin clearing the same member's cannot run at
        once — the second caller is refused, exactly as two commands were.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {"started": False, "reason": "guild_unavailable"}
        if user_id in self._active_deletions:
            return {"started": False, "reason": "already_running"}

        state = _WebRun(mode=mode)
        self._prune_web_runs()
        self._web_runs[(guild_id, user_id)] = state
        self._active_deletions.add(user_id)
        # Held on the state: a bare create_task reference can be collected
        # mid-flight, which would cancel the run at an arbitrary point.
        state.task = asyncio.create_task(
            self._run_web_deletion(guild, user_id, mode, state)
        )
        return {"started": True}

    async def _run_web_deletion(
        self, guild: discord.Guild, user_id: int, mode: str, state: _WebRun
    ) -> None:
        try:
            result = await run_deletion(
                guild, user_id, mode=mode, progress=_StateProgress(state)
            )
            state.phase = "done"
            state.result = result
        except Exception:  # noqa: BLE001 - the panel must see a failure, not a hang
            log.exception("Dashboard deletion failed for user %d", user_id)
            state.phase = "failed"
        finally:
            state.finished_at = time.monotonic()
            self._active_deletions.discard(user_id)

    def deletion_status(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        """The current run's progress, or None if this member has no run here."""
        state = self._web_state(guild_id, user_id)
        if state is None:
            return None
        out: dict[str, Any] = {
            "phase": state.phase,
            "mode": state.mode,
            "done": state.done,
            "total": state.total,
            "found": state.found,
        }
        result = state.result
        if result is not None:
            # Same two renderers the Discord card uses, so both surfaces report
            # a finished run in identical words.
            out["summary"] = (
                render_empty_summary(mode=state.mode)
                if result.empty
                else render_deletion_summary(
                    deleted=result.deleted,
                    failed=result.failed,
                    replaced=result.replaced,
                    mode=state.mode,
                )
            )
            out["deleted"] = result.deleted
            out["failed"] = result.failed
            out["replaced"] = result.replaced
        return out

    # ── departure auto-anonymise ────────────────────────────────────────
    #
    # The listener only queues; the worker acts after the settle delay. Why the
    # work cannot happen inside the event, and why the queue row dies in the
    # anonymise transaction, is in departure_erasure_service's docstring.

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild_id, user_id, is_bot = member.guild.id, member.id, member.bot

        def _queue() -> str | None:
            with self.bot.ctx.open_db() as conn:
                reason = departure.hold_reason(conn, guild_id, user_id, is_bot=is_bot)
                if reason is None:
                    departure.enqueue(conn, guild_id, user_id, now=time.time())
                return reason

        try:
            reason = await asyncio.to_thread(_queue)
        except Exception:  # noqa: BLE001 - a leave must never break the other listeners
            log.exception("Could not queue a departure in guild %d", guild_id)
            return
        if reason in (departure.HOLD_JAILED, departure.HOLD_TICKET):
            log.info("Departure in guild %d kept: member is %s", guild_id, reason)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild_id, user_id = member.guild.id, member.id

        def _cancel() -> bool:
            with self.bot.ctx.open_db() as conn:
                return departure.cancel(conn, guild_id, user_id)

        try:
            if await asyncio.to_thread(_cancel):
                log.info("Queued departure in guild %d cancelled: member rejoined", guild_id)
        except Exception:
            log.exception("Could not cancel a queued departure in guild %d", guild_id)

    @tasks.loop(seconds=60)
    async def _departure_loop(self) -> None:
        """Run at most one due departure per tick.

        One at a time on purpose: each is a full history scan against Discord's
        rate limits, and a member already under ``/mydata`` or a dashboard
        run is left for a later tick rather than scanned twice at once.
        """
        try:
            db_path = self.bot.ctx.db_path

            def _next() -> departure.Job | None:
                with self.bot.ctx.open_db() as conn:
                    return departure.next_job(conn, now=time.time())

            job = await asyncio.to_thread(_next)
            if job is None or job.user_id in self._active_deletions:
                return
            bot_user = self.bot.user
            if bot_user is None:
                return
            outcome = await departure.process_job(
                db_path,
                job,
                self._departure_hooks(),
                primary_guild_id=self.bot.ctx.guild_id,
                actor_id=bot_user.id,
            )
            # Never the member id: the log would outlive the anonymisation.
            log.info(
                "Departure job in guild %d: %s %s", job.guild_id, outcome.status, outcome.detail
            )
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Departure worker tick failed")

    @_departure_loop.before_loop
    async def _before_departure_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def delete_guess_post(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        """Take one Guess round's crop post down, for every Guess erasure.

        ``guess_cleanup_service.clear_member_media`` drives it for the My Data
        button, the dashboard's Erase and the departure job. A post already
        gone, or a guild or channel the bot can no longer see, is done — there
        is nothing left it could delete. Any other Discord error propagates, so
        the erasure stops before the rows that record the post go.
        """
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel_or_thread(channel_id) if guild else None
        if not isinstance(
            channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)
        ):
            return
        try:
            await channel.get_partial_message(message_id).delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    async def erase_everything(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        *,
        accent: "discord.Color | None",
    ) -> None:
        """My Data → Delete everything, once both confirms are pressed.

        The order, the holds and why live state is not protected are in
        ``self_erasure_service``; this is the Discord side. One card spans the
        run: the message sweep reports into it and the outcome replaces it.
        """
        user_id = interaction.user.id
        bot_user = self.bot.user
        if bot_user is None:
            return
        reporter = _ProgressReporter(interaction, self.bot.ctx.db_path, guild)
        shared = self._departure_hooks()

        async def delete_messages() -> DeletionResult:
            return await run_deletion(
                guild, user_id, mode=MODE_ALL, progress=_EmbedProgress(reporter, accent)
            )

        async def end_break() -> bool:
            # Ended, not deleted: they are still here, and a deleted row
            # would leave them wearing the break role with nothing to restore.
            def _on_break() -> bool:
                with self.bot.ctx.open_db() as conn:
                    return get_break(conn, guild.id, user_id) is not None

            if not await asyncio.to_thread(_on_break):
                return True
            member = guild.get_member(user_id)
            if member is None:
                return False
            return (await breaks_service.end_break(self.bot.ctx, member)).ok

        async def present_elsewhere() -> bool:
            for other in shared.guild_ids():
                if other != guild.id and await shared.is_member(other, user_id):
                    return True
            return False

        outcome = await self_erasure_service.erase_everything(
            self.bot.ctx.db_path,
            guild_id=guild.id,
            user_id=user_id,
            primary_guild_id=self.bot.ctx.guild_id,
            actor_id=bot_user.id,
            hooks=self_erasure_service.SelfErasureHooks(
                delete_messages=delete_messages,
                end_break=end_break,
                present_elsewhere=present_elsewhere,
                channel_ids=lambda: shared.channel_ids(guild.id),
                delete_guess_post=self.delete_guess_post,
            ),
        )
        messages = outcome.messages
        await reporter.update(
            _summary_embed(
                render_everything_summary(
                    outcome.status,
                    deleted=messages.deleted if messages else 0,
                    failed=messages.failed if messages else 0,
                    detail=outcome.detail,
                    all_guilds=outcome.detail == departure.SCOPE_ALL,
                )
            )
        )
        # Never the member id: the log would outlive the anonymisation.
        log.info("Delete everything in guild %d: %s", guild.id, outcome.status)

    def _departure_hooks(self) -> departure.DepartureHooks:
        def _guild(guild_id: int) -> discord.Guild:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                raise departure.DepartureError(f"guild {guild_id} is unavailable")
            return guild

        async def delete_messages(guild_id: int, user_id: int) -> int:
            guild = _guild(guild_id)
            self._active_deletions.add(user_id)
            try:
                result = await run_deletion(guild, user_id, mode=MODE_ALL)
            finally:
                self._active_deletions.discard(user_id)
            return result.failed

        async def is_member(guild_id: int, user_id: int) -> bool:
            guild = _guild(guild_id)
            if guild.get_member(user_id) is not None:
                return True
            try:
                await guild.fetch_member(user_id)
            except discord.NotFound:
                return False
            # Any other HTTP error propagates: an unknown answer must not be
            # read as "gone", or a member still here could be anonymised.
            return True

        def guild_ids() -> list[int]:
            return [g.id for g in self.bot.guilds]

        def channel_ids(guild_id: int) -> list[int]:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return []
            return [c.id for c in guild.channels] + [t.id for t in guild.threads]

        return departure.DepartureHooks(
            delete_messages=delete_messages,
            is_member=is_member,
            guild_ids=guild_ids,
            channel_ids=channel_ids,
            delete_guess_post=self.delete_guess_post,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(PrivacyCog(bot))
