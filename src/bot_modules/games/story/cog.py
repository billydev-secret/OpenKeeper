import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.layout import disable_all
from bot_modules.core.utils import allow_pings, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.start_ping_service import resolve_start_epoch
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    create_game,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_name,
    channel_name,
)
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.lobby_art import NO_ART, read_lobby_art
from bot_modules.core.branding_service import PanelArt
from bot_modules.core.sticky import StickyPanel
from bot_modules.games.board_sticky_service import LiveBoards
from bot_modules.games.story.layouts import (
    LOBBY_TITLE,
    build_lobby_layout,
    build_story_board_layout,
    build_story_recap_layout,
    build_turn_prompt_layout,
)
from bot_modules.games.story.logic import (
    ATTRIBUTION_CARD_BUDGET,
    DEFAULT_TURN_SECONDS,
    STORY_TEXT_CARD_BUDGET,
    add_player,
    append_sentence,
    assemble_story_text,
    build_attribution_lines,
    build_context,
    build_turn_order,
    clamp_max_sentences,
    format_drop_notice,
    format_leave_notice,
    format_skip_notice,
    format_story_opening,
    note_turn_outcome,
    pick_current_player,
    remove_player,
    resolve_starter,
    rotation_after_turn,
    roster_for_payout,
    should_drop_writer,
    should_end_after_skip,
    trim_attribution_lines,
    writer_may_skip,
    writer_skip_unlock_at,
)

log = logging.getLogger(__name__)

_TURN_TIMEOUT = DEFAULT_TURN_SECONDS  # seconds per turn


def _card(items: list[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one send.

    The recap carries no buttons, so nothing dispatches to it and nothing
    has to expire — but a view instance belongs to one message
    (dm_perms_cog._card, games_help.views._card — the same pattern).
    """
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _bind(
    view: discord.ui.LayoutView,
    custom_id: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
) -> None:
    """Attach ``callback`` to the persistent item carrying ``custom_id``.

    Layout builders return components and no behaviour (guide § Code
    conventions), so the view wires itself up after adding them — every
    ``_rebuild`` call, since a restick or a fresh press clears and re-adds
    brand-new ``Button`` instances with nothing wired to them yet
    (dm_perms_cog._bind, same pattern).
    """
    for item in view.walk_children():
        if getattr(item, "custom_id", None) == custom_id:
            item.callback = callback  # type: ignore[method-assign]
            return
    raise KeyError(f"no component with custom_id {custom_id!r} in {view!r}")


class StorySentenceModal(discord.ui.Modal, title="Add Your Sentence"):
    """The sentence box. Submitting hands the sentence straight to the turn
    view (``on_submit`` sets its event); the modal is never awaited, so a
    box the writer dismisses leaks nothing — it simply times out with the
    turn (anon-tail-78: each dismissal used to park a coroutine on
    ``modal.wait()`` for the life of the process).
    """

    context_field = discord.ui.TextInput(
        label="Context (for reference)",
        style=discord.TextStyle.paragraph,
        required=False,
    )
    sentence = discord.ui.TextInput(
        label="Your Sentence",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Continue the story…",
    )

    def __init__(
        self, game_id: str, player_id: int, context_text: str = "",
        turn_view: "StoryTurnView | None" = None,
    ):
        super().__init__(timeout=_TURN_TIMEOUT)
        self.game_id = game_id
        self.player_id = player_id
        self.turn_view = turn_view
        self._submitted = False
        self._value: str | None = None
        if context_text:
            self.context_field._underlying.value = context_text[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted story sentence in #%s", interaction.user.display_name, channel_name(interaction.channel))
        self._submitted = True
        self._value = self.sentence.value
        view = self.turn_view
        if view is not None and self._value and not view._submitted_event.is_set():
            view._submitted_text = self._value
            view._submitted_event.set()
            view.stop()
        await interaction.response.send_message("✅ Your sentence has been added!", ephemeral=True)


class StoryTurnView(discord.ui.LayoutView):
    """Per-turn board + prompt panel — implements ``game_board_sticky_
    service.LiveBoardLayoutView`` (``game_id``, ``guild``, ``_panel``,
    ``_board_retired``, ``_rebuild``), so the turn loop opens and retires it
    through ``LiveBoards`` exactly as an embed-shaped board would.
    ``_rebuild`` redraws the card in place — the board half
    (:func:`build_story_board_layout`) plus the prompt half
    (:func:`build_turn_prompt_layout`) — every time a restick reposts it, and
    re-binds the Write/Skip/Leave callbacks each call since a rebuild clears
    and re-adds brand-new ``Button`` instances.

    Skip is the host's or a mod's at any time, and **any writer's** once the
    turn has been open :data:`WRITER_SKIP_AFTER_SECONDS` (anon-tail-73: only
    the host could skip, so an AFK host stalled the story). Leave takes the
    presser out of the rotation after this turn.
    """

    def __init__(
        self, game_id: str, host_id: int, current_player_id: int, context_text: str, db, bot,
        turn_order: list[int] | None = None,
        *,
        guild: "discord.Guild | None" = None,
        sentence_count: int = 1,
        max_sentences: int = 0,
        name_resolver: Callable[[int], str] | None = None,
    ):
        super().__init__(timeout=_TURN_TIMEOUT)
        self.game_id = game_id
        self.host_id = host_id
        self.current_player_id = current_player_id
        self.context_text = context_text
        self.db = db
        self.bot = bot
        self.turn_order = list(turn_order or [])
        self.opened_at = time.time()
        self._submitted_event = asyncio.Event()
        self._submitted_text: str | None = None
        self._skipped = False
        self._left: set[int] = set()
        # Only read by the sticky board wiring, which needs a guild to key a
        # StickyPanel by; the card resolves its own names below.
        self.guild = guild
        # What a render needs, resolved once when the turn opens so the
        # board can be rebuilt from the view alone — the sticky panel's
        # ``build`` runs from core, with no loop state in hand.
        self._sentence_count = sentence_count
        self._max_sentences = max_sentences
        self._name_fn: Callable[[int], str] = name_resolver or (lambda pid: str(pid))
        # Set when the guild turns on the "keep the board at the bottom"
        # dial (Games Global Config → Live Game Boards): the StickyPanel
        # that owns re-posting this turn's card, and the flag its ``build``
        # checks once the turn is over.
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        self._rebuild()

    def _rebuild(self) -> None:
        """What the sticky panel re-posts. A turn's board content doesn't
        change while the turn is open — what has to stay reachable is the
        buttons, re-bound below every call."""
        self.clear_items()
        for item in build_story_board_layout(
            sentence_count=self._sentence_count,
            max_sentences=self._max_sentences,
            current_player_id=self.current_player_id,
            turn_order=self.turn_order,
            name_fn=self._name_fn,
        ):
            self.add_item(item)
        for item in build_turn_prompt_layout(
            current_player_id=self.current_player_id,
            name_fn=self._name_fn,
        ):
            self.add_item(item)
        _bind(self, "story_write", self._on_write)
        _bind(self, "story_skip", self._on_skip)
        _bind(self, "story_leave_turn", self._on_leave)

    async def _on_write(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '✍️ Write Your Sentence' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if interaction.user.id != self.current_player_id:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return
        modal = StorySentenceModal(self.game_id, self.current_player_id, self.context_text, turn_view=self)
        await interaction.response.send_modal(modal)

    def _skip_now(self) -> None:
        self._skipped = True
        self._submitted_event.set()
        self.stop()

    async def _on_skip(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '⏭️ Skip' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id) and not writer_may_skip(
            interaction.user.id, turn_order=self.turn_order, opened_at=self.opened_at, now=time.time(),
        ):
            if interaction.user.id in self.turn_order:
                unlock = writer_skip_unlock_at(self.opened_at)
                await interaction.response.send_message(
                    f"❌ Only the host or a mod can skip right now — any writer can <t:{unlock}:R>.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("❌ Only the host, a mod, or a writer can skip.", ephemeral=True)
            return
        self._skip_now()
        await interaction.response.send_message("⏩ Player skipped.", ephemeral=True)

    async def _on_leave(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '🚪 Leave' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id
        if uid not in self.turn_order or uid in self._left:
            await interaction.response.send_message("You're not in this story's rotation.", ephemeral=True)
            return
        self._left.add(uid)
        await interaction.response.send_message(
            "✅ You've left the story — you'll be dropped from the rotation after this turn.", ephemeral=True,
        )
        # The current writer leaving is also a skip: nothing is coming.
        if uid == self.current_player_id:
            self._skip_now()


class StoryJoinView(discord.ui.LayoutView):
    """The join lobby. **Paradigm 8 — live board**: rebuilt and re-edited in
    place on every Join/Leave, mirroring a sticky board's shape even though
    the lobby itself is not on the Live Game Boards dial. The row's own
    ``games_active_games`` message is what ``StoryCog.recover_game``
    re-registers this against after a restart, so ``story_join`` /
    ``story_leave`` / ``story_start`` / ``story_htp`` (frozen custom_ids)
    keep working.
    """

    def __init__(
        self, game_id: str, host_id: int, db, bot, cog,
        *,
        guild: "discord.Guild | None" = None,
        host_name: str = "",
        visibility: str = "blind",
        max_sentences: int = 10,
        players: list[int] | None = None,
        start_at: int | None = None,
        art: PanelArt = NO_ART,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.guild = guild
        self.host_name = host_name
        self.visibility = visibility
        self.max_sentences = max_sentences
        self.players: list[int] = list(players or [])
        self.start_at = start_at
        self.art = art
        self._rebuild()

    def _name_fn(self, uid: int) -> str:
        return resolve_name(self.guild, uid)

    def _rebuild(self) -> None:
        self.clear_items()
        for item in build_lobby_layout(
            host_name=self.host_name,
            visibility=self.visibility,
            max_sentences=self.max_sentences,
            player_ids=self.players,
            name_fn=self._name_fn,
            start_at=self.start_at,
            header_url=self.art.header,
            footer_url=self.art.footer,
            title_in_header=self.art.title_in_header,
        ):
            self.add_item(item)
        _bind(self, "story_join", self._on_join)
        _bind(self, "story_leave", self._on_leave)
        _bind(self, "story_start", self._on_start)
        _bind(self, "story_htp", self._on_help)

    async def _on_join(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Join' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id

        def _add(payload):
            add_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _add)
        log.info("%s joined game %s", interaction.user.display_name, self.game_id)

        self.players = payload.get("players", [])
        self._rebuild()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ You've joined!", ephemeral=True)

    async def _on_leave(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Leave' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        uid = interaction.user.id

        def _remove(payload):
            remove_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _remove)
        log.info("%s left game %s", interaction.user.display_name, self.game_id)

        self.players = payload.get("players", [])
        self._rebuild()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ You've left.", ephemeral=True)

    async def _on_start(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Start Story' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        if len(players) < 2:
            await interaction.response.send_message("❌ Need at least 2 writers to start!", ephemeral=True)
            return

        self.stop()
        disable_all(self)
        await interaction.response.edit_message(view=self)

        # Ping joined players
        if interaction.guild:
            mentions = [
                member.mention
                for uid in players
                if (member := interaction.guild.get_member(uid))
            ]
            if mentions:
                assert isinstance(interaction.channel, discord.abc.Messageable)  # games run in text channels
                await interaction.channel.send(
                    f"📖 **Story Builder is starting!** {' '.join(mentions)} — get ready to write!",
                    delete_after=15,
                    allowed_mentions=allow_pings(users=players),
                )

        # The row must stop reading as an open lobby — the start-ping sweep
        # polls state='joining' and a story outlives its countdown. The host
        # stays the row's host: a mod pressing Start on the host's behalf used
        # to become the host for Skip purposes (anon-tail-77).
        await update_game_state(self.db, self.game_id, "playing")
        await self.cog._run_story(
            interaction, self.game_id, payload, interaction.channel, host_id=self.host_id,
        )

    async def _on_help(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["story"], ephemeral=True)


class StoryCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # The turn card this cog owns while a turn is open (Games Global
        # Config → Live Game Boards). Keyed by game_id — one turn is live at
        # a time per story, and a guild can run several stories in different
        # channels at once. The lobby is not on the dial.
        #
        # ``tracked=False``: the game row names the *lobby* message, which
        # recovery re-registers the join view on, and a turn card is deleted
        # the moment the turn is spent — so writing its id into that row
        # would point the busy-check's jump link and ``/games end`` at a
        # message that no longer exists. The card's ids live in memory
        # instead, which is all a card that dies with its turn needs.
        self.live_boards = LiveBoards(bot, self.db, "story board", tracked=False)

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
        # A reload mid-turn must not leave a debounced restick armed
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

    @app_commands.command(name="story", description=play_description("story"))
    @app_commands.describe(
        max_sentences="Total sentences in the story (max 30)",
        visibility="blind = only see previous sentence, full = see whole story",
        starter="Opening sentence (blank = use default)",
        start_in="Countdown before the start, in minutes",
    )
    @app_commands.choices(
        visibility=[
            app_commands.Choice(name="Blind", value="blind"),
            app_commands.Choice(name="Full", value="full"),
        ],
    )
    async def story(
        self,
        interaction: discord.Interaction,
        max_sentences: int = 10,
        visibility: str = "blind",
        starter: str = "",
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info("%s used /games play story in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "story")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "max_sentences": max_sentences,
                "visibility": visibility,
                "starter": starter,
                "start_in": start_in,
            },
        )
        await finish_launch_response(interaction, game_id)

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
        max_sentences = clamp_max_sentences(options.get("max_sentences", 10))
        visibility = options.get("visibility", "blind")
        if visibility not in ("blind", "full"):
            visibility = "blind"
        starter = options.get("starter", "")
        start_epoch = resolve_start_epoch(options)

        payload = {
            "max_sentences": max_sentences,
            "visibility": visibility,
            "starter": starter,
            "players": [],
            "sentences": [],
        }
        if start_epoch:
            payload["start_epoch"] = start_epoch
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "story",
            state="joining",
            payload=payload,
        )

        guild = getattr(channel, "guild", None)

        log.info("Game %s (story) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = StoryJoinView(
            game_id, host_id, self.db, self.bot, self,
            guild=guild,
            host_name=host_name,
            visibility=visibility,
            max_sentences=max_sentences,
            players=[],
            start_at=start_epoch,
            art=await read_lobby_art(self.bot, channel, LOBBY_TITLE),
        )
        self.bot.active_views[game_id] = view

        try:
            msg = await retry_transient(lambda: channel.send(view=view), label="story launch")
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("story launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_story(
        self, interaction, game_id: str, payload: dict, channel, *, host_id: int | None = None,
    ):
        """The turn loop. ``host_id`` is the lobby's host (the row's, not
        whoever pressed Start); a caller without one falls back to the
        payload's legacy ``host_id`` key.

        Pacing (anon-tail-73): a turn is :data:`_TURN_TIMEOUT` seconds; a
        writer who misses two turns in a row (timed out, dismissed the box or
        was skipped) is dropped from the rotation with a notice; a writer who
        pressed Leave is dropped after the turn; an all-miss lap still ends
        the story.
        """
        guild = channel.guild if hasattr(channel, "guild") else None
        if host_id is None:
            host_id = int(payload.get("host_id", 0) or 0)

        players = payload["players"]
        max_sentences = payload.get("max_sentences", 10)
        visibility = payload.get("visibility", "blind")
        starter = resolve_starter(payload.get("starter", ""))

        sentences: list[dict] = [{"author_id": None, "text": starter}]
        payload["sentences"] = sentences
        await update_game_payload(self.db, game_id, payload)

        await channel.send(
            format_story_opening(starter),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        turn_order = build_turn_order(players)

        sentence_count = 1  # starter already counted
        turn_index = 0
        consecutive_skips = 0
        misses: dict[int, int] = {}   # writer -> consecutive missed turns
        left: set[int] = set()        # writers who pressed Leave

        def _name_for(pid: int) -> str:
            return resolve_name(guild, pid)

        while sentence_count < max_sentences and turn_order:
            # Check if game was closed
            if game_id not in self.bot.active_views:
                break

            current_player_id = pick_current_player(turn_order, turn_index)
            current_member = guild.get_member(current_player_id) if guild else None
            player_name = current_member.display_name if current_member else str(current_player_id)

            # Build context for the modal
            context_text = build_context(sentences, visibility)

            mention = current_member.mention if current_member else f"**{player_name}**"
            turn_view = StoryTurnView(
                game_id, host_id, current_player_id, context_text, self.db, self.bot,
                turn_order=turn_order,
                guild=guild,
                sentence_count=sentence_count,
                max_sentences=max_sentences,
                name_resolver=_name_for,
            )

            # The ping and the card are two messages, not one. A sticky board
            # re-posts itself, and a re-post of a message whose content is
            # "@writer — it's your turn" would ping that writer again every
            # time chat buried the card. So the ping goes out once, on its
            # own (Spin the Compliment splits its participant ping the same
            # way), and the card — buttons and all — is what follows the
            # channel bottom. Both are cleared when the turn is spent.
            timeout_min = max(1, _TURN_TIMEOUT // 60)
            ping_text = (
                f"{mention} — it's your turn! You have **{timeout_min} minute{'s' if timeout_min != 1 else ''}** "
                "to write. Click below to start."
            )
            # Bound as defaults, so a retry can never send a later turn's
            # writer or timeout (B023). A lambda, not functools.partial: the
            # mention audit can't see a send inside a partial.
            ping_msg = await retry_transient(
                lambda text=ping_text, member=current_member: channel.send(
                    content=text, allowed_mentions=allow_pings(users=[member]),
                ),
                label="story turn ping",
            )
            turn_msg = await self.live_boards.open(
                game_id, turn_view, channel, guild,
                suffix=f" turn {sentence_count}",
            )

            # Wait for submission, skip, or timeout
            try:
                await asyncio.wait_for(turn_view._submitted_event.wait(), timeout=_TURN_TIMEOUT)
            except asyncio.TimeoutError:
                turn_view._skipped = True

            # The turn is spent, so the panel goes rather than lingering as
            # a dead disabled copy — Price is Right already deletes its host
            # prompt this way. Story used to leave one behind per player per
            # round, so a 5-player 4-round game buried its own story text
            # under twenty exhausted panels.
            #
            # Retire first, and delete what retire hands back: with the dial
            # on, the card may have been re-posted since, and ``turn_msg``
            # would then name a message the restick already removed.
            spent = self.live_boards.retire(turn_view, channel) or turn_msg
            for stale in (ping_msg, spent):
                try:
                    await stale.delete()
                except discord.HTTPException:
                    pass

            # Check if game was closed via the close button
            if game_id not in self.bot.active_views:
                break

            # Writers who pressed Leave during the turn go after it resolves.
            leaving = set(turn_view._left) - left
            for pid in leaving:
                left.add(pid)
                await channel.send(
                    format_leave_notice(_name_for(pid)),
                    delete_after=15,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            if leaving:
                # Persist the leavers: the sweep and ``/games end`` rebuild
                # the roster from the payload (``game_roster._story``) and
                # must drop a never-wrote leaver the way the reveal does.
                payload["left"] = sorted(left)
                await update_game_payload(self.db, game_id, payload)
            drop: set[int] = set(leaving)

            missed = turn_view._skipped and not turn_view._submitted_text
            if missed:
                await channel.send(
                    format_skip_notice(player_name),
                    delete_after=15,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                consecutive_skips += 1
                note_turn_outcome(misses, current_player_id, missed=True)
                if current_player_id not in drop and should_drop_writer(misses, current_player_id):
                    drop.add(current_player_id)
                    await channel.send(
                        format_drop_notice(player_name),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                if drop:
                    # A shrunken rotation starts a fresh lap: the missed turns
                    # were the leaver's, not the writers who remain.
                    consecutive_skips = 0
                turn_order, turn_index = rotation_after_turn(turn_order, turn_index, drop)
                # If every writer in the rotation was skipped, end the story
                if not turn_order or should_end_after_skip(consecutive_skips, len(turn_order)):
                    await channel.send("📖 All writers were skipped — ending the story.")
                    break
                continue

            consecutive_skips = 0  # reset on successful submission
            note_turn_outcome(misses, current_player_id, missed=False)
            new_sentence = turn_view._submitted_text
            assert new_sentence is not None  # not skipped ⇒ a sentence was submitted
            append_sentence(payload, current_player_id, new_sentence)
            sentences = payload["sentences"]
            await update_game_payload(self.db, game_id, payload)

            await channel.send(f"> *{discord.utils.escape_markdown(new_sentence)}*", allowed_mentions=discord.AllowedMentions.none())
            sentence_count += 1
            turn_order, turn_index = rotation_after_turn(turn_order, turn_index, drop)

        # If game was closed by host, skip final reveal
        if game_id not in self.bot.active_views:
            return

        await self._reveal_story(
            channel, game_id, sentences, roster_for_payout(players, sentences, left), guild,
        )

    async def _reveal_story(self, channel, game_id: str, sentences: list, players: list, guild):
        def _name_for(author_id: int) -> str:
            return resolve_name(guild, author_id)

        story_text = assemble_story_text(sentences, max_len=STORY_TEXT_CARD_BUDGET)
        lines = build_attribution_lines(sentences, _name_for)
        attribution_lines, omitted = trim_attribution_lines(lines, ATTRIBUTION_CARD_BUDGET)

        footer_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            footer_line = await payout_footer_line(self.bot, guild.id, "story")

        recap = _card(build_story_recap_layout(
            story_text=story_text,
            player_count=len(players),
            sentence_count=len(sentences),
            attribution_lines=attribution_lines,
            omitted=omitted,
            footer_line=footer_line,
        ))
        await channel.send(view=recap)

        payload = await get_game_payload(self.db, game_id)
        log.info("Game %s ended — %d players", game_id, len(players))
        await end_game(
            self.db, game_id,
            player_count=len(players),
            round_count=len(sentences),
            payload=payload,
            bot=self.bot, player_ids=list(players),
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Recover after a restart.

        The join lobby re-registers cleanly, rebuilt from the payload's own
        state (players, mode, countdown) rather than trusting the message's
        existing body — a Components V2 card is redrawn whole on every press,
        so the view needs that state in hand the moment a press lands, not
        only after one. Once the story is underway, play runs in a blocking
        per-turn loop whose turn messages aren't tracked, so it can't be
        resumed — end it gracefully so players aren't left waiting on a dead
        turn prompt.
        """
        if payload.get("sentences"):
            try:
                await channel.send(
                    "📖 This Story game was interrupted by a bot restart and can't be "
                    "resumed — start a new one with `/games play story`."
                )
            except discord.HTTPException:
                pass
            await end_game(self.db, row["game_id"])
            self.bot.active_views.pop(row["game_id"], None)
            log.info("story game %s was mid-play at restart; ended gracefully.", row["game_id"])
            return True
        game_id = row["game_id"]
        guild = getattr(channel, "guild", None)
        host_id = int(row["host_id"])
        view = StoryJoinView(
            game_id, host_id, self.db, self.bot, self,
            guild=guild,
            host_name=resolve_name(guild, host_id),
            visibility=payload.get("visibility", "blind"),
            max_sentences=payload.get("max_sentences", 10),
            players=payload.get("players", []),
            start_at=payload.get("start_epoch"),
            art=await read_lobby_art(self.bot, channel, LOBBY_TITLE),
        )
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered story game %s (join phase) in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = StoryCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("story")
    play.add_command(cog.story, override=True)
    bot.game_launchers["story"] = cog.launch
    bot.game_recoverers["story"] = cog.recover_game
