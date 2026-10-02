"""
Two Truths and a Lie — game cog.

Everyone submits three statements in a lobby; once three are in the host
starts the guessing, and the room votes on each subject's lie in turn. Pure
logic lives in ``games/ttl/logic.py`` and the Components V2 card builders
in ``games/ttl/layouts.py``; this module keeps only the Discord glue (slash
command, modal, views, the guessing loop). Nothing host-only is on a public
card: Start Guessing and ⏭️ Next are on the ephemeral host panel
(``/games host``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.branding_service import PanelArt
from bot_modules.core.layout import V2_EDIT, bind_present, card
from bot_modules.core.utils import allow_pings, is_host_or_mod
from bot_modules.shared.name_resolver import build_name_fn
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    create_game,
    update_game_state,
    update_game_message,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
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
from bot_modules.games.utils.names import build_names, host_display, cached_names, plain_name
from bot_modules.games.utils.recovery import start_redrive
from bot_modules.core.sticky import StickyPanel
from bot_modules.games.board_sticky_service import (
    LiveBoards,
    redraw_board,
    redraw_embed_era_lobby,
)
from bot_modules.games.ttl.layouts import (
    HELP_ID,
    HOST_NEXT_ID,
    HOST_START_ID,
    JOIN_ID,
    JOIN_LABEL,
    NUM_GLYPHS,
    START_LABEL,
    SUBMIT_ID,
    SUBMIT_LABEL,
    TITLE,
    VOTE_IDS,
    build_guess_layout,
    build_host_panel_layout,
    build_lobby_layout,
    build_recap_layout,
    build_reveal_layout,
    winner_ids,
)
from bot_modules.games.ttl.logic import (
    MIN_PLAYERS,
    add_submission,
    compute_recap_winners,
    is_lobby_row,
    mark_played,
    parse_lie_index,
    played_ids_from_payload,
    roster_ids,
    shuffle_statements,
    submission_locked,
    tally_votes,
    update_scores,
)

log = logging.getLogger(__name__)

ALREADY_GUESSING_REPLY = "Guessing has already started."
NOT_GUESSING_REPLY = f"Guessing hasn't started yet — press **{START_LABEL}** once three have submitted."


class SubmitStatementsModal(discord.ui.Modal):
    def __init__(
        self,
        game_id: str,
        db,
        prompt: str | None = None,
        lobby: "TTLSubmitView | None" = None,
        existing: dict | None = None,
    ):
        super().__init__(title="Two Truths and a Lie")
        self.game_id = game_id
        self.db = db
        # Only ever the lobby view (threaded through by its Submit button).
        # Falling back to the interaction's message here used to clobber
        # statement 1's field on the active guess embed when someone joined
        # mid-game; the guess card's Join / Edit passes no lobby at all.
        self._lobby = lobby

        # Components-v2 layout: the full prompt rides as static text, so it
        # can't be missed the way the 45-char-truncated title used to be.
        if prompt:
            self.add_item(discord.ui.TextDisplay(
                f"**Prompt:** {discord.utils.escape_markdown(prompt)}\n"
                "Your statements should answer the prompt — two true, one lie."
            ))
        ex_statements = (existing or {}).get("statements") or ["", "", ""]
        ex_lie = (existing or {}).get("lie")
        self._inputs: list[discord.ui.TextInput] = []
        for i in range(3):
            ti = discord.ui.TextInput(
                label=None, max_length=200, default=ex_statements[i] or None,
            )
            self._inputs.append(ti)
            self.add_item(discord.ui.Label(text=f"Statement {i + 1}", component=ti))
        self._lie_input = discord.ui.TextInput(
            label=None,
            max_length=1,
            placeholder="1, 2, or 3",
            default=str(ex_lie + 1) if ex_lie is not None else None,
        )
        self.add_item(discord.ui.Label(
            text="Which statement is the lie? (1, 2, or 3)", component=self._lie_input,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Statements", channel_name(interaction.channel))
        lie_idx = parse_lie_index(self._lie_input.value)
        if lie_idx is None:
            await interaction.response.send_message(
                "❌ Please enter **1**, **2**, or **3** to indicate which statement is the lie.",
                ephemeral=True,
            )
            return

        uid = interaction.user.id
        display_name = interaction.user.display_name
        statements = [ti.value for ti in self._inputs]

        locked = False

        def _add_submission(payload):
            nonlocal locked
            # Once a player's round has been revealed their statements are
            # history — replacing them would rewrite a finished round.
            if submission_locked(payload, uid):
                locked = True
                return
            add_submission(payload, uid, display_name, statements, lie_idx)

        payload = await modify_payload(self.db, self.game_id, _add_submission)
        if locked:
            await interaction.response.send_message(
                "❌ Your round has already been played — statements can't change now.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("✅ Your statements have been submitted!", ephemeral=True)

        # The roster is rebuilt from the payload — the embed era read the
        # Players field back off the lobby message and patched it in place.
        if self._lobby is not None:
            await self._lobby.show_players(list(payload.get("submitter_names", {}).values()))


class _HostControls:
    """The two host-panel hand-offs both Two Truths views answer.

    The lobby turns into the guessing on its own once started, so the panel
    carries both controls and hands each press to whichever view is live
    (``host_panel.GameHostPanel``). A phase with no use for a control
    answers with the line saying so; the phase that has one overrides.
    """

    host_id: int

    async def start_guessing(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(ALREADY_GUESSING_REPLY, ephemeral=True)

    async def next_round(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NOT_GUESSING_REPLY, ephemeral=True)


class TTLSubmitView(_HostControls, discord.ui.LayoutView):
    """The submission lobby. **Paradigm 8 — live board**: rebuilt and
    re-edited in place as statements arrive. ``ttl_submit`` / ``ttl_htp``
    are frozen custom ids — ``recover_game`` re-registers this view against
    the lobby message after a restart. Start Guessing is not here: the host
    panel calls :meth:`start_guessing`.
    """

    def __init__(
        self, game_id: str, host_id: int, db, bot, cog, prompt: str | None = None,
        *,
        players: list[str] | None = None,
        art: PanelArt = NO_ART,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.prompt = prompt
        self.players: list[str] = list(players or [])
        self.art = art
        self.message: discord.Message | None = None
        self._closed = False
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, re-binding its
        buttons — a rebuild adds brand-new ``Button`` instances. The closed
        frame carries only Help."""
        self.clear_items()
        for item in build_lobby_layout(
            self.prompt,
            self.players,
            closed=self._closed,
            header_url=self.art.header,
            footer_url=self.art.footer,
            title_in_header=self.art.title_in_header,
        ):
            self.add_item(item)
        bind_present(self, {SUBMIT_ID: self.submit, HELP_ID: self.how_to_play})

    async def _redraw(self) -> None:
        self._rebuild()
        if self.message is None:
            return
        try:
            await self.message.edit(view=self, **V2_EDIT)
        except discord.HTTPException:
            pass

    async def show_players(self, names: list[str]) -> None:
        """A submission landed: the lobby's roster, from the payload."""
        if self._closed:
            return
        self.players = list(names)
        await self._redraw()

    async def submit(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, SUBMIT_LABEL, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message(ALREADY_GUESSING_REPLY, ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        existing = payload.get("submissions", {}).get(str(interaction.user.id))
        modal = SubmitStatementsModal(
            self.game_id, self.db, prompt=self.prompt, lobby=self, existing=existing,
        )
        await interaction.response.send_modal(modal)

    async def start_guessing(self, interaction: discord.Interaction) -> None:
        """Start Guessing — the host panel's button lands here."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, START_LABEL, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start guessing.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message(ALREADY_GUESSING_REPLY, ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        submissions = payload.get("submissions", {})
        # Three, not two: at two each round is decided by a single vote
        # (the subject can't vote on their own statements; vote-games-57).
        if len(submissions) < MIN_PLAYERS:
            await interaction.response.send_message(
                f"❌ Need at least {MIN_PLAYERS} players to start guessing!", ephemeral=True,
            )
            return

        await interaction.response.defer()
        # The lobby's closed frame: no Submit button. Edited on the lobby's
        # own message — the press arrived on the ephemeral panel.
        self._closed = True
        self.stop()
        await self._redraw()
        # The row must stop reading as a lobby: boot recovery re-registers the
        # submit view on a 'joining' row and only re-drives guessing on this.
        await update_game_state(self.db, self.game_id, "guessing")

        # The lobby's channel, not the press's: a panel press arrives on the
        # ephemeral panel, whose channel is the same room but not the board.
        channel = getattr(self.message, "channel", None) or interaction.channel
        assert channel is not None and not isinstance(
            channel, (discord.ForumChannel, discord.CategoryChannel)
        )
        # Ping all submitters
        if interaction.guild:
            mentions = [
                member.mention
                for uid in submissions
                if (member := interaction.guild.get_member(int(uid)))
            ]
            if mentions:
                await channel.send(
                    f"🎮 **Two Truths and a Lie is starting!** {' '.join(mentions)} — get ready!",
                    delete_after=15,
                    allowed_mentions=allow_pings(users=list(submissions)),
                )

        await self.cog._run_guessing(
            interaction=interaction,
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=interaction.user.display_name,
            channel=channel,
        )

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["ttl"], ephemeral=True)


class TTLGuessView(_HostControls, discord.ui.LayoutView):
    """One subject's round — a ``LiveBoardLayoutView`` (``game_id``,
    ``guild``, ``_panel``, ``_board_retired``, ``_rebuild``), so
    ``LiveBoards`` opens, redraws and retires it.

    The card carries what any member may press: the three votes and Join /
    Edit. ⏭️ Next is on the ephemeral host panel, which calls
    :meth:`next_round` here.
    """

    def __init__(
        self,
        game_id: str,
        host_id: int,
        subject_id: int,
        statements: list[str],
        lie_index: int,
        db,
        bot,
        host_name: str,
        advance_callback,
        prompt: str | None = None,
        subject_name: str = "",
        guild: "discord.Guild | None" = None,
        advance_at: int | None = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.subject_id = subject_id
        # Resolved once, when the round's view is built, so a render needs
        # nothing but the view — which is what the sticky board's ``build``
        # hook can supply (it runs from core, with no interaction in hand).
        self.subject_name = subject_name or plain_name(cached_names(guild), subject_id)
        self.guild = guild
        self.statements = statements
        self.lie_index = lie_index
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.prompt = prompt
        # The live countdown of a timed round (``vote_timer``), or None.
        self.advance_at = advance_at
        self.votes: dict[int, int] = {}
        self._updater = LiveBarUpdater()
        self._closed = False
        self._advanced_event: asyncio.Event | None = None
        self.message: discord.Message | None = None
        # Set when the guild turns on the "keep the board at the bottom"
        # dial (Games Global Config → Live Game Boards): the StickyPanel
        # that owns re-posting this round's board, and the flag its ``build``
        # checks — never ``_closed``, which is set one legitimate render
        # earlier (the closed frame that still has to go out).
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, then bind whatever
        controls that state shows — a rebuild adds brand-new buttons with
        nothing wired to them; the closed frame shows none."""
        self.clear_items()
        for item in build_guess_layout(
            self.subject_name,
            self.statements,
            self.votes,
            closed=self._closed,
            prompt=self.prompt,
            advance_at=self.advance_at,
        ):
            self.add_item(item)
        bind_present(self, {
            VOTE_IDS[0]: self.vote_1,
            VOTE_IDS[1]: self.vote_2,
            VOTE_IDS[2]: self.vote_3,
            JOIN_ID: self.join_btn,
            HELP_ID: self.how_to_play,
        })

    async def vote_1(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, 0)

    async def vote_2(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, 1)

    async def vote_3(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, 2)

    async def _vote(self, interaction: discord.Interaction, idx: int):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        if interaction.user.id == self.subject_id:
            await interaction.response.send_message("❌ You can't vote on your own statements!", ephemeral=True)
            return
        prev = self.votes.get(interaction.user.id)
        self.votes[interaction.user.id] = idx
        changed = prev is not None and prev != idx
        msg = f"✅ Voted **{NUM_GLYPHS[idx]}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        message = self.message or interaction.message
        # Rate-limited as before, but routed through wherever the board is
        # now: with the dial on, a restick may have re-posted it between this
        # click and the gate opening.
        await self._updater.schedule_render(lambda: redraw_board(self, message))

    async def next_round(self, interaction: discord.Interaction) -> None:
        """⏭️ Next, from the host panel. Advances the *board's* message, not
        the one the press came from — a panel press arrives on the
        ephemeral panel."""
        log.info("%s pressed 'Next' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can advance.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This round is already over.", ephemeral=True)
            return
        await interaction.response.defer()
        assert self.advance_callback is not None
        await self.advance_callback(self.message or interaction.message)

    async def join_btn(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, JOIN_LABEL, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        if submission_locked(payload, interaction.user.id):
            await interaction.response.send_message(
                "Your round has already been played — statements can't change now. You can still vote!",
                ephemeral=True,
            )
            return
        existing = payload.get("submissions", {}).get(str(interaction.user.id))
        modal = SubmitStatementsModal(self.game_id, self.db, prompt=self.prompt, existing=existing)
        await interaction.response.send_modal(modal)

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["ttl"], ephemeral=True)


class TTLCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # Every live round board this cog owns (Games Global Config → Live
        # Game Boards). Keyed by game_id, so the board a round opens is the
        # board that round retires — one guild can run several Two Truths
        # games in different channels at once. The lobby is not on the dial;
        # only the subject's board being guessed on is.
        self.live_boards = LiveBoards(bot, self.db, "ttl board")

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

    @app_commands.command(name="twotruths", description=play_description("ttl"))
    @app_commands.describe(prompt="Optional topic prompt for players' statements")
    async def twotruths(self, interaction: discord.Interaction, prompt: str | None = None):
        log.info("%s used /games play twotruths in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "ttl")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"prompt": prompt},
        )
        await finish_launch_response(interaction, game_id)
        # Nothing host-only is on the cards, so hand the host their controls
        # now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a Two Truths game: Start Guessing and ⏭️ Next
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
            {HOST_START_ID: "start_guessing", HOST_NEXT_ID: "next_round"},
            view_type=_HostControls,
        )
        await send_panel(interaction, panel, label="ttl host panel")

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        prompt = options.get("prompt")
        # The vote timer is a per-schedule launch option (Games → Scheduling);
        # 0 means "no timer". There is no server-wide default: the fallback
        # that used to read one out of games_game_config had no dashboard
        # writer anywhere, so it could never be anything but 0.
        try:
            vote_timer = int(options.get("vote_timer", 0))
        except (TypeError, ValueError):
            vote_timer = 0
        vote_timer = max(0, min(vote_timer, 300))
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ttl",
            state="joining",
            payload={
                "submissions": {}, "submission_count": 0, "submitter_names": {},
                "scores": {}, "prompt": prompt, "vote_timer": vote_timer,
            },
        )

        log.info("Game %s (ttl) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = TTLSubmitView(
            game_id, host_id, self.db, self.bot, self, prompt=prompt,
            art=await read_lobby_art(self.bot, channel, TITLE),
        )
        self.bot.active_views[game_id] = view

        try:
            msg = await retry_transient(lambda: channel.send(view=view), label="ttl launch")
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ttl launch lacked send perms in channel %s", channel.id)
            return None
        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_guessing(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
        resume: bool = False,
    ):
        guild = channel.guild if hasattr(channel, "guild") else None
        # On resume after a restart, seed from persisted scores so already-played
        # subjects are skipped; the subject whose round was interrupted is still
        # "unplayed" and gets a fresh round.
        if resume:
            payload = await get_game_payload(self.db, game_id)
            scores: dict[str, dict] = dict(payload.get("scores", {}))
            played_ids: set[str] = played_ids_from_payload(payload)
        else:
            scores = {}
            played_ids = set()
        round_num = len(played_ids)

        while True:
            # Re-read submissions each iteration so late joiners are picked up
            payload = await get_game_payload(self.db, game_id)
            submissions = payload.get("submissions", {})
            unplayed = [uid for uid in submissions if uid not in played_ids]
            if not unplayed:
                break

            subject_id_str = unplayed[0]
            played_ids.add(subject_id_str)
            round_num += 1

            subject_id = int(subject_id_str)
            data = submissions[subject_id_str]
            # Shuffle statements so the lie isn't always in the submitted position
            statements, lie_index = shuffle_statements(
                data["statements"], data["lie"]
            )

            subject_member = guild.get_member(subject_id) if guild else None
            subject_name = subject_member.display_name if subject_member else plain_name(
                await build_names(self.bot, guild, [subject_id]), subject_id,
            )

            if subject_member:
                await channel.send(
                    f"{subject_member.mention} It's your turn!",
                    delete_after=15,
                    allowed_mentions=allow_pings(users=[subject_member]),
                )

            advanced = asyncio.Event()
            # vote_timer == 0 keeps the classic host-presses-Next pacing;
            # otherwise the round closes itself when time runs out (the host
            # can still advance early) and the card shows the countdown.
            vote_timer = int(payload.get("vote_timer") or 0)

            # Built with a placeholder callback and wired up right below:
            # `advance` binds *this* round's view and its own Event as
            # default arguments instead of closing over the loop's `view`/
            # `advanced` names (B023), so a stale click on an already-
            # retired round can never run against — or prematurely close —
            # whatever round the loop has since moved on to. Safe to leave
            # the callback unset for this instant: the view isn't posted
            # (and so can't be clicked) until further down.
            view = TTLGuessView(
                game_id=game_id,
                host_id=host_id,
                subject_id=subject_id,
                statements=statements,
                lie_index=lie_index,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=None,
                prompt=payload.get("prompt"),
                subject_name=subject_name,
                guild=guild,
                advance_at=int(time.time()) + vote_timer if vote_timer > 0 else None,
            )
            view._advanced_event = advanced

            async def advance(
                message: discord.Message,
                _view: TTLGuessView = view,
                _advanced: asyncio.Event = advanced,
                _sub_id: int = subject_id,
                _lie: int = lie_index,
                _round: int = round_num,
            ) -> None:
                if _view._closed:
                    return
                _view._closed = True

                correct, fooled = tally_votes(_view.votes, _lie)
                update_scores(scores, _sub_id, correct, fooled, len(_view.votes))

                def _flush_scores(p):
                    p["scores"] = dict(scores)
                    mark_played(p, _sub_id)
                await modify_payload(self.db, game_id, _flush_scores)

                # Retire *before* painting the closed frame, and paint on
                # what retire hands back: a restick could otherwise land on
                # top of the final frame, and a board re-posted mid-round
                # leaves this local ``message`` naming something deleted.
                target = self.live_boards.retire(_view, channel) or message
                _view._rebuild()
                try:
                    await target.edit(view=_view, **V2_EDIT)
                except discord.HTTPException:
                    pass

                reveal_names = await build_names(self.bot, guild, [*correct, *fooled])
                await channel.send(view=card(build_reveal_layout(
                    _view.subject_name, _view.statements, _lie,
                    [plain_name(reveal_names, uid) for uid in correct],
                    [plain_name(reveal_names, uid) for uid in fooled],
                )))

                _advanced.set()

            view.advance_callback = advance
            self.bot.active_views[game_id] = view

            # Sticky for the length of this subject's round when the
            # guild's dial is on, a plain send otherwise. A fresh board per
            # round, so the round that just closed stays where it is as that
            # subject's record — ``open`` records the new board's id either
            # way, which is what the jump link and /games end read.
            msg = await self.live_boards.open(
                game_id, view, channel, guild,
                suffix=f" round {round_num}",
            )
            view.message = msg

            if vote_timer > 0:
                try:
                    await asyncio.wait_for(advanced.wait(), timeout=vote_timer)
                except asyncio.TimeoutError:
                    await advance(msg)
            else:
                await advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(2)

        # If the game was already closed by the host, skip final results
        if game_id not in self.bot.active_views:
            return

        payload = await get_game_payload(self.db, game_id)
        payload["scores"] = scores
        # Every subject plus every voter — a member who guessed all game
        # and never submitted is paid too (vote-games-57). game_roster._ttl
        # rebuilds the same list for the sweep and /games end.
        player_ids = roster_ids(played_ids, scores)

        stats = compute_recap_winners(scores, played_ids)
        winners = winner_ids(stats)
        name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=getattr(guild, "id", 0),
            user_ids=winners,
        )
        # A winner still in the guild is pinged from the card itself (a V2
        # message has no content= line to carry it); one who has left is
        # named through name_fn and never pinged. The allow-list names
        # exactly the pinged ids — the client default suppresses the rest.
        pingable = [uid for uid in winners if guild is not None and guild.get_member(uid)]
        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "ttl")

        recap = card(build_recap_layout(
            stats, name_fn=name_fn, pingable=pingable, payout_line=payout_line,
        ))
        await channel.send(view=recap, allowed_mentions=allow_pings(users=pingable))
        log.info("Game %s ended — %d players", game_id, len(player_ids))
        await end_game(
            self.db, game_id,
            player_count=len(player_ids),
            round_count=len(played_ids),
            payload=payload,
            bot=self.bot, player_ids=player_ids,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Recover after a restart, by phase.

        A lobby (``state == 'joining'``) gets its submit view re-registered on
        the lobby message so members keep writing and the host still presses
        Start Guessing — on the host panel — with the roster rebuilt from the
        payload; a lobby still an embed on screen (posted before the
        conversion) is redrawn as a card at once. Recovery used to re-drive
        guessing for any row with a submission, bypassing that button and its
        3-submission floor, and left an empty lobby with dead buttons
        (vote-games-56).

        Once Start Guessing has run (``state == 'guessing'``) the loop is
        re-driven: completed rounds live in payload["scores"]; the in-progress
        round can't be reconstructed (its shuffled statements/votes aren't
        persisted), so we retire the stale round message and restart that
        subject's round. The re-driven loop reconstructs scores from the
        payload and continues.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        # A pre-'guessing' row that already holds a scored round is a game
        # mid-guessing, not a lobby (Hot Takes guards the same case).
        if is_lobby_row(row["state"], payload):
            view = TTLSubmitView(
                game_id, host_id, self.db, self.bot, self, prompt=payload.get("prompt"),
                players=list((payload.get("submitter_names") or {}).values()),
                art=await read_lobby_art(self.bot, channel, TITLE),
            )
            view.message = message
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            await redraw_embed_era_lobby(view, message)
            log.info("Recovered ttl game %s (lobby) in #%s", game_id, getattr(channel, "name", channel.id))
            return True
        if not payload.get("submissions"):
            return False
        guild = getattr(channel, "guild", None)
        host_name = await host_display(self.bot, guild, host_id)
        await start_redrive(
            self.bot, game_id, message,
            self._run_guessing(
                interaction=None, game_id=game_id, host_id=host_id,
                host_name=host_name, channel=channel, resume=True,
            ),
            channel=channel, log_label=f"ttl game {game_id} (re-driving guessing)",
        )
        return True


async def setup(bot: "Bot"):
    cog = TTLCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("twotruths")
    play.add_command(cog.twotruths, override=True)
    bot.game_launchers["ttl"] = cog.launch
    bot.game_recoverers["ttl"] = cog.recover_game
