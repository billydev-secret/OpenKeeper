"""
Mt. Rushmore Draft — game cog.

A topic is chosen and players snake-draft their top 4 picks.  No duplicates
across any player.  After 4 rounds everyone's board is revealed, and the room
votes on the best Mt. Rushmore.

Pure logic (snake order, duplicate checks, recap stats, vote tally) lives in
``bot_modules.games.rushmore.logic``; the Components V2 card builders live in
``bot_modules.games.rushmore.layouts``. This cog is just the Discord glue:
it wraps those builders' items in ``LayoutView``s and binds their ids.

Nothing host-only is on a public card. Start Draft — the one host-only
control the game has — is on the ephemeral host panel (``/games host``).
"""

import asyncio
import logging
import random
import time as _time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401
    from bot_modules.games.utils.timer import GameTimer  # noqa: F401

import discord

from bot_modules.core.branding_service import PanelArt
from bot_modules.core.layout import (
    V2_EDIT as _V2_EDIT,
    bind,
    card,
    disable_all,
)
from bot_modules.core.sticky import StickyPanel
from bot_modules.games.board_sticky_service import (
    LiveBoards,
    redraw_board,
    redraw_embed_era_lobby,
)
from bot_modules.games.start_ping_service import (
    extract_start_epoch,
    resolve_start_epoch,
)
from bot_modules.core.utils import allow_pings, disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    sign_off_game_chore,
    finish_launch_response,
    create_game,
    modify_payload,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    get_game_options,
    end_game,
    update_session,
    channel_name,
)
from bot_modules.games.utils.host_panel import (
    GAME_ENDED_REPLY,
    GameHostPanel,
    NOT_HOST_REPLY,
    hand_over,
    reply as host_reply,
    send_panel,
)
from bot_modules.games.utils.lobby_art import NO_ART, read_lobby_art
from bot_modules.games.utils.names import build_names, host_display, cached_names, plain_name
from bot_modules.shared.name_resolver import GuildNames
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.question_source import get_rushmore_topic, channel_allows_nsfw
from bot_modules.games.utils.timer import now_plus
from bot_modules.games.rushmore.logic import (
    BACKFILL_SECONDS,
    DEFAULT_PICK_SECONDS,
    DRAFT_ROUNDS,
    MAX_PLAYERS,
    MIN_PLAYERS,
    SKIPPED_MARKER,
    RedrawCoalescer,
    apply_backfill,
    can_start,
    clamp_player_limits,
    clamp_settings,
    compute_recap_stats,
    eligible_voters,
    find_who_picked,
    first_skipped_slot,
    generate_snake_order,
    lobby_is_full,
    players_with_skips,
    tally_votes,
)
from bot_modules.games.rushmore.layouts import (
    HELP_ID,
    JOIN_ID,
    LEAVE_ID,
    PICK_ID,
    RUN_AGAIN_ID,
    START_ID,
    TITLE,
    VOTE_SELECT_ID,
    VOTE_TIMER_ITEM_ID,
    build_draft_layout,
    build_final_boards_pages,
    build_host_panel_layout,
    build_interrupted_layout,
    build_lobby_layout,
    build_recap_layout,
    build_vote_layout,
    build_winner_layout,
)

log = logging.getLogger(__name__)

# ── Modals ───────────────────────────────────────────────────────────────────

class HostTopicModal(discord.ui.Modal, title="Choose a Topic"):
    topic_input = discord.ui.TextInput(
        label="Topic",
        placeholder="e.g. Snacks, Movie Villains, Excuses to Leave a Party",
        required=True,
        max_length=100,
    )

    def __init__(self):
        super().__init__()
        self._result: str | None = None
        self._event = asyncio.Event()

    async def on_submit(self, interaction: discord.Interaction):
        self._result = self.topic_input.value.strip()
        await interaction.response.send_message(
            f"Topic set: **{discord.utils.escape_markdown(self._result)}**", ephemeral=True, delete_after=5
        )
        self._event.set()

    async def wait_for_result(self, timeout: float = 60.0) -> str | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._result


class PickModal(discord.ui.Modal):
    """Pick-entry modal shared by the draft (both modes) and backfill views.

    The owning view supplies ``boards`` (for the duplicate check),
    ``_closed`` (phase-over guard) and ``accept_pick(text, uid)`` (storage);
    everything else here is presentation.
    """

    pick = discord.ui.TextInput(
        label="Pick",
        required=True,
        max_length=100,
    )

    def __init__(self, round_num: int, topic: str, owner_view):
        title = f"Your Pick — Round {round_num}"
        if len(title) > 45:
            title = f"Round {round_num} Pick"
        super().__init__(title=title)
        self.pick.placeholder = f"Enter your pick for Mt. Rushmore of {topic}"[:100]
        self._owner_view = owner_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted pick modal in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        view = self._owner_view
        if view._closed:
            await interaction.response.send_message("This draft has ended.", ephemeral=True)
            return
        pick_text = self.pick.value.strip()
        if not pick_text:
            await interaction.response.send_message("Pick can't be empty.", ephemeral=True)
            return

        # Duplicate check
        who = find_who_picked(pick_text, view.boards)
        if who:
            who_name = plain_name(view.names, who)
            await interaction.response.send_message(
                f"❌ **{discord.utils.escape_markdown(pick_text)}** was already taken by **{discord.utils.escape_markdown(who_name)}**! Try again.",
                ephemeral=True,
            )
            return

        # Valid pick — store it
        view.accept_pick(pick_text, interaction.user.id)
        await interaction.response.send_message(
            f"✅ Picked **{discord.utils.escape_markdown(pick_text)}**!", ephemeral=True, delete_after=5,
        )


# ── Views ────────────────────────────────────────────────────────────────────

class RushmoreJoinView(discord.ui.LayoutView):
    """The lobby. **Paradigm 8 — live board**: rebuilt and re-edited in place
    on every Join/Leave. ``rushmore_join`` / ``rushmore_leave`` /
    ``rushmore_htp`` are frozen custom ids — ``recover_game`` re-registers
    this view against the lobby message after a restart.

    Start Draft is not here: the host panel (``host_panel.GameHostPanel``) calls
    :meth:`start_draft`.
    """

    def __init__(
        self, game_id: str, host_id: int, host_name: str, topic: str | None,
        source: str, db, bot, cog, mode: str = "snake",
        *,
        guild: "discord.Guild | None" = None,
        players: list[int] | None = None,
        start_at: int | None = None,
        art: PanelArt = NO_ART,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.topic = topic
        self.source = source
        self.mode = mode
        self.db = db
        self.bot = bot
        self.cog = cog
        self.guild = guild
        self.players: list[int] = list(players or [])
        self.names: GuildNames = cached_names(guild)
        self.start_at = start_at
        # The guild's lobby art, read once at launch/recovery and reused on
        # every redraw — never re-read per join/leave click.
        self.art = art
        self._msg: discord.Message | None = None
        self._closed = False
        self._rebuild()

    async def refresh_names(self) -> None:
        """Re-resolve the roster's names (live cache, then ``known_users``)
        before a redraw; ``_rebuild`` itself stays synchronous."""
        self.names = await build_names(self.bot, self.guild, self.players)

    def _rebuild(self) -> None:
        self.clear_items()
        for item in build_lobby_layout(
            self.host_name,
            [plain_name(self.names, uid) for uid in self.players],
            self.topic,
            mode=self.mode,
            start_at=self.start_at,
            closed=self._closed,
            header_url=self.art.header,
            footer_url=self.art.footer,
            title_in_header=self.art.title_in_header,
        ):
            self.add_item(item)
        bind(self, HELP_ID, self.how_to_play)
        if not self._closed:
            bind(self, JOIN_ID, self.join)
            bind(self, LEAVE_ID, self.leave)

    async def _redraw(self, interaction: discord.Interaction, payload: dict) -> None:
        self.players = payload.get("players", [])
        # Re-read from the payload, not the view: the countdown must
        # survive a restart that rebuilt this view from the DB.
        self.start_at = extract_start_epoch(payload)
        self.guild = interaction.guild or self.guild
        await self.refresh_names()
        self._rebuild()
        await interaction.response.edit_message(view=self, **_V2_EDIT)

    async def join(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Join' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id
        state: dict = {}

        def _add(p):
            players = p.setdefault("players", [])
            limit = int(p.get("settings", {}).get("max_players") or MAX_PLAYERS)
            state["full"] = uid not in players and lobby_is_full(players, limit)
            state["limit"] = limit
            if not state["full"] and uid not in players:
                players.append(uid)

        payload = await modify_payload(self.db, self.game_id, _add)
        if state.get("full"):
            await interaction.response.send_message(
                f"❌ This draft is full — it takes up to {state['limit']} players.",
                ephemeral=True,
            )
            return
        await self._redraw(interaction, payload)
        await interaction.followup.send("✅ You've joined!", ephemeral=True)

    async def leave(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Leave' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id
        def _remove(p):
            players = p.get("players", [])
            if uid in players:
                players.remove(uid)
        payload = await modify_payload(self.db, self.game_id, _remove)
        await self._redraw(interaction, payload)
        await interaction.followup.send("You've left.", ephemeral=True)

    async def start_draft(self, interaction: discord.Interaction) -> None:
        """Start the draft — the host panel's one button lands here."""
        log.info("%s pressed 'Start Draft' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message(DRAFT_RUNNING_REPLY, ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        floor = int(payload.get("settings", {}).get("min_players") or MIN_PLAYERS)
        if not can_start(self.players, floor):
            await interaction.response.send_message(
                f"Need at least {floor} players to start a Mt. Rushmore draft.",
                ephemeral=True,
            )
            return

        # Resolve topic if needed
        topic = self.topic
        if not topic:
            if self.source == "host":
                modal = HostTopicModal()
                await interaction.response.send_modal(modal)
                topic = await modal.wait_for_result(timeout=60.0)
                if not topic:
                    try:
                        await interaction.followup.send("Topic entry timed out. Try again.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return
            # "ai" was retired with the Prompts & AI studios; a game persisted
            # under it draws from the bank rather than erroring out.
            elif self.source in ("bank", "ai"):
                await interaction.response.defer()
                _tags = (await get_game_payload(self.db, self.game_id)).get("settings", {}).get("tags") or None
                topic = await get_rushmore_topic(
                    self.db, tags=_tags, allow_nsfw=channel_allows_nsfw(interaction.channel)
                )
                if not topic:
                    await interaction.followup.send("No topics in the question bank.", ephemeral=True)
                    return
            else:
                await interaction.response.defer()
        else:
            await interaction.response.defer()

        # Two presses can both get this far (a second panel, or a mod's); the
        # first to arrive owns the start.
        if self._closed:
            return
        self._closed = True
        self.topic = topic
        # Close the lobby: no Join, no Leave. The draft board takes this
        # message over in a moment (or, under the Live Game Boards dial,
        # posts below it and removes it).
        await self.refresh_names()
        self._rebuild()
        assert self._msg
        try:
            await self._msg.edit(view=self, **_V2_EDIT)
        except discord.HTTPException:
            pass

        # Start the draft
        await self.cog._start_draft(
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=self.host_name,
            topic=topic,
            players=list(self.players),
            channel=self._msg.channel,
            guild=self._msg.guild or interaction.guild,
            msg=self._msg,
            settings=await self.cog._get_settings(self.game_id),
        )

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '❓ Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["rushmore"], ephemeral=True)


#: What ``/games host`` says once the draft is under way: the draft, the
#: vote and the recap carry nothing only a host may press.
DRAFT_RUNNING_REPLY = "The draft is under way — there's nothing for the host to press until it ends."


class RushmoreDraftView(discord.ui.LayoutView):
    """The live draft board (snake or blitz mode) — implements
    ``game_board_sticky_service.LiveBoardLayoutView`` (``game_id``,
    ``guild``, ``_panel``, ``_board_retired``, ``_rebuild``), so the draft
    loops open, redraw and retire it through ``LiveBoards``."""

    def __init__(self, game_id: str, host_id: int, host_name: str, topic: str,
                 players: list[int], timer_secs: int, guild, db, bot, cog,
                 mode: str = "snake"):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.topic = topic
        self.players = players
        self.names: GuildNames = cached_names(guild)
        self.timer_secs = timer_secs
        self.guild = guild
        self.db = db
        self.bot = bot
        self.cog = cog
        self.mode = mode

        # Draft state
        random.shuffle(self.players)
        self.draft_order = generate_snake_order(self.players)
        self.current_pick_index = 0
        self.boards: dict[str, list] = {str(uid): [None] * DRAFT_ROUNDS for uid in self.players}
        self.all_picks: list[str] = []
        self.pick_times: dict[str, float | None] = {}
        self.skipped: list[str] = []
        self._msg: discord.Message | None = None
        self._closed = False
        # Set when the game turns on the "keep the board at the bottom"
        # dial: the per-game StickyPanel that owns reposting this board, and
        # the flag the cog sets once the game is over — the panel's
        # ``build`` callback checks this, not ``_closed`` (which is set one
        # legitimate render *before* the board is actually retired; see
        # retired). Both stay None/False for the life of the game
        # when the dial is off.
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        self._pick_event: asyncio.Event | None = None
        self._pick_start: float = 0.0
        self._draft_start: float = _time.time()
        self._active_player_id: int | None = None
        # Blitz-mode state: the current synced round and who still owes it a
        # pick. Unused in snake mode.
        self._blitz_round: int = 0
        self._blitz_pending: set[int] = set()
        # The current turn/round's countdown deadline (Unix ts), computed
        # once when the turn/round opens and threaded into every render —
        # see build_draft_layout's ``deadline`` param. Blitz mode holds this
        # fixed across a round's coalesced mid-round redraws; snake mode
        # refreshes it at every redraw, same as before. Set here too, so the
        # first frame never shows a countdown that already "ended".
        self._round_deadline: int = now_plus(timer_secs)
        # Coalesces blitz's mid-round board redraws (see RedrawCoalescer) so
        # a burst of simultaneous picks costs one Discord edit, not one per
        # pick. Unused in snake mode, which redraws directly.
        self._redraw_coalescer = RedrawCoalescer()
        self._rebuild()

    def _player_tuples(self) -> list[tuple[int, str]]:
        return [(uid, plain_name(self.names, uid)) for uid in self.players]

    async def start_draft(self, interaction: discord.Interaction) -> None:
        """A stale host panel's Start Draft, once the draft is under way."""
        await interaction.response.send_message(DRAFT_RUNNING_REPLY, ephemeral=True)

    async def refresh_names(self) -> None:
        """Re-resolve the players' names once (the roster is fixed once the
        draft starts): the live cache, then ``known_users`` for a player who
        has since left. ``_rebuild`` stays synchronous."""
        self.names = await build_names(self.bot, self.guild, self.players)

    async def refresh_board(self) -> None:
        """Redraw the board message with the current state.

        The single seam every board redraw goes through — both draft loops
        and the blitz coalescer call this instead of editing ``self._msg``
        directly, so there's exactly one place that knows how. When the
        sticky dial is on, ``redraw_board`` routes through the panel's own
        refresh instead of the cached ``self._msg`` — the panel may have
        re-posted the board to the bottom of the channel since the last
        redraw, and an edit against the old ``self._msg`` would then
        silently 404.
        """
        await redraw_board(self, self._msg)

    def accept_pick(self, pick_text: str, user_id: int):
        """Called by PickModal when a valid pick is made."""
        if self.mode == "blitz":
            rnd = self._blitz_round
            if user_id not in self._blitz_pending or rnd < 1:
                return
            self.boards[str(user_id)][rnd - 1] = pick_text
            self.all_picks.append(pick_text)
            self.pick_times[f"{user_id}_{rnd}"] = _time.time() - self._pick_start
            self._blitz_pending.discard(user_id)
            self._redraw_coalescer.schedule(self.refresh_board)
            if not self._blitz_pending and self._pick_event:
                self._pick_event.set()
            return

        if self._active_player_id != user_id:
            return
        rnd, pid = self.draft_order[self.current_pick_index]
        if pid != user_id:
            return

        self.boards[str(user_id)][rnd - 1] = pick_text
        self.all_picks.append(pick_text)
        elapsed = _time.time() - self._pick_start
        key = f"{user_id}_{rnd}"
        self.pick_times[key] = elapsed

        if self._pick_event:
            self._pick_event.set()

    def _rebuild(self) -> None:
        """Clear and re-add the board from current state, re-binding Make
        Your Pick — a rebuild adds a brand-new ``Button`` with nothing wired
        to it. The closed frame carries no button to bind."""
        if self.mode == "blitz":
            rnd, pid, name = max(1, self._blitz_round), None, None
        elif self.current_pick_index < len(self.draft_order):
            rnd, pid = self.draft_order[self.current_pick_index]
            name = plain_name(self.names, pid)
        else:
            rnd, pid, name = DRAFT_ROUNDS, None, None
        self.clear_items()
        for item in build_draft_layout(
            self.topic, self._player_tuples(), self.boards, pid, name, rnd,
            self._round_deadline, closed=self._closed,
        ):
            self.add_item(item)
        if not self._closed:
            bind(self, PICK_ID, self.make_pick)

    async def handle_pick_click(self, interaction: discord.Interaction):
        """Shared click handler for the board button and turn-ping button."""
        if self._closed:
            await interaction.response.send_message("The draft is over.", ephemeral=True)
            return
        if self.mode == "blitz":
            if interaction.user.id not in self._blitz_pending:
                if str(interaction.user.id) not in self.boards:
                    msg = "You're not in this draft."
                else:
                    msg = "You've already picked this round — wait for the next one!"
                await interaction.response.send_message(msg, ephemeral=True)
                return
            await interaction.response.send_modal(
                PickModal(self._blitz_round, self.topic, self)
            )
            return
        if interaction.user.id != self._active_player_id:
            active_name = plain_name(self.names, self._active_player_id) if self._active_player_id else "someone"
            await interaction.response.send_message(
                f"It's not your turn! Waiting on **{discord.utils.escape_markdown(active_name)}**.",
                ephemeral=True,
            )
            return
        rnd = self.draft_order[self.current_pick_index][0]
        await interaction.response.send_modal(PickModal(rnd, self.topic, self))

    async def make_pick(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Make Your Pick' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self.handle_pick_click(interaction)


class RushmorePingView(discord.ui.View):
    """Pick button that rides on turn-ping messages.

    The board message scrolls away as banter piles up mid-draft; putting the
    button on the ping itself means the player never has to scroll. The ping
    is sent with ``delete_after``, which also retires this view. The ping is
    a plain message on purpose — it has to notify, and it is gone in seconds
    — so this stays an ordinary ``View`` beside its ``content``.
    """

    def __init__(self, draft_view: RushmoreDraftView):
        super().__init__(timeout=None)
        self._draft_view = draft_view

    @discord.ui.button(label="\U0001f5ff Make Your Pick", style=discord.ButtonStyle.secondary, custom_id="rushmore_pick_ping", row=0)
    async def make_pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (ping) in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._draft_view.handle_pick_click(interaction)


class RushmoreBackfillView(discord.ui.View):
    """Post-draft window where players fill their own skipped slots. Rides a
    plain ping message, like :class:`RushmorePingView`."""

    def __init__(self, draft_view: RushmoreDraftView):
        super().__init__(timeout=None)
        self._draft_view = draft_view
        self.boards = draft_view.boards
        self._closed = False
        self._done: asyncio.Event = asyncio.Event()

    def accept_pick(self, pick_text: str, user_id: int):
        """Fill the player's first skipped slot (PickModal contract)."""
        slot = apply_backfill(
            self._draft_view.boards, self._draft_view.skipped, user_id, pick_text,
        )
        if slot is None:
            return
        self._draft_view.all_picks.append(pick_text)
        if not players_with_skips(self._draft_view.boards):
            self._done.set()

    @discord.ui.button(label="\U0001f5ff Fill a Skipped Pick", style=discord.ButtonStyle.secondary, custom_id="rushmore_backfill", row=0)
    async def backfill(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("The backfill window is over.", ephemeral=True)
            return
        board = self._draft_view.boards.get(str(interaction.user.id))
        if board is None:
            await interaction.response.send_message("You're not in this draft.", ephemeral=True)
            return
        slot = first_skipped_slot(board)
        if slot is None:
            await interaction.response.send_message("You have no skipped picks to fill!", ephemeral=True)
            return
        await interaction.response.send_modal(
            PickModal(slot + 1, self._draft_view.topic, self)
        )


class RushmoreVoteView(discord.ui.LayoutView):
    """The vote card (``layouts.build_vote_layout``) and its dropdown."""

    def __init__(self, game_id: str, topic: str, eligible_players: list[int],
                 deadline: int, guild, db, bot, names: GuildNames | None = None):
        super().__init__(timeout=None)
        self.names: GuildNames = names or cached_names(guild)
        self.game_id = game_id
        self.eligible_players = eligible_players
        self.guild = guild
        self.db = db
        self.bot = bot
        self.votes: dict[int, int] = {}
        self._closed = False
        self._msg: discord.Message | None = None
        self._timer_obj: "GameTimer | None" = None
        self._done_event: asyncio.Event | None = None

        # A Discord Select allows at most 25 options; the builder slices
        # defensively so an oversized roster can never 400 the vote message.
        for item in build_vote_layout(
            topic, deadline,
            [(plain_name(self.names, uid), str(uid)) for uid in eligible_players],
        ):
            self.add_item(item)
        bind(self, VOTE_SELECT_ID, self._on_vote)

    async def start_draft(self, interaction: discord.Interaction) -> None:
        """A stale host panel's Start Draft, once the vote is on."""
        await interaction.response.send_message(DRAFT_RUNNING_REPLY, ephemeral=True)

    async def _on_vote(self, interaction: discord.Interaction) -> None:
        if self._closed:
            await interaction.response.send_message("The vote is over.", ephemeral=True)
            return
        uid = interaction.user.id
        values = (interaction.data or {}).get("values") or []
        if not values:
            await interaction.response.defer()
            return
        target = int(values[0])
        if target == uid:
            await interaction.response.send_message("❌ You can't vote for yourself!", ephemeral=True)
            return
        changed = uid in self.votes
        self.votes[uid] = target
        target_name = plain_name(self.names, target)
        msg = f"✅ Voted for **{discord.utils.escape_markdown(target_name)}**"
        if changed:
            msg += " (changed)"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=5)
        if self.all_voted():
            self.skip_timer()

    def all_voted(self) -> bool:
        for uid in self.eligible_players:
            if uid not in self.votes:
                return False
        return True

    def skip_timer(self):
        if self._done_event:
            self._done_event.set()


class RushmoreRecapView(discord.ui.LayoutView):
    """The recap card: **Run Again** opens the next draft under whoever
    pressed it — that *is* the hand-off, so the old Hand Off button (which
    only printed a command hint and disabled Run Again) is gone
    (social-prompt-45)."""

    def __init__(self, game_id: str, host_id: int, cog: "RushmoreCog", settings: dict,
                 items: "list[discord.ui.Item[Any]]"):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.cog = cog
        self._settings = settings
        for item in items:
            self.add_item(item)
        bind(self, RUN_AGAIN_ID, self.run_again)

    async def run_again(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Run Again' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # Any member may press it and becomes the new host. The same gate as
        # the slash entry still applies: an admin who unticks the game on the
        # dashboard mid-evening must not be overridden by the recap card.
        refusal = await refuse_launch(self.cog.db, interaction, "rushmore")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        disable_all(self)
        assert interaction.message
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
                "topic": "",
                "timer": self._settings.get("timer", DEFAULT_PICK_SECONDS),
                "source": self._settings.get("source", "host"),
                "vote_timer": self._settings.get("vote_timer", 30),
                "mode": self._settings.get("mode", "blitz"),
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


# ── Cog ──────────────────────────────────────────────────────────────────────

class RushmoreCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # Every live draft board this cog owns (Games Global Config → Live
        # Game Boards). One StickyPanel per *live game*, not one shared
        # instance — a game board is per-channel, but StickyPanel's
        # internals are keyed per guild and one guild can run several
        # Rushmore drafts in different channels at once. The dict, the
        # gateway forwarding and the unload sweep all live in LiveBoards;
        # this cog supplies the view and the call sites.
        self.live_boards = LiveBoards(bot, self.db, "rushmore board")

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
        # Stop any blitz redraw coalescer still holding a pending task —
        # a reload mid-draft must not leave one running after the cog (and
        # the Discord state it edits) is gone.
        for view in list(self.bot.active_views.values()):
            coalescer = getattr(view, "_redraw_coalescer", None)
            if coalescer is not None:
                coalescer.cancel()
        # Likewise for every live game's sticky board — a reload must not
        # leave a debounced restick armed against a cog that's gone.
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
        """Recover after a restart.

        Rushmore is a single linear game (join -> draft -> vote -> recap), not a
        series of rounds. The join lobby re-registers cleanly. Once drafting or
        voting is underway there's no prior round to fall back to, and those run
        in blocking loops that can't be safely re-driven, so we end the game
        gracefully rather than leave dead buttons.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = await host_display(self.bot, guild, host_id)

        if row["state"] == "joining":
            settings = payload.get("settings", {})
            view = RushmoreJoinView(
                game_id, host_id, host_name,
                payload.get("topic"), settings.get("source", "host"),
                self.db, self.bot, self, mode=settings.get("mode", "snake"),
                guild=guild,
                players=list(payload.get("players", [])),
                start_at=extract_start_epoch(payload),
                art=await read_lobby_art(self.bot, channel, TITLE),
            )
            view._msg = message
            # Rebuild before ``add_view``: the store keeps the items present then.
            await view.refresh_names()
            view._rebuild()
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            await redraw_embed_era_lobby(view, message)
            log.info("Recovered rushmore game %s (join phase) in #%s", game_id, getattr(channel, "name", channel.id))
            return True

        # Drafting/voting underway — end gracefully.
        # A V2 board is nothing but its view, so it can't be stripped the way
        # an embed-era board's buttons were — it becomes a one-line notice.
        try:
            if getattr(getattr(message, "flags", None), "components_v2", False):
                await message.edit(view=card(build_interrupted_layout()))
            else:
                await message.edit(view=None)
        except discord.HTTPException:
            pass
        try:
            await channel.send(
                "🗿 This Rushmore game was interrupted by a bot restart and can't be "
                "resumed — start a new one with `/games play rushmore`."
            )
        except discord.HTTPException:
            pass
        await end_game(self.db, game_id)
        self.bot.active_views.pop(game_id, None)
        log.info("Rushmore game %s was mid-play at restart; ended gracefully.", game_id)
        return True

    async def _get_settings(self, game_id: str) -> dict:
        payload = await get_game_payload(self.db, game_id)
        return payload.get("settings", {})

    # ── Slash command ────────────────────────────────────────────────

    @app_commands.command(name="rushmore", description=play_description("rushmore"))
    @app_commands.describe(
        topic="The topic (leave blank for AI/bank/manual entry)",
        source="Where topics come from",
        mode="Snake draft (turns) or blitz (everyone picks at once)",
        start_in="Countdown before the start, in minutes",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Host picks topic", value="host"),
            app_commands.Choice(name="Question bank", value="bank"),
        ],
        mode=[
            app_commands.Choice(name="Snake draft (one at a time)", value="snake"),
            app_commands.Choice(name="Blitz (everyone picks at once)", value="blitz"),
        ],
    )
    async def rushmore_cmd(
        self,
        interaction: discord.Interaction,
        topic: str = "",
        source: str = "host",
        mode: str = "",
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info(
            "%s used /games play rushmore in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "rushmore")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        options: dict = {"topic": topic, "source": source, "start_in": start_in}
        if mode:
            options["mode"] = mode
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options=options,
        )
        await finish_launch_response(interaction, game_id)
        # Start Draft isn't on the lobby, so hand the host their controls
        # now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a Rushmore game: Start Draft, while the lobby
        is open. The host always gets it; anyone else needs the same mod
        standing the control itself checks (``is_host_or_mod``)."""
        view = self.bot.active_views.get(game_id)
        if view is None:
            await host_reply(interaction, GAME_ENDED_REPLY)
            return
        # The vote card's view carries no host; nothing past the lobby has a
        # control to guard, so only a view that names its host is gated.
        host_id = getattr(view, "host_id", None)
        if host_id is not None and not is_host_or_mod(interaction, host_id):
            await host_reply(interaction, NOT_HOST_REPLY)
            return
        if not isinstance(view, RushmoreJoinView) or view._closed:
            await host_reply(interaction, DRAFT_RUNNING_REPLY)
            return
        panel = GameHostPanel(
            self.bot, game_id, build_host_panel_layout(), {START_ID: "start_draft"},
            view_type=(RushmoreJoinView, RushmoreDraftView, RushmoreVoteView),
        )
        await send_panel(interaction, panel, label="rushmore host panel")

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
        topic = options.get("topic") or None
        source = options.get("source", "host")
        # Pacing knobs come from the per-server dashboard config; an explicit
        # *options* value (e.g. from a saved schedule) still wins.
        game_opts = await get_game_options(self.db, "rushmore", guild_id)
        timer, vote_timer = clamp_settings(
            int(options.get("timer", game_opts.get("timer", DEFAULT_PICK_SECONDS))),
            int(options.get("vote_timer", game_opts.get("vote_timer", 30))),
        )
        # Blitz by default (social-prompt-37): a six-player snake is 24 timed
        # turns of dead time for five people; blitz is four rounds for all.
        mode = options.get("mode") or game_opts.get("mode") or "blitz"
        if mode not in ("snake", "blitz"):
            mode = "blitz"
        min_players, max_players = clamp_player_limits(
            options.get("min_players", game_opts.get("min_players", MIN_PLAYERS)),
            options.get("max_players", game_opts.get("max_players", MAX_PLAYERS)),
        )
        settings = {
            "timer": timer, "source": source, "vote_timer": vote_timer,
            "mode": mode, "tags": options.get("tags") or [],
            "min_players": min_players, "max_players": max_players,
        }

        start_epoch = resolve_start_epoch(options)
        payload = {"settings": settings, "players": [], "topic": topic}
        if start_epoch:
            payload["start_epoch"] = start_epoch
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "rushmore",
            state="joining",
            payload=payload,
        )
        log.info("Game %s (rushmore) created by host %s", game_id, host_id)

        # The guild's lobby art, read once here and reused on every redraw.
        join_view = RushmoreJoinView(
            game_id, host_id, host_name,
            topic, source, self.db, self.bot, self, mode=mode,
            guild=getattr(channel, "guild", None),
            start_at=start_epoch,
            art=await read_lobby_art(self.bot, channel, TITLE),
        )
        try:
            msg = await retry_transient(
                lambda: channel.send(view=join_view),
                label="rushmore launch",
            )
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("rushmore launch lacked send perms in channel %s", channel.id)
            return None
        join_view._msg = msg
        self.bot.active_views[game_id] = join_view
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    # ── Draft execution ──────────────────────────────────────────────

    async def _start_draft(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        topic: str,
        players: list[int],
        channel,
        guild,
        msg: discord.Message,
        settings: dict,
    ):
        await update_game_state(self.db, game_id, "playing")
        payload = await get_game_payload(self.db, game_id)
        payload["topic"] = topic
        await update_game_payload(self.db, game_id, payload)

        mode = settings.get("mode", "snake")
        timer_secs = settings.get("timer", DEFAULT_PICK_SECONDS)
        draft_view = RushmoreDraftView(
            game_id, host_id, host_name, topic,
            players, timer_secs, guild, self.db, self.bot, self,
            mode=mode,
        )
        await draft_view.refresh_names()
        draft_view._rebuild()
        self.bot.active_views[game_id] = draft_view

        # Show initial draft board — sticky (reposts to the bottom of the
        # channel as chat buries it) when the guild's dial is on, an in-place
        # edit of the lobby message otherwise.
        msg = await self.live_boards.open(
            game_id, draft_view, channel, guild, msg
        )
        draft_view._msg = msg

        # From here until the loop takes over, a failure must still retire
        # the board — the loops' own try/finally is the only other place
        # that retires it, and nothing wraps this span. Without it an
        # exception here (an unguarded channel.send included) would leave the
        # panel live forever, resticking a frozen board under every future
        # message in the channel.
        try:
            # Show draft order announcement
            if mode == "blitz":
                await channel.send(
                    "**⚡ Blitz draft:** everyone picks at the same time each round — "
                    "duplicates go to the fastest fingers!"
                )
            else:
                order_names = [plain_name(draft_view.names, uid) for uid in draft_view.players]
                rev_names = list(reversed(order_names))
                await channel.send(
                    f"**Draft order:** {' → '.join(order_names)}\n"
                    f"(Round 2 reverses: {' → '.join(rev_names)})"
                )

            # Save draft state
            payload = await get_game_payload(self.db, game_id)
            payload["draft_order"] = draft_view.draft_order
            payload["boards"] = draft_view.boards
            payload["all_picks"] = draft_view.all_picks
            await update_game_payload(self.db, game_id, payload)
        except Exception:
            self.live_boards.retire(draft_view)
            raise

        # Run each pick
        if mode == "blitz":
            await self._run_blitz_loop(draft_view, channel, guild, settings)
        else:
            await self._run_draft_loop(draft_view, channel, guild, settings)

    async def _run_draft_loop(self, draft_view: RushmoreDraftView, channel, guild, settings: dict):
        timer_secs = settings.get("timer", DEFAULT_PICK_SECONDS)
        assert draft_view._msg  # set by _start_draft before this loop runs

        try:
            while draft_view.current_pick_index < len(draft_view.draft_order):
                if draft_view._closed:
                    return

                rnd, pid = draft_view.draft_order[draft_view.current_pick_index]
                draft_view._active_player_id = pid
                player_name = plain_name(draft_view.names, pid)

                # Update the draft board — a fresh deadline every turn, same as
                # every render used to get before the deadline became explicit.
                draft_view._round_deadline = now_plus(timer_secs)
                await draft_view.refresh_board()

                # Ping the player — with its own pick button, so nobody has to
                # scroll back up to the board message to act. One message per
                # turn: the 10-second nudge edits this same ping in place rather
                # than posting a second one (social-prompt-37 — a six-player
                # snake used to leave ~70 self-deleting messages behind).
                member = guild.get_member(pid) if guild else None
                ping_text = (
                    f"{member.mention if member else player_name} It's your turn! "
                    f"Pick for Round {rnd} of your Mt. Rushmore of **{discord.utils.escape_markdown(draft_view.topic)}**!"
                )
                ping_msg = None
                try:
                    ping_msg = await channel.send(
                        ping_text, view=RushmorePingView(draft_view), delete_after=timer_secs,
                        allowed_mentions=allow_pings(users=[member]),
                    )
                except discord.HTTPException:
                    pass

                # Wait for pick or timeout
                pick_event = asyncio.Event()
                draft_view._pick_event = pick_event
                draft_view._pick_start = _time.time()

                # Schedule nudge
                nudge_task = None
                if timer_secs > 15 and ping_msg is not None:
                    async def _nudge(msg=ping_msg, _event=pick_event, _text=ping_text):
                        await asyncio.sleep(timer_secs - 10)
                        if not _event.is_set() and not draft_view._closed:
                            try:
                                await msg.edit(content=f"{_text}\n⏰ **10 seconds left to pick!**")
                            except discord.HTTPException:
                                pass
                    nudge_task = asyncio.create_task(_nudge())

                try:
                    await asyncio.wait_for(draft_view._pick_event.wait(), timeout=timer_secs)
                except asyncio.TimeoutError:
                    if not draft_view._closed:
                        # Skipped
                        key = f"{pid}_{rnd}"
                        draft_view.boards[str(pid)][rnd - 1] = SKIPPED_MARKER
                        draft_view.skipped.append(key)
                        draft_view.pick_times[key] = None
                        try:
                            m = member.mention if member else player_name
                            await channel.send(
                                f"{m} ⏱️ Time's up! Your pick was skipped.",
                                delete_after=10,
                                allowed_mentions=allow_pings(users=[member]),
                            )
                        except discord.HTTPException:
                            pass

                if nudge_task and not nudge_task.done():
                    nudge_task.cancel()

                if draft_view._closed:
                    return

                # Advance to next pick
                draft_view.current_pick_index += 1

                # Save progress to DB
                payload = await get_game_payload(self.db, draft_view.game_id)
                payload["boards"] = draft_view.boards
                payload["all_picks"] = draft_view.all_picks
                payload["current_pick_index"] = draft_view.current_pick_index
                payload["pick_times"] = draft_view.pick_times
                payload["skipped"] = draft_view.skipped
                await update_game_payload(self.db, draft_view.game_id, payload)

                # Update board after pick — same fresh-deadline treatment; the
                # next turn (or the "draft complete" frame below) gets its own.
                if not draft_view._closed:
                    draft_view._round_deadline = now_plus(timer_secs)
                    await draft_view.refresh_board()

            # Draft complete — disable draft view
            draft_view._closed = True
            draft_view._redraw_coalescer.cancel()
            await draft_view.refresh_board()

        finally:
            # Guarantees the board stops reposting on every exit path —
            # natural completion just above, or the early return on a
            # forced end (/games end -> force_end_active_game sets
            # _closed and this loop returns before ever reaching that
            # completion code).
            self.live_boards.retire(draft_view)

        # Backfill window for skipped slots, then final boards
        await self._run_backfill(draft_view, channel, guild)
        await self._show_final_boards(draft_view, channel, guild, settings)

    async def _run_blitz_loop(self, draft_view: RushmoreDraftView, channel, guild, settings: dict):
        """Blitz mode: every round, all players pick simultaneously."""
        timer_secs = settings.get("timer", DEFAULT_PICK_SECONDS)
        assert draft_view._msg  # set by _start_draft before this loop runs

        try:
            for rnd in range(1, DRAFT_ROUNDS + 1):
                if draft_view._closed:
                    return

                pending = {
                    uid for uid in draft_view.players
                    if draft_view.boards[str(uid)][rnd - 1] is None
                }
                draft_view._blitz_round = rnd
                draft_view._blitz_pending = pending

                round_done = asyncio.Event()
                draft_view._pick_event = round_done
                draft_view._pick_start = _time.time()

                # One deadline for the whole round — held fixed while picks land
                # mid-round (see build_draft_layout's ``deadline`` param and
                # RedrawCoalescer) instead of resetting on every coalesced redraw.
                draft_view._round_deadline = now_plus(timer_secs)
                await draft_view.refresh_board()

                mentions = " ".join(
                    m.mention for uid in pending
                    if guild and (m := guild.get_member(uid))
                )
                try:
                    await channel.send(
                        f"{mentions} ⚡ **Round {rnd}/{DRAFT_ROUNDS}** — everyone pick now! "
                        f"First come, first served on duplicates.",
                        view=RushmorePingView(draft_view), delete_after=timer_secs,
                        allowed_mentions=allow_pings(users=pending),
                    )
                except discord.HTTPException:
                    pass

                nudge_task = None
                if timer_secs > 15:
                    async def _nudge(_event=round_done):
                        await asyncio.sleep(timer_secs - 10)
                        if not _event.is_set() and not draft_view._closed and draft_view._blitz_pending:
                            stragglers = " ".join(
                                m.mention for uid in draft_view._blitz_pending
                                if guild and (m := guild.get_member(uid))
                            )
                            try:
                                await channel.send(
                                    f"{stragglers} ⏰ 10 seconds left to pick!",
                                    view=RushmorePingView(draft_view), delete_after=10,
                                    allowed_mentions=allow_pings(
                                        users=draft_view._blitz_pending,
                                    ),
                                )
                            except discord.HTTPException:
                                pass
                    nudge_task = asyncio.create_task(_nudge())

                try:
                    await asyncio.wait_for(round_done.wait(), timeout=timer_secs)
                except asyncio.TimeoutError:
                    pass

                if nudge_task and not nudge_task.done():
                    nudge_task.cancel()

                # The round is over — cancel any redraw the coalescer still has
                # in flight for a pick that landed right at the buzzer. The next
                # round's own redraw (or the "draft complete" one below) covers
                # showing that pick; nothing should fire after the round it
                # belongs to has already moved on.
                draft_view._redraw_coalescer.cancel()

                if draft_view._closed:
                    return

                # Anyone still pending ran out the clock
                for uid in sorted(draft_view._blitz_pending):
                    key = f"{uid}_{rnd}"
                    draft_view.boards[str(uid)][rnd - 1] = SKIPPED_MARKER
                    draft_view.skipped.append(key)
                    draft_view.pick_times[key] = None
                draft_view._blitz_pending = set()

                # Save progress to DB
                payload = await get_game_payload(self.db, draft_view.game_id)
                payload["boards"] = draft_view.boards
                payload["all_picks"] = draft_view.all_picks
                payload["pick_times"] = draft_view.pick_times
                payload["skipped"] = draft_view.skipped
                await update_game_payload(self.db, draft_view.game_id, payload)

            draft_view._closed = True
            draft_view._redraw_coalescer.cancel()
            await draft_view.refresh_board()

        finally:
            # Same guarantee as the snake loop: retire on every exit,
            # not just natural completion.
            self.live_boards.retire(draft_view)

        await self._run_backfill(draft_view, channel, guild)
        await self._show_final_boards(draft_view, channel, guild, settings)

    async def _run_backfill(self, draft_view: RushmoreDraftView, channel, guild):
        """Give players a short window to fill their own skipped slots."""
        # Skip when the game was force-ended mid-draft.
        if draft_view.game_id not in self.bot.active_views:
            return
        owed = players_with_skips(draft_view.boards)
        if not owed:
            return

        view = RushmoreBackfillView(draft_view)
        mentions = " ".join(
            m.mention for uid_str in owed
            if guild and (m := guild.get_member(int(uid_str)))
        )
        try:
            msg = await channel.send(
                f"{mentions} ⏭️ You have skipped picks! "
                f"**{BACKFILL_SECONDS}s** to fill them before the boards are final.",
                view=view,
                allowed_mentions=allow_pings(users=owed),
            )
        except discord.HTTPException:
            return

        try:
            await asyncio.wait_for(view._done.wait(), timeout=BACKFILL_SECONDS)
        except asyncio.TimeoutError:
            pass
        view._closed = True
        disable_all_items(view)
        try:
            await msg.edit(view=view)
        except discord.HTTPException:
            pass

        # Persist whatever got backfilled
        payload = await get_game_payload(self.db, draft_view.game_id)
        payload["boards"] = draft_view.boards
        payload["all_picks"] = draft_view.all_picks
        payload["skipped"] = draft_view.skipped
        await update_game_payload(self.db, draft_view.game_id, payload)

    async def _show_final_boards(self, draft_view: RushmoreDraftView, channel, guild, settings: dict):
        game_id = draft_view.game_id
        # All-skip boards are left off the final display (they're already
        # excluded from the vote) — no sense parading an empty board.
        with_picks = set(eligible_voters(draft_view.players, draft_view.boards))
        player_tuples = [
            (uid, name) for uid, name in draft_view._player_tuples()
            if uid in with_picks
        ]

        # One card as a rule; a full lobby of long picks pages (whole
        # boards per message, never a cut one).
        try:
            for page in build_final_boards_pages(
                draft_view.topic, player_tuples, draft_view.boards,
            ):
                await channel.send(view=card(page))
        except discord.HTTPException:
            pass

        # Determine eligible voters (players with at least 1 real pick)
        eligible = eligible_voters(draft_view.players, draft_view.boards)

        if len(eligible) <= 1:
            # Auto-win or no valid boards
            if eligible:
                winner_uid = eligible[0]
                winner_name = plain_name(draft_view.names, winner_uid)
                await channel.send(
                    f"\U0001f3c6 **{discord.utils.escape_markdown(winner_name)}** wins by default!",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                winner_uid = None
                await channel.send("No valid boards — nobody wins!")
            await self._show_recap(
                draft_view, channel, guild, settings,
                votes={}, winner_uids=[winner_uid] if winner_uid else [],
            )
            return

        await asyncio.sleep(5)

        # Voting phase
        vote_timer = settings.get("vote_timer", 30)
        vote_view = RushmoreVoteView(
            game_id, draft_view.topic, eligible, now_plus(vote_timer),
            guild, self.db, self.bot, names=draft_view.names,
        )
        self.bot.active_views[game_id] = vote_view

        try:
            vote_msg = await channel.send(view=vote_view)
            vote_view._msg = vote_msg
        except Exception:  # noqa: BLE001 — a vote card that fails to post for any reason must still reach the recap, or the game stalls
            await self._show_recap(draft_view, channel, guild, settings, votes={}, winner_uids=[])
            return

        # Timer
        vote_done = asyncio.Event()
        vote_view._done_event = vote_done

        async def _on_vote_timer():
            vote_done.set()

        from bot_modules.games.utils.timer import GameTimer
        timer = GameTimer(
            duration=vote_timer, message=vote_msg, callback=_on_vote_timer,
            timer_item_id=VOTE_TIMER_ITEM_ID,
        )
        vote_view._timer_obj = timer
        await timer.start()
        await vote_done.wait()

        # Bail if the game was force-ended (e.g. /games end) while voting.
        if game_id not in self.bot.active_views:
            return

        # Close the vote: a late pick must not land after the tally. The view
        # keeps listening until the winner card has replaced the dropdown —
        # ``stop()`` first left a press in that gap with "This interaction
        # failed" — so the closed flag answers it plainly meanwhile.
        vote_view._closed = True

        # Tally — a tie goes to fewest skips, then the quickest drafter.
        winner_uids, max_votes, results_by_uid = tally_votes(
            vote_view.votes, eligible,
            skipped=draft_view.skipped, pick_times=draft_view.pick_times,
        )

        winner_names = [plain_name(draft_view.names, uid) for uid in winner_uids]
        winner_boards_list = [draft_view.boards.get(str(uid), []) for uid in winner_uids]

        # Resolve names for the sorted results
        all_results = [(plain_name(draft_view.names, uid), v) for uid, v in results_by_uid]

        winner_card = card(build_winner_layout(
            draft_view.topic, winner_names, max_votes, winner_boards_list, all_results,
        ))
        try:
            await vote_msg.edit(view=winner_card)
        except discord.HTTPException:
            pass
        vote_view.stop()

        await asyncio.sleep(5)
        await self._show_recap(draft_view, channel, guild, settings, vote_view.votes, winner_uids)

    async def _show_recap(
        self,
        draft_view: RushmoreDraftView,
        channel,
        guild,
        settings: dict,
        votes: dict[int, int],
        winner_uids: list[int],
    ):
        game_id = draft_view.game_id
        duration = _time.time() - draft_view._draft_start

        # Recompute max_votes from the (possibly empty) vote tally
        tally: dict[int, int] = {}
        for _voter, target in votes.items():
            tally[target] = tally.get(target, 0) + 1
        max_votes = max(tally.values()) if tally else 0

        winner_names = [plain_name(draft_view.names, uid) for uid in winner_uids]
        winner_boards_list = [draft_view.boards.get(str(uid), []) for uid in winner_uids]

        stats = compute_recap_stats(
            # Blitz has no meaningful "first overall pick" — everyone's round-1
            # picks land at once — so the stat is suppressed by passing no order.
            [] if draft_view.mode == "blitz" else draft_view.draft_order,
            draft_view.boards,
            draft_view.all_picks,
            draft_view.pick_times,
            draft_view.skipped,
            votes,
            name_resolver=lambda uid: plain_name(draft_view.names, uid),
        )

        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "rushmore")
        recap_view = RushmoreRecapView(
            game_id, draft_view.host_id, self, settings,
            build_recap_layout(
                draft_view.topic, len(draft_view.players), duration,
                winner_names, max_votes, winner_boards_list, stats,
                payout_line=payout_line,
            ),
        )

        try:
            await channel.send(view=recap_view)
        except discord.HTTPException:
            pass

        # End game
        payload = await get_game_payload(self.db, game_id)
        payload["votes"] = {str(k): str(v) for k, v in votes.items()}
        await end_game(
            self.db, game_id,
            player_count=len(draft_view.players),
            round_count=DRAFT_ROUNDS,
            payload=payload,
            bot=self.bot, player_ids=list(draft_view.players),
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]


async def setup(bot: "Bot"):
    cog = RushmoreCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("rushmore")
    play.add_command(cog.rushmore_cmd, override=True)
    bot.game_launchers["rushmore"] = cog.launch
    bot.game_recoverers["rushmore"] = cog.recover_game
