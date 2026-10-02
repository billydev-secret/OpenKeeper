"""
Fantasies & Dealbreakers — game cog.

Multi-round: each round collects anonymous 💖 fantasies and 🚩 dealbreakers,
then shows each entry for a ✅ Same / ❌ Not for me vote; the host runs as
many rounds as they like and ends the game with its recap. Pure logic
lives in ``games/fantasies/logic.py`` and the Components V2 card builders
in ``games/fantasies/layouts.py``; this module keeps only the Discord glue
(slash command, modal, views, the round loop). Nothing host-only is on a
public card: Start Round, Close Submissions, ⏭️ Next and End Game are on
the ephemeral host panel (``/games host``).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.branding_service import PanelArt
from bot_modules.core.layout import V2_EDIT, bind_present, card
from bot_modules.core.sticky import StickyPanel
from bot_modules.core.utils import is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.shared.anon_audit_service import (
    EVENT_ENTRY_SUBMITTED,
)
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    update_game_message,
    update_game_state,
    get_game_payload,
    end_game,
    update_session,
    modify_payload,
    channel_name,
)
from bot_modules.games.utils.host_panel import (
    GAME_ENDED_REPLY,
    NOT_HOST_REPLY,
    GameHostPanel,
    hand_over,
    reply as host_reply,
    send_panel,
)
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.lobby_art import NO_ART, read_lobby_art
from bot_modules.games.utils.names import host_display
from bot_modules.games.board_sticky_service import (
    LiveBoards,
    redraw_board,
    redraw_embed_era_lobby,
)
from bot_modules.games.start_ping_service import extract_start_epoch
from bot_modules.games.utils.round_pacing import (
    RoundPacing,
    launch_pacing,
)
from bot_modules.games.fantasies.layouts import (
    CLOSE_LABEL,
    END_LABEL,
    HELP_ID,
    HOST_CLOSE_ID,
    HOST_END_ID,
    HOST_NEXT_ID,
    HOST_START_ID,
    NOPE_ID,
    NOPE_LABEL,
    SAME_ID,
    SAME_LABEL,
    START_LABEL,
    SUBMIT_DEALBREAKER_ID,
    SUBMIT_FANTASY_ID,
    SUBMIT_LABELS,
    TITLE,
    build_host_panel_layout,
    build_lobby_layout,
    build_recap_layout,
    build_submit_layout,
    build_vote_layout,
)
from bot_modules.games.fantasies.logic import (
    CATEGORY_DEALBREAKER,
    CATEGORY_FANTASY,
    DEFAULT_ENTRY_SECONDS,
    SELF_VOTE_REFUSAL,
    active_voters,
    add_entry,
    apply_vote,
    build_result_entry,
    everyone_has_voted,
    get_round_entries,
    roster_from_results,
    round_in_progress,
)

log = logging.getLogger(__name__)

ROUND_RUNNING_REPLY = "❌ A round is already running."
NO_WINDOW_REPLY = "No submission window is open right now."
NO_ENTRY_REPLY = "No entry is being voted on right now."
GAME_OVER_REPLY = "This game has ended."


class SubmitEntryModal(discord.ui.Modal):
    """The entry box alone — the button that opened it chose the category.

    It used to ask members to *type* "Fantasy" or "Dealbreaker" into a box
    beside their 500-character entry. An unrecognised word closed the modal
    with an ephemeral error, and the entry went with it: a binary choice was
    costing people everything they had just written.
    """

    entry: discord.ui.TextInput = discord.ui.TextInput(
        label="Your Entry",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, game_id: str, db, round_num: int, category: str):
        super().__init__(title=f"Submit a {category}")
        self.game_id = game_id
        self.db = db
        self.round_num = round_num
        self.category = category

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Submit Entry", channel_name(interaction.channel))
        category = self.category

        def _add_entry(payload):
            add_entry(
                payload,
                round_num=self.round_num,
                user_id=interaction.user.id,
                text=self.entry.value,
                category=category,
            )

        await modify_payload(self.db, self.game_id, _add_entry)

        await interaction.response.send_message(
            f"Your {category.lower()} has been submitted!", ephemeral=True
        )

        # After the member has been answered — this is best-effort logging and
        # must not spend any of Discord's 3s initial-response budget.
        if interaction.guild:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="fantasies", user=interaction.user,
                event=EVENT_ENTRY_SUBMITTED,
                content=self.entry.value, label=f"{category} Submission",
                game_id=self.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"category": category, "round": self.round_num},
            )


class _HostControls:
    """The four host-panel hand-offs the game's live views answer.

    ``bot.active_views[game_id]`` is the panel between rounds and during a
    submission window, and the entry's vote view during a vote; the host
    panel hands each press to whichever is live
    (``host_panel.GameHostPanel``). The panel view routes each control to
    the round's live piece; the vote view answers Next itself and hands the
    rest back to the panel view it belongs to.
    """

    host_id: int

    async def start_round(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(ROUND_RUNNING_REPLY, ephemeral=True)

    async def close_submissions(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NO_WINDOW_REPLY, ephemeral=True)

    async def next_entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NO_ENTRY_REPLY, ephemeral=True)

    async def end_game_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(GAME_OVER_REPLY, ephemeral=True)


class FantasiesMainView(_HostControls, discord.ui.LayoutView):
    """The game's panel — its one tracked message. **Paradigm 8 — live
    board**: rebuilt and re-edited after each round. ``fan_htp`` is a frozen
    custom id — ``recover_game`` re-registers this view against the panel
    message after a restart. Start Round and End Game are not on it: the
    host panel calls :meth:`start_round` and :meth:`end_game_button`, and
    routes Close Submissions and ⏭️ Next through here to the round's live
    piece.
    """

    def __init__(
        self, game_id: str, host_id: int, db, bot, cog,
        *,
        host_name: str = "Host",
        start_at: int | None = None,
        art: PanelArt = NO_ART,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.host_name = host_name
        self.start_at = start_at
        self.art = art
        self.round_num = 0
        self.message: discord.Message | None = None
        self._ended = False
        # The round loop's live pieces, so End Game can wake and disable them.
        self._active_submit_view: SubmitRoundView | None = None
        self._active_vote_view: FantasiesVoteView | None = None
        self._round_running = False
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, re-binding its one
        button — a rebuild adds a brand-new ``Button``; the ended frame
        carries none."""
        self.clear_items()
        for item in build_lobby_layout(
            self.host_name,
            self.round_num,
            start_at=self.start_at,
            ended=self._ended,
            header_url=self.art.header,
            footer_url=self.art.footer,
            title_in_header=self.art.title_in_header,
        ):
            self.add_item(item)
        bind_present(self, {HELP_ID: self.how_to_play})

    async def redraw(self) -> None:
        self._rebuild()
        if self.message is None:
            return
        try:
            await self.message.edit(view=self, **V2_EDIT)
        except discord.HTTPException:
            pass

    def _channel(self, interaction: discord.Interaction):
        # The panel's channel, not the press's: a panel press arrives on the
        # ephemeral panel, whose channel is the same room but not the board.
        return getattr(self.message, "channel", None) or interaction.channel

    async def start_round(self, interaction: discord.Interaction) -> None:
        """Start Round — the host panel's button lands here."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, START_LABEL, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start rounds.", ephemeral=True)
            return
        if self._ended:
            await interaction.response.send_message(GAME_OVER_REPLY, ephemeral=True)
            return
        if round_in_progress(self._active_submit_view, self._active_vote_view, running=self._round_running):
            await interaction.response.send_message(ROUND_RUNNING_REPLY, ephemeral=True)
            return

        self.round_num += 1
        await interaction.response.defer()
        # The row must stop reading as an open lobby — the start-ping sweep
        # polls state='joining' (Fantasies is a lobby game since 2026-09-04)
        # and a game with a round underway is not idle.
        await update_game_state(self.db, self.game_id, "playing")

        self._round_running = True
        try:
            await self.cog._run_round(
                game_id=self.game_id,
                host_id=self.host_id,
                host_name=self.host_name,
                round_num=self.round_num,
                channel=self._channel(interaction),
                main_view=self,
            )
        finally:
            self._round_running = False

    async def close_submissions(self, interaction: discord.Interaction) -> None:
        """Close Submissions, from the host panel: routed to the round's open
        window, if there is one."""
        sub = self._active_submit_view
        if sub is None or sub._closed:
            await interaction.response.send_message(NO_WINDOW_REPLY, ephemeral=True)
            return
        await sub.close(interaction)

    async def next_entry(self, interaction: discord.Interaction) -> None:
        """⏭️ Next, from the host panel: routed to the entry being voted on,
        if there is one."""
        vote = self._active_vote_view
        if vote is None or vote._closed:
            await interaction.response.send_message(NO_ENTRY_REPLY, ephemeral=True)
            return
        await vote.next_entry(interaction)

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["fantasies"], ephemeral=True)

    async def end_game_button(self, interaction: discord.Interaction) -> None:
        """Post the recap and pay the room — the ending the game never had.

        Fantasies shipped with Start Round and Help only: the recap builder
        was dead code and ``end_game`` was reachable solely through ``/games
        end`` or the 24h sweep (anon-tail-65). Host or mod, from the host
        panel, behind the usual confirm popup.
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, END_LABEL, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can end the game.", ephemeral=True)
            return
        if self._ended:
            await interaction.response.send_message(GAME_OVER_REPLY, ephemeral=True)
            return
        channel = self._channel(interaction)

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self.cog._finish_game(self, channel=channel)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )


class SubmitRoundView(discord.ui.LayoutView):
    """One round's submission window — a ``LiveBoardLayoutView``
    (``game_id``, ``guild``, ``_panel``, ``_board_retired``, ``_rebuild``),
    so ``LiveBoards`` opens, redraws and retires it. The two category
    buttons are on it; Close Submissions is on the host panel and reaches
    :meth:`close` through the panel view."""

    def __init__(
        self,
        game_id: str,
        host_id: int,
        round_num: int,
        db,
        bot,
        guild: "discord.Guild | None" = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.round_num = round_num
        self.db = db
        self.bot = bot
        # Only read by the sticky board wiring, which needs a guild to key a
        # StickyPanel by; the card itself names nobody.
        self.guild = guild
        self.message: discord.Message | None = None
        self._closed = False
        # Set when the guild turns on the "keep the board at the bottom"
        # dial (Games Global Config → Live Game Boards): the StickyPanel
        # that owns reposting this round's submissions card while the window
        # is open, and the flag the round loop sets once it closes. Both stay
        # None/False for the life of the round when the dial is off.
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        self._rebuild()

    def _rebuild(self) -> None:
        """What the sticky panel re-posts, and the closed frame the round
        loop paints — the card is the same every time it is open; it is the
        buttons under it that have to stay reachable."""
        self.clear_items()
        for item in build_submit_layout(self.round_num, closed=self._closed):
            self.add_item(item)
        bind_present(self, {
            SUBMIT_FANTASY_ID: self.submit_fantasy,
            SUBMIT_DEALBREAKER_ID: self.submit_dealbreaker,
        })

    async def submit_fantasy(self, interaction: discord.Interaction) -> None:
        await self._open_entry_modal(interaction, CATEGORY_FANTASY)

    async def submit_dealbreaker(self, interaction: discord.Interaction) -> None:
        await self._open_entry_modal(interaction, CATEGORY_DEALBREAKER)

    async def _open_entry_modal(self, interaction: discord.Interaction, category: str) -> None:
        """One button per category — the choice is made before any typing."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, SUBMIT_LABELS[category], channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("Submissions are closed for this round.", ephemeral=True)
            return
        await interaction.response.send_modal(SubmitEntryModal(self.game_id, self.db, self.round_num, category))

    async def close(self, interaction: discord.Interaction) -> None:
        """Close Submissions, from the host panel."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, CLOSE_LABEL, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can close submissions.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message(NO_WINDOW_REPLY, ephemeral=True)
            return
        self._closed = True
        self.stop()
        # Ack only. The round loop is already waiting on stop() and paints
        # the closed card itself, because it is the one holding the sticky
        # panel — with the dial on, the message this press came from may
        # have been re-posted somewhere else since.
        await interaction.response.defer()


class FantasiesVoteView(_HostControls, discord.ui.LayoutView):
    """One entry's card and its controls — a ``LiveBoardLayoutView``
    (``game_id``, ``guild``, ``_panel``, ``_board_retired``, ``_rebuild``),
    so ``LiveBoards`` opens, redraws and retires it.

    The card carries what any member may press: ✅ Same and ❌ Not for Me.
    ⏭️ Next is on the ephemeral host panel, which calls :meth:`next_entry`
    here; the panel's other controls are handed back to ``main_view``.
    """

    def __init__(
        self,
        game_id: str,
        host_id: int,
        entry_text: str,
        entry_num: int,
        category: str,
        db,
        bot,
        host_name: str,
        advance_callback,
        entry_author_id: int = 0,
        total_entries: int = 0,
        pacing: RoundPacing | None = None,
        expected_voters: "set[int] | None" = None,
        guild: "discord.Guild | None" = None,
        main_view: "FantasiesMainView | None" = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        # Only read by the sticky board wiring, which needs a guild to key a
        # StickyPanel by; the entry card names nobody.
        self.guild = guild
        self.host_id = host_id
        self.entry_text = entry_text
        self.entry_num = entry_num
        self.total_entries = total_entries
        self.category = category
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.entry_author_id = entry_author_id
        self.main_view = main_view
        # Per-entry pacing (anon-tail-71): the timer that closes the vote,
        # the event Next/End/force-end set, and the room the entry waits on —
        # the vote closes itself once every one of them has voted.
        self.pacing = pacing or RoundPacing()
        self.expected_voters: set[int] = set(expected_voters or ())
        self.same_votes: list[int] = []
        self.nope_votes: list[int] = []
        self._updater = LiveBarUpdater()
        self._closed = False
        # force_end_active_game pokes this alias to wake the round loop.
        self._advanced_event = self.pacing.advanced
        self.message: discord.Message | None = None
        # Set when the guild turns on the "keep the board at the bottom"
        # dial (Games Global Config → Live Game Boards): the StickyPanel
        # that owns reposting this entry's board, and the flag the round
        # loop sets once the entry is over — the panel's ``build`` checks
        # that flag, never ``_closed``, which is set one legitimate render
        # *earlier* (the closed final frame, which still has to go out).
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, then bind whatever
        controls that state shows — a rebuild adds brand-new buttons with
        nothing wired to them; the closed frame shows none."""
        self.clear_items()
        for item in build_vote_layout(
            entry_text=self.entry_text,
            entry_num=self.entry_num,
            category=self.category,
            same_votes=self.same_votes,
            nope_votes=self.nope_votes,
            total_entries=self.total_entries,
            closed=self._closed,
            advance_at=self.pacing.advance_at(),
        ):
            self.add_item(item)
        bind_present(self, {SAME_ID: self.vote_same, NOPE_ID: self.vote_nope, HELP_ID: self.how_to_play})

    async def _after_vote(self, interaction: discord.Interaction) -> None:
        """Refresh the bars — or close the entry when the room is done."""
        message = self.message or interaction.message
        if everyone_has_voted(self.expected_voters, self.same_votes + self.nope_votes):
            await self.advance_callback(message)
            return
        # Rate-limited, but routed through the board's current location
        # rather than the message this click came from: a restick may have
        # reposted the board between the click and the gate opening.
        await self._updater.schedule_render(lambda: redraw_board(self, message))

    async def _vote(self, interaction: discord.Interaction, kind: str, label: str) -> None:
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        if interaction.user.id == self.entry_author_id:
            await interaction.response.send_message(SELF_VOTE_REFUSAL, ephemeral=True)
            return
        changed = apply_vote(self.same_votes, self.nope_votes, interaction.user.id, kind)
        msg = f"✅ Voted **{label}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._after_vote(interaction)

    async def vote_same(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "same", SAME_LABEL[2:])

    async def vote_nope(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "nope", NOPE_LABEL[2:])

    async def next_entry(self, interaction: discord.Interaction) -> None:
        """⏭️ Next, from the host panel: the timer (or a complete vote)
        closes the entry on its own; Next is the host's early close.
        Advances the *board's* message, not the one the press came from."""
        log.info("%s pressed 'Next' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can advance.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message(NO_ENTRY_REPLY, ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(self.message or interaction.message)

    async def start_round(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(ROUND_RUNNING_REPLY, ephemeral=True)

    async def close_submissions(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NO_WINDOW_REPLY, ephemeral=True)

    async def end_game_button(self, interaction: discord.Interaction) -> None:
        """End Game mid-vote lands on this view; the panel view owns the
        ending, so hand it there."""
        if self.main_view is None:
            await interaction.response.send_message(GAME_OVER_REPLY, ephemeral=True)
            return
        await self.main_view.end_game_button(interaction)

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["fantasies"], ephemeral=True)


class FantasiesCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # Every live round board this cog owns (Games Global Config → Live
        # Game Boards): the submissions card while its window is open, then
        # one board per entry being voted on — never two at once, so one
        # entry per game_id is enough. ``tracked=False`` because the game
        # row's message is the host's *control panel*, which recover_game
        # re-registers after a restart: a board id written there would hand
        # the panel's message to the wrong view.
        self.live_boards = LiveBoards(bot, self.db, "fantasies board", tracked=False)

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
        # A reload mid-round must not leave a debounced restick armed
        # against a cog that's gone.
        self.live_boards.unload()

    @commands.Cog.listener("on_message")
    async def _restick_boards(self, message: discord.Message) -> None:
        await self.live_boards.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_board_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        await self.live_boards.on_channel_delete(channel)

    @app_commands.command(name="fantasies", description=play_description("fantasies"))
    @app_commands.describe(
        start_in="Countdown before the first round, in minutes",
    )
    async def fantasies(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        # These moved to the dashboard. Passing None is exactly what an
        # omitted option always meant, so the per-guild dial applies.
        entry_seconds = None
        log.info("%s used /games play fantasies in #%s", interaction.user.display_name, channel_name(interaction.channel))
        refusal = await refuse_launch(self.db, interaction, "fantasies")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"start_in": start_in, "round_seconds": entry_seconds},
        )
        await finish_launch_response(interaction, game_id)
        # Nothing host-only is on the cards, so hand the host their controls
        # now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a Fantasies & Dealbreakers game: Start Round,
        Close Submissions, ⏭️ Next and End Game on one panel. The host
        always gets it; anyone else needs the same mod standing the controls
        check (``is_host_or_mod``)."""
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, _HostControls):
            await host_reply(interaction, GAME_ENDED_REPLY)
            return
        if not is_host_or_mod(interaction, view.host_id):
            await host_reply(interaction, NOT_HOST_REPLY)
            return
        panel = GameHostPanel(
            self.bot, game_id, build_host_panel_layout(),
            {
                HOST_START_ID: "start_round", HOST_CLOSE_ID: "close_submissions",
                HOST_NEXT_ID: "next_entry", HOST_END_ID: "end_game_button",
            },
            view_type=_HostControls,
        )
        await send_panel(interaction, panel, label="fantasies host panel")

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None.

        The per-entry timer is the launch's ``round_seconds`` (a slash
        ``entry_seconds`` or schedule option, even 0), else the dashboard's
        **Seconds per Entry** dial, else :data:`DEFAULT_ENTRY_SECONDS`; ``0``
        is host-paced. The panel opens as a ``joining`` lobby (Fantasies is
        in ``LOBBY_GAME_TYPES`` since 2026-09-04): a ``start_in`` stamps
        ``start_epoch`` for the countdown and the host nudge, and the
        idle-lobby dials and Game Lobby ping apply until the first round.
        """
        pacing = await launch_pacing(
            self.db, "fantasies", guild_id, options,
            default_round_seconds=DEFAULT_ENTRY_SECONDS,
        )
        start_epoch = pacing.start_epoch
        payload: dict = pacing.stamp(
            {"rounds": {}, "results": [], "round_seconds": pacing.round_seconds},
        )
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "fantasies",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )

        log.info("Game %s (fantasies) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = FantasiesMainView(
            game_id, host_id, self.db, self.bot, self,
            host_name=host_name, start_at=start_epoch,
            art=await read_lobby_art(self.bot, channel, TITLE),
        )
        self.bot.active_views[game_id] = view

        try:
            msg = await retry_transient(
                lambda: channel.send(view=view),
                label="fantasies launch",
            )
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("fantasies launch lacked send perms in channel %s", channel.id)
            return None
        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_round(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        main_view: "FantasiesMainView | None" = None,
    ):
        """Run one round: collect entries, then vote on each in turn.

        ``main_view`` is the control panel that started the round. While a
        round runs, ``bot.active_views[game_id]`` points at the live vote view
        (that is where ``/games end`` looks for the event to wake), and the
        panel is put back when the round is over — until 2026-09-04 it never
        was, so from round two on nothing could find the submit view to stop.
        """
        if main_view is None:
            candidate = self.bot.active_views.get(game_id)
            main_view = candidate if isinstance(candidate, FantasiesMainView) else None
        guild = getattr(channel, "guild", None)
        submit_view = SubmitRoundView(game_id, host_id, round_num, self.db, self.bot, guild=guild)
        # Let the main view know so it can stop us on close
        if main_view is not None:
            main_view._active_submit_view = submit_view
        # The submissions card is sticky (re-posting to the bottom of the
        # channel as chat buries it) for as long as the window is open when
        # the guild's dial is on, a plain send otherwise. This is the board
        # most worth keeping reachable: the window stays open until the host
        # closes it, so its buttons are what chat buries.
        submit_view.message = await self.live_boards.open(
            game_id, submit_view, channel, guild,
            suffix=f" round {round_num} submissions",
        )

        await submit_view.wait()

        # The submissions board's life ends here. Retire before painting the
        # closed card: that cancels any pending restick, and hands back
        # wherever the board actually ended up.
        closed_card = self.live_boards.retire(submit_view, channel) or submit_view.message
        submit_view._closed = True
        submit_view._rebuild()
        await self._edit_quietly(closed_card, view=submit_view, **V2_EDIT)

        # Clear reference now that submission phase is over
        if main_view is not None:
            main_view._active_submit_view = None

        # If game was closed during submission, bail out
        if game_id not in self.bot.active_views:
            return

        payload = await get_game_payload(self.db, game_id)
        entries = get_round_entries(payload, round_num)

        if not entries:
            # The zero-entry round is the skip: the panel stays live, and the
            # host picks between another round and the ending.
            await channel.send(
                f"No entries this round — press **{START_LABEL}** to try again, or **{END_LABEL}** to wrap up."
            )
            await self._restore_panel(game_id, main_view)
            return

        results = list(payload.get("results") or [])
        for i, entry_data in enumerate(entries):
            entry_text = entry_data["text"]
            entry_category = entry_data.get("category", "Fantasy")
            entry_num = i + 1
            entry_author = entry_data["user_id"]

            # Per-entry pacing: the dial's timer, and the room the entry waits
            # on — this round's submitters plus everyone who has voted so far,
            # minus the author, who cannot vote on their own entry.
            entry_seconds = int(payload.get("round_seconds", 0) or 0)
            pacing = RoundPacing(round_seconds=entry_seconds, opened_at=time.time())
            expected = active_voters(entries, results, exclude=entry_author)

            # ``view`` is created first, unbound, so ``advance`` below can
            # close over *this* iteration's view as a default argument
            # instead of the loop variable — the closure is handed to the
            # view itself as its ``advance_callback`` and can be invoked
            # again by a stale button press after the loop has moved on to
            # the next entry, so it must not resolve "the view" through a
            # name the loop keeps reassigning.
            view = FantasiesVoteView(
                game_id=game_id,
                host_id=host_id,
                entry_text=entry_text,
                entry_num=entry_num,
                category=entry_category,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=None,
                entry_author_id=entry_author,
                total_entries=len(entries),
                pacing=pacing,
                expected_voters=expected,
                guild=guild,
                main_view=main_view,
            )

            async def advance(
                message: discord.Message,
                _view=view,
                _text=entry_text,
                _num=entry_num,
                _author=entry_author,
                _cat=entry_category,
            ) -> None:
                if _view._closed:
                    return
                _view._closed = True

                result_entry = build_result_entry(
                    text=_text,
                    category=_cat,
                    author=_author,
                    same_votes=_view.same_votes,
                    nope_votes=_view.nope_votes,
                )
                results.append(result_entry)

                # Persist incrementally so mid-game close doesn't lose prior results
                def _save_result(payload, _entry=result_entry):
                    payload.setdefault("results", []).append(_entry)
                await modify_payload(self.db, game_id, _save_result)

                # Retire first: that cancels any pending restick, so
                # nothing can repost the entry *over* its own closing frame,
                # and it hands back wherever the board actually ended up —
                # ``message`` may be the one captured when the entry opened,
                # which a restick can have moved since.
                target = self.live_boards.retire(_view, channel) or message
                # The closed frame is the entry's record: the final bars and
                # no controls, so nothing on it can be pressed.
                _view._rebuild()
                try:
                    await target.edit(view=_view, **V2_EDIT)
                except discord.HTTPException:
                    pass
                _view.pacing.advanced.set()

            view.advance_callback = advance
            self.bot.active_views[game_id] = view
            if main_view is not None:
                main_view._active_vote_view = view

            # Post this entry's board — sticky for the length of its vote
            # when the guild's dial is on, a plain send otherwise. No ``msg``
            # to hand over: each entry posts a *fresh* board and leaves the
            # entry that just closed on screen as its record.
            sent = await self.live_boards.open(
                game_id, view, channel, guild, suffix=f" entry {entry_num}",
            )
            view.message = sent
            # The timer closes the entry unless Next, a complete vote, End or
            # a force-end gets there first (round_pacing's wait_for pattern).
            closer: Callable[[], Awaitable[None]] = functools.partial(advance, sent)
            pacing.start_timer(closer)
            await pacing.advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(1)

        if main_view is not None:
            main_view._active_vote_view = None

        # If the game was already closed by the host, skip saving
        if game_id not in self.bot.active_views:
            return

        # Results were saved incrementally in advance(); no extra save needed
        await self._restore_panel(game_id, main_view)

    async def _restore_panel(self, game_id: str, main_view: "FantasiesMainView | None") -> None:
        """Hand ``active_views`` back to the control panel after a round, and
        redraw it with the round counted."""
        if main_view is not None and game_id in self.bot.active_views:
            self.bot.active_views[game_id] = main_view
            await main_view.redraw()

    async def _post_recap(self, channel, payload: dict) -> bool:
        """Post the final recap with the payout line; False when there is
        nothing to recap (no entry was ever voted on)."""
        results = payload.get("results", [])
        guild = getattr(channel, "guild", None)
        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "fantasies")
        items = build_recap_layout(results, payout_line=payout_line)
        if items is None:
            return False
        await channel.send(view=card(items))
        return True

    async def _finish_game(self, main_view: FantasiesMainView, *, channel) -> None:
        """The host's ending: wake and close whatever the round loop is
        blocked on, close the panel, post the recap, pay the roster.

        Mirrors Hot Takes' completion: the roster is entry authors plus every
        voter (``roster_from_results``), the round count is the entries voted
        on, and ``end_game`` gets ``bot=``/``player_ids=`` so the faucet
        fires. An entry mid-vote when End is pressed is dropped, as it is on
        ``/games end`` — its votes were never persisted.
        """
        game_id = main_view.game_id
        # Claim the game in memory first so the round loop returns at its
        # guard instead of posting the next entry on top of the recap.
        self.bot.active_views.pop(game_id, None)

        sub = main_view._active_submit_view
        if sub is not None and not sub.is_finished():
            sub.stop()
            sub._closed = True
            sub._rebuild()
            # Retire whichever board is live — at most one is, since the
            # submissions card closes before the first entry opens — so no
            # panel is left re-posting a board this ending just closed.
            await self._edit_quietly(
                self.live_boards.retire(sub, channel) or sub.message, view=sub, **V2_EDIT,
            )
        vote = main_view._active_vote_view
        if vote is not None and not vote._closed:
            vote._closed = True
            vote._rebuild()
            await self._edit_quietly(
                self.live_boards.retire(vote, channel) or vote.message, view=vote, **V2_EDIT,
            )
            if vote._advanced_event is not None:
                vote._advanced_event.set()

        main_view._ended = True
        main_view.stop()
        await main_view.redraw()

        payload = await get_game_payload(self.db, game_id)
        results = payload.get("results", [])
        roster = roster_from_results(results)
        if not await self._post_recap(channel, payload):
            await channel.send("✨ Fantasies & Dealbreakers ended — no entries were voted on, so there's nothing to recap.")
        log.info("Game %s (fantasies) ended — %d players, %d entries", game_id, len(roster), len(results))
        await end_game(
            self.db, game_id,
            player_count=len(roster), round_count=len(results), payload=payload,
            bot=self.bot, player_ids=roster,
        )

    @staticmethod
    async def _edit_quietly(message, **kwargs) -> None:
        """Best-effort edit of a message we may no longer hold or that is gone."""
        if message is None:
            return
        try:
            await message.edit(**kwargs)
        except discord.HTTPException:
            pass

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the host control panel after a restart.

        Fantasies is host-driven: the FantasiesMainView (the tracked message) is
        the persistent control panel, and the host presses "Start Round" — on
        the host panel — to run each round. A round's submit/vote messages
        aren't tracked, so a round interrupted by a crash is abandoned — the
        host simply starts the next round. We restore the round counter so
        numbering continues correctly. That is also why a round's boards keep
        their sticky-panel ids in memory rather than in this row (see
        ``TransientBoardLocation``): this row's message is the control panel
        being re-registered right here. A panel still an embed on screen
        (posted before the conversion) is redrawn as a card at once.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        view = FantasiesMainView(
            game_id, host_id, self.db, self.bot, self,
            host_name=await host_display(self.bot, guild, host_id),
            start_at=extract_start_epoch(payload),
            art=await read_lobby_art(self.bot, channel, TITLE),
        )
        view.message = message
        rounds = payload.get("rounds", {})
        if rounds:
            try:
                view.round_num = max(int(k) for k in rounds)
            except ValueError:
                view.round_num = 0
        view._rebuild()
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        await redraw_embed_era_lobby(view, message)
        log.info(
            "Recovered fantasies game %s (control panel, round_num=%d) in #%s",
            game_id, view.round_num, getattr(channel, "name", channel.id),
        )
        return True


async def setup(bot: "Bot"):
    cog = FantasiesCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("fantasies")
    play.add_command(cog.fantasies, override=True)
    bot.game_launchers["fantasies"] = cog.launch
    bot.game_recoverers["fantasies"] = cog.recover_game
