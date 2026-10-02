import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.layout import V2_EDIT, bind_present, card
from bot_modules.core.sticky import StickyPanel
from bot_modules.shared.name_resolver import NameFn, build_name_fn, mention
from discord.ext import commands
from discord import app_commands
from bot_modules.games.command_groups import play
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    get_active_game_by_id,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    is_game_expired,
    resolve_name,
    channel_name,
)
from bot_modules.games.utils.host_panel import (
    GAME_ENDED_REPLY,
    GameHostPanel,
    hand_over,
    reply as host_reply,
    send_panel,
)
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.board_sticky_service import (
    LiveBoards,
    redraw_board,
    redraw_embed_era_board,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.question_source import (
    get_wyr_question,
    channel_allows_nsfw,
)
from bot_modules.games.utils.round_pacing import (
    CONTROL_DENIED,
    END_DENIED,
    REASON_EXPIRED,
    REASON_HOST_ENDED,
    REASON_ROUND_CAP,
    RoundPacing,
    advance_check,
    is_scheduled_launch,
    may_control,
    launch_pacing,
    round_cap_reached,
    seconds_left,
)
from bot_modules.games.wyr.layouts import (
    HELP_ID,
    HOST_END_ID,
    HOST_NEXT_ID,
    NEXT_ID,
    POSE_ID,
    POSE_LABEL,
    SHOW_VOTE_ID,
    SHOW_VOTE_LABEL,
    VOTE_A_ID,
    VOTE_A_LABEL,
    VOTE_B_ID,
    VOTE_B_LABEL,
    build_host_panel_layout,
    build_recap_layout,
    build_round_layout,
)
from bot_modules.games.wyr.logic import (
    parse_question_input,
    played_rounds,
    record_show_vote,
    toggle_vote,
)
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.shared.anon_audit_service import (
    EVENT_QUESTION_POSED,
    EVENT_VOTE,
)

log = logging.getLogger(__name__)

# Cap the player-submitted question queue to prevent flooding.
_MAX_QUEUED_QUESTIONS = 15

# After a restart, a timed round that already ran out still gets a moment
# for the room to see the board before it auto-advances.
_RECOVERY_GRACE_SECONDS = 5.0


class PoseWYRModal(discord.ui.Modal, title="Pose a Question"):
    option_a = discord.ui.TextInput(
        label="Option A",
        placeholder="e.g. fly",
        style=discord.TextStyle.short,
        max_length=200,
    )
    option_b = discord.ui.TextInput(
        label="Option B",
        placeholder="e.g. be invisible",
        style=discord.TextStyle.short,
        max_length=200,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Question", channel_name(interaction.channel))
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        a = self.option_a.value.strip()
        b = self.option_b.value.strip()
        if not a or not b:
            await interaction.response.send_message("Both options are required.", ephemeral=True)
            return
        if self._view.waiting:
            # The bank had nothing to serve, so this question *is* the round.
            await self._view.begin_round(a, b, self._message)
            await interaction.response.send_message("✅ Your question opened the round!", ephemeral=True)
            count = 0
        else:
            if len(self._view.queued_questions) >= _MAX_QUEUED_QUESTIONS:
                await interaction.response.send_message(
                    f"The question queue is full ({_MAX_QUEUED_QUESTIONS}). Let some play first!",
                    ephemeral=True,
                )
                return
            self._view.queued_questions.append((a, b))
            count = len(self._view.queued_questions)
            # The card's subtext carries the queue count; redrawn through the
            # board seam, since a restick may have moved the board.
            try:
                await redraw_board(self._view, self._message)
            except discord.HTTPException:
                pass
            await interaction.response.send_message("✅ Your question has been queued!", ephemeral=True)

        # Free-text a member wrote that the channel will see with no name on
        # it. Queued rather than posted, so there is no message to point at.
        if interaction.guild is not None:
            await audit_anonymous(
                self._view.bot, self._view.db, interaction.guild,
                game_type="wyr", user=interaction.user,
                event=EVENT_QUESTION_POSED,
                content=f"A: {a} / B: {b}", label="WYR Posed Question",
                game_id=self._view.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"queue_position": count},
            )


class WYRRoundView(discord.ui.LayoutView):
    """One round's card and its controls — a ``LiveBoardLayoutView``
    (``game_id``, ``guild``, ``_panel``, ``_board_retired``, ``_rebuild``),
    so ``LiveBoards`` opens, redraws and retires it.

    The card carries what any member may press. ⏭️ Next is on it only for a
    scheduled game, where a voter may press it; on a hosted game Next and
    🏁 End Game are on the ephemeral host panel, which calls
    :meth:`press_next` and :meth:`press_end` here.
    """

    def __init__(
        self,
        game_id: str,
        host_id: int,
        option_a: str,
        option_b: str,
        round_num: int,
        anonymous: bool,
        db,
        bot,
        host_name: str,
        advance_callback,
        guild: "discord.Guild | None" = None,
        *,
        pacing: RoundPacing | None = None,
        finish_callback=None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        # Only read by the sticky board wiring, which needs a guild to key a
        # StickyPanel by; every render here takes its names from _name_fn.
        self.guild = guild
        self.host_id = host_id
        self.option_a = option_a
        self.option_b = option_b
        self.round_num = round_num
        self.anonymous = anonymous
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.finish_callback = finish_callback
        self.votes_a: list[int] = []
        self.votes_b: list[int] = []
        # Voters who pressed Show My Vote — named beside their pick, by their
        # own choice (vote-games-61). Persisted with the votes.
        self.shown: list[int] = []
        # A name only renders for a shown voter; refresh_name_fn swaps this
        # for a real resolver (prefetched over ``shown``) before any render,
        # so the mention fallback never reaches a rendered card.
        self._name_fn: NameFn = mention
        self._updater = LiveBarUpdater()
        self._closed = False
        self.queued_questions: list[tuple[str, str]] = []
        self.pacing = pacing or RoundPacing()
        # Set when the guild turns on the "keep the board at the bottom"
        # dial (Games Global Config → Live Game Boards): the StickyPanel
        # that owns reposting this round's board, and the flag the cog sets
        # once the round is over — the panel's ``build`` checks that flag,
        # never ``_closed``, which is set one legitimate render *earlier*
        # (the closed final frame, which still has to go out). Both stay
        # None/False for the life of the round when the dial is off.
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        # force_end_active_game pokes this alias to wake a timed round.
        self._advanced_event = self.pacing.advanced
        self.message: discord.Message | None = None
        # No question to show yet (the bank had nothing to serve): only Pose
        # and Help are on the card until someone poses one (vote-games-50).
        self.waiting = not (option_a and option_b)
        self._rebuild()

    async def refresh_name_fn(self, guild) -> None:
        """Resolve the shown voters' names once, ahead of any render."""
        if not self.shown:
            return
        self._name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=getattr(guild, "id", 0) or 0,
            user_ids=list(self.shown),
        )

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, then bind whatever
        controls that state shows — a rebuild adds brand-new buttons with
        nothing wired to them."""
        self.clear_items()
        for item in build_round_layout(
            self.option_a,
            self.option_b,
            self.votes_a,
            self.votes_b,
            self.anonymous,
            self.round_num,
            closed=self._closed,
            name_fn=self._name_fn,
            waiting=self.waiting,
            advance_at=self.pacing.advance_at(),
            shown=self.shown,
            queued=len(self.queued_questions),
            scheduled=self.pacing.scheduled,
        ):
            self.add_item(item)
        bind_present(self, {
            VOTE_A_ID: self.vote_a,
            VOTE_B_ID: self.vote_b,
            SHOW_VOTE_ID: self.show_my_vote,
            POSE_ID: self.pose_question,
            NEXT_ID: self.press_next,
            HELP_ID: self.how_to_play,
        })

    async def persist_votes(self) -> None:
        """Write this round's tallies on every vote, not only on Next, so a
        restart mid-round rebuilds the bars it shows (vote-games-58)."""
        a, b, shown = list(self.votes_a), list(self.votes_b), list(self.shown)

        def _save(payload):
            rd = payload.setdefault("rounds", {}).setdefault(str(self.round_num), {})
            rd["a"] = a
            rd["b"] = b
            rd["shown"] = shown

        await modify_payload(self.db, self.game_id, _save)

    async def begin_round(self, option_a: str, option_b: str, message: discord.Message) -> None:
        """A posed question starts a round that was waiting for one."""
        self.option_a, self.option_b = option_a, option_b
        self.waiting = False
        opened = self.pacing.open()

        def _save(payload):
            rd = payload.setdefault("rounds", {}).setdefault(str(self.round_num), {})
            rd["q"] = f"{option_a} OR {option_b}"
            rd["opened_at"] = opened

        await modify_payload(self.db, self.game_id, _save)
        self.message = message
        self.pacing.start_timer(lambda: self.advance_callback(message))
        await redraw_board(self, message)

    async def _audit_vote(
        self, interaction: discord.Interaction, option: str, changed: bool,
        *, was_already_there: bool,
    ) -> None:
        """Record a vote only while the round is actually anonymous.

        With ``anonymous`` off there is no anonymity for the audit trail to
        account for.

        ``was_already_there`` skips re-presses of the side the member is
        already on. ``toggle_vote`` treats those as no-ops, so auditing them
        would let one member write unbounded rows by tapping the same button.
        """
        if not self.anonymous or interaction.guild is None or was_already_there:
            return
        await audit_anonymous(
            self.bot, self.db, interaction.guild,
            game_type="wyr", user=interaction.user,
            event=EVENT_VOTE,
            game_id=self.game_id,
            message_id=interaction.message.id if interaction.message else None,
            channel_id=interaction.channel.id if interaction.channel else None,
            extra={"option": option, "changed": changed, "round": self.round_num},
        )

    async def _vote(self, interaction: discord.Interaction, choice: str, label: str) -> None:
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed or self.waiting:
            await interaction.response.send_message("This round is over." if self._closed else "No question yet — pose one first!", ephemeral=True)
            return
        side = self.votes_a if choice == "a" else self.votes_b
        was_already_there = interaction.user.id in side
        changed = toggle_vote(self.votes_a, self.votes_b, interaction.user.id, choice)
        msg = f"✅ Voted **{label}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self.persist_votes()
        # Rate-limited, but routed through the board's current location
        # rather than the message this click came from: a restick may have
        # reposted the board between the click and the gate opening.
        await self._updater.schedule_render(
            lambda: redraw_board(self, interaction.message)
        )
        await self._audit_vote(
            interaction, choice, changed, was_already_there=was_already_there
        )

    async def vote_a(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "a", VOTE_A_LABEL)

    async def vote_b(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, "b", VOTE_B_LABEL)

    async def pose_question(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, POSE_LABEL, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        assert interaction.message  # component interactions always carry their message
        await interaction.response.send_modal(PoseWYRModal(self, interaction.message))

    async def press_next(self, interaction: discord.Interaction) -> None:
        """⏭️ Next — from the host panel, or from the card of a scheduled
        game. Advances the *board's* message, not the one the press came
        from: a panel press arrives on the ephemeral panel."""
        log.info("%s pressed 'Next' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id
        refusal = await advance_check(
            interaction, host_id=self.host_id, db=self.db, pacing=self.pacing,
            has_voted=uid in self.votes_a or uid in self.votes_b,
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This round is already over.", ephemeral=True)
            return
        if self.waiting:
            await interaction.response.send_message("No question yet — pose one first!", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(self.message or interaction.message)

    async def show_my_vote(self, interaction: discord.Interaction) -> None:
        """Put the presser's own name beside their pick — anyone's to press.

        The host/mod-only **Reveal Voters** it replaces named the whole room
        and refused everyone else (five refused presses in one prod game,
        vote-games-61). A vote is its owner's to disclose, so this names one
        voter by their own hand and writes no audit row — the vote itself is
        already in the anonymity audit for mods.
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, SHOW_VOTE_LABEL, channel_name(interaction.channel))
        if self._closed or self.waiting:
            await interaction.response.send_message("This round is over." if self._closed else "No question yet — pose one first!", ephemeral=True)
            return
        outcome = record_show_vote(self.votes_a, self.votes_b, self.shown, interaction.user.id)
        if outcome == "not_voted":
            await interaction.response.send_message(
                "❌ Vote 🅰️ or 🅱️ first — then you can show which side you picked.", ephemeral=True,
            )
            return
        if outcome == "already":
            await interaction.response.send_message("You're already showing your vote this round.", ephemeral=True)
            return
        # Resolve the name before rendering: a <@id> inside the card would
        # show as a bare number to anyone who hasn't cached that member.
        await self.refresh_name_fn(interaction.guild)
        # Ack, then redraw through the seam rather than editing the clicked
        # message directly: with the sticky dial on, the board this press
        # came from may already have been reposted somewhere else.
        await interaction.response.defer()
        await redraw_board(self, interaction.message)
        await self.persist_votes()

    async def press_end(self, interaction: discord.Interaction) -> None:
        """🏁 End Game, from the host panel: close the game with its recap
        and pay the room (vote-games-52)."""
        log.info("%s pressed 'End Game' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await may_control(interaction, self.host_id, self.db):
            await interaction.response.send_message(END_DENIED, ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This game already ended.", ephemeral=True)
            return

        async def _confirmed(_confirm_interaction: discord.Interaction) -> None:
            if self.finish_callback is not None:
                await self.finish_callback(self.message, REASON_HOST_ENDED)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["wyr"], ephemeral=True)


class WYRCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # Every live round board this cog owns (Games Global Config → Live
        # Game Boards). Keyed by game_id, so the board a round opens is the
        # board that round retires — one guild can run several Would You
        # Rather games in different channels at once.
        self.live_boards = LiveBoards(bot, self.db, "wyr board")

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

    @app_commands.command(name="wyr", description=play_description("wyr"))
    @app_commands.describe(
        question="Opening question (format: 'option A | option B') — defaults to question bank",
        tags="Comma-separated tags to filter the question bank",
    )
    async def wyr(
        self,
        interaction: discord.Interaction,
        question: str = "",
        tags: str = "",
    ):
        # These moved to the dashboard. Passing None is exactly what an
        # omitted option always meant, so the per-guild dial applies.
        round_seconds = rounds = None
        log.info("%s used /wyr in #%s", interaction.user.display_name, channel_name(interaction.channel))
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        # The one launch guard every door shares: allowed channel, enabled
        # dial, no game already running here, and a bank with something to
        # serve unless the host brought their own question.
        refusal = await refuse_launch(
            self.db, interaction, "wyr",
            tags=tag_list, host_supplied=bool(question.strip()),
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        if question.strip() and parse_question_input(question) is None:
            await interaction.response.send_message(
                "❌ Question must have two options separated by `|`, e.g. `fly | be invisible`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "question": question, "tags": tag_list,
                "round_seconds": round_seconds, "max_rounds": rounds,
            },
        )
        await finish_launch_response(interaction, game_id)
        # Next and End Game aren't on the card, so hand the host their
        # controls now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a Would You Rather game: ⏭️ Next and 🏁 End
        Game. Open to whoever the controls themselves accept — the host, a
        mod, or a Game Host (``may_control``)."""
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, WYRRoundView):
            await host_reply(interaction, GAME_ENDED_REPLY)
            return
        if not await may_control(interaction, view.host_id, self.db):
            await host_reply(interaction, CONTROL_DENIED)
            return
        panel = GameHostPanel(
            self.bot, game_id, build_host_panel_layout(),
            {HOST_NEXT_ID: "press_next", HOST_END_ID: "press_end"},
            view_type=WYRRoundView,
        )
        await send_panel(interaction, panel, label="wyr host panel")

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
        question = (options.get("question") or "").strip()
        custom_question: tuple[str, str] | None = None
        if question:
            custom_question = parse_question_input(question)
            if custom_question is None:
                log.warning("WYR launch: invalid question %r ignored", question)

        pacing = await launch_pacing(self.db, "wyr", guild_id, options)
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "wyr",
            state="playing",
            payload={
                "anonymous": True, "rounds": {}, "tags": options.get("tags") or [],
                "round_seconds": pacing.round_seconds, "max_rounds": pacing.max_rounds,
                "scheduled": is_scheduled_launch(options, host_id),
            },
            guild_id=guild_id,
        )
        log.info("Game %s (wyr) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        try:
            await self._run_round(
                interaction=None,
                game_id=game_id,
                host_id=host_id,
                host_name=host_name,
                round_num=1,
                channel=channel,
                custom_question=custom_question,
            )
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("WYR launch lacked send perms in channel %s", channel.id)
            return None
        # _run_round can end the game and unwind normally (a send that failed
        # for a reason other than permissions). Reporting a game id for that
        # would mark the schedule 'launched' and ping under a dead board. A
        # missing row is the signal the round bailed.
        if await get_active_game_by_id(self.db, game_id) is None:
            return None
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    @staticmethod
    def _voter_roster_from(payload: dict) -> list[int]:
        """Everyone who voted for either option in any round — the real
        participant set for economy payouts."""
        return sorted({
            int(v)
            for rd in payload.get("rounds", {}).values()
            for v in (rd.get("a") or []) + (rd.get("b") or [])
        })

    async def _voter_roster(self, game_id: str) -> list[int]:
        return self._voter_roster_from(await get_game_payload(self.db, game_id))

    @staticmethod
    def _pacing_from_payload(payload: dict, opened_at: float | None) -> RoundPacing:
        return RoundPacing(
            round_seconds=payload.get("round_seconds", 0),
            max_rounds=payload.get("max_rounds"),
            scheduled=bool(payload.get("scheduled")),
            opened_at=opened_at,
        )

    async def _run_round(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        custom_question: tuple[str, str] | None = None,
        carry_over_queue: list[tuple[str, str]] | None = None,
    ):
        payload = await get_game_payload(self.db, game_id)
        if custom_question:
            option_a, option_b = custom_question
        else:
            tags = payload.get("tags") or None
            question = await get_wyr_question(
                self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel)
            )
            # Nothing to serve: the round opens *waiting* for a posed question
            # rather than ending the game (vote-games-50).
            option_a, option_b = question if question else ("", "")

        waiting = not (option_a and option_b)
        pacing = self._pacing_from_payload(payload, None)
        if not waiting:
            pacing.open()
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {
            "a": [], "b": [],
            "q": "" if waiting else f"{option_a} OR {option_b}",
            "opened_at": pacing.opened_at,
        }
        await update_game_payload(self.db, game_id, payload)

        guild = getattr(channel, "guild", None)
        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=round_num,
            channel=channel,
            option_a=option_a,
            option_b=option_b,
            anonymous=payload.get("anonymous", True),
            interaction=interaction,
            pacing=pacing,
        )
        if carry_over_queue:
            view.queued_questions = carry_over_queue
            view._rebuild()
        self.bot.active_views[game_id] = view

        # Post this round's board — sticky (reposting to the bottom of the
        # channel as chat buries it) for the length of the round when the
        # guild's dial is on, a plain send otherwise. No ``msg`` to hand
        # over: each round posts a *fresh* board and leaves the round that
        # just closed on screen as its record, so the panel has nothing of
        # its own to move yet.
        try:
            msg = await self.live_boards.open(
                game_id, view, channel, guild, suffix=f" round {round_num}"
            )
        except discord.Forbidden:
            # Clean up and let the caller (slash wrapper / scheduler) report the failure.
            await end_game(self.db, game_id)
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            raise
        view.message = msg
        if not waiting:
            view.pacing.start_timer(lambda: view.advance_callback(msg))

    def _build_round_view(
        self,
        *,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        option_a: str,
        option_b: str,
        anonymous: bool = True,
        interaction=None,
        pacing: RoundPacing | None = None,
    ) -> "WYRRoundView":
        """Construct a round view with its advance and finish callbacks wired.

        Shared by _run_round (fresh round) and recover_game (post-restart) so
        round-to-round advancement behaves identically after a crash.
        """

        async def close_round(message: discord.Message | None) -> None:
            # Retire first: that cancels any pending restick, so nothing can
            # repost the round *over* its own closing frame, and it hands
            # back wherever the board actually ended up — ``message`` was
            # captured when the round opened and a restick may have moved
            # the board since. ``None`` back means the dial was off, in
            # which case ``message`` is still right.
            target = self.live_boards.retire(view, channel) or message
            view._closed = True
            # Wakes a timed round's wait (and never cancels it — the timer
            # task may be the caller).
            view.pacing.advanced.set()
            # The closed frame is the round's record: the final tally and no
            # controls, so nothing on it can be pressed.
            view._rebuild()
            if target is not None:
                try:
                    await target.edit(view=view, **V2_EDIT)
                except discord.HTTPException:
                    pass
            await view.persist_votes()

        async def finish(message: discord.Message | None, reason: str = REASON_HOST_ENDED) -> None:
            """End with the recap through the paying path — the host's End
            Game, the round cap, an expired game at Next, and /games end."""
            if not view._closed:
                await close_round(message)
            await self._finish_game(game_id, channel, reason=reason)

        async def advance(message: discord.Message) -> None:
            if view._closed:
                return
            await close_round(message)

            if await is_game_expired(self.db, game_id):
                # Past the 24h line: end with the recap and pay the room
                # (vote-games-59 — this used to be a bare, guild-0 end).
                await self._finish_game(game_id, channel, reason=REASON_EXPIRED)
                return

            if round_cap_reached(round_num, view.pacing.max_rounds):
                await self._finish_game(game_id, channel, reason=REASON_ROUND_CAP)
                return

            remaining = list(view.queued_questions)
            next_custom = remaining.pop(0) if remaining else None
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    channel=channel,
                    custom_question=next_custom,
                    carry_over_queue=remaining if remaining else None,
                )
            except Exception:
                log.exception("Error advancing WYR game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id, reason="crash")
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except discord.HTTPException:
                    pass

        view = WYRRoundView(
            game_id=game_id,
            host_id=host_id,
            option_a=option_a,
            option_b=option_b,
            round_num=round_num,
            anonymous=anonymous,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            # pyright reports a circular inference here (advance captures
            # `view`, whose initializer takes `advance`); the closure itself
            # is fully annotated above.
            advance_callback=advance,  # pyright: ignore[reportGeneralTypeIssues]
            guild=getattr(channel, "guild", None),
            pacing=pacing,
            finish_callback=finish,
        )
        return view

    async def _finish_game(self, game_id: str, channel, *, reason: str) -> bool:
        """Post the game-over recap and end through the paying path.

        Returns False when the game had already been ended by another path
        (``end_game``'s DELETE claim makes the payout exactly-once either way).
        """
        row = await get_active_game_by_id(self.db, game_id)
        if row is None:
            self.bot.active_views.pop(game_id, None)
            return False
        payload = await get_game_payload(self.db, game_id)
        rounds = payload.get("rounds", {})
        roster = self._voter_roster_from(payload)
        guild = getattr(channel, "guild", None)
        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "wyr")
        try:
            await channel.send(view=card(build_recap_layout(
                rounds, reason=reason, payout_line=payout_line,
            )))
        except discord.HTTPException:
            log.info("WYR recap not delivered for game %s", game_id)
        ended = await end_game(
            self.db, game_id,
            player_count=len(roster), round_count=len(played_rounds(rounds)), payload=payload,
            bot=self.bot, player_ids=roster, reason=reason,
        )
        self.bot.active_views.pop(game_id, None)
        return ended is not None

    async def end_with_recap(self, channel, game_id: str) -> bool:
        """``/games end`` on a Would You Rather game: the same recap ending as
        the host's 🏁 End Game, instead of the red Force-Closed card."""
        view = self.bot.active_views.get(game_id)
        if isinstance(view, WYRRoundView) and view.finish_callback is not None:
            await view.finish_callback(view.message, REASON_HOST_ENDED)
            return True
        return await self._finish_game(game_id, channel, reason=REASON_HOST_ENDED)

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild the current round's view after a restart, restoring votes.

        A recovered round is deliberately **not** re-stuck when the guild's
        Live Game Boards dial is on: the view is re-registered against the
        message that is already on screen, and the board starts following the
        channel bottom again from the next round.
        """
        rounds = payload.get("rounds", {})
        if not rounds:
            return False
        cur = max(rounds, key=lambda k: int(k))
        rd = rounds.get(cur, {})
        q = rd.get("q", "") or ""
        option_a, option_b = (q.split(" OR ", 1) + [""])[:2] if " OR " in q else (q, "")

        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"

        pacing = self._pacing_from_payload(payload, rd.get("opened_at"))
        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=int(cur),
            channel=channel,
            option_a=option_a,
            option_b=option_b,
            anonymous=payload.get("anonymous", True),
            interaction=None,
            pacing=pacing,
        )
        view.votes_a = list(rd.get("a", []))
        view.votes_b = list(rd.get("b", []))
        view.shown = list(rd.get("shown", []))
        await view.refresh_name_fn(guild)
        view._rebuild()
        view.message = message
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        await redraw_embed_era_board(view, message)
        if not view.waiting:
            left = seconds_left(pacing.opened_at, pacing.round_seconds)
            if left is not None:
                view.pacing.start_timer(
                    lambda: view.advance_callback(message),
                    seconds=max(left, _RECOVERY_GRACE_SECONDS),
                )
        log.info("Recovered wyr game %s (round %s) in #%s", game_id, cur, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = WYRCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("wyr")
    play.add_command(cog.wyr, override=True)
    bot.game_launchers["wyr"] = cog.launch
    bot.game_recoverers["wyr"] = cog.recover_game
