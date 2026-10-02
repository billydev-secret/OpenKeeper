"""
Name Your Price — game cog.

A scenario is posed and everyone secretly submits a dollar amount for how much
money it would take for them to do it.  Prices are revealed sorted lowest to
highest, then the room votes on "Most Reasonable" and "Most Unhinged."

The game opens a join lobby (``LOBBY_GAME_TYPES``): the roster is what lets a
round close the moment everyone has answered, and the lobby is what invites
the room in — until 2026-09-04 the command posted a placeholder and pinged the
host to write a scenario, and every round ran its full timer (trivia-tail-85 /
86). Scenarios come from the question bank unless the host chose otherwise;
when the host or the room writes them, the prompt is a button on the board,
not a public ping (trivia-tail-97).

Pure logic and the Components V2 card builders live in
``bot_modules/games/price/{logic,layouts}.py``; this module keeps only
the Discord glue (slash command, modals, views, round loop). Nothing
host-only is on a public card: Start, Cancel, Write Scenario, Draw From the
Bank, Skip, Add Rounds and End Game are on the ephemeral host panel
(``/games host``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.background import spawn
from bot_modules.core.branding_service import PanelArt
from bot_modules.core.layout import V2_EDIT, bind, bind_present, card, disable_all
from bot_modules.core.sticky import StickyPanel
from bot_modules.core.utils import is_host_or_mod
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    sign_off_game_chore,
    finish_launch_response,
    create_game,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    get_game_options,
    modify_payload,
    end_game,
    update_session,
    is_game_expired,
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
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.lobby_art import NO_ART, read_lobby_art
from bot_modules.games.utils.names import build_names, cached_names, host_display, plain_name
from bot_modules.shared.name_resolver import GuildNames
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.question_source import (
    channel_allows_nsfw,
    get_price_scenario,
    has_matching_questions,
)
from bot_modules.games.utils.timer import GameTimer, now_plus
from bot_modules.games.price.layouts import (
    CANCELLED_TEXT,
    HELP_ID,
    HOST_ADD_ROUNDS_ID,
    HOST_CANCEL_ID,
    HOST_DRAW_ID,
    HOST_END_ID,
    HOST_SKIP_ID,
    HOST_START_ID,
    HOST_WRITE_ID,
    JOIN_ID,
    LEAVE_ID,
    LOBBY_HELP_ID,
    PICKING_UP_TEXT,
    REASONABLE_ID,
    RUN_AGAIN_ID,
    SUBMIT_ID,
    SUBMIT_TIMER_ITEM_ID,
    TITLE,
    UNHINGED_ID,
    VOTE_TIMER_ITEM_ID,
    WRITE_ID,
    build_lobby_layout,
    build_host_panel_layout,
    build_notice_layout,
    build_recap_layout,
    build_reveal_layout,
    build_round_results_layout,
    build_scenario_layout,
    build_scenario_wait_layout,
    build_start_layout,
    build_vote_layout,
)
from bot_modules.games.price.logic import (
    MIN_PLAYERS,
    SOURCE_HOST,
    SOURCE_PLAYERS,
    build_ladder,
    collect_all_players,
    compute_highlight,
    compute_recap_awards,
    format_price,
    lobby_players,
    parse_price,
    resolve_source,
    roster_all_in,
    tally_winners,
    toggle_player,
    vote_possible,
)
from bot_modules.games.board_sticky_service import (
    LiveBoards,
    redraw_board,
    redraw_embed_era_lobby,
)
from bot_modules.games.start_ping_service import (
    extract_start_epoch,
    resolve_start_epoch,
)

log = logging.getLogger(__name__)

SCENARIO_WAIT_SECONDS = 120.0


# ── Modals ───────────────────────────────────────────────────────────────────

class PriceModal(discord.ui.Modal, title="Name Your Price"):
    price = discord.ui.TextInput(
        label="Your price ($)",
        placeholder="e.g. 500, $1,000, 5k, 1M",
        required=True,
        max_length=20,
    )

    def __init__(self, game_view: "PriceGameView"):
        super().__init__()
        self._view = game_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted price modal in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        amount = parse_price(self.price.value)
        if amount is None:
            await interaction.response.send_message(
                "Couldn't parse that as a price. Try something like `500`, `$1,000`, `5k`, or `1M`.",
                ephemeral=True,
            )
            return

        view = self._view
        uid = interaction.user.id
        changed = uid in view.prices
        view.prices[uid] = amount
        label = f"✅ Submitted **{format_price(amount)}**"
        if changed:
            label += " (updated)"
        await interaction.response.send_message(label, ephemeral=True, delete_after=5)

        # Update submission count on embed
        await view.refresh_embed()

        # Auto-advance once everyone who joined has answered
        if view.everyone_in():
            view.skip_timer()


class HostScenarioModal(discord.ui.Modal, title="Write a Scenario"):
    scenario = discord.ui.TextInput(
        label="Scenario",
        placeholder="How much money would it cost for you to…",
        style=discord.TextStyle.long,
        required=True,
        max_length=500,
    )

    def __init__(self):
        super().__init__()
        self._result: str | None = None
        self._event = asyncio.Event()

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted scenario modal in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        self._result = self.scenario.value.strip()
        await interaction.response.send_message("✅ Scenario submitted!", ephemeral=True, delete_after=5)
        self._event.set()

    def give_up(self) -> None:
        """Resolve with no text — the caller draws from the bank instead."""
        self._result = None
        self._event.set()

    @property
    def resolved(self) -> bool:
        """Has a scenario been submitted (or the bank chosen)? Discord never
        reports a dismissed modal, so *opening* must not count."""
        return self._event.is_set()

    async def wait_for_result(self, timeout: float = SCENARIO_WAIT_SECONDS) -> str | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._result


class AddRoundsModal(discord.ui.Modal, title="Add Rounds"):
    count = discord.ui.TextInput(
        label="How many rounds to add?",
        placeholder="e.g. 3",
        required=True,
        max_length=3,
    )

    def __init__(self, cog: "PriceCog", game_id: str):
        super().__init__()
        self._cog = cog
        self._game_id = game_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.count.value.strip())
            if n < 1 or n > 20:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a number between 1 and 20.", ephemeral=True)
            return

        payload = await get_game_payload(self._cog.db, self._game_id)
        payload["total_rounds"] = payload.get("total_rounds", 5) + n
        await update_game_payload(self._cog.db, self._game_id, payload)

        # Update view if tracked
        view = self._cog.bot.active_views.get(self._game_id)
        if isinstance(view, (PriceGameView, PriceVoteView)):
            view.total_rounds += n

        await interaction.response.send_message(
            f"✅ Added **{n}** rounds! New total: **{payload['total_rounds']}**.",
            ephemeral=True,
        )


# ── Views ────────────────────────────────────────────────────────────────────

NOT_STARTED_REPLY = "The game hasn't started yet — press Start once the pool is in."
ALREADY_STARTED_REPLY = "The game has already started."
NO_SCENARIO_WAIT_REPLY = "No round is waiting for a scenario right now."
NO_ROUND_REPLY = "No round is taking prices right now."
#: Each phase answers an End Game / Add Rounds press it cannot act on in its
#: own words — the lobby's "hasn't started yet" is wrong once a game is under
#: way (ship review, 2026-10-01).
SCENARIO_WAIT_ADD_REPLY = "A scenario is still being written — add rounds once prices are open, or during the vote."
SCENARIO_WAIT_END_REPLY = "A scenario is still being written — End Game works once prices are open."
VOTE_END_REPLY = "Voting is under way — End Game works while prices are open."
ROUND_OVER_REPLY = "This round's prices are already in — End Game works once the next round opens."


class _HostControls:
    """The seven host-panel hand-offs every Price view answers.

    The game moves between phases on its own, so the panel carries every
    control and hands each press to whichever view is live
    (``host_panel.GameHostPanel``). A phase that has no use for a control
    answers with the line saying so; a phase that does overrides the method.
    """

    host_id: int

    async def start_game(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(ALREADY_STARTED_REPLY, ephemeral=True)

    async def cancel_lobby(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(ALREADY_STARTED_REPLY, ephemeral=True)

    async def write_scenario(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NO_SCENARIO_WAIT_REPLY, ephemeral=True)

    async def draw_bank(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NO_SCENARIO_WAIT_REPLY, ephemeral=True)

    async def skip_round(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NO_ROUND_REPLY, ephemeral=True)

    async def add_rounds(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NOT_STARTED_REPLY, ephemeral=True)

    async def end_game_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NOT_STARTED_REPLY, ephemeral=True)


class ScenarioPromptView(_HostControls, discord.ui.LayoutView):
    """The board while a scenario is being written (``build_scenario_wait_
    layout``). Sits on the game message instead of a public ``<@host>``
    ping. For the ``players`` source anyone may write and the button is on
    the card; for ``host`` the panel's Write Scenario and Draw From the
    Bank land here. A second press after a submission is told so — after a
    *dismissed* modal the button simply opens it again, since Discord never
    reports the dismissal and the host used to be locked out for the whole
    wait. Registered as the game's live view for the wait so ``/games
    host`` and a force-close both reach it.
    """

    def __init__(self, modal: HostScenarioModal, host_id: int, round_num: int, total_rounds: int, *, open_to_all: bool):
        super().__init__(timeout=SCENARIO_WAIT_SECONDS)
        self._modal = modal
        self.host_id = host_id
        self.open_to_all = open_to_all
        self._closed = False
        # force_end_active_game pokes this to wake the wait.
        self._done_event = modal._event
        for item in build_scenario_wait_layout(
            round_num, total_rounds, SOURCE_PLAYERS if open_to_all else SOURCE_HOST,
        ):
            self.add_item(item)
        bind_present(self, {WRITE_ID: self.write_scenario})

    def _may_write(self, interaction: discord.Interaction) -> bool:
        return self.open_to_all or is_host_or_mod(interaction, self.host_id)

    async def write_scenario(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Write Scenario' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not self._may_write(interaction):
            await interaction.response.send_message("❌ Only the host or a mod can write the scenario.", ephemeral=True)
            return
        if self._modal.resolved:
            await interaction.response.send_message("Someone already submitted a scenario!", ephemeral=True)
            return
        await interaction.response.send_modal(self._modal)

    async def draw_bank(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Draw From the Bank' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can draw from the bank.", ephemeral=True)
            return
        await interaction.response.defer()
        self._modal.give_up()

    async def add_rounds(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(SCENARIO_WAIT_ADD_REPLY, ephemeral=True)

    async def end_game_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(SCENARIO_WAIT_END_REPLY, ephemeral=True)


class PriceLobbyView(_HostControls, discord.ui.LayoutView):
    """The join lobby. **Paradigm 8 — live board**: rebuilt and re-edited in
    place on every Join/Leave. ``price_join`` / ``price_leave`` /
    ``price_lobby_htp`` are frozen custom ids — ``recover_game``
    re-registers this view against the lobby message after a restart.
    Start and Cancel are not here: the host panel calls
    :meth:`start_game` and :meth:`cancel_lobby`.
    """

    def __init__(
        self, game_id: str, host_id: int, db, bot, cog: "PriceCog",
        *,
        guild: "discord.Guild | None" = None,
        host_name: str = "Host",
        payload: dict | None = None,
        art: PanelArt = NO_ART,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.guild = guild
        self.names: GuildNames = cached_names(guild)
        self.host_name = host_name
        self.art = art
        self.message: discord.Message | None = None
        self._closed = False
        self._rebuild(payload or {})

    async def refresh_names(self, payload: dict) -> None:
        """Re-resolve the roster (live cache, then ``known_users``) before a
        redraw; ``_rebuild`` stays synchronous."""
        self.names = await build_names(self.bot, self.guild, lobby_players(payload))

    def _rebuild(self, payload: dict) -> None:
        settings = payload.get("settings") or {}
        self.clear_items()
        for item in build_lobby_layout(
            self.host_name,
            [plain_name(self.names, uid) for uid in lobby_players(payload)],
            payload.get("total_rounds", settings.get("rounds", 5)),
            settings.get("source", SOURCE_HOST),
            # Re-read from the payload, not the view: the countdown must
            # survive a restart that rebuilt this view from the DB.
            start_at=extract_start_epoch(payload),
            min_players=MIN_PLAYERS,
            closed=self._closed,
            header_url=self.art.header,
            footer_url=self.art.footer,
            title_in_header=self.art.title_in_header,
        ):
            self.add_item(item)
        bind_present(self, {JOIN_ID: self.join, LEAVE_ID: self.leave, LOBBY_HELP_ID: self.how_to_play})

    async def _refresh_lobby(self, interaction: discord.Interaction, payload: dict) -> None:
        self.guild = interaction.guild or self.guild
        await self.refresh_names(payload)
        self._rebuild(payload)
        await interaction.response.edit_message(view=self, **V2_EDIT)

    async def join(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Join' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id
        result: dict[str, str] = {}

        def _toggle(payload):
            if uid in lobby_players(payload):
                result["action"] = "already"
                return
            result["action"] = toggle_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _toggle)
        await self._refresh_lobby(interaction, payload)
        if result.get("action") == "already":
            await interaction.followup.send("You're already in — press **Leave** to drop out.", ephemeral=True)

    async def leave(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Leave' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id

        def _remove(payload):
            if uid in lobby_players(payload):
                toggle_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _remove)
        await self._refresh_lobby(interaction, payload)

    async def start_game(self, interaction: discord.Interaction) -> None:
        """Start — the host panel's button lands here."""
        log.info("%s pressed 'Start' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = lobby_players(payload)
        if len(players) < MIN_PLAYERS:
            await interaction.response.send_message(
                f"Need at least {MIN_PLAYERS} players to start Name Your Price. Currently: {len(players)}.",
                ephemeral=True,
            )
            return
        if self._closed:
            await interaction.response.send_message(ALREADY_STARTED_REPLY, ephemeral=True)
            return
        await interaction.response.defer()
        await self.close(payload)
        message = self.message
        assert message is not None  # set at launch and recovery
        await self.cog._begin_game(self.game_id, payload)
        await self.cog._start_rounds(self.game_id, self.host_id, message.channel, message, payload)

    async def close(self, payload: dict) -> None:
        """The lobby's closed frame: no Join, no Leave. Shared by Start and
        the countdown auto-start."""
        self._closed = True
        self.stop()
        await self.refresh_names(payload)
        self._rebuild(payload)
        if self.message is not None:
            try:
                await self.message.edit(view=self, **V2_EDIT)
            except discord.HTTPException:
                pass

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["price"], ephemeral=True)

    async def cancel_lobby(self, interaction: discord.Interaction) -> None:
        """Close the lobby before it starts — host or mod, behind the usual
        confirm popup. A lobby has no roster that played, so this pays nobody."""
        log.info("%s pressed 'Cancel Lobby' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can cancel the lobby.", ephemeral=True)
            return
        anchor = self.message

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self._cancel(anchor)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    async def _cancel(self, anchor: discord.Message | None) -> None:
        self._closed = True
        self.stop()
        if anchor is not None:
            try:
                await anchor.edit(view=card(build_notice_layout(CANCELLED_TEXT)), **V2_EDIT)
            except discord.HTTPException:
                pass
        await end_game(self.db, self.game_id, reason="cancelled")
        self.bot.active_views.pop(self.game_id, None)


class PriceGameView(_HostControls, discord.ui.LayoutView):
    """The submission board — a ``LiveBoardLayoutView`` (``game_id``,
    ``guild``, ``_panel``, ``_board_retired``, ``_rebuild``), so the round
    loop opens, redraws and retires it through ``LiveBoards``. Skip, Add
    Rounds and End Game land here from the host panel."""

    def __init__(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        scenario: str,
        round_num: int,
        total_rounds: int,
        timer_secs: int,
        db,
        bot,
        cog: "PriceCog",
        expected_ids: set[int] | None = None,
        settings: dict | None = None,
        guild: "discord.Guild | None" = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.scenario = scenario
        self.round_num = round_num
        self.total_rounds = total_rounds
        self.timer_secs = timer_secs
        self.guild = guild
        self.db = db
        self.bot = bot
        self.cog = cog
        # The lobby roster: the round closes as soon as every one of these
        # ids has priced. Anyone in the channel may submit, so this is a set
        # of ids, never a headcount. Empty (no roster) runs the full timer.
        self.expected_ids: set[int] = set(expected_ids or ())
        self.settings = settings or {}
        self.prices: dict[int, int] = {}
        self._msg: discord.Message | None = None
        self._timer: GameTimer | None = None
        self._closed = False
        # Set when the guild's sticky dial is on for this round's submission
        # board: the StickyPanel that owns reposting it, and the flag
        # PriceCog retires once this round's board is done (the
        # panel's build callback checks this — see PriceCog._board_content).
        # Both stay None/False for a round when the dial is off.
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        # The countdown's deadline, fixed when the round opens: the builder
        # prints what it is given, so a mid-round redraw never resets it.
        self._deadline: int = now_plus(timer_secs)
        self._rebuild()

    def everyone_in(self) -> bool:
        """Has everyone who joined named a price? (A spectator's price counts
        in the reveal but fills nobody's seat.)"""
        return roster_all_in(self.expected_ids, set(self.prices))

    def _rebuild(self) -> None:
        """Clear and re-add the board from current state, re-binding its
        buttons — a rebuild adds brand-new ``Button`` instances. The closed
        frame carries none."""
        self.clear_items()
        for item in build_scenario_layout(
            self.scenario,
            self.round_num,
            self.total_rounds,
            self._deadline,
            len(self.prices),
            len(self.expected_ids) or None,
            roster_submitted=(
                len(self.expected_ids & set(self.prices)) if self.expected_ids else None
            ),
            closed=self._closed,
        ):
            self.add_item(item)
        bind_present(self, {SUBMIT_ID: self.submit_price, HELP_ID: self.how_to_play})

    async def refresh_embed(self):
        """Redraw the submission board with the current price count.

        When the sticky dial is on, ``redraw_board`` routes through the
        panel's own refresh instead of the cached ``self._msg`` — the panel
        may have reposted the board to the bottom of the channel since the
        last redraw, and an edit against the old ``self._msg`` would then
        silently 404 (same reasoning as RushmoreDraftView.refresh_board).

        ``PriceModal.on_submit`` calls this directly — unlike Rushmore's
        blitz picks, there's no coalescer/cancel between a submission and
        the round retiring its board, so two players submitting close
        together can race: the second one's redraw can land after the cog's
        retire has already flipped ``_board_retired``. That reaches
        ``redraw_board`` as ``_board_content``'s deliberate
        resurrection-refusal, which it swallows the same way it swallows an
        edit 404; the round is over, there's nothing left to redraw.
        """
        await redraw_board(self, self._msg)

    def skip_timer(self):
        if self._timer:
            self._timer.skip()

    async def submit_price(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Name Your Price' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        await interaction.response.send_modal(PriceModal(self))

    async def skip_round(self, interaction: discord.Interaction) -> None:
        """⏭️ Skip, from the host panel."""
        log.info("%s pressed 'Skip' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can skip.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message(NO_ROUND_REPLY, ephemeral=True)
            return
        await interaction.response.defer()
        self.skip_timer()

    async def add_rounds(self, interaction: discord.Interaction) -> None:
        """➕ Add Rounds, from the host panel."""
        log.info("%s pressed 'Add Rounds' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can add rounds.", ephemeral=True)
            return
        await interaction.response.send_modal(AddRoundsModal(self.cog, self.game_id))

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["price"], ephemeral=True)

    async def end_game_button(self, interaction: discord.Interaction) -> None:
        """End Game, from the host panel: stop after this round's prices and
        post the recap — host or mod, behind the usual confirm popup. The
        recap pays everyone who submitted in any round, this one included
        (trivia-tail-87)."""
        log.info("%s pressed 'End Game' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can end the game.", ephemeral=True)
            return
        # The round is already closed (the reveal's few seconds): there is
        # nothing for ``end_early`` to end, and it would return without a word
        # after the host had confirmed.
        if self._closed:
            await interaction.response.send_message(ROUND_OVER_REPLY, ephemeral=True)
            return
        # The board's channel, not the press's: a panel press arrives on the
        # ephemeral panel, whose channel is the same room but not the board.
        channel = getattr(self._msg, "channel", None) or interaction.channel
        guild = self.guild or interaction.guild

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self.cog.end_early(self, channel, guild)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )


class PriceVoteView(_HostControls, discord.ui.LayoutView):
    """The voting card and its two dropdowns (``build_vote_layout``)."""

    def __init__(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        scenario: str,
        submitters: list[int],
        prices: dict[int, int],
        round_num: int,
        total_rounds: int,
        timer_secs: int,
        guild,
        db,
        bot,
        cog: "PriceCog | None" = None,
        names: GuildNames | None = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.scenario = scenario
        self.submitters = submitters
        self.prices = prices
        self.round_num = round_num
        self.total_rounds = total_rounds
        self.timer_secs = timer_secs
        self.guild = guild
        self.db = db
        self.bot = bot
        self.cog = cog
        self.names: GuildNames = names or cached_names(guild)
        self.reasonable_votes: dict[int, int] = {}
        self.unhinged_votes: dict[int, int] = {}
        self._msg: discord.Message | None = None
        self._timer: GameTimer | None = None
        self._closed = False
        for item in build_vote_layout(
            scenario, round_num, total_rounds, now_plus(timer_secs),
            [(f"{plain_name(self.names, uid)} — {format_price(prices[uid])}", str(uid)) for uid in submitters],
        ):
            self.add_item(item)
        bind_present(self, {REASONABLE_ID: self._vote_reasonable, UNHINGED_ID: self._vote_unhinged})

    async def _vote(self, interaction: discord.Interaction, votes: dict[int, int], glyph: str, what: str) -> None:
        if self._closed:
            await interaction.response.send_message("This vote is over.", ephemeral=True)
            return
        values = (interaction.data or {}).get("values") or []
        if not values:
            await interaction.response.defer()
            return
        uid = interaction.user.id
        target = int(values[0])
        if uid == target:
            await interaction.response.send_message("❌ You can't vote for yourself!", ephemeral=True)
            return
        changed = uid in votes
        votes[uid] = target
        target_name = plain_name(self.names, target)
        msg = f"✅ Voted **{glyph} {discord.utils.escape_markdown(target_name)}** as {what}"
        if changed:
            msg += " (changed)"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=5)
        if self.all_voted():
            self.skip_timer()

    async def _vote_reasonable(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, self.reasonable_votes, "🎯", "Most Reasonable")

    async def _vote_unhinged(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, self.unhinged_votes, "🤯", "Most Unhinged")

    def all_voted(self) -> bool:
        """True if every submitter has voted in both categories."""
        for uid in self.submitters:
            if uid not in self.reasonable_votes or uid not in self.unhinged_votes:
                return False
        return True

    def skip_timer(self):
        if self._timer:
            self._timer.skip()

    async def add_rounds(self, interaction: discord.Interaction) -> None:
        """➕ Add Rounds works mid-vote too — the total is read at the next
        advance."""
        log.info("%s pressed 'Add Rounds' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can add rounds.", ephemeral=True)
            return
        if self.cog is None:
            await interaction.response.send_message(NO_ROUND_REPLY, ephemeral=True)
            return
        await interaction.response.send_modal(AddRoundsModal(self.cog, self.game_id))

    async def end_game_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(VOTE_END_REPLY, ephemeral=True)


class PriceRecapView(discord.ui.LayoutView):
    """Rides on the recap: **Run Again** opens the next lobby under whoever
    pressed it — that *is* the hand-off, so the old Hand Off button is gone
    and the host/mod gate with it, matching Mt. Rushmore Draft's recap card
    (social-prompt-45; Price's own Hand Off came from trivia-tail-97)."""

    def __init__(self, game_id: str, host_id: int, cog: "PriceCog", settings: dict,
                 items: "list[discord.ui.Item[Any]]"):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.cog = cog
        self._settings = settings
        for item in items:
            self.add_item(item)
        bind(self, RUN_AGAIN_ID, self.run_again)

    async def _relaunch(self, interaction: discord.Interaction) -> None:
        """Open a fresh lobby with the presser as host, behind the same gate
        as the slash entry: an admin who unticks the game on the dashboard
        mid-evening must not be overridden by the recap card."""
        refusal = await refuse_launch(self.cog.db, interaction, "price")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        # Disable buttons on old recap
        disable_all(self)
        assert interaction.message  # component interactions always carry their message
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()
        await interaction.response.defer()
        game_id = await self.cog.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "rounds": self._settings.get("rounds", 5),
                "timer": self._settings.get("timer", 30),
                "vote_timer": self._settings.get("vote_timer", 20),
                "source": self._settings.get("source"),
            },
        )
        # A recap's relaunch button goes straight to the launcher, missing the
        # shared finish_launch_response seam — but a mod restarting a round by
        # hand has run a game. Signed off only on a launch that actually
        # produced a game (a falsy id means it failed, e.g. no send permission),
        # and after it, so a chore is never ticked green for a round that never
        # started.
        if game_id:
            await sign_off_game_chore(
                self.cog.bot, interaction.guild_id, interaction.user.id
            )
            # The presser is the new host, and the lobby carries nothing
            # host-only — hand them their controls.
            await hand_over(self.cog.bot, interaction, self.cog.open_host_panel, game_id)

    async def run_again(self, interaction: discord.Interaction) -> None:
        # Any member may press it and becomes the new host: same settings, a
        # fresh lobby, the presser at the keyboard.
        log.info("%s pressed 'Run Again' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self._relaunch(interaction)


# ── Cog ──────────────────────────────────────────────────────────────────────

class PriceCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self._auto_tasks: set[asyncio.Task] = set()
        # Every live submission board this cog owns (Games Global Config →
        # Live Game Boards). One StickyPanel per *live round's submission
        # board* — same shape as RushmoreCog.live_boards (see
        # docs/plans/sticky-panel-extraction.md, Group E), but scoped to a
        # round rather than a whole game: a fresh panel each round, dropped
        # once that round's submission window closes (disabled frame shown,
        # or reveal posted). Keyed by game_id inside LiveBoards — only one
        # round is ever live per game at a time.
        self.live_boards = LiveBoards(bot, self.db, "price board")

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

    async def recover_game(self, row, payload, channel, message) -> bool:
        """After a restart: re-register a lobby's view, or re-drive the round
        loop from the next un-played round.

        ``completed_rounds`` counts rounds that finished *scoring* (a round's
        entry is written to payload["rounds"] earlier, at submission time, so it
        isn't a safe completion marker). _run_round is recursive, so we re-invoke
        it at completed_rounds+1 and roll scores back to the matching checkpoint,
        so a round interrupted mid-scoring neither double-counts nor is lost.
        """
        settings = payload.get("settings")
        if not settings:
            return False
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = await host_display(self.bot, guild, host_id)

        if row["state"] == "joining":
            view = PriceLobbyView(
                game_id, host_id, self.db, self.bot, self,
                guild=guild, host_name=host_name, payload=payload,
                art=await read_lobby_art(self.bot, channel, TITLE),
            )
            view.message = message
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            await redraw_embed_era_lobby(view, message)
            log.info("Recovered price lobby %s in #%s", game_id, getattr(channel, "name", channel.id))
            return True

        total_rounds = payload.get("total_rounds", settings.get("rounds", 0))
        start_round = payload.get("completed_rounds", 0) + 1

        # Roll scores back to the last completed round so the interrupted round
        # (which may have written partial scores) re-runs from a clean base.
        if "scores_checkpoint" in payload:
            payload["scores"] = {k: dict(v) for k, v in payload["scores_checkpoint"].items()}
            await update_game_payload(self.db, game_id, payload)

        try:
            await message.edit(view=card(build_notice_layout(PICKING_UP_TEXT)), **V2_EDIT)
        except discord.HTTPException:
            pass
        if start_round > total_rounds:
            spawn(self._show_recap(game_id, host_id, host_name, channel, guild, settings), label="price recap", logger=log)
        else:
            spawn(self._run_round(
                game_id=game_id, host_id=host_id, host_name=host_name,
                channel=channel, guild=guild, round_num=start_round,
                settings=settings, msg=message,
            ), label="price round", logger=log)
        log.info(
            "Recovering price game %s (resuming at round %d) in #%s",
            game_id, start_round, getattr(channel, "name", channel.id),
        )
        return True

    async def _advance_round(
        self, game_id, host_id, host_name, channel, guild, round_num, settings, msg,
        *, pre_round_delay: int = 0,
    ):
        """Finish round_num: checkpoint scores, then go to the next round / recap.

        Called at every round-completion site so the checkpoint (used by
        recover_game) is written in exactly one place, after scoring is final.
        """
        payload = await get_game_payload(self.db, game_id)
        payload["completed_rounds"] = round_num
        scores = payload.get("scores", {})
        payload["scores_checkpoint"] = {k: dict(v) for k, v in scores.items()}
        await update_game_payload(self.db, game_id, payload)

        total_rounds = payload.get("total_rounds", settings["rounds"])
        if round_num < total_rounds:
            if pre_round_delay:
                await asyncio.sleep(pre_round_delay)
            await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg)
        else:
            await self._show_recap(game_id, host_id, host_name, channel, guild, settings)

    # ── Slash command ────────────────────────────────────────────────

    @app_commands.command(name="price", description=play_description("price"))
    @app_commands.describe(
        source="Where scenarios come from (default: the question bank, or the host if it's empty)",
        start_in="Countdown before the start, in minutes",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Question bank", value="bank"),
            app_commands.Choice(name="Host writes", value="host"),
            app_commands.Choice(name="Players submit", value="players"),
        ],
    )
    async def price_cmd(
        self,
        interaction: discord.Interaction,
        source: str | None = None,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info(
            "%s used /games play price in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "price")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"source": source, "start_in": start_in},
        )
        await finish_launch_response(interaction, game_id)
        # Nothing host-only is on the cards, so hand the host their controls
        # now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a Name Your Price game: every host-only control
        on one panel. The host always gets it; anyone else needs the same
        mod standing the controls check (``is_host_or_mod``)."""
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
                HOST_START_ID: "start_game", HOST_CANCEL_ID: "cancel_lobby",
                HOST_WRITE_ID: "write_scenario", HOST_DRAW_ID: "draw_bank",
                HOST_SKIP_ID: "skip_round", HOST_ADD_ROUNDS_ID: "add_rounds",
                HOST_END_ID: "end_game_button",
            },
            view_type=_HostControls,
        )
        await send_panel(interaction, panel, label="price host panel")

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Opens the lobby;
        returns game_id, or None."""
        # Pacing knobs come from the per-server dashboard config; an explicit
        # *options* value (e.g. from a saved schedule) still wins.
        game_opts = await get_game_options(self.db, "price", guild_id)
        rounds = max(1, min(int(options.get("rounds", game_opts.get("rounds", 5))), 20))
        timer = max(10, min(int(options.get("timer", game_opts.get("timer", 30))), 120))
        vote_timer = max(10, min(int(options.get("vote_timer", game_opts.get("vote_timer", 20))), 60))
        tags_raw = options.get("tags") or []
        if isinstance(tags_raw, str):
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        else:
            tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        guild = getattr(channel, "guild", None)
        # No choice made → the bank when it holds anything, else the host.
        bank_has_rows = await has_matching_questions(
            self.db, "price", tags, allow_nsfw=channel_allows_nsfw(channel),
        )
        source = resolve_source(options.get("source"), bank_has_rows)
        start_epoch = resolve_start_epoch(options)

        settings = {
            "rounds": rounds,
            "timer": timer,
            "vote_timer": vote_timer,
            "source": source,
            "tags": tags,
        }
        payload: dict = {
            "settings": settings,
            "total_rounds": rounds,
            "rounds": {},
            "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
            "players": [],
        }
        if start_epoch:
            payload["start_epoch"] = start_epoch

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "price",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )
        log.info("Game %s (price) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        view = PriceLobbyView(
            game_id, host_id, self.db, self.bot, self,
            guild=guild, host_name=host_name, payload=payload,
            art=await read_lobby_art(self.bot, channel, TITLE),
        )
        self.bot.active_views[game_id] = view
        try:
            msg = await retry_transient(lambda: channel.send(view=view), label="price launch")
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("price launch lacked send perms in channel %s", channel.id)
            return None
        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    # ── Lobby → play ─────────────────────────────────────────────────

    async def _begin_game(self, game_id: str, payload: dict) -> None:
        """Retire the ``joining`` state. Shared by Start and the countdown
        auto-start so both paths start a game the same way; the caller has
        already checked the roster against the floor."""
        await update_game_state(self.db, game_id, "playing")

    async def _start_rounds(self, game_id: str, host_id: int, channel, msg, payload: dict) -> None:
        """Kick off round 1 as a background task (the caller is a button or
        the sweep, neither of which can be held for the length of a game)."""
        guild = getattr(channel, "guild", None)
        settings = payload["settings"]
        host_name = await host_display(self.bot, guild, host_id)
        try:
            await msg.edit(view=card(build_start_layout(
                host_name, 1, payload.get("total_rounds", settings["rounds"]),
            )), **V2_EDIT)
        except discord.HTTPException:
            pass
        task = asyncio.create_task(self._run_round(
            game_id=game_id, host_id=host_id, host_name=host_name,
            channel=channel, guild=guild, round_num=1,
            settings=settings, msg=msg,
        ))
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)

    async def auto_start(self, row, payload: dict, channel) -> bool:
        """Start a countdown lobby without a button press.

        Registered in ``bot.lobby_auto_starters``; the start-ping sweep calls
        it when ``start_epoch`` arrives with at least ``MIN_PLAYERS`` joined.
        Returns False when the lobby can't start as it stands, in which case
        the sweep nudges the host instead.
        """
        game_id = row["game_id"]
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, PriceLobbyView):
            log.debug("auto-start: price lobby %s has no live view", game_id)
            return False
        # Fresh roster: a join may have landed since the sweep read the row.
        payload = await get_game_payload(self.db, game_id)
        if len(lobby_players(payload)) < MIN_PLAYERS:
            return False

        message = view.message
        if message is None and row["message_id"]:
            try:
                message = await channel.fetch_message(int(row["message_id"]))
            except discord.HTTPException:
                message = None
        if message is None:
            return False
        view.message = message
        await view.close(payload)

        await self._begin_game(game_id, payload)
        await self._start_rounds(game_id, int(row["host_id"]), channel, message, payload)
        log.info("Game %s (price) auto-started at its countdown with %d players", game_id, len(lobby_players(payload)))
        return True

    # ── Round loop ───────────────────────────────────────────────────

    async def _get_scenario(
        self, settings: dict, game_id: str, host_id: int, channel, msg,
        round_num: int, total_rounds: int,
    ) -> str | None:
        """Fetch a scenario based on the source setting."""
        source = settings["source"]
        tags = settings.get("tags") or None

        if source in (SOURCE_HOST, SOURCE_PLAYERS):
            written = await self._prompt_scenario(
                game_id, host_id, channel, msg, round_num, total_rounds,
                open_to_all=source == SOURCE_PLAYERS,
            )
            if written:
                return written
            log.info("No %s scenario written for round %d, falling back to question bank", source, round_num)

        # "ai" and "both" were retired with the Prompts & AI studios; a game
        # persisted under either value falls through to the bank rather than
        # erroring out mid-round.
        return await get_price_scenario(self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel))

    async def _prompt_scenario(
        self, game_id: str, host_id: int, channel, msg, round_num: int,
        total_rounds: int, *, open_to_all: bool,
    ) -> str | None:
        """Put the scenario wait on the board and wait for text.

        Returns None when nobody wrote one in time, or the host chose the
        bank. No public ping: the board is where the room is looking. For
        ``players`` the Write Scenario button is on the card; for the host
        it is on the host panel, so the wait's view is registered as the
        game's live one for its duration — that is how the panel reaches it,
        and how a force-close wakes it.
        """
        modal = HostScenarioModal()
        view = ScenarioPromptView(modal, host_id, round_num, total_rounds, open_to_all=open_to_all)
        try:
            await msg.edit(view=view, **V2_EDIT)
        except discord.HTTPException:
            try:
                await channel.send(view=view)
            except discord.HTTPException:
                return None

        self.bot.active_views[game_id] = view
        result = await modal.wait_for_result(timeout=SCENARIO_WAIT_SECONDS)
        view.stop()
        return result or None

    async def _run_round(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
        guild,
        round_num: int,
        settings: dict,
        msg: discord.Message,
    ):
        """Execute one full round: scenario → submit → reveal → vote → results."""
        # Check if game still exists
        if await is_game_expired(self.db, game_id):
            return

        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])

        # ── Get scenario ──
        scenario = await self._get_scenario(
            settings, game_id, host_id, channel, msg, round_num, total_rounds,
        )
        if not scenario:
            # A force-close during the wait wakes it with no scenario; the
            # row is gone, so there is no next round to advance to.
            if await is_game_expired(self.db, game_id):
                return
            try:
                await channel.send("❌ Couldn't generate a scenario. Skipping round.")
            except discord.HTTPException:
                pass
            # Advance to next round or end
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, msg, pre_round_delay=2)
            return

        if await is_game_expired(self.db, game_id):
            return
        payload = await get_game_payload(self.db, game_id)

        # ── Submission phase ──
        game_view = PriceGameView(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            scenario=scenario,
            round_num=round_num,
            total_rounds=total_rounds,
            timer_secs=settings["timer"],
            db=self.db,
            bot=self.bot,
            cog=self,
            # The lobby roster: the round closes once everyone on it has answered.
            expected_ids=set(lobby_players(payload)),
            settings=settings,
            guild=guild,
        )
        self.bot.active_views[game_id] = game_view

        # Post the submission board — sticky (reposts to the bottom of the
        # channel as chat buries it) for the length of this round's
        # submission window when the guild's dial is on, an in-place edit
        # of ``msg`` otherwise.
        msg = await self.live_boards.open(
            game_id, game_view, channel, guild, msg,
            suffix=f" round {game_view.round_num}",
        )
        game_view._msg = msg

        # Start timer
        submission_done = asyncio.Event()

        async def on_submission_timer():
            submission_done.set()

        timer = GameTimer(
            duration=settings["timer"],
            message=msg,
            callback=on_submission_timer,
            timer_item_id=SUBMIT_TIMER_ITEM_ID,
        )
        game_view._timer = timer
        await timer.start()
        await submission_done.wait()

        if game_view._closed:
            # Forced end (/games end, or the host's own End Game button —
            # see end_early) while submissions were open. Whichever fired,
            # the board's retired already or is retiring right now;
            # idempotent either way.
            self.live_boards.retire(game_view, channel)
            return

        # The board's closed frame — its last render before it retires
        # below, so it goes through the panel too when sticky (same
        # reasoning as refresh_embed: an edit against a possibly stale
        # ``msg`` would silently 404 if a restick moved the board).
        game_view._closed = True
        game_view._rebuild()
        if game_view._panel is not None and guild is not None:
            await game_view._panel.refresh(guild.id)
        else:
            try:
                await msg.edit(view=game_view, **V2_EDIT)
            except discord.HTTPException:
                pass

        prices = dict(game_view.prices)

        # Save round data to payload
        round_data = await self._record_round(game_id, round_num, scenario, prices)
        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])

        # The submission board's life ends here — everything from this
        # point on (reveal, vote, results, next round) is a fresh edit/send
        # against wherever the board actually ended up, not necessarily
        # ``msg``: the sticky panel may have reposted it since the board opened
        # handed ``msg`` back at the top of this round.
        current_msg = self.live_boards.retire(game_view, channel) or msg

        # ── Handle 0 or 1 submissions ──
        if len(prices) == 0:
            try:
                await channel.send("Nobody submitted a price this round. Moving on…")
            except discord.HTTPException:
                pass
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg, pre_round_delay=3)
            return

        # ── Reveal phase ──
        ladder = build_ladder(prices)
        ladder_names = await build_names(self.bot, guild, [uid for uid, _ in ladder])
        named_ladder = [(plain_name(ladder_names, uid), amt) for uid, amt in ladder]
        try:
            await current_msg.edit(view=card(build_reveal_layout(
                scenario, round_num, total_rounds, named_ladder,
            )), **V2_EDIT)
        except discord.HTTPException:
            pass

        if not vote_possible(len(prices)):
            # One price has nothing to compare; two would be a forced 1-1
            # cross-vote and a guaranteed double tie.
            notice = (
                "Only one price submitted — skipping the vote."
                if len(prices) == 1
                else "Only two prices in — no vote this round. Moving on…"
            )
            try:
                await channel.send(notice)
            except discord.HTTPException:
                pass
            await asyncio.sleep(3)
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg)
            return

        # 5s pause for reactions
        await asyncio.sleep(5)

        if await is_game_expired(self.db, game_id):
            return

        # ── Voting phase ──
        submitters = [uid for uid, _ in ladder]
        vote_view = PriceVoteView(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            scenario=scenario,
            submitters=submitters,
            prices=prices,
            round_num=round_num,
            total_rounds=total_rounds,
            timer_secs=settings["vote_timer"],
            guild=guild,
            db=self.db,
            bot=self.bot,
            cog=self,
            names=await build_names(self.bot, guild, submitters),
        )
        self.bot.active_views[game_id] = vote_view

        try:
            vote_msg = await channel.send(view=vote_view)
            vote_view._msg = vote_msg
        except Exception:  # noqa: BLE001 — a vote card that fails to post for any reason must still advance the round, or the game stalls
            # Can't send vote view — skip voting
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg)
            return

        vote_done = asyncio.Event()

        async def on_vote_timer():
            vote_done.set()

        vote_timer = GameTimer(
            duration=settings["vote_timer"],
            message=vote_msg,
            callback=on_vote_timer,
            timer_item_id=VOTE_TIMER_ITEM_ID,
        )
        vote_view._timer = vote_timer
        await vote_timer.start()
        await vote_done.wait()

        # Bail if the game was force-ended (e.g. /games end) while voting.
        if game_id not in self.bot.active_views:
            return

        # Close the vote: the results card below replaces the dropdowns,
        # but stop listening first so a late pick can't land after the tally.
        vote_view._closed = True
        vote_view.stop()

        # ── Tally votes ──
        r_winners, r_votes = tally_winners(vote_view.reasonable_votes)
        u_winners, u_votes = tally_winners(vote_view.unhinged_votes)

        # Save votes to payload
        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])
        rd = payload.setdefault("rounds", {}).setdefault(str(round_num), round_data)
        rd["votes"] = {
            "reasonable": {str(k): str(v) for k, v in vote_view.reasonable_votes.items()},
            "unhinged": {str(k): str(v) for k, v in vote_view.unhinged_votes.items()},
        }

        # Update running scores — all tied winners get a point
        scores = payload.setdefault("scores", {"reasonable_wins": {}, "unhinged_wins": {}})
        for uid in r_winners:
            key = str(uid)
            scores["reasonable_wins"][key] = scores["reasonable_wins"].get(key, 0) + 1
        for uid in u_winners:
            key = str(uid)
            scores["unhinged_wins"][key] = scores["unhinged_wins"].get(key, 0) + 1

        await update_game_payload(self.db, game_id, payload)

        # ── Show round results ── (a tie lists every winner)
        r_price = prices.get(r_winners[0], 0) if r_winners else 0
        u_price = prices.get(u_winners[0], 0) if u_winners else 0
        try:
            await vote_msg.edit(view=card(build_round_results_layout(
                round_num, total_rounds,
                [plain_name(vote_view.names, uid) for uid in r_winners], r_price, r_votes,
                [plain_name(vote_view.names, uid) for uid in u_winners], u_price, u_votes,
            )), **V2_EDIT)
        except discord.HTTPException:
            pass

        # ── Next round or recap ──
        await asyncio.sleep(5)
        await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg)

    async def _record_round(self, game_id: str, round_num: int, scenario: str, prices: dict[int, int]) -> dict:
        """Write a round's prices to the payload (locked): this is what the
        recap and every outside end path pay from."""
        round_data = {
            "scenario": scenario,
            "prices": {str(uid): amt for uid, amt in prices.items()},
            "votes": {"reasonable": {}, "unhinged": {}},
        }

        def _write(payload: dict) -> None:
            payload.setdefault("rounds", {})[str(round_num)] = round_data

        await modify_payload(self.db, game_id, _write)
        return round_data

    # ── Endings ──────────────────────────────────────────────────────

    async def end_early(self, game_view: PriceGameView, channel, guild) -> None:
        """The host's End Game during a round: keep the prices already in,
        stop the round, and post the recap — which pays everyone who
        submitted in any round through the same path a finished game uses."""
        if game_view._closed:
            return
        game_view._closed = True
        game_view._rebuild()
        if game_view._panel is not None and guild is not None:
            await game_view._panel.refresh(guild.id)
        elif game_view._msg is not None:
            try:
                await game_view._msg.edit(view=game_view, **V2_EDIT)
            except discord.HTTPException:
                pass
        # Retire the board here rather than waiting for the round loop's own
        # wake-up to notice _closed (it's about to, via skip_timer() below,
        # but that's a second, concurrent coroutine) — same promptness rule
        # as Rushmore's own retire/forget() does: a chat message
        # landing in that window could otherwise still restick a submission
        # board for a round whose recap is already posting. Idempotent
        # against the round loop's own (redundant) call to the same thing.
        self.live_boards.retire(game_view, channel)
        if game_view.prices:
            await self._record_round(game_view.game_id, game_view.round_num, game_view.scenario, dict(game_view.prices))
        # Wake the round loop; it sees _closed and returns without advancing.
        game_view.skip_timer()
        await self._show_recap(
            game_view.game_id, game_view.host_id, game_view.host_name,
            channel, guild, game_view.settings,
        )

    async def _show_recap(self, game_id: str, host_id: int, host_name: str, channel, guild, settings: dict):
        payload = await get_game_payload(self.db, game_id)
        if not payload:
            return  # already archived by another end path
        rounds_data = payload.get("rounds", {})
        scores = payload.get("scores", {"reasonable_wins": {}, "unhinged_wins": {}})

        all_players = collect_all_players(rounds_data)
        rounds_played = len(rounds_data)

        # Build awards — logic returns (label, [uids], detail); resolve uids here.
        raw_awards = compute_recap_awards(rounds_data, scores)
        award_names = await build_names(
            self.bot, guild, [uid for _label, uids, _detail in raw_awards.values() for uid in uids],
        )
        awards: dict[str, tuple[str, list[str], str]] = {
            slug: (label, [plain_name(award_names, uid) for uid in uids], detail)
            for slug, (label, uids, detail) in raw_awards.items()
        }

        # Highlight — widest spread round
        highlight: str | None = None
        hi = compute_highlight(rounds_data)
        if hi is not None:
            rnum, lo, hi_amt = hi
            highlight = (
                f"Round {rnum} had the widest spread — "
                f"{format_price(lo)} to {format_price(hi_amt)}"
            )

        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "price")
        recap_view = PriceRecapView(
            game_id, host_id, self, settings,
            build_recap_layout(rounds_played, len(all_players), awards, highlight, payout_line=payout_line),
        )

        try:
            await channel.send(view=recap_view)
        except discord.HTTPException:
            pass

        # End the game
        await end_game(
            self.db,
            game_id,
            player_count=len(all_players),
            round_count=rounds_played,
            payload=payload,
            bot=self.bot, player_ids=list(all_players),
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]


async def setup(bot: "Bot"):
    cog = PriceCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("price")
    play.add_command(cog.price_cmd, override=True)
    bot.game_launchers["price"] = cog.launch
    bot.game_recoverers["price"] = cog.recover_game
    bot.lobby_auto_starters["price"] = cog.auto_start
