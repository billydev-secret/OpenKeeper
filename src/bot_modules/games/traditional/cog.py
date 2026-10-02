import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401
    from bot_modules.core.sticky import StickyPanel  # noqa: F401

import discord

from bot_modules.core.utils import is_host_or_mod
from discord import app_commands
from discord.ext import commands

from bot_modules.core.layout import bind, card, disable_all
from bot_modules.core.sticky import PanelContent
from bot_modules.core.branding_service import PanelArt
from bot_modules.games.board_sticky_service import (
    LiveBoards,
    LivePanels,
    redraw_board,
)
from bot_modules.games.bottom_bar_service import (
    make_bar_panel,
    # aliased: the view's own method is called ``retire_bar`` too, and the
    # bare name inside it would read as recursion to anyone skimming.
    retire_bar as retire_bar_panel,
)
from bot_modules.no_contact.service import no_contact_partners
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    ConfirmCloseView,
    create_game,
    end_game,
    get_game_options,
    get_game_payload,
    modify_payload,
    update_session,
    channel_name,
)
from bot_modules.games.utils.host_panel import (
    GAME_ENDED_REPLY,
    NOT_HOST_REPLY,
    HostPanelView,
    reply as host_reply,
)
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.lobby_art import NO_ART, read_lobby_art
from bot_modules.games.traditional.layouts import (
    ASK_ID,
    build_host_panel_layout,
    CAT_EMOJI,
    END_ID,
    HELP_ID,
    NSFW_DARE_ID,
    NSFW_TRUTH_ID,
    SFW_DARE_ID,
    SFW_TRUTH_ID,
    TITLE,
    WRITE_OWN_ID,
    build_question_layout,
    build_recap_layout,
    build_room_layout,
    question_mentions,
)
from bot_modules.games.utils.question_source import (
    channel_allows_nsfw,
    get_traditional_question,
)
from bot_modules.games.traditional.logic import (
    BAR_ID_KEY,
    BAR_TEXT,
    CAT_LABELS,
    IDLE_MINUTES_KEY,
    all_asked,
    all_asked_notice,
    category_allowed,
    clamp_idle_minutes,
    filter_nsfw_prefs,
    idle_close_notice,
    idle_seconds_left,
    record_asked,
    select_next_question_target,
    toggle_pref,
    touch_activity,
)

log = logging.getLogger(__name__)

NO_PLAYERS_REPLY = "No players have joined yet!"
IDLE_CLOSE_REASON = "idle"

#: An edit that lands on a message which may still carry the pre-conversion
#: embed card — a room recovered after this shipped, or an already-open room
#: whose board hasn't redrawn yet — has to null every embed-era field or the
#: old embed rides along beside the new view (``core/layout.py`` module
#: docstring; the pattern Risky Rolls introduced first).
_V2_EDIT: dict = {"content": None, "embed": None, "attachments": []}


class AskQuestionModal(discord.ui.Modal):
    """The host's question for one player.

    ``bank_default`` pre-fills the box with a bank question (Ask Question);
    the host can send it as-is, edit it, or replace it. Write My Own opens
    the same modal empty.
    """

    question = discord.ui.TextInput(
        label="Your Question",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(
        self, game_id: str, db, channel, host_id: int, bot, target_id: str,
        target_name: str, category: str, bank_default: str | None = None,
    ):
        super().__init__(title=f"{CAT_LABELS[category]} for {target_name}"[:45])
        self.game_id = game_id
        self.db = db
        self.channel = channel
        self.host_id = host_id
        self.bot = bot
        self.target_id = target_id
        self.target_name = target_name
        self.cat = category
        self.bank_default = bank_default
        if bank_default:
            self.question.default = bank_default[:500]

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, self.title, channel_name(interaction.channel))
        text = self.question.value
        bank_default = self.bank_default
        completed: dict[str, int] = {}

        # One locked read-modify-write: a category toggle landing while the
        # modal was open is kept, not overwritten (trivia-tail-96).
        def _record(payload: dict) -> None:
            # The 0 -> 1 transition, read inside the same lock that writes
            # it: the first question of the room is what unsticks the board.
            if not payload.get("asked"):
                completed["first"] = 1
            record_asked(payload, self.target_id, self.cat, text)
            if bank_default:
                used = payload.setdefault("bank_used", [])
                if bank_default not in used:
                    used.append(bank_default)
                if text.strip() == bank_default.strip():
                    payload["bank_asked"] = payload.get("bank_asked", 0) + 1
            touch_activity(payload, time.time())
            if all_asked(payload.get("prefs", {}), payload.get("asked", {})):
                completed["all"] = 1

        payload = await modify_payload(self.db, self.game_id, _record)
        # Defer before the channel send, not after: posting the card is an
        # API call, and a modal submit has three seconds to be answered.
        await interaction.response.defer()

        # Names the target exactly once: the raw ping when the player is a
        # cached member (so the card itself fires their notification — a V2
        # message has no ``content=`` to carry a ping separately from the
        # card body), a plain resolved name otherwise.
        target_member = interaction.guild.get_member(int(self.target_id)) if interaction.guild else None
        target_id = target_member.id if target_member else None

        await self.channel.send(
            view=card(build_question_layout(
                self.cat, text, target_id=target_id, target_name=self.target_name,
            )),
            allowed_mentions=question_mentions(target_id),
        )
        if "all" in completed:
            # Told to the presser alone. The room is reading the card this
            # landed behind, and the only person with anything left to do is
            # whoever can press End Game.
            await interaction.followup.send(all_asked_notice(), ephemeral=True)

        view = self.bot.active_views.get(self.game_id)
        if view:
            if "first" in completed:
                # The handover: the board stops following the chat and the
                # thin opt-in strip starts, so a latecomer still has the four
                # buttons in reach once question cards fill the channel.
                view.unstick_board(self.channel)
                await view.open_bar(self.channel)
            await view.refresh_board(interaction.guild, payload)
            view.arm_idle(payload)


class TraditionalBottomView(discord.ui.View):
    """The opt-in strip that follows the chat once questions start.

    Every category button delegates to the room card's own handler, so a
    press here and a press on the card are the same press: the same age gate
    on the NSFW pair, the same single-choice rule, the same board redraw and
    the same ephemeral reply. The strip holds no state of its own — losing it
    (a failed re-post, a deleted message) costs a shortcut, never a game.

    All four categories are offered whatever the channel is, exactly as the
    room card offers them; pressing an NSFW one outside an age-restricted
    channel gets the card's refusal rather than a button that was never there.

    Stays a classic ``discord.ui.View``: it was never an embed (always plain
    ``content=BAR_TEXT``, see ``game_bottom_bar_service``), so nothing about
    it needed converting.
    """

    def __init__(self, host_view: "TraditionalHostView", lobby_url: str):
        super().__init__(timeout=None)
        self.host_view = host_view
        # Last of the five, after the four decorated buttons above.
        self.add_item(discord.ui.Button(
            label="Jump to Lobby",
            emoji="↗️",
            style=discord.ButtonStyle.link,
            url=lobby_url,
        ))

    async def _press(
        self, interaction: discord.Interaction, button: discord.ui.Button, category: str
    ) -> None:
        log.info(
            "%s pressed '%s' (bottom bar) in #%s",
            interaction.user.display_name, button.label, channel_name(interaction.channel),
        )
        await self.host_view._toggle_pref(interaction, category)

    @discord.ui.button(label="SFW Truth", emoji=CAT_EMOJI["sfw_truth"], style=discord.ButtonStyle.secondary, custom_id="tod_bar_sfw_truth")
    async def sfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, button, "sfw_truth")

    @discord.ui.button(label="SFW Dare", emoji=CAT_EMOJI["sfw_dare"], style=discord.ButtonStyle.secondary, custom_id="tod_bar_sfw_dare")
    async def sfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, button, "sfw_dare")

    @discord.ui.button(label="NSFW Truth", emoji=CAT_EMOJI["nsfw_truth"], style=discord.ButtonStyle.secondary, custom_id="tod_bar_nsfw_truth")
    async def nsfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, button, "nsfw_truth")

    @discord.ui.button(label="NSFW Dare", emoji=CAT_EMOJI["nsfw_dare"], style=discord.ButtonStyle.secondary, custom_id="tod_bar_nsfw_dare")
    async def nsfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, button, "nsfw_dare")


class TraditionalHostView(discord.ui.LayoutView):
    """The room board — join screen and working board in one persistent
    Components V2 card (``games_traditional.layouts.build_room_layout``,
    paradigm 8 — live board). Doubles as the ``LiveBoardLayoutView`` the
    sticky board machinery drives (``game_id``, ``guild``, ``_panel``,
    ``_board_retired``, ``_rebuild``)."""

    def __init__(
        self, game_id: str, host_id: int, db, bot,
        *, boards: LiveBoards | None = None, bars: LivePanels | None = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        #: The cog's board holder. ``None`` only where this view is built on
        #: its own (tests): a room whose board never went through
        #: ``LiveBoards`` can never have been sticky in the first place.
        self._boards = boards
        #: The cog's bar holder, separate from the board's because the bar is
        #: not on the Live Game Boards dial. ``None`` where this view is built
        #: on its own (tests), which simply means no bar is ever posted.
        self._bars = bars
        self._message: discord.Message | discord.PartialMessage | None = None
        self._idle_task: asyncio.Task | None = None
        self._closed = False
        # --- Render state, cached for the dial ----------------------------
        #
        # ``_rebuild`` is called from ``core.sticky`` off a plain gateway
        # event: no interaction, no guild handed in, nothing to await. So
        # every input the card needs lives here, refreshed by the one path
        # that already had to redraw the board anyway.
        self.guild: discord.Guild | None = None
        self._host_name = "Host"
        self._names: dict[str, str] = {}
        self._payload: dict = {}
        self._single_choice = False
        self.art: PanelArt = NO_ART
        #: The room's one message is the join screen until the first render
        #: of the working board, so a repost before anyone joins has to put
        #: the joining instructions back rather than a board of zeroes.
        self._lobby_card = True
        self._panel: "StickyPanel | None" = None
        self._board_retired = False
        self._rebuild()

    async def _get_payload(self) -> dict:
        return await get_game_payload(self.db, self.game_id)

    def _resolve_names(self, guild: discord.Guild | None, payload: dict) -> dict[str, str]:
        if not guild:
            return {}
        names: dict[str, str] = {}
        for uid in payload.get("prefs", {}):
            member = guild.get_member(int(uid))
            if member:
                names[uid] = member.display_name
        return names

    def _rebuild(self) -> None:
        """Redraw the card from cached state only.

        Called with nothing to await — see ``game_board_sticky_service`` — so
        every input lives on ``self``, cached by :meth:`refresh_board`. A
        rebuild adds brand-new buttons, so the callbacks are re-bound every
        call.
        """
        self.clear_items()
        for item in build_room_layout(
            self._host_name, self._payload,
            lobby=self._lobby_card, names=self._names,
            single_choice=self._single_choice,
            header_url=self.art.header, footer_url=self.art.footer,
            title_in_header=self.art.title_in_header,
        ):
            self.add_item(item)
        bind(self, SFW_TRUTH_ID, self.sfw_truth)
        bind(self, SFW_DARE_ID, self.sfw_dare)
        bind(self, NSFW_TRUTH_ID, self.nsfw_truth)
        bind(self, NSFW_DARE_ID, self.nsfw_dare)
        bind(self, HELP_ID, self.how_to_play)

    async def refresh_board(self, guild: discord.Guild | None, payload: dict):
        """Redraw the room board wherever it currently is.

        Caches every render input first, so a repost that happens between two
        presses draws the same card this did. A ``None`` guild leaves the
        names as they were rather than blanking them.
        """
        if guild is not None:
            self.guild = guild
            host_member = guild.get_member(self.host_id)
            self._host_name = host_member.display_name if host_member else "Host"
            self._names = self._resolve_names(guild, payload)
        self._payload = payload
        self._lobby_card = False
        await redraw_board(self, self._message)

    # --- The Live Game Boards dial ----------------------------------------
    #
    # The room board is sticky for the first stretch of the room only. While
    # people are still picking categories it is the thing to keep in reach;
    # once the first question lands the question cards are the thread of the
    # game, and a board that went on jumping past them would bury them one
    # at a time.

    def unstick_board(self, channel=None) -> None:
        """First question asked: let the board settle where it is.

        Not a retire — the board carries on rendering for the rest of the
        room. It just stops following the channel.
        """
        if self._boards is None:
            return
        moved = self._boards.unstick(self, channel)
        if moved is not None:
            # A restick may have moved the board since launch, so every
            # later edit has to aim at where it actually ended up.
            self._message = moved

    def retire_board(self, channel=None) -> discord.PartialMessage | None:
        """Room closing: stop the board and refuse to render it again."""
        self._board_retired = True
        if self._boards is None:
            return None
        return self._boards.retire(self, channel)

    # --- The opt-in bar -----------------------------------------------------
    #
    # Posted at the first question — the same moment the board stops
    # following — and taken down when the room closes. Not on the Live Game
    # Boards dial; see the note in ``games_traditional.logic``.

    def _attach_bar(self, bar_view: TraditionalBottomView) -> "StickyPanel | None":
        """Build this room's bar panel and hand it to the cog's holder.

        Registering it is what puts it on the gateway forwarders and the
        unload sweep. Does not post it — ``open_bar`` places a new bar, and
        recovery adopts one that is already on screen.
        """
        if self._bars is None:
            return None

        async def _build(_guild: discord.Guild) -> PanelContent:
            return PanelContent(content=BAR_TEXT, view=bar_view, signature=BAR_TEXT)

        panel = make_bar_panel(
            self.bot,
            self.game_id,
            # Named for the room, not the game type: two Truth or Dare games
            # can be live in two channels, and this is the only place an
            # admin sees which one the bot cannot post in.
            name=f"traditional bar {self.game_id}",
            id_key=BAR_ID_KEY,
            build=_build,
        )
        self._bars.add(self.game_id, panel)
        return panel

    async def open_bar(self, channel) -> None:
        """Post the opt-in strip and start it following the chat.

        Best-effort and idempotent: the first question has already gone out,
        the room card still carries the same four buttons, and a channel the
        bot cannot post in must not take the game down with it. A failed
        placement drops the panel again so the listener stops watching for a
        bar that was never posted.
        """
        if self._bars is None or self._closed or self.game_id in self._bars:
            return
        guild = getattr(channel, "guild", None) or self.guild
        if guild is None or self._message is None:
            return
        bar_view = TraditionalBottomView(self, self._message.jump_url)
        panel = self._attach_bar(bar_view)
        if panel is None:
            return
        try:
            msg = await panel.place(guild, channel)
        except Exception:  # noqa: BLE001 — place() can raise from panel build or its db save, not just Discord; best-effort per docstring, must not take the game down
            log.warning(
                "traditional: could not post the opt-in bar for %s",
                self.game_id, exc_info=True,
            )
            msg = None
        if msg is None:
            self._bars.release(self.game_id)
            return
        self.bot.add_view(bar_view, message_id=msg.id)

    async def retire_bar(self, channel=None) -> None:
        """Take the opt-in bar down for good, through the shared teardown.

        What ``unpost`` does, and why the release happens before the guild
        check, is documented on ``game_bottom_bar_service.retire_bar``.

        The two guards here are this game's own. ``_bars`` can be ``None`` on
        a view rebuilt by crash recovery before the cog handed it a holder;
        and *channel* is whatever the caller happened to hold — a mod's
        ``/games end`` passes the channel it resolved, the idle close passes
        nothing — so the view's own guild is the fallback.
        """
        if self._bars is None:
            return
        guild = getattr(channel, "guild", None) or self.guild
        await retire_bar_panel(self._bars, self.game_id, guild, label="traditional")

    async def _update_board(self, interaction: discord.Interaction, payload: dict):
        await self.refresh_board(interaction.guild, payload)

    # --- Idle close -------------------------------------------------------
    #
    # The room ends itself after the dashboard's quiet window (payload
    # ``idle_minutes``, 0 = off). Every press re-arms it through arm_idle;
    # after a restart recover_game re-arms from the stored last_activity.

    def arm_idle(self, payload: dict) -> None:
        """(Re)start the idle countdown from the payload's last activity."""
        if self._closed:
            return
        current = asyncio.current_task()
        if self._idle_task is not None and self._idle_task is not current:
            self._idle_task.cancel()
        self._idle_task = None
        left = idle_seconds_left(payload, time.time())
        if left is None:
            return
        self._idle_task = asyncio.create_task(self._idle_wait(left))

    async def _idle_wait(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        await self._idle_fire()

    async def _idle_fire(self) -> None:
        """The window ran out: re-check against the stored payload (a press
        may have landed on another worker), then close with the recap."""
        if self._closed:
            return
        try:
            payload = await self._get_payload()
        except Exception:
            log.exception("traditional idle check failed for %s", self.game_id)
            return
        if not payload:
            return  # already archived elsewhere
        left = idle_seconds_left(payload, time.time())
        if left is None:
            return
        if left > 0:
            self.arm_idle(payload)
            return
        minutes = clamp_idle_minutes(payload.get(IDLE_MINUTES_KEY), default=0)
        channel = getattr(self._message, "channel", None)
        if channel is None:
            log.warning("traditional idle close for %s has no channel to post in", self.game_id)
        log.info("Game %s (traditional) idle for %d minutes — closing", self.game_id, minutes)
        await self._do_close(
            None, game_msg=self._message, channel=channel,
            note=idle_close_notice(minutes), reason=IDLE_CLOSE_REASON,
        )

    # --- Player preference toggles ---
    #
    # Bound to the room card's own buttons in ``_rebuild`` (and delegated to
    # from the opt-in bar's decorated ones), rather than decorated
    # themselves: a ``LayoutView`` gets its behaviour wired after its items
    # are built (``core.layout.bind``, guide § Code conventions).

    async def sfw_truth(self, interaction: discord.Interaction):
        log.info("%s pressed 'SFW Truth' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "sfw_truth")

    async def sfw_dare(self, interaction: discord.Interaction):
        log.info("%s pressed 'SFW Dare' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "sfw_dare")

    async def nsfw_truth(self, interaction: discord.Interaction):
        log.info("%s pressed 'NSFW Truth' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "nsfw_truth")

    async def nsfw_dare(self, interaction: discord.Interaction):
        log.info("%s pressed 'NSFW Dare' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "nsfw_dare")

    async def _toggle_pref(self, interaction: discord.Interaction, category: str):
        # The room card's own buttons are disabled when the game ends, so
        # this guard is for the *bar*: a strip that outlived its room (a
        # cleanup that could not delete the message, a channel the bot lost
        # Manage Messages in) would otherwise write a preference into a dead
        # game and answer as though it worked.
        if self._closed:
            await interaction.response.send_message(
                "❌ That game has ended.", ephemeral=True,
            )
            return
        # NSFW prompts ride Discord's own age gate, never a bot-side toggle.
        # Gating the preference is what keeps NSFW content out of a SFW
        # channel: every serve path draws only from opted-in categories.
        if not category_allowed(category, channel_allows_nsfw(interaction.channel)):
            await interaction.response.send_message(
                "❌ NSFW categories are only available in age-restricted channels.",
                ephemeral=True,
            )
            return
        user_id = interaction.user.id
        action_holder: dict[str, str] = {}

        def _do_toggle(payload):
            single_choice = bool(payload.get("single_choice", False))
            action_holder["action"] = toggle_pref(
                payload, user_id, category, single_choice=single_choice
            )
            touch_activity(payload, time.time())

        payload = await modify_payload(self.db, self.game_id, _do_toggle)
        await self._update_board(interaction, payload)
        self.arm_idle(payload)
        action = action_holder["action"]
        if action == "switched":
            msg = f"Switched to **{CAT_LABELS[category]}**."
        else:
            msg = f"**{CAT_LABELS[category]}** {action} from your preferences."
        await interaction.response.send_message(msg, ephemeral=True)

    # --- Host controls ---

    async def _pick_target(self, interaction: discord.Interaction) -> tuple[str, str, str] | None:
        """Choose who answers next: ``(user_id, category, display_name)``.

        Shared by Ask Question and Write My Own. Replies for the caller and
        returns None when there is nobody to ask. Applies, in order: the
        host/mod gate; the channel's age-gate on every player's preferences
        (a room that lost its age-restriction mid-game stops serving NSFW at
        once — safety-sweep-10); and the presser's no-contact partners. Once
        every pair has been asked there is nobody left to pick — passes were
        removed 2026-09-20, so this reports the ordinary "all asked" reply
        instead of rolling the game onto a second pass.
        """
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can ask questions.", ephemeral=True)
            return None
        payload = await self._get_payload()
        prefs = filter_nsfw_prefs(payload.get("prefs", {}), channel_allows_nsfw(interaction.channel))
        asked = payload.get("asked", {})
        if not any(prefs.values()):
            await interaction.response.send_message(NO_PLAYERS_REPLY, ephemeral=True)
            return None

        # The no-contact gate. The bot picks who answers and the presser writes
        # a directed question at them, so the two must not be a blocked pair.
        # Keyed on the presser, not the host — a mod can ask too. When the
        # only open player is the blocked partner the picker reports
        # exhaustion and the presser sees the ordinary "all asked" reply.
        guild_id = interaction.guild_id or 0
        actor_id = interaction.user.id
        excluded = await asyncio.to_thread(
            no_contact_partners, self.bot.ctx.db_path, guild_id, actor_id
        )
        choice = select_next_question_target(prefs, asked, excluded=excluded)
        if choice is None:
            await interaction.response.send_message(all_asked_notice(), ephemeral=True)
            return None

        chosen_uid, chosen_cat = choice
        member = interaction.guild.get_member(int(chosen_uid)) if interaction.guild else None
        # ``User <id>``, the name resolver's own fallback, never a bare number:
        # this name is the question card's "For …" line when the player is
        # no longer cached (style guide § Mentions and names).
        chosen_name = member.display_name if member else f"User {chosen_uid}"
        return chosen_uid, chosen_cat, chosen_name

    async def ask_question(self, interaction: discord.Interaction):
        """Pick a player and open the question box pre-filled from the bank.

        The bank is the default path (trivia-tail-89 / discovery-9): the host
        can send the drawn question, edit it, or type over it. An empty bank
        for that category opens the box empty, the way Write My Own does.
        """
        log.info("%s pressed 'Ask Question' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        target = await self._pick_target(interaction)
        if target is None:
            return
        chosen_uid, chosen_cat, chosen_name = target
        payload = await self._get_payload()
        used: list[str] = list(payload.get("bank_used", []))
        bank_default = await get_traditional_question(self.db, chosen_cat, exclude=used)
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.host_id, self.bot,
            target_id=chosen_uid, target_name=chosen_name, category=chosen_cat,
            bank_default=bank_default,
        )
        await interaction.response.send_modal(modal)

    async def write_own(self, interaction: discord.Interaction):
        """Pick a player and open an empty question box."""
        log.info("%s pressed 'Write My Own' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        target = await self._pick_target(interaction)
        if target is None:
            return
        chosen_uid, chosen_cat, chosen_name = target
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.host_id, self.bot,
            target_id=chosen_uid, target_name=chosen_name, category=chosen_cat,
        )
        await interaction.response.send_modal(modal)

    async def how_to_play(self, interaction: discord.Interaction):
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["traditional"], ephemeral=True)

    async def end_game_button(self, interaction: discord.Interaction):
        """Close the game, post the recap, and pay the room.

        Truth or Dare shipped without this button, so ``_do_close`` — the only
        call site that passes ``bot=``/``player_ids=`` to ``end_game`` — was
        unreachable and no game ever paid out.
        """
        log.info("%s pressed 'End Game' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can end the game.", ephemeral=True)
            return

        channel = interaction.channel

        async def _confirmed(confirm_interaction: discord.Interaction) -> None:
            # Close through the game message, not the confirm interaction —
            # ConfirmCloseView has already responded to that one.
            await self._do_close(confirm_interaction, game_msg=self._message, channel=channel)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=view, ephemeral=True,
        )

    async def _do_close(
        self, interaction: discord.Interaction | None, game_msg=None, channel=None,
        *, note: str | None = None, reason: str | None = None,
    ):
        """Post the recap, pay the room, archive the row.

        Reached from End Game (``interaction`` in hand, no ``note``) and from
        the idle close (no interaction; ``note`` says why the recap appeared
        and ``reason`` lands in the archived payload).
        """
        if self._closed:
            return
        self._closed = True
        # The board stops reposting and refuses to render again before the
        # final frame goes out — and that frame goes wherever a restick left
        # the board, not to the id the room opened with.
        close_channel = channel or getattr(game_msg, "channel", None)
        moved = self.retire_board(close_channel)
        if moved is not None:
            game_msg = moved
        await self.retire_bar(close_channel)
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._idle_task = None

        payload = await self._get_payload()
        participants = payload.get("participants", [])
        asked = payload.get("asked", {})
        total_q = len(asked)

        guild = (interaction.guild if interaction else None) or getattr(channel, "guild", None)
        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "traditional")
        recap = card(build_recap_layout(payload, note=note, payout_line=payout_line))

        self.stop()
        disable_all(self)

        if game_msg:
            try:
                await game_msg.edit(**_V2_EDIT, view=self)
            except discord.HTTPException:
                pass
            if channel is not None:
                try:
                    await channel.send(view=recap)
                except discord.HTTPException:
                    log.info("traditional recap not delivered for %s", self.game_id)
        elif interaction is not None:
            await interaction.response.edit_message(**_V2_EDIT, view=self)
            await interaction.followup.send(view=recap)

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(self.db, self.game_id, player_count=len(participants), round_count=total_q, payload=payload,
                       bot=self.bot, player_ids=participants, reason=reason)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class TraditionalHostPanel(HostPanelView):
    """The host's ephemeral controls (``layouts.build_host_panel_layout``).

    Holds no state of its own: every press hands off to the room's
    :class:`TraditionalHostView`, which keeps its own host/mod gates — the
    panel is only ever sent to someone who passed one, but a gate on the
    action is the one that counts. A press after the room closed gets
    :data:`GAME_ENDED_REPLY` rather than acting on a dead room.
    """

    def __init__(self, host_view: TraditionalHostView):
        super().__init__()
        self.host_view = host_view
        for item in build_host_panel_layout():
            self.add_item(item)
        bind(self, ASK_ID, self._ask)
        bind(self, WRITE_OWN_ID, self._write_own)
        bind(self, END_ID, self._end)

    async def _live(self, interaction: discord.Interaction) -> bool:
        view = self.host_view
        if view._closed or view.bot.active_views.get(view.game_id) is not view:
            await interaction.response.send_message(GAME_ENDED_REPLY, ephemeral=True)
            return False
        return True

    async def _ask(self, interaction: discord.Interaction) -> None:
        if await self._live(interaction):
            await self.host_view.ask_question(interaction)

    async def _write_own(self, interaction: discord.Interaction) -> None:
        if await self._live(interaction):
            await self.host_view.write_own(interaction)

    async def _end(self, interaction: discord.Interaction) -> None:
        if await self._live(interaction):
            await self.host_view.end_game_button(interaction)


#: The shared helper, so ``/games host`` can hold this game's refusal back
#: when another game in the channel opens a panel (host_panel.reply).
_reply = host_reply


async def send_host_panel(interaction: discord.Interaction, host_view: TraditionalHostView) -> None:
    """Send *host_view*'s host panel to the presser, ephemerally.

    Works on a fresh interaction (``/games host``) and on one already
    answered (the launch, whose deferred reply ``finish_launch_response``
    has deleted) alike.
    """
    panel = TraditionalHostPanel(host_view)
    if interaction.response.is_done():
        panel.message = await retry_transient(
            lambda: interaction.followup.send(view=panel, ephemeral=True, wait=True),
            label="traditional host panel",
        )
    else:
        await interaction.response.send_message(view=panel, ephemeral=True)
        panel.message = await interaction.original_response()


class TraditionalCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self.live_boards = LiveBoards(bot, self.db, "traditional board")
        #: game_id -> the panel keeping that room's opt-in bar at the bottom
        #: of its channel. A second holder rather than a second entry in
        #: ``live_boards``: that one reads the Live Game Boards dial on the
        #: way to posting, and the bar is not on it.
        self.live_bars = LivePanels()

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
        # A reload mid-room must not leave a debounced restick armed against
        # a cog that's gone.
        self.live_boards.unload()
        self.live_bars.unload()

    async def cleanup_ended_game(self, channel_id: int, game_id: str, channel=None) -> None:
        """Take down what a force-close cannot reach: the room's opt-in bar.

        ``/games end`` and the 24-hour sweep both go through
        ``force_end_active_game``, which pops the view and greys the room card
        but knows nothing about a second message this cog posted. Without this
        the bar stayed in the channel with four live buttons under a game that
        was over, and its panel went on running a SELECT per message for a row
        that no longer existed. The host's own End Game and the quiet close do
        not come through here — they take the same bar down via ``_do_close``.

        Runs *before* the force-close, so ``self.bot.active_views`` still holds
        the view whose panel owns the bar.
        """
        view = self.bot.active_views.get(game_id)
        if channel is None:
            channel = self.bot.get_channel(channel_id)
        if isinstance(view, TraditionalHostView):
            await view.retire_bar(channel)
            return
        # No view to ask — a reload, or a game recovered without one. Drop the
        # panel anyway so the listener stops watching for a bar nobody owns.
        self.live_bars.release(game_id)

    @commands.Cog.listener("on_message")
    async def _restick_boards(self, message: discord.Message) -> None:
        await self.live_boards.on_message(message)
        await self.live_bars.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_board_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        await self.live_boards.on_channel_delete(channel)
        await self.live_bars.on_channel_delete(channel)

    @app_commands.command(name="traditional", description=play_description("traditional"))
    @app_commands.describe(
        single_choice="Each player picks only one category (radio-style) instead of as many as they like",
    )
    async def traditional(self, interaction: discord.Interaction, single_choice: bool = False):
        log.info("%s used /games play traditional in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "traditional")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"single_choice": single_choice},
        )
        await finish_launch_response(interaction, game_id)
        host_view = self.bot.active_views.get(game_id) if game_id else None
        if isinstance(host_view, TraditionalHostView):
            # The host's controls aren't on the board, so hand them over
            # now rather than make the host go find ``/games host``.
            try:
                await send_host_panel(interaction, host_view)
            except discord.HTTPException:
                log.info("traditional host panel not delivered for %s", game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a Truth or Dare room: the host's controls.

        The host always gets them; anyone else needs the same mod standing
        the controls themselves check (``is_host_or_mod``).
        """
        host_view = self.bot.active_views.get(game_id)
        if not isinstance(host_view, TraditionalHostView) or host_view._closed:
            await _reply(interaction, GAME_ENDED_REPLY)
            return
        if not is_host_or_mod(interaction, host_view.host_id):
            await _reply(interaction, NOT_HOST_REPLY)
            return
        await send_host_panel(interaction, host_view)

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
        single_choice = bool(options.get("single_choice", False))
        # The quiet window comes from the per-server dashboard dial and is
        # stored on the payload, so a restart re-arms it without a config read.
        game_opts = await get_game_options(self.db, "traditional", guild_id)
        idle_minutes = clamp_idle_minutes(options.get("idle_minutes", game_opts.get("idle_minutes")))
        payload: dict = {IDLE_MINUTES_KEY: idle_minutes}
        touch_activity(payload, time.time())
        if single_choice:
            payload["single_choice"] = True
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "traditional",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )

        guild = getattr(channel, "guild", None)
        art = await read_lobby_art(self.bot, channel, TITLE)

        log.info("Game %s (traditional) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        host_view = TraditionalHostView(
            game_id, host_id, self.db, self.bot,
            boards=self.live_boards, bars=self.live_bars,
        )
        host_view.guild = guild
        host_view._host_name = host_name
        host_view._single_choice = single_choice
        host_view.art = art
        host_view._rebuild()
        self.bot.active_views[game_id] = host_view

        try:
            # No prior message to take over: the lobby card *is* the board's
            # first frame. With the dial on the panel posts it and records
            # where it went; with the dial off this is the same
            # ``channel.send`` plus game-row write as before.
            msg = await self.live_boards.open(game_id, host_view, channel, guild)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("traditional launch lacked send perms in channel %s", channel.id)
            return None
        host_view._message = msg
        await update_session(self.db, channel.id, game_id, [host_id])
        host_view.arm_idle(payload)
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the host view after a restart so its buttons work again,
        and re-arm the idle close from the payload's last activity."""
        game_id = row["game_id"]
        host_view = TraditionalHostView(
            game_id, int(row["host_id"]), self.db, self.bot,
            boards=self.live_boards, bars=self.live_bars,
        )
        host_view._message = message
        # The board is past its sticky window by definition here: the dial is
        # only ever read when a board is first posted, and this room's was
        # posted before the restart. Seed the render state so the next redraw
        # has it; ``_lobby_card`` is still true only if nobody ever joined,
        # which is exactly when the message on screen is still the lobby card.
        host_view.guild = getattr(channel, "guild", None)
        host_view._payload = payload or {}
        host_view._single_choice = bool(host_view._payload.get("single_choice", False))
        host_view._lobby_card = not host_view._payload.get("prefs")
        host_view.art = await read_lobby_art(self.bot, channel, TITLE)
        host_view._rebuild()
        self.bot.active_views[game_id] = host_view
        self.bot.add_view(host_view, message_id=message.id)

        # The opt-in bar, if this room got as far as its first question. Its
        # panel reads the persisted id straight out of the payload, so there
        # is no fetch here: a stored id whose message has since gone just
        # makes the next re-post's delete 404, which is the ordinary case.
        bar_id = host_view._payload.get(BAR_ID_KEY)
        if bar_id:
            bar_view = TraditionalBottomView(host_view, message.jump_url)
            self.bot.add_view(bar_view, message_id=int(bar_id))
            host_view._attach_bar(bar_view)

        host_view.arm_idle(payload or {})
        log.info("Recovered traditional game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = TraditionalCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("traditional")
    play.add_command(cog.traditional, override=True)
    bot.game_launchers["traditional"] = cog.launch
    bot.game_recoverers["traditional"] = cog.recover_game
