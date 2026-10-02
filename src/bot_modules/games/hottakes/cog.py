"""
Hot Takes — game cog.

Anonymous takes are collected in a lobby, then the room rates each one on a
five-step 🧊 → 🔥 scale, one take at a time, and the recap crowns the
hottest. Pure logic lives in ``games/hottakes/logic.py`` and the Components
V2 card builders in ``games/hottakes/layouts.py``; this module keeps only
the Discord glue (slash command, modal, views, the vote loop). Nothing
host-only is on a public card: Start Voting, Cancel Game and ⏭️ Next Take
are on the ephemeral host panel (``/games host``).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

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
    EVENT_TAKE_SUBMITTED,
)
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    update_game_message,
    update_game_payload,
    update_game_state,
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
from bot_modules.games.utils.recovery import start_redrive
from bot_modules.games.utils.send_retry import is_transient, retry_transient
from bot_modules.games.utils.round_pacing import (
    RoundPacing,
    launch_pacing,
)
from bot_modules.games.hottakes.layouts import (
    CANCELLED_TEXT,
    HELP_ID,
    HOST_CANCEL_ID,
    HOST_NEXT_ID,
    HOST_START_ID,
    NEXT_LABEL,
    START_LABEL,
    SUBMIT_ID,
    SUBMIT_LABEL,
    TITLE,
    VOTE_IDS,
    VOTE_SUBMIT_ID,
    VOTE_SUBMIT_LABEL,
    build_host_panel_layout,
    build_lobby_layout,
    build_notice_layout,
    build_recap_layout,
    build_vote_layout,
)
from bot_modules.games.hottakes.logic import (
    DEFAULT_TAKE_SECONDS,
    SELF_VOTE_REFUSAL,
    VOTE_LABELS,
    VOTE_VALUES,
    active_voters,
    add_take,
    build_voting_start_message,
    everyone_has_voted,
    shuffle_takes,
    tally_votes,
    voting_refusal,
)

log = logging.getLogger(__name__)

VOTING_STARTED_REPLY = "Voting has already started."
NOT_VOTING_REPLY = f"Voting hasn't started yet — press **{START_LABEL}** once the takes are in."
CANCEL_AFTER_START_REPLY = "Voting has started — `/games end` closes the game from here."


class SubmitHotTakeModal(discord.ui.Modal, title="Your Hot Take"):
    take = discord.ui.TextInput(
        label="Hot Take",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Type your spiciest opinion here…",
    )

    def __init__(
        self, game_id: str, db, lobby: "HotTakesSubmitView | None" = None, *, queue_mode: bool = False,
    ):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self._lobby = lobby
        self.queue_mode = queue_mode

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Hot Take", channel_name(interaction.channel))

        def _add_take(payload):
            add_take(payload, interaction.user.id, self.take.value)

        payload = await modify_payload(self.db, self.game_id, _add_take)

        if self.queue_mode:
            await interaction.response.send_message(
                "✅ Hot take queued! It will be voted on after the current takes.",
                ephemeral=True,
            )
        else:
            take_count = len(payload.get("takes", []))
            await interaction.response.send_message(
                f"✅ Hot take submitted! Total submissions: {take_count}", ephemeral=True
            )
            # The count is rebuilt from the payload — the embed era read the
            # Submissions field back off the message and edited it in place.
            if self._lobby is not None:
                await self._lobby.show_count(take_count)

        # After the member has been answered — this is best-effort logging and
        # must not spend any of Discord's 3s initial-response budget.
        if interaction.guild:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="hottakes", user=interaction.user,
                event=EVENT_TAKE_SUBMITTED,
                content=self.take.value, label="Hot Take Submission",
                game_id=self.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"queued": self.queue_mode},
            )


class _HostControls:
    """The three host-panel hand-offs both Hot Takes views answer.

    The game moves from the lobby to the vote on its own, so the panel
    carries every control and hands each press to whichever view is live
    (``host_panel.GameHostPanel``). A phase with no use for a control
    answers with the line saying so; the phase that has one overrides.
    """

    host_id: int

    async def start_voting(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(VOTING_STARTED_REPLY, ephemeral=True)

    async def cancel_game(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(CANCEL_AFTER_START_REPLY, ephemeral=True)

    async def next_take(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(NOT_VOTING_REPLY, ephemeral=True)


class HotTakesSubmitView(_HostControls, discord.ui.LayoutView):
    """The submission lobby. **Paradigm 8 — live board**: rebuilt and
    re-edited in place as takes arrive. ``ht_submit`` / ``ht_htp`` are
    frozen custom ids — ``recover_game`` re-registers this view against the
    lobby message after a restart. Start Voting and Cancel Game are not
    here: the host panel calls :meth:`start_voting` and :meth:`cancel_game`.
    """

    def __init__(
        self, game_id: str, host_id: int, db, bot, cog,
        *,
        host_name: str = "Host",
        submission_count: int = 0,
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
        self.submission_count = submission_count
        self.start_at = start_at
        self.art = art
        self.message: discord.Message | None = None
        self._closed = False
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, re-binding its
        buttons — a rebuild adds brand-new ``Button`` instances. The closed
        frame carries none."""
        self.clear_items()
        for item in build_lobby_layout(
            self.host_name,
            self.submission_count,
            start_at=self.start_at,
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

    async def show_count(self, take_count: int) -> None:
        """A take landed: the lobby's count, from the payload."""
        if self._closed:
            return
        self.submission_count = take_count
        await self._redraw()

    async def submit(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, SUBMIT_LABEL, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message(VOTING_STARTED_REPLY, ephemeral=True)
            return
        await interaction.response.send_modal(SubmitHotTakeModal(self.game_id, self.db, lobby=self))

    async def start_voting(self, interaction: discord.Interaction) -> None:
        """Start Voting — the host panel's button lands here."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, START_LABEL, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start voting.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message(VOTING_STARTED_REPLY, ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        takes = payload.get("takes", [])
        # Two takes minimum (anon-tail-72): with one, everyone knows whose it is.
        refusal = voting_refusal(takes)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        payload["takes"] = shuffle_takes(takes)
        await update_game_payload(self.db, self.game_id, payload)
        # The phase is what a restart branches on: 'joining' re-registers this
        # lobby's buttons, anything else resumes the vote. Until 2026-09-04
        # nothing wrote it, so recovery guessed from the take count and
        # force-started voting on any lobby that held a take (anon-tail-66).
        await update_game_state(self.db, self.game_id, "playing")

        await interaction.response.defer()
        # The lobby's closed frame: no Submit button. Edited on the lobby's
        # own message — the press arrived on the ephemeral panel.
        self._closed = True
        self.stop()
        await self._redraw()

        # The lobby's channel, not the press's: a panel press arrives on the
        # ephemeral panel, whose channel is the same room but not the board.
        channel = getattr(self.message, "channel", None) or interaction.channel
        assert channel is not None and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel))

        # A public heads-up that names nobody: takes are anonymous, and
        # @-mentioning the submitters here (as this once did) gave them away.
        # Caught broadly, and this one matters most of the three: it sits
        # *outside* the try below, so a connection that died before it got an
        # HTTP status escaped the narrower except and killed the whole handler
        # before voting was ever started — losing the game with no notice and
        # no classification.
        try:
            await channel.send(
                build_voting_start_message(takes),
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:  # noqa: BLE001 — a dead connection before an HTTP status must not escape and kill the handler before voting starts; see comment above
            log.warning("hottakes: voting heads-up failed in #%s", channel_name(channel))

        try:
            await self.cog._run_voting(
                interaction=interaction,
                game_id=self.game_id,
                host_id=self.host_id,
                host_name=self.host_name,
                channel=channel,
            )
        except Exception as e:
            if is_transient(e):
                # Discord having a bad second is not a broken game. The row
                # holds the takes and the results so far, which is exactly
                # what recover_game re-drives voting from on the next
                # restart; the 24h sweep archives and *pays* it if no restart
                # comes first. Archiving it here did neither — the bare
                # end_game below carries no payload, no bot and no
                # player_ids, so a hiccup cost the room its payout as well as
                # its game (see Clapback game 959cd749, 2026-09-10).
                log.warning(
                    "hottakes game %s hit a transient Discord failure (%s) — "
                    "leaving it live for recovery", self.game_id, e,
                )
                # Caught broadly, not just discord.HTTPException: the failure
                # this whole path exists for ("reset before headers") never
                # makes it to an HTTP status, and a narrower except here would
                # let this courtesy send's own transient failure skip the
                # active_views pop below it.
                try:
                    await channel.send(
                        "⚠️ Discord is having trouble right now, so this game is "
                        "paused — nothing submitted is lost. It'll pick up after "
                        "the next restart, or the host or a mod can close it out "
                        "with `/games end`."
                    )
                except Exception:  # noqa: BLE001 — courtesy send; a narrower except would let its own transient failure skip the active_views pop below it, see comment above
                    log.warning("hottakes: couldn't post the pause notice")
                self.bot.active_views.pop(self.game_id, None)
                return
            log.exception("Failed to start voting for game %s", self.game_id)
            try:
                await channel.send("❌ Something went wrong starting the vote. Game ended.")
            except Exception:  # noqa: BLE001 — courtesy send after a crash; must not mask the crash it's reporting or skip the archive call that follows
                log.warning("hottakes: couldn't post the crash notice")
            # reason= so the archive can tell a crashed game from a played
            # one, as every other game's crash path already does (compliment,
            # nhie, wyr, mlt, and clapback's _cancel_game). This call stays
            # bare of bot=/player_ids= on purpose: a crash archive records but
            # does not pay, repo-wide — see games_system_spec.md, "Which end
            # paths pay". Whether that policy is right is a separate question
            # than this branch.
            await end_game(self.db, self.game_id, reason="crash")
            self.bot.active_views.pop(self.game_id, None)

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["hottakes"], ephemeral=True)

    async def cancel_game(self, interaction: discord.Interaction) -> None:
        """Scrap the lobby before voting starts — host or mod, from the host
        panel, behind the usual confirm popup. Before this the only ways out
        of a lobby that never got going were ``/games end`` or the 24h sweep,
        and a lobby left over a restart sat with dead buttons (anon-tail-66).
        """
        log.info("%s pressed 'Cancel Game' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can cancel the game.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message(CANCEL_AFTER_START_REPLY, ephemeral=True)
            return
        anchor = self.message

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self._cancel(anchor)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    async def _cancel(self, anchor: discord.Message | None) -> None:
        """Turn the lobby into the cancelled notice and archive the row as
        ``cancelled``. A lobby has no roster yet (nobody has voted), so a
        bare ``end_game`` pays nobody and records the takes that were
        collected."""
        self._closed = True
        self.stop()
        if anchor is not None:
            try:
                await anchor.edit(view=card(build_notice_layout(CANCELLED_TEXT)), **V2_EDIT)
            except discord.HTTPException:
                pass
        await end_game(self.db, self.game_id, reason="cancelled")
        self.bot.active_views.pop(self.game_id, None)


class HotTakeVoteView(_HostControls, discord.ui.LayoutView):
    """One take's card and its controls — a ``LiveBoardLayoutView``
    (``game_id``, ``guild``, ``_panel``, ``_board_retired``, ``_rebuild``),
    so ``LiveBoards`` opens, redraws and retires it.

    The card carries what any member may press: the five votes and 📝
    Submit Take. ⏭️ Next Take is on the ephemeral host panel, which calls
    :meth:`next_take` here.
    """

    def __init__(
        self,
        game_id: str,
        host_id: int,
        take_text: str,
        take_num: int,
        total_takes: int,
        db,
        bot,
        host_name: str,
        # Optional only for the brief window between a round's view being
        # constructed and `_run_voting` binding its own `advance` closure to
        # it right after (see there) — no interaction can land in that gap,
        # since the view isn't posted anywhere yet.
        advance_callback: Callable[[discord.Message], Awaitable[None]] | None,
        take_author_id: int | None = None,
        pacing: RoundPacing | None = None,
        expected_voters: "set[int] | None" = None,
        guild: "discord.Guild | None" = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        # Only read by the sticky board wiring, which needs a guild to key a
        # StickyPanel by; the take card names nobody.
        self.guild = guild
        self.host_id = host_id
        self.take_text = take_text
        self.take_num = take_num
        self.total_takes = total_takes
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        # The take's author may not rate their own take (anon-tail-74): in a
        # two-voter room one 🔥 self-vote decided the winner bonus.
        self.take_author_id = take_author_id
        # Per-take pacing (anon-tail-71): the timer that closes the vote, the
        # event Next/force-end set, and the room the take is waiting on — the
        # vote closes itself the moment every one of them has voted.
        self.pacing = pacing or RoundPacing()
        self.expected_voters: set[int] = set(expected_voters or ())
        self.votes: dict[int, int] = {}  # user_id -> 0-4 index
        self._updater = LiveBarUpdater()
        self._closed = False
        self.message: discord.Message | None = None
        # Set when the guild turns on the "keep the board at the bottom"
        # dial (Games Global Config → Live Game Boards): the StickyPanel
        # that owns reposting this take's board, and the flag the cog sets
        # once the take is over — the panel's ``build`` checks that flag,
        # never ``_closed``, which is set one legitimate render *earlier*
        # (the closed final frame, which still has to go out). Both stay
        # None/False for the life of the take when the dial is off.
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False
        # force_end_active_game pokes this alias to wake the vote loop.
        self._advanced_event = self.pacing.advanced
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, then bind whatever
        controls that state shows — a rebuild adds brand-new buttons with
        nothing wired to them; the closed frame shows none."""
        self.clear_items()
        for item in build_vote_layout(
            self.take_text,
            self.take_num,
            self.total_takes,
            self.votes,
            closed=self._closed,
            advance_at=self.pacing.advance_at(),
        ):
            self.add_item(item)
        callbacks: dict[str, Callable[[discord.Interaction], Awaitable[None]]] = {
            custom_id: functools.partial(self._do_vote, idx=idx)
            for idx, custom_id in enumerate(VOTE_IDS)
        }
        callbacks[VOTE_SUBMIT_ID] = self.submit_take
        callbacks[HELP_ID] = self.how_to_play
        bind_present(self, callbacks)

    async def vote_0(self, interaction: discord.Interaction) -> None:
        await self._do_vote(interaction, idx=0)

    async def vote_1(self, interaction: discord.Interaction) -> None:
        await self._do_vote(interaction, idx=1)

    async def vote_2(self, interaction: discord.Interaction) -> None:
        await self._do_vote(interaction, idx=2)

    async def vote_3(self, interaction: discord.Interaction) -> None:
        await self._do_vote(interaction, idx=3)

    async def vote_4(self, interaction: discord.Interaction) -> None:
        await self._do_vote(interaction, idx=4)

    async def _do_vote(self, interaction: discord.Interaction, *, idx: int) -> None:
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This vote is closed.", ephemeral=True)
            return
        if self.take_author_id is not None and interaction.user.id == self.take_author_id:
            await interaction.response.send_message(SELF_VOTE_REFUSAL, ephemeral=True)
            return
        prev = self.votes.get(interaction.user.id)
        self.votes[interaction.user.id] = idx
        label = VOTE_LABELS[idx]
        changed = prev is not None and prev != idx
        msg = f"✅ Voted **{label}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        message = self.message or interaction.message
        assert message is not None  # component interactions always carry their message
        # Everyone the take was waiting on has spoken: close it now rather
        # than sit out the rest of the timer.
        if everyone_has_voted(self.expected_voters, self.votes):
            assert self.advance_callback is not None
            await self.advance_callback(message)
            return
        # Rate-limited, but routed through the board's current location
        # rather than the message this click came from: a restick may have
        # reposted the board between the click and the gate opening.
        await self._updater.schedule_render(lambda: redraw_board(self, message))

    async def submit_take(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, VOTE_SUBMIT_LABEL, channel_name(interaction.channel))
        if self.game_id not in self.bot.active_views:
            await interaction.response.send_message(GAME_ENDED_REPLY, ephemeral=True)
            return
        await interaction.response.send_modal(SubmitHotTakeModal(self.game_id, self.db, queue_mode=True))

    async def next_take(self, interaction: discord.Interaction) -> None:
        """⏭️ Next Take, from the host panel: the timer (or a complete vote)
        closes the take on its own; Next is the host's early close, never
        the only way on. Advances the *board's* message, not the one the
        press came from — a panel press arrives on the ephemeral panel."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, NEXT_LABEL, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can advance.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This take is already over.", ephemeral=True)
            return
        await interaction.response.defer()
        message = self.message or interaction.message
        assert message is not None
        assert self.advance_callback is not None
        await self.advance_callback(message)

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["hottakes"], ephemeral=True)


class HotTakesCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # Every live take board this cog owns (Games Global Config → Live
        # Game Boards). Keyed by game_id, so the board a take opens is the
        # board that take retires — one guild can run several Hot Takes
        # games in different channels at once. The submission phase is not
        # on the dial; only the in-play vote board is.
        self.live_boards = LiveBoards(bot, self.db, "hottakes board")

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
        # A reload mid-take must not leave a debounced restick armed
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

    @app_commands.command(name="hottakes", description=play_description("hottakes"))
    @app_commands.describe(
        start_in="Show a lobby countdown — voting starts in this many minutes (host still clicks Start Voting)",
    )
    async def hottakes(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        # These moved to the dashboard. Passing None is exactly what an
        # omitted option always meant, so the per-guild dial applies.
        take_seconds = None
        log.info("%s used /games play hottakes in #%s", interaction.user.display_name, channel_name(interaction.channel))
        refusal = await refuse_launch(self.db, interaction, "hottakes")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"start_in": start_in, "round_seconds": take_seconds},
        )
        await finish_launch_response(interaction, game_id)
        # Nothing host-only is on the cards, so hand the host their controls
        # now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a Hot Takes game: Start Voting, Cancel Game
        and ⏭️ Next Take on one panel. The host always gets it; anyone else
        needs the same mod standing the controls check (``is_host_or_mod``)."""
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, _HostControls):
            await host_reply(interaction, GAME_ENDED_REPLY)
            return
        if not is_host_or_mod(interaction, view.host_id):
            await host_reply(interaction, NOT_HOST_REPLY)
            return
        panel = GameHostPanel(
            self.bot, game_id, build_host_panel_layout(),
            {HOST_START_ID: "start_voting", HOST_CANCEL_ID: "cancel_game", HOST_NEXT_ID: "next_take"},
            view_type=_HostControls,
        )
        await send_panel(interaction, panel, label="hottakes host panel")

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

        The per-take timer is the launch's ``round_seconds`` (a slash
        ``take_seconds`` or schedule option, even 0), else the dashboard's
        **Seconds per Take** dial, else :data:`DEFAULT_TAKE_SECONDS`; ``0`` is
        host-paced. Hot Takes is a lobby game (``LOBBY_GAME_TYPES``): a
        ``start_in`` stamps ``start_epoch`` for the countdown and the host
        nudge, and the idle-lobby dials and Game Lobby ping apply.
        """
        pacing = await launch_pacing(
            self.db, "hottakes", guild_id, options,
            default_round_seconds=DEFAULT_TAKE_SECONDS,
        )
        start_epoch = pacing.start_epoch
        payload: dict = pacing.stamp({
            "takes": [], "results": [], "participants": [],
            "round_seconds": pacing.round_seconds,
        })
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "hottakes",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )

        log.info("Game %s (hottakes) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        view = HotTakesSubmitView(
            game_id, host_id, self.db, self.bot, self,
            host_name=host_name, start_at=start_epoch,
            art=await read_lobby_art(self.bot, channel, TITLE),
        )
        self.bot.active_views[game_id] = view

        try:
            msg = await retry_transient(
                lambda: channel.send(view=view),
                label="hottakes launch",
            )
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("hottakes launch lacked send perms in channel %s", channel.id)
            return None
        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_voting(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
        resume: bool = False,
    ):
        # On resume after a restart, seed from persisted results so already-voted
        # takes are skipped; the take whose round was interrupted is re-voted.
        results: list[dict] = []
        if resume:
            payload = await get_game_payload(self.db, game_id)
            results = list(payload.get("results", []))
            processed = len(results)
        else:
            processed = 0

        view: HotTakeVoteView | None = None
        while True:
            payload = await get_game_payload(self.db, game_id)
            if game_id not in self.bot.active_views:
                return

            all_takes = payload.get("takes", [])
            if processed >= len(all_takes):
                break

            take_data = all_takes[processed]
            processed += 1
            take_text = take_data["text"]
            take_num = processed
            total_takes = len(all_takes)
            take_author = take_data.get("user_id")

            # Per-take pacing: the dial's timer, and the room the take waits
            # on — every submitter plus everyone who has voted so far, minus
            # the author, who cannot rate their own take.
            take_seconds = int(payload.get("round_seconds", 0) or 0)
            pacing = RoundPacing(round_seconds=take_seconds, opened_at=time.time())
            expected = active_voters(all_takes, results, exclude=take_author)

            # Built with a placeholder callback and wired up right below:
            # `advance` binds *this* take's view as a default argument
            # instead of closing over the loop's `view` name (B023), so a
            # stale call — a duplicate click, or the timer racing a
            # vote-complete close — can never run against whichever take the
            # loop has since moved on to. Safe to leave the callback unset
            # for this instant: the view isn't posted (and so can't be
            # clicked) until further down.
            view = HotTakeVoteView(
                game_id=game_id,
                host_id=host_id,
                take_text=take_text,
                take_num=take_num,
                total_takes=total_takes,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=None,
                take_author_id=int(take_author) if take_author is not None else None,
                pacing=pacing,
                expected_voters=expected,
                guild=getattr(channel, "guild", None),
            )

            async def advance(
                message: discord.Message,
                _view: HotTakeVoteView = view,
                _take: str = take_text,
                _num: int = take_num,
                _taker_id: Any = take_author,
            ) -> None:
                if _view._closed:
                    return
                _view._closed = True

                vote_counts, avg, std = tally_votes(_view.votes)
                voters = list(_view.votes.keys())

                result_entry = {
                    "text": _take,
                    "counts": vote_counts,
                    "avg": avg,
                    "std": std,
                    "voters": voters,
                    "author": _taker_id,
                }
                results.append(result_entry)

                # Persist incrementally so mid-game close doesn't lose prior results
                def _save_result(game_payload, _entry=result_entry):
                    game_payload.setdefault("results", []).append(_entry)
                await modify_payload(self.db, game_id, _save_result)

                # Retire first: that cancels any pending restick, so
                # nothing can repost the take *over* its own closing frame,
                # and it hands back wherever the board actually ended up —
                # ``message`` may be the one captured when the take opened,
                # which a restick can have moved since. ``None`` back means
                # the dial was off, so ``message`` is still right.
                target = self.live_boards.retire(_view, channel) or message
                # The closed frame is the take's record: the final bars and
                # no controls, so nothing on it can be pressed.
                _view._rebuild()
                try:
                    await target.edit(view=_view, **V2_EDIT)
                except discord.HTTPException:
                    pass
                _view.pacing.advanced.set()

            view.advance_callback = advance
            self.bot.active_views[game_id] = view

            # Post this take's board — sticky (reposting to the bottom of
            # the channel as chat buries it) for the length of the vote when
            # the guild's dial is on, a plain send otherwise. No ``msg`` to
            # hand over: each take posts a *fresh* board and leaves the take
            # that just closed on screen as its record, so the panel has
            # nothing of its own to move yet. Still wrapped in
            # retry_transient — a 503 on the send is what that is for, and
            # a placement that never posted is safe to re-attempt.
            # partial, not a lambda: ``view`` is declared optional for the
            # advance closure above, and a lambda body would be checked
            # against that declaration rather than the value it just got.
            msg = await retry_transient(
                functools.partial(
                    self.live_boards.open,
                    game_id,
                    view,
                    channel,
                    getattr(channel, "guild", None),
                    suffix=f" take {take_num}",
                ),
                label=f"hottakes take {take_num}/{total_takes}",
            )
            view.message = msg
            # The timer closes the take unless Next, a complete vote, or a
            # force-end gets there first (round_pacing's wait_for pattern).
            closer: Callable[[], Awaitable[None]] = functools.partial(advance, msg)
            pacing.start_timer(closer)

            await pacing.advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(1)

        # If the game was already closed by the host, skip final results
        if game_id not in self.bot.active_views:
            return

        # Results were saved incrementally in advance(); just read final state
        payload = await get_game_payload(self.db, game_id)

        if processed > 0:
            await self._post_recap(channel, payload)
        # Roster = everyone who voted or authored a take; the winning take's
        # author may not have voted, so a voters-only set would drop their bonus.
        participants = sorted(
            {v for r in results for v in r.get("voters", [])}
            | {r["author"] for r in results if r.get("author") is not None}
        )
        await end_game(
            self.db, game_id,
            player_count=len(participants),
            round_count=processed,
            payload=payload,
            bot=self.bot, player_ids=participants,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def _post_recap(self, channel, payload: dict) -> None:
        results = payload.get("results", [])
        guild = getattr(channel, "guild", None)
        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "hottakes")
        items = build_recap_layout(results, payout_line=payout_line)
        if items is None:
            return
        try:
            await channel.send(view=card(items))
        except discord.HTTPException:
            log.info("hottakes recap not delivered in #%s", channel_name(channel))

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Bring a game back after a restart, by phase.

        A lobby (row state ``joining``, no results yet) gets its submit view
        re-registered on the anchor message, the way WYR and Story do — the
        host still presses Start Voting, on the host panel. A lobby still an
        embed on screen (posted before the conversion) is redrawn as a card
        at once. Until 2026-09-04 the phase was never recorded and recovery
        keyed on the take count alone: an empty lobby was skipped (dead
        buttons until the sweep) and a lobby holding any take was re-driven
        straight into voting (platform-29, anon-tail-66). A row with results
        but still marked ``joining`` predates the phase write and is treated
        as voting underway — results are only written during a vote.

        Voting underway re-drives the loop: completed takes live in
        payload["results"]; the take being voted on at crash time can't be
        reconstructed (live votes aren't persisted), so we retire the stale
        message and re-vote that take. The re-driven loop seeds results from
        the payload and continues with the remaining takes.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        takes = payload.get("takes", [])
        results = payload.get("results", [])
        guild = getattr(channel, "guild", None)
        host_name = await host_display(self.bot, guild, host_id)
        if row["state"] == "joining" and not results:
            view = HotTakesSubmitView(
                game_id, host_id, self.db, self.bot, self,
                host_name=host_name,
                submission_count=len(takes),
                start_at=extract_start_epoch(payload),
                art=await read_lobby_art(self.bot, channel, TITLE),
            )
            view.message = message
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            await redraw_embed_era_lobby(view, message)
            log.info(
                "Recovered hottakes game %s (lobby, %d takes) in #%s",
                game_id, len(takes), getattr(channel, "name", channel.id),
            )
            return True
        if not takes or len(results) >= len(takes):
            return False  # nothing left to resume; cleanup loop will archive it
        await start_redrive(
            self.bot, game_id, message,
            self._run_voting(
                interaction=None, game_id=game_id, host_id=host_id,
                host_name=host_name, channel=channel, resume=True,
            ),
            channel=channel, log_label=f"hottakes game {game_id} (re-driving voting)",
        )
        return True


async def setup(bot: "Bot"):
    cog = HotTakesCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("hottakes")
    play.add_command(cog.hottakes, override=True)
    bot.game_launchers["hottakes"] = cog.launch
    bot.game_recoverers["hottakes"] = cog.recover_game


# Re-export VOTE_VALUES so any tests / external callers that imported it
# from this module continue to work.
__all__ = [
    "HotTakesCog",
    "HotTakesSubmitView",
    "HotTakeVoteView",
    "SubmitHotTakeModal",
    "VOTE_LABELS",
    "VOTE_VALUES",
    "setup",
]
