import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.sticky import StickyPanel
from bot_modules.core.layout import V2_EDIT, bind, bind_present, card
from bot_modules.core.utils import allow_pings, disable_all_items, is_host_or_mod, jump_url
from bot_modules.core.db_utils import open_db
from bot_modules.dm.batch.logic import PendingDM
from bot_modules.dm.batch.service import register_handler, unregister_handler
from bot_modules.dm.batch.store import enqueue
from bot_modules.shared.dm_branding import send_branded_dm
from bot_modules.games.board_sticky_service import LivePanels, redraw_embed_era_lobby
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    get_game_options,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    channel_name,
)
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.host_panel import (
    GAME_ENDED_REPLY,
    NOT_HOST_REPLY,
    GameHostPanel,
    hand_over,
    reply as host_reply,
    send_panel,
)
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.names import build_names, cached_names, plain_name
from bot_modules.shared.name_resolver import NameFn
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.shared.anon_audit_service import (
    EVENT_HOT_SEAT_CHANGED,
    EVENT_HOT_SEAT_SKIPPED,
    EVENT_QUESTION_ANSWERED,
    EVENT_QUESTION_APPROVED,
    EVENT_QUESTION_ASKED,
    EVENT_QUESTION_PASSED,
    EVENT_QUESTION_REJECTED,
)
from bot_modules.no_contact import service as no_contact_service
from bot_modules.no_contact.logic import (
    SURFACE_AMA,
    candidate_members_for,
)
from bot_modules.games.ama.layouts import (
    APPROVE_ID,
    ASK_ID,
    HELP_ID,
    PASS_ID,
    REJECT_ID,
    REPLY_ID,
    STATE_ENDED,
    STATE_EXPIRED,
    STATE_PASSED,
    VOLUNTEER_ID,
    build_answered_layout,
    build_asker_dm_layout,
    build_board_layout,
    build_host_panel_layout,
    host_panel_handlers,
    build_question_layout,
    build_recap_layout,
    build_screened_dm_layout,
    question_mentions,
)
from bot_modules.games.ama.bottom_bar_service import BAR_ID_KEY, make_bar_panel
from bot_modules.games.bottom_bar_service import retire_bar
from bot_modules.games.ama.ping_role_service import resolve_ping_role_id
from bot_modules.games.ama.logic import (
    AMA_FORMAT_HOT_SEAT,
    AMA_FORMAT_PANEL,
    ANSWER_BATCH_KIND,
    DEFAULT_QUESTIONS_PER_TURN,
    HOT_SEAT_SECONDS,
    add_question,
    build_question_entry,
    compute_recap_stats,
    hot_seat_seconds_remaining,
    is_panel_target,
    is_resolved_status,
    mark_question_answered,
    mark_question_approved,
    mark_question_failed,
    mark_question_expired,
    mark_question_passed,
    mark_question_pending_dm,
    mark_question_rejected,
    normalize_format,
    open_question_cards,
    parse_iso_ts,
    pending_screened_dms,
    question_message_id,
    recompute_totals,
    resolve_questions_per_turn,
    should_expire,
    toggle_panel_member,
    utcnow_iso,
)

log = logging.getLogger(__name__)

# Response copy shared between the ordinary paths and the no-contact gate.
# The gate must reply with text byte-identical to the real path it is
# impersonating — a reworded copy in one branch and not the other turns the
# silent refusal into a tell, and no test would catch it. One constant per
# message is what keeps the two in step (see docs/specs/no_contact_spec.md).
STALE_PANEL_TEXT = (
    "⚠️ That person left the panel while you were typing — please try again."
)
STALE_HOT_SEAT_TEXT = (
    "⚠️ The hot seat changed while you were typing — please try again."
)
SUBMITTED_FOR_REVIEW_TEXT = "✅ Your question has been submitted for host review."
POSTED_ANONYMOUSLY_TEXT = "Your question has been posted anonymously!"
QUESTION_NOT_POSTED_TEXT = (
    "⚠️ Discord wouldn't take your question just then — nothing was posted. "
    "Please ask it again."
)
GAME_ENDED_TEXT = "This AMA has ended."
# Stamped on every still-open question card when the game closes, the way
# the 7-day prune stamps its own (social-prompt-43).
ENDED_CARD_FOOTER = STATE_ENDED
END_DENIED_TEXT = "❌ Only the host or a mod can end the AMA."

# Per-game dashboard dials (games-ama.js optSchema). Read once at launch and
# carried in the payload so a restart keeps them. The ping role's key is
# declared here — the dial-enforcement sweep reads this file to check every
# dial the panel offers is one the cog reads — but reading and one-time
# seeding of that dial belong to ``games_ama.ping_role_service``, whose
# ``PING_ROLE_KEY`` is the same string (pinned by a test).
OPT_PING_ROLE = "hot_seat_ping_role_id"
OPT_QUESTIONS_PER_TURN = "questions_per_turn"

# ``end_game(reason=...)`` values, matching the vote games' vocabulary.
REASON_HOST_ENDED = "ended"


async def seat_name(bot, guild, user_id: int) -> str:
    """The seat's plain display name for a card body: the live cache, then
    ``known_users`` (a member who left), then ``User <id>``."""
    return plain_name(await build_names(bot, guild, [user_id]), user_id)


async def prune_question_card(
    channel, message_id: int, footer_text: str | None = None, *,
    question: dict | None = None, bot=None,
) -> bool:
    """Retire one posted question card: drop its buttons, optionally stamp
    why (``footer_text``, one of the layout's ``STATE_*`` lines). False when
    the message is gone or can't be edited.

    A card is rebuilt from ``question`` (the payload entry) rather than read
    back off the message: a V2 message is nothing but its view, so there is
    no embed to take a footer. A card posted as an embed before the
    conversion is still handled the old way, footer and all; a card that
    already carries no controls (answered, passed) is left as it is.
    """
    try:
        msg = await channel.fetch_message(int(message_id))
    except discord.HTTPException:
        return False

    embed = msg.embeds[0] if msg.embeds else None
    if embed is not None:
        if footer_text:
            embed.set_footer(text=footer_text)
            edit_kwargs: dict = {"embed": embed, "view": None}
        else:
            edit_kwargs = {"view": None}
        try:
            await msg.edit(**edit_kwargs)
        except discord.HTTPException:
            return False
        return True

    if not footer_text:
        # A V2 card past its Reply / Pass has already been edited into its
        # resolved state; there is nothing left to strip.
        return True
    guild = getattr(channel, "guild", None)
    target = int((question or {}).get("hot_seat_id") or 0) or None
    try:
        await msg.edit(view=card(build_question_layout(
            (question or {}).get("text", ""),
            target_name=(
                await seat_name(bot, guild, target) if bot is not None else plain_name(cached_names(guild), target)
            ) if target else None,
            state=footer_text,
        )))
    except discord.HTTPException:
        return False
    return True


async def _fire_ama_ask_trigger(client, channel, asker_id: int, game_id: str, q_idx: int) -> None:
    """Credit an AMA question-asker's ``ama_ask`` economy quest trigger.

    Fired only when the question actually becomes visible: on submit in
    unfiltered mode, on host approval in screened mode (rejected questions
    never pay). ``channel`` is the guild text channel — screened approval
    happens in the host's DMs, so we take the guild from it rather than the
    interaction. Occurrence keys per question so "ask N questions" quests
    count each one once.
    """
    from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    await fire_member_trigger(
        cast("Bot", client), guild.id, asker_id, "ama_ask",
        occurrence=f"{game_id}:{q_idx}",
    )


# ── Modals ───────────────────────────────────────────────────────────────────


class AskQuestionModal(discord.ui.Modal, title="Your Question"):
    question = discord.ui.TextInput(
        label="Question",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Ask anything…",
    )

    def __init__(self, game_id: str, db, channel, mode: str, host_id: int, target_id: int, ama_view):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self.channel = channel
        self.mode = mode
        self.host_id = host_id
        # Who the question is directed at, captured at button-press time. In
        # hot-seat format this is the current hot seat; in panel format it's
        # the panelist the asker picked from the dropdown.
        self.target_id = target_id
        self.ama_view = ama_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Question", channel_name(interaction.channel))

        # Guard: game was closed while the modal was open
        if self.ama_view._closed:
            await interaction.response.send_message(
                "⚠️ The game closed while you were typing — your question was not submitted.",
                ephemeral=True,
            )
            return

        # Guard: the target moved on while the modal was open. In hot-seat
        # format the seat may have rotated; in panel format the person may
        # have left the panel. Either way the captured target is stale.
        if self.ama_view.game_format == AMA_FORMAT_PANEL:
            if not is_panel_target(self.ama_view.panel, self.target_id):
                await interaction.response.send_message(
                    STALE_PANEL_TEXT,
                    ephemeral=True,
                )
                return
        elif self.ama_view.hot_seat_id != self.target_id:
            await interaction.response.send_message(
                STALE_HOT_SEAT_TEXT,
                ephemeral=True,
            )
            return

        # No-contact gate. An AMA question is directed — the asker names a
        # panelist — so it carries contact even though he is anonymous to
        # everyone reading. Placed after the closed-game and stale-target
        # guards so every ordinary refusal still reaches him unchanged.
        #
        # The response differs by mode, because "fake success" is only
        # invisible when nothing was going to be publicly visible:
        #
        #   screened  → the question would have gone to the HOST's DMs and
        #               nowhere he can see. Reporting success costs nothing;
        #               a question the host never approved is the ordinary
        #               outcome and looks identical.
        #   unfiltered → the question would have been POSTED TO THE CHANNEL.
        #               Telling him it posted, when he can scroll up and see
        #               that it did not, leaves an unexplained hole — and
        #               since he chose the panelist, the hole points at her.
        #               So we return the existing stale-target error instead:
        #               an ordinary, believable race he has probably hit for
        #               real, which explains the absence without involving
        #               her. Indistinguishable from an ordinary failure
        #               rather than from success — the other half of the rule.
        if interaction.guild is not None and await asyncio.to_thread(
            no_contact_service.check_and_record,
            cast("Bot", interaction.client).ctx.db_path,
            interaction.guild.id,
            actor_id=interaction.user.id,
            target_id=self.target_id,
            surface=SURFACE_AMA,
        ):
            if self.mode != "unfiltered":
                await interaction.response.send_message(
                    SUBMITTED_FOR_REVIEW_TEXT,
                    ephemeral=True,
                )
            elif self.ama_view.game_format == AMA_FORMAT_PANEL:
                await interaction.response.send_message(
                    STALE_PANEL_TEXT,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    STALE_HOT_SEAT_TEXT,
                    ephemeral=True,
                )
            return

        q_entry = build_question_entry(
            asker_id=interaction.user.id,
            text=self.question.value,
            hot_seat_id=self.target_id,
        )

        def _add_question(payload):
            add_question(payload, q_entry)

        payload = await modify_payload(self.db, self.game_id, _add_question)
        q_idx = len(payload.get("questions", [])) - 1

        if self.mode == "unfiltered":
            target_member = interaction.guild.get_member(self.target_id) if interaction.guild else None
            question_view = QuestionView(
                self.game_id, self.target_id, self.db, q_idx, interaction.user.id,
                self.ama_view, self.question.value, guild=interaction.guild,
                seat_name=await seat_name(interaction.client, interaction.guild, self.target_id),
            )
            # Defer before the post: retry_transient can spend 3s+ on a bad
            # edge, past Discord's deadline for the first response, and the
            # asker would then get "This interaction failed" instead of being
            # told whether their question went up.
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                # The card names the seat once, in its body — the ping the
                # embed carried in ``content=`` — allow-listed to that one id.
                question_msg = await retry_transient(
                    lambda: self.channel.send(
                        view=question_view,
                        allowed_mentions=question_mentions(
                            target_member.id if target_member else None,
                        ),
                    ),
                    label="ama question card",
                )
            except discord.HTTPException:
                # Every HTTP failure, not just a 503: a 403 in this channel and
                # a 404 on a deleted one leave exactly the same hole.
                # The entry is already in the payload — it has to be, because
                # the card's buttons carry its index and the index does not
                # exist until it is appended. Left alone it would sit at
                # "pending" forever: no card to answer, too young to prune,
                # and counted on the progress bar against a question nobody
                # can see. Mark it instead of removing it; every later
                # question's index is live in the buttons of cards already in
                # the channel, so the list must not shift.
                def _mark_failed(payload):
                    mark_question_failed(payload, q_idx)

                await modify_payload(self.db, self.game_id, _mark_failed)
                log.warning(
                    "ama question card failed to post in channel %s; question rolled back",
                    self.channel.id,
                )
                await interaction.followup.send(QUESTION_NOT_POSTED_TEXT, ephemeral=True)
                return

            def _mark_posted(payload):
                mark_question_approved(payload, q_idx, message_id=question_msg.id)

            await modify_payload(self.db, self.game_id, _mark_posted)
            await interaction.followup.send(POSTED_ANONYMOUSLY_TEXT, ephemeral=True)
            # Recorded after the post so the row carries the message pointer the
            # dashboard joins against to recover the question text.
            audit_label = "AMA Question"
            audit_message_id = question_msg.id
            audit_extra = {"mode": "unfiltered", "question_idx": q_idx}
            # Advance turn/status only after the question is actually posted
            await self.ama_view.after_question_posted(self.channel)
            # Unfiltered questions post immediately → credit the asker now.
            await _fire_ama_ask_trigger(
                interaction.client, self.channel, interaction.user.id, self.game_id, q_idx
            )
        else:
            # Screened — DM the host so the question stays hidden from the channel
            target_member = interaction.guild.get_member(self.target_id) if interaction.guild else None
            approve_view = ScreenedQuestionView(
                game_id=self.game_id,
                question_text=self.question.value,
                question_idx=q_idx,
                db=self.db,
                channel=self.channel,
                hot_seat_id=self.target_id,
                asker_id=interaction.user.id,
                ama_view=self.ama_view,
                target_name=target_member.display_name if target_member else "the hot seat",
            )
            dm_sent = False
            try:
                guild = interaction.guild
                host_member = guild.get_member(self.host_id) if guild else None
                if host_member and guild:
                    # The card is the view: branding wraps its first line
                    # with the guild icon and adds the server's name.
                    dm_msg = await send_branded_dm(
                        host_member,
                        db_path=self.db,
                        guild=guild,
                        layout=approve_view,
                    )
                    dm_sent = dm_msg is not None
                    dm_msg_id = getattr(dm_msg, "id", None)
                    if dm_msg_id:
                        # The approval buttons never expire; the DM's id is
                        # what recover_game re-registers them against after
                        # a restart (social-prompt-35).
                        def _remember_dm(payload, _mid=int(dm_msg_id)):
                            mark_question_pending_dm(payload, q_idx, _mid)

                        await modify_payload(self.db, self.game_id, _remember_dm)
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass
            # Recorded at submit, not at approval: a question the host rejects
            # never becomes a message, and that is exactly the one a mod needs
            # to be able to trace. No message pointer exists for it, so the row
            # is who-and-when only (migration 145).
            audit_label = "AMA Question (screened)"
            audit_message_id = None
            audit_extra = {
                "mode": "screened",
                "question_idx": q_idx,
                "delivered_to_host": dm_sent,
            }
            if dm_sent:
                await interaction.response.send_message(
                    SUBMITTED_FOR_REVIEW_TEXT, ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "⚠️ Couldn't reach the host (DMs may be disabled) — your question was not submitted.", ephemeral=True
                )
            # Turn counter for screened mode advances only when the host approves

        # One call for both modes — they differ only in the label, the message
        # pointer and a few `extra` keys, set per-branch above.
        if interaction.guild:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="ama", user=interaction.user,
                event=EVENT_QUESTION_ASKED,
                content=self.question.value, label=audit_label,
                target_id=self.target_id, game_id=self.game_id,
                message_id=audit_message_id, channel_id=self.channel.id,
                extra=audit_extra,
            )


def _queue_answer_dm(
    db_path, *, guild_id: int, asker_id: int, channel_id: int,
    answerer_id: int, card_id: int,
) -> None:
    with open_db(db_path) as conn:
        enqueue(
            conn,
            kind=ANSWER_BATCH_KIND,
            guild_id=guild_id,
            user_id=asker_id,
            group_key=channel_id,
            actor_id=answerer_id,
            ref_id=card_id,
        )


async def _deliver_answer_batch(
    bot: "Bot", guild: discord.Guild, asker: discord.Member, rows: list[PendingDM]
) -> None:
    """One DM for every answer an asker got in one AMA channel over the window.

    ``dm_batch`` has already dropped answers from a hot seat the asker is now
    under a no-contact rule with, and checked the asker is still here.
    """
    head = rows[0]
    await send_branded_dm(
        asker,
        db_path=bot.ctx.db_path,
        guild=guild,
        layout=card(build_asker_dm_layout(
            f"<#{head.group_key}>",
            answer_url=jump_url(guild.id, head.group_key, head.ref_id),
            answer_count=len(rows),
        )),
    )


class ReplyModal(discord.ui.Modal, title="Your Reply"):
    """Modal for the hot-seat player to reply to a question."""

    reply = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Type your answer…",
    )

    def __init__(self, game_id: str, db, question_idx: int, asker_id: int, ama_view, question_text: str):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self.question_idx = question_idx
        self.asker_id = asker_id
        self.ama_view = ama_view
        self.question_text = question_text

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted reply modal in #%s", interaction.user.display_name, channel_name(interaction.channel))

        if self.ama_view is not None and self.ama_view._closed:
            await interaction.response.send_message(GAME_ENDED_TEXT, ephemeral=True)
            return

        await interaction.response.edit_message(
            view=card(build_answered_layout(
                self.question_text, self.reply.value, interaction.user.display_name,
            )),
            **V2_EDIT,
        )

        # DM the anonymous asker so they know their question was answered
        try:
            asker = interaction.guild.get_member(self.asker_id) if interaction.guild else None
            channel = interaction.channel
            # No-contact gate on the answer DM — the second contact channel in
            # this feature, and the one that runs in the opposite direction.
            # A question asked BEFORE the pair was added is still sitting in
            # the payload, so answering it would DM him on her behalf. Skipping
            # is invisible to both: he is not told his question was answered,
            # and she gets no delivery receipt either way.
            # Plain check, not check_and_record: the answerer here is the
            # PROTECTED member replying to a question that predates the pair.
            # Recording it as an "attempt" would put her in the mod log as the
            # one attempting contact, which is both wrong and unpleasant to
            # read in a harassment case.
            blocked_dm = bool(
                asker
                and interaction.guild
                and await asyncio.to_thread(
                    no_contact_service.is_no_contact,
                    cast("Bot", interaction.client).ctx.db_path,
                    interaction.guild.id,
                    interaction.user.id,
                    self.asker_id,
                )
            )
            if (
                asker
                and not blocked_dm
                and interaction.guild is not None
                and interaction.message is not None
                and channel is not None
                and not isinstance(channel, (discord.DMChannel, discord.GroupChannel))
            ):
                # Held, not sent: every answer the asker gets in this channel
                # over the next ten minutes goes out as one DM
                # (_deliver_answer_batch). The card we just edited is the
                # ref, so a deleted card is never announced.
                await asyncio.to_thread(
                    _queue_answer_dm,
                    cast("Bot", interaction.client).ctx.db_path,
                    guild_id=interaction.guild.id,
                    asker_id=self.asker_id,
                    channel_id=channel.id,
                    answerer_id=interaction.user.id,
                    card_id=interaction.message.id,
                )
        except Exception as e:  # noqa: BLE001 — a courtesy DM must not break marking the question answered
            log.debug("Could not queue the answer DM for asker %s: %s", self.asker_id, e)

        # Mark question as answered in payload — does NOT increment game tallies
        payload = await get_game_payload(self.db, self.game_id)
        assert interaction.message
        mark_question_answered(
            payload,
            self.question_idx,
            message_id=interaction.message.id,
        )
        await update_game_payload(self.db, self.game_id, payload)

        # The answerer is the public hot seat, not an anonymous poster — logged
        # so a question and its answer sit together in one trail.
        if interaction.guild is not None:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="ama", user=interaction.user,
                event=EVENT_QUESTION_ANSWERED,
                target_id=self.asker_id, game_id=self.game_id,
                message_id=interaction.message.id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"question_idx": self.question_idx},
            )

        # Quest hook: answering as the hot seat — twin of ama_ask, keyed per
        # question so a busy AMA counts each answer once. Guarded wrapper.
        if interaction.guild is not None:
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            await fire_member_trigger(
                cast("Bot", interaction.client), interaction.guild.id,
                interaction.user.id, "ama_answer",
                occurrence=f"{self.game_id}:{self.question_idx}",
            )

        # An answered question is what uses up the seat's turn (social-prompt-34).
        if self.ama_view is not None:
            await self.ama_view.after_question_resolved(interaction.channel, interaction.user.id)


# ── Views ────────────────────────────────────────────────────────────────────


class ScreenedQuestionView(discord.ui.LayoutView):
    """The host's DM for one screened question — the card and its Approve /
    Reject (``layouts.build_screened_dm_layout``, paradigm 5).

    Persistent (``timeout=None``, fixed custom_ids): a host who steps away
    for longer than a view timeout used to come back to "This interaction
    failed" and a question nobody could recover (social-prompt-35). The DM's
    message id is stored on the question so ``recover_game`` re-registers
    this view after a restart. Once the host acts the card edits into its
    receipt, buttons gone.
    """

    def __init__(
        self, game_id, question_text, question_idx, db, channel, hot_seat_id, asker_id, ama_view,
        *, target_name: str = "the hot seat",
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.question_text = question_text
        self.question_idx = question_idx
        self.db = db
        self.channel = channel
        self.hot_seat_id = hot_seat_id
        self.asker_id = asker_id
        self.ama_view = ama_view
        self.target_name = target_name
        for item in build_screened_dm_layout(
            question_text, target_name, getattr(channel, "mention", "the channel"),
        ):
            self.add_item(item)
        bind(self, APPROVE_ID, self.approve)
        bind(self, REJECT_ID, self.reject)

    async def _receipt(self, interaction: discord.Interaction, receipt: str) -> None:
        """Edit the DM into its receipt: the question stays, the buttons go."""
        self.stop()
        await interaction.response.edit_message(view=card(build_screened_dm_layout(
            self.question_text, self.target_name, getattr(self.channel, "mention", "the channel"),
            receipt=receipt,
        )), **V2_EDIT)

    async def _ended(self, interaction: discord.Interaction) -> bool:
        """Retire the buttons if the game closed while the DM sat unread."""
        if not self.ama_view._closed:
            return False
        await self._receipt(interaction, f"🛑 {GAME_ENDED_TEXT} This question was not posted.")
        return True

    async def approve(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Approve' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if interaction.user.id != self.ama_view.host_id:
            await interaction.response.send_message("❌ Only the host can approve questions.", ephemeral=True)
            return
        if await self._ended(interaction):
            return
        # No-contact gate on approval, not just on asking. A screened question
        # sits in the host's DMs for as long as the view lives, and the pair
        # can be created in that window — or the question can predate the pair
        # entirely. Approving it would post it to the channel pinging her, on
        # the host's action rather than his. Silently drop it and tell the
        # host it was approved: the host is a third party here, and telling
        # them "this asker is blocked from this panelist" would leak the
        # entry to someone who has no business knowing it.
        if interaction.guild is not None and await asyncio.to_thread(
            no_contact_service.check_and_record,
            cast("Bot", interaction.client).ctx.db_path,
            interaction.guild.id,
            actor_id=self.asker_id,
            target_id=self.hot_seat_id,
            surface=SURFACE_AMA,
        ):
            await self._receipt(interaction, "✅ Question approved.")
            return

        # The host approves from their DMs, so the guild is the channel's.
        guild = getattr(self.channel, "guild", None)
        hot_seat_member = guild.get_member(self.hot_seat_id) if guild else None
        question_view = QuestionView(
            self.game_id, self.hot_seat_id, self.db, self.question_idx, self.asker_id,
            self.ama_view, self.question_text, guild=guild,
            seat_name=await seat_name(interaction.client, guild, self.hot_seat_id),
        )
        question_msg = await self.channel.send(
            view=question_view,
            allowed_mentions=question_mentions(hot_seat_member.id if hot_seat_member else None),
        )
        await self._receipt(interaction, "✅ Question approved.")

        # Update status in payload
        payload = await get_game_payload(self.db, self.game_id)
        mark_question_approved(
            payload,
            self.question_idx,
            message_id=question_msg.id,
            hot_seat_id=self.hot_seat_id,
            now_iso=utcnow_iso(),
        )
        await update_game_payload(self.db, self.game_id, payload)

        # The host approves from their DMs, so interaction.guild is None here —
        # the guild comes from the game channel. Recording the message pointer
        # now is what makes an approved screened question's text recoverable.
        if getattr(self.channel, "guild", None):
            await audit_anonymous(
                interaction.client, self.db, self.channel.guild,
                game_type="ama", user=interaction.user,
                event=EVENT_QUESTION_APPROVED,
                target_id=self.asker_id, game_id=self.game_id,
                message_id=question_msg.id, channel_id=self.channel.id,
                extra={"question_idx": self.question_idx},
            )

        # Advance turn/status now that the question is actually posted
        await self.ama_view.after_question_posted(self.channel)
        # Screened questions credit the asker only on approval (rejected ones
        # never reach here). The host approves from their DMs, so the guild
        # comes from the game channel, not this interaction.
        await _fire_ama_ask_trigger(
            interaction.client, self.channel, self.asker_id, self.game_id, self.question_idx
        )

    async def reject(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Reject' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if interaction.user.id != self.ama_view.host_id:
            await interaction.response.send_message("❌ Only the host can reject questions.", ephemeral=True)
            return
        if await self._ended(interaction):
            return
        await self._receipt(interaction, "❌ Question rejected.")

        payload = await get_game_payload(self.db, self.game_id)
        mark_question_rejected(payload, self.question_idx)
        await update_game_payload(self.db, self.game_id, payload)

        # A rejected question never becomes a message, so this row is the only
        # record that it was ever submitted.
        if getattr(self.channel, "guild", None):
            await audit_anonymous(
                interaction.client, self.db, self.channel.guild,
                game_type="ama", user=interaction.user,
                event=EVENT_QUESTION_REJECTED,
                target_id=self.asker_id, game_id=self.game_id,
                channel_id=self.channel.id,
                extra={"question_idx": self.question_idx},
            )


class QuestionView(discord.ui.LayoutView):
    """One posted question — the card and its Reply / Pass for the seat
    (``layouts.build_question_layout``, paradigm 9).

    The card names the seat once, in its body: a raw ping when the seat
    is a cached member of ``guild`` (the send allow-lists that one id), a
    plain name otherwise. Every question card shares the same two custom
    ids, so the cog registers each against its own message id.
    """

    def __init__(
        self, game_id, hot_seat_id, db, question_idx, asker_id, ama_view=None,
        question_text: str = "", *, guild=None, seat_name: str | None = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.hot_seat_id = hot_seat_id
        self.db = db
        self.question_idx = question_idx
        self.asker_id = asker_id
        self.ama_view = ama_view
        self.question_text = question_text
        self.guild = guild
        # Resolved by the async caller (cache, then known_users) so a seat who
        # has left still reads their name; ``None`` means cache-only.
        self.seat_name = seat_name
        self._render()

    def _target(self) -> dict:
        member = self.guild.get_member(int(self.hot_seat_id)) if self.guild and self.hot_seat_id else None
        if member is not None:
            return {"target_id": member.id}
        if not self.hot_seat_id:
            return {"target_name": None}
        return {"target_name": self.seat_name or plain_name(cached_names(self.guild), self.hot_seat_id)}

    def _render(self, state: str | None = None) -> None:
        self.clear_items()
        for item in build_question_layout(self.question_text, state=state, **self._target()):
            self.add_item(item)
        bind_present(self, {REPLY_ID: self.reply_question, PASS_ID: self.pass_question})

    def _game_closed(self) -> bool:
        return self.ama_view is not None and getattr(self.ama_view, "_closed", False)

    async def reply_question(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Reply' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if interaction.user.id != self.hot_seat_id:
            await interaction.response.send_message("❌ Only the hot seat player can reply.", ephemeral=True)
            return
        if self._game_closed():
            await interaction.response.send_message(GAME_ENDED_TEXT, ephemeral=True)
            return
        modal = ReplyModal(self.game_id, self.db, self.question_idx, self.asker_id, self.ama_view, self.question_text)
        await interaction.response.send_modal(modal)

    async def pass_question(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Pass' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if interaction.user.id != self.hot_seat_id:
            await interaction.response.send_message("❌ Only the hot seat player can pass.", ephemeral=True)
            return
        if self._game_closed():
            await interaction.response.send_message(GAME_ENDED_TEXT, ephemeral=True)
            return
        assert interaction.message
        # The card keeps its question and loses its buttons — rebuilt from
        # state, not read back off the message.
        self.stop()
        self._render(state=STATE_PASSED)
        await interaction.response.edit_message(view=self, **V2_EDIT)

        payload = await get_game_payload(self.db, self.game_id)
        mark_question_passed(
            payload,
            self.question_idx,
            message_id=interaction.message.id,
        )
        await update_game_payload(self.db, self.game_id, payload)

        # Metadata only — no content arg, so this doesn't spam the audit
        # channel with a contentless embed.
        if interaction.guild is not None:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="ama", user=interaction.user,
                event=EVENT_QUESTION_PASSED,
                target_id=self.asker_id, game_id=self.game_id,
                message_id=interaction.message.id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"question_idx": self.question_idx},
            )

        # A pass uses up the seat's turn the same as an answer.
        if self.ama_view is not None:
            await self.ama_view.after_question_resolved(interaction.channel, self.hot_seat_id)


class AMAView(discord.ui.LayoutView):
    """The AMA's board (``layouts.build_board_layout``, paradigm 8) and the
    game's state. The card carries what any member may press — Volunteer,
    Ask a Question, Help; the host's Skip, New Hot Seat and End AMA are on
    the ephemeral host panel, which calls :meth:`skip_hot_seat`,
    :meth:`new_hot_seat` and :meth:`end_ama` here."""

    def __init__(
        self, game_id: str, host_id: int, mode: str, db, bot,
        game_format: str = AMA_FORMAT_HOT_SEAT,
        *,
        ping_role_id: int | None = None,
        per_turn: int = DEFAULT_QUESTIONS_PER_TURN,
        host_name: str = "Host",
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.mode = mode
        self.game_format = normalize_format(game_format)
        self.db = db
        self.bot = bot
        # The dashboard's Hot-Seat Ping Role dial (None = no role ping) and
        # Questions per Turn dial, both resolved at launch and persisted.
        self.ping_role_id = ping_role_id
        self.per_turn = per_turn
        self.hot_seat_id: int | None = None
        self._game_msg: discord.Message | None = None
        self._hot_seat_name: str | None = None
        self.names: NameFn = cached_names(None)
        self.queue: list[int] = []          # user IDs waiting for hot seat (hot-seat format)
        self.panel: list[int] = []          # user IDs answering questions (panel format)
        self.questions_this_turn: int = 0   # answered questions for current hot seat
        self._closed: bool = False          # True once close has been confirmed; blocks new questions
        #: The bar's sticky panel, set by ``launch``/``recover_game``. None
        #: where the bar was never posted (a launch that lacked Send
        #: Messages), which is what makes every bar call below a no-op.
        self._bar: StickyPanel | None = None
        self._ping_subscribers: set[int] = set()  # users who want pings on new hot seat
        self._hot_seat_timer_task: asyncio.Task | None = None  # 1-hour auto-rotate timer
        self._rebuild()

    async def refresh_names(self) -> None:
        """Re-resolve the volunteers' and queue's names — the live cache,
        then ``known_users`` — before a redraw. ``_rebuild`` stays sync."""
        guild = self._game_msg.guild if self._game_msg else None
        self.names = await build_names(self.bot, guild, [*self.queue, *self.panel])

    def _rebuild(self, payload: dict | None = None) -> None:
        """Clear and re-add the board from current state, then bind the
        buttons the card shows — a rebuild adds brand-new ``Button``
        instances with nothing wired. ``payload`` adds the progress bar;
        the closed frame (after the recap) carries no controls."""
        guild = self._game_msg.guild if self._game_msg else None
        names = self.names if getattr(self.names, "guild", None) is guild else cached_names(guild)
        self.clear_items()
        for item in build_board_layout(
            self.host_name,
            self.mode,
            game_format=self.game_format,
            hot_seat_name=self._hot_seat_name,
            questions_this_turn=self.questions_this_turn,
            per_turn=self.per_turn,
            queue=list(self.queue),
            panel=list(self.panel),
            name_fn=names,
            payload=payload,
            closed=self._closed,
        ):
            self.add_item(item)
        bind_present(self, {
            VOLUNTEER_ID: self.volunteer,
            ASK_ID: self.ask_question,
            HELP_ID: self.how_to_play,
        })

    async def refresh_status(self, channel):
        """Re-fetch payload and redraw the board with current stats."""
        if self._game_msg is None:
            return
        try:
            payload = await get_game_payload(self.db, self.game_id)
            host_member = channel.guild.get_member(self.host_id) if channel.guild else None
            if host_member is not None:
                self.host_name = host_member.display_name
            await self.refresh_names()
            self._rebuild(payload)
            try:
                await self._game_msg.edit(view=self, **V2_EDIT)
            except discord.HTTPException:
                # The rebuild above replaced the board's buttons, and
                # discord.py's view store only learns the new ones from a
                # *successful* edit: after a 5xx/429 it still holds the old
                # ones, whose view is gone, so presses stopped routing until
                # the next good edit. Register what is on the card now.
                self.bot.add_view(self, message_id=self._game_msg.id)
                raise
        except Exception as e:  # noqa: BLE001 — a status-bar refresh spans DB read, card build and Discord edit; must never break the caller's flow
            log.debug("Failed to refresh AMA status bar: %s", e)

    def _cancel_hot_seat_timer(self) -> None:
        """Cancel the hour timer — unless it is the task we are running in.

        The timed-out rotation re-arms (or clears) the timer from inside the
        timer's own task; cancelling that task lands at its next ``await`` and
        silently kills the new-seat announcement and the seat clearing
        (``except Exception`` never sees a ``CancelledError``). A firing timer
        is about to return anyway, so it is simply left alone.
        """
        task = self._hot_seat_timer_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _start_hot_seat_timer(self, channel, seconds: int = HOT_SEAT_SECONDS):
        """Start (or restart) the auto-rotate timer for the current hot seat.

        ``seconds`` is the full hour on a fresh seat; ``recover_game`` passes
        what is left of it after a restart (social-prompt-42).
        """
        self._cancel_hot_seat_timer()

        async def _timeout():
            await asyncio.sleep(seconds)
            if self._closed or self.hot_seat_id is None:
                return
            try:
                await channel.send("⏰ Hot seat timed out after 1 hour — rotating to next player.")
                self.questions_this_turn = self.per_turn
                await self.check_turn_rotation(channel)
            except Exception as e:  # noqa: BLE001 — top-level guard for a detached asyncio task; an uncaught error here is silently lost anyway
                log.debug("AMA timeout handler error: %s", e)

        self._hot_seat_timer_task = asyncio.create_task(_timeout())

    def _ama_role_mention(self) -> str | None:
        """The Hot-Seat Ping Role dial as a mention, or None when unset.

        A dashboard dial, not a role looked up by name (social-prompt-41):
        renaming the role no longer silently stops the announcement, and a
        second guild can point it at its own role.
        """
        if not self.ping_role_id:
            return None
        return f"<@&{int(self.ping_role_id)}>"

    async def after_question_posted(self, channel):
        """Refresh the board after a question is posted (either format).

        Posting no longer advances the hot seat's turn: the seat rotates on
        *answered* questions (:meth:`after_question_resolved`), so a seat is
        never emptied while cards are still open (social-prompt-34).
        """
        await self.refresh_status(channel)

    async def after_question_resolved(self, channel, hot_seat_id: int | None):
        """Count an answer or pass toward the current seat's turn.

        ``hot_seat_id`` is who resolved the card; a card left over from an
        earlier seat (or answered by a panelist in panel format) refreshes
        the board but does not move the current seat's turn along.
        """
        if (
            self.game_format == AMA_FORMAT_HOT_SEAT
            and self.hot_seat_id is not None
            and hot_seat_id == self.hot_seat_id
        ):
            self.questions_this_turn += 1
            await self.refresh_status(channel)
            await self.check_turn_rotation(channel)
            return
        await self.refresh_status(channel)

    async def _handle_volunteer(self, interaction: discord.Interaction):
        """Shared Volunteer handler for the main view and the bottom bar."""
        if self._closed:
            await interaction.response.send_message("This game is closing — no new volunteers.", ephemeral=True)
            return
        if self.game_format == AMA_FORMAT_PANEL:
            await self._toggle_panel(interaction)
            return

        user_id = interaction.user.id
        # Already in the hot seat
        if user_id == self.hot_seat_id:
            await interaction.response.send_message("You're already in the hot seat!", ephemeral=True)
            return
        # Already queued — treat a second tap as "leave the queue"
        if user_id in self.queue:
            self.queue.remove(user_id)
            await interaction.response.send_message("You've left the queue.", ephemeral=True)
            await self.refresh_status(interaction.channel)
            await self._update_bottom_bar()
            return
        # No one in the hot seat — go straight in
        if self.hot_seat_id is None:
            assert isinstance(interaction.user, discord.Member)
            await interaction.response.defer()
            await self._set_hot_seat(interaction.user, interaction.channel, announce=True)
            return
        # Seat is taken — add to queue
        self.queue.append(user_id)
        pos = len(self.queue)
        await interaction.response.send_message(
            f"You're #{pos} in the queue. You'll be notified when it's your turn!",
            ephemeral=True,
        )
        await self.refresh_status(interaction.channel)
        await self._update_bottom_bar()

    async def _toggle_panel(self, interaction: discord.Interaction):
        """Join or leave the panel of people taking questions (panel format)."""
        assert isinstance(interaction.user, discord.Member)
        member = interaction.user
        joined = toggle_panel_member(self.panel, member.id)
        if joined:
            await interaction.response.send_message(
                "🙋 You're on the panel — anyone can now ask you anonymous questions. "
                "Tap **Volunteer** again to leave.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "👋 You've left the panel — no new questions will be directed to you.",
                ephemeral=True,
            )
        await self.refresh_status(interaction.channel)
        await self._update_bottom_bar()
        if joined and isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.channel.send(
                f"🙋 {member.mention} joined the AMA panel — ask them anything!",
                allowed_mentions=allow_pings(users=[member]),
            )

    async def _begin_ask(self, interaction: discord.Interaction):
        """Shared 'Ask a Question' handler for the main view and the bottom bar."""
        if self._closed:
            await interaction.response.send_message("This game is closing — no new questions.", ephemeral=True)
            return

        if self.game_format == AMA_FORMAT_PANEL:
            guild = interaction.guild
            # You can't plant a question on yourself — and dropping the
            # presser is what makes a two-person panel a straight modal
            # rather than a dropdown listing one name (social-prompt-34).
            panel_others = [uid for uid in self.panel if uid != interaction.user.id]
            candidates = [
                m for m in (guild.get_member(uid) if guild else None for uid in panel_others) if m
            ]
            # Drop no-contact partners from the picker so he never selects her
            # in the first place. This is strictly better than refusing the
            # question afterwards: a name absent from a dropdown he has no
            # baseline for is unnoticeable, whereas a question that vanishes
            # after he chose a specific panelist is not. The submit-time gate
            # below still stands as the enforcement — this only spares the
            # common case from ever reaching it.
            if guild is not None:
                partners = await asyncio.to_thread(
                    no_contact_service.no_contact_partners,
                    cast("Bot", interaction.client).ctx.db_path,
                    guild.id,
                    interaction.user.id,
                )
                had_candidates = bool(candidates)
                candidates = candidate_members_for(candidates, partners)
                # Filtering is invisible while other names remain, but it can
                # empty the list — and "nobody has joined yet" is checkable
                # against the join announcement and the status embed both
                # naming her. Say the panel is unavailable to him instead:
                # true, and it makes no claim the channel contradicts.
                if had_candidates and not candidates:
                    await interaction.response.send_message(
                        "❌ You can't ask a question in this round.", ephemeral=True
                    )
                    return
            if not candidates:
                if interaction.user.id in self.panel:
                    await interaction.response.send_message(
                        "You're the only one on the panel so far — questions go to the other panelists.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "No one has joined the panel yet — tap 🙋 Volunteer to be the first!",
                        ephemeral=True,
                    )
                return
            if len(candidates) == 1:
                # One person to ask: skip the dropdown and open the modal.
                modal = AskQuestionModal(
                    self.game_id, self.db, interaction.channel, self.mode,
                    self.host_id, candidates[0].id, self,
                )
                await interaction.response.send_modal(modal)
                return
            select = AskTargetSelect(self, candidates)
            view = discord.ui.View(timeout=120)
            view.add_item(select)
            note = f"\n(Showing the first 25 of {len(candidates)} panelists.)" if len(candidates) > 25 else ""
            await interaction.response.send_message(
                f"Who do you want to ask?{note}", view=view, ephemeral=True
            )
            return

        # Hot-seat format
        if self.hot_seat_id is None:
            await interaction.response.send_message("No one is in the hot seat yet!", ephemeral=True)
            return
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.mode, self.host_id, self.hot_seat_id, self
        )
        await interaction.response.send_modal(modal)

    async def _set_hot_seat(self, member: discord.Member, channel, announce: bool = True):
        """Put a member in the hot seat and reset the turn counter."""
        self.hot_seat_id = member.id
        self._hot_seat_name = member.display_name
        self.questions_this_turn = 0

        payload = await get_game_payload(self.db, self.game_id)
        payload["hot_seat_id"] = member.id
        payload["hot_seat_rotations"] = payload.get("hot_seat_rotations", 0) + 1
        # When the seat started — recover_game re-arms the timer from it.
        payload["hot_seat_started_at"] = utcnow_iso()
        await update_game_payload(self.db, self.game_id, payload)

        self._start_hot_seat_timer(channel)

        await self.refresh_status(channel)
        await self._update_bottom_bar()

        if announce:
            # Notify the configured ping role + opt-in users when the hot
            # seat changes. Both halves are named in the allow-list, and it
            # is built on every path: the bot's client default is
            # AllowedMentions.none() (core.app_context.Bot), so a bare
            # ``None`` here is not "the old permissive default" but silence
            # — which is how the 🔔 Notify Me pings would have died in a
            # guild with no ping role dialled. The seat is allow-listed too:
            # their own mention is in the sentence and has always pinged.
            subscriber_ids = [
                uid for uid in self._ping_subscribers if uid != member.id
            ]
            mentions: list[str] = []
            role_mention = self._ama_role_mention()
            if role_mention:
                mentions.append(role_mention)
            mentions.extend(f"<@{uid}>" for uid in subscriber_ids)
            mention_prefix = f"{' '.join(mentions)} " if mentions else ""
            allowed = allow_pings(
                users=[*subscriber_ids, member.id],
                roles=[self.ping_role_id] if self.ping_role_id else [],
            )
            await channel.send(
                f"{mention_prefix}A new host: {member.mention} is in the hot seat!",
                allowed_mentions=allowed,
            )

    async def _update_bottom_bar(self):
        """Redraw the bar's one line wherever the bar actually is now.

        Through the panel, not a stored message handle: a re-post may have
        moved the bar since this view last saw it, and the panel is what
        knows where it went. The redraw is skipped entirely when the line
        would be unchanged (the panel compares its own signature), and a bar
        that has been deleted is re-posted rather than left dead. Nothing to
        do once the game has closed — cleanup zeroes the stored ids, so the
        refresh below finds no bar and returns.
        """
        if self._bar is None:
            return
        guild = self._game_msg.guild if self._game_msg else None
        if guild is None:
            return
        await self._bar.refresh(guild.id)

    async def check_turn_rotation(self, channel):
        """Rotate the seat once it has answered its ``per_turn`` questions."""
        if self.questions_this_turn < self.per_turn:
            return

        if self._closed:
            # A card resolved after 🏁 End AMA confirmed: the close has
            # already run (or is running) — never close a second time.
            return

        if not self.queue:
            # No one queued — announce turn is done, seat opens up
            self._cancel_hot_seat_timer()
            self.hot_seat_id = None
            self._hot_seat_name = None
            self.questions_this_turn = 0

            payload = await get_game_payload(self.db, self.game_id)
            payload["hot_seat_id"] = None
            await update_game_payload(self.db, self.game_id, payload)

            await channel.send("🎙️ Hot seat turn complete! Volunteer to take the next turn.")
            await self.refresh_status(channel)
            await self._update_bottom_bar()
            return

        # Pop next from queue and set them in the hot seat
        next_id = self.queue.pop(0)
        guild = channel.guild if channel else None
        member = guild.get_member(next_id) if guild else None
        if member:
            await self._set_hot_seat(member, channel, announce=True)
        else:
            # Member left — try next in queue recursively
            await self.check_turn_rotation(channel)

    async def volunteer(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Volunteer' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self._handle_volunteer(interaction)

    async def ask_question(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Ask a Question' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await self._begin_ask(interaction)

    async def skip_hot_seat(self, interaction: discord.Interaction) -> None:
        """⏭️ Skip, from the host panel."""
        log.info("%s pressed 'Skip' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if self.game_format == AMA_FORMAT_PANEL:
            await interaction.response.send_message("An open panel has no hot seat to skip.", ephemeral=True)
            return
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can skip.", ephemeral=True)
            return
        if self.hot_seat_id is None:
            await interaction.response.send_message("No one is in the hot seat.", ephemeral=True)
            return
        await interaction.response.defer()
        skipped_id = self.hot_seat_id
        # Force rotation by maxing out the turn counter. The channel is the
        # board's, not the press's: a panel press arrives on the ephemeral
        # panel, whose channel is the same room but whose message is not.
        self.questions_this_turn = self.per_turn
        await self.check_turn_rotation(self._channel(interaction))

        # Host/mod action on a live game — who cut a hot seat short, and when.
        if interaction.guild is not None:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="ama", user=interaction.user,
                event=EVENT_HOT_SEAT_SKIPPED,
                target_id=skipped_id, game_id=self.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
            )

    def _channel(self, interaction: discord.Interaction):
        """The game's channel — the board's when the press came from the
        host panel, which shares the channel but not the message."""
        return getattr(self._game_msg, "channel", None) or interaction.channel

    async def new_hot_seat(self, interaction: discord.Interaction) -> None:
        """🔄 New Hot Seat, from the host panel."""
        log.info("%s pressed 'New Hot Seat' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if self.game_format == AMA_FORMAT_PANEL:
            await interaction.response.send_message("An open panel has no hot seat to set.", ephemeral=True)
            return
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can select the hot seat.", ephemeral=True)
            return
        # Only members who volunteered (are in the queue) may be promoted —
        # nobody gets forced into the public hot seat without opting in.
        guild = interaction.guild
        candidates = [
            m for m in (guild.get_member(uid) if guild else None for uid in self.queue) if m
        ]
        if not candidates:
            await interaction.response.send_message(
                "No one is waiting in the queue. Members can tap **🙋 Volunteer** "
                "to opt in, then you can pick them here.",
                ephemeral=True,
            )
            return
        select = HotSeatSelect(self.game_id, self.db, self, candidates)
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "Select the new hot seat (from volunteers):", view=view, ephemeral=True
        )

    async def end_ama(self, interaction: discord.Interaction) -> None:
        """🏁 End AMA, from the host panel: close the game with its recap and
        pay the room (social-prompt-33).

        Until 2026-09-04 the recap was reachable only through the feature
        rotation; the host's only exit was ``/games end`` (a red force-close
        card) or the silent 24h sweep.
        """
        log.info("%s pressed 'End AMA' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(END_DENIED_TEXT, ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This game already ended.", ephemeral=True)
            return
        channel = self._channel(interaction)

        async def _confirmed(_confirm_interaction: discord.Interaction) -> None:
            await self.close_now(channel, reason=REASON_HOST_ENDED)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["ama"], ephemeral=True)

    async def close_now(self, channel, *, reason: str | None = None):
        """Close this AMA from outside its own buttons, recap and payout intact.

        The one handle every outside ending uses: the feature rotation
        duck-types ``close_now`` on whatever view is registered for the game,
        and since 2026-09-04 so do ``/games end`` (via ``AMACog.end_with_recap``)
        and the 24h sweep — so the recap and its payout footer post on every
        end path rather than only the rotation's (social-prompt-33).
        ``reason`` lands in the archived payload (``'ended'`` / ``'expired'``).
        Idempotent — a second call after the view has closed finds ``_closed``
        set and does nothing.
        """
        if self._closed:
            return
        await self._do_close(channel, reason=reason)

    async def _do_close(self, channel, *, reason: str | None = None):
        # Idempotent: the flag is set before the first await, so a second
        # caller (a late card's rotation, a sweep racing the host) returns
        # here rather than posting a second recap.
        if self._closed:
            return
        self._closed = True
        self._cancel_hot_seat_timer()

        # Remove the bottom bar immediately when the game closes.
        cog = self.bot.get_cog("AMACog")
        if cog and hasattr(cog, "cleanup_ended_game"):
            await cog.cleanup_ended_game(channel.id, self.game_id, channel=channel)

        payload = await get_game_payload(self.db, self.game_id)
        # Retire every still-open question card (social-prompt-43): its
        # Reply / Pass buttons would keep working until the next restart and
        # then fail, on a game that no longer exists.
        questions = payload.get("questions", [])
        for idx, msg_id in open_question_cards(questions):
            await prune_question_card(
                channel, msg_id, footer_text=ENDED_CARD_FOOTER, question=questions[idx],
                bot=self.bot,
            )
        stats = compute_recap_stats(payload)
        total_q = stats["total_q"]

        payout_line = None
        if channel.guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, channel.guild.id, "ama")

        self.stop()
        # The board's closed frame: the final numbers, no controls.
        await self.refresh_names()
        self._rebuild(payload)

        if self._game_msg:
            try:
                await self._game_msg.edit(view=self, **V2_EDIT)
            except discord.HTTPException:
                pass
        await channel.send(view=card(build_recap_layout(self.mode, stats, payout_line=payout_line)))

        log.info("Game %s ended — %d questions asked", self.game_id, total_q)
        # Participants = members who asked a question (an id of 0 is a
        # malformed entry — mirror unique_asker_count's filter) plus the
        # hot-seat occupants who answered them.
        _qs = payload.get("questions", [])
        participants = sorted(
            {q["asker_id"] for q in _qs if q.get("asker_id", 0) > 0}
            | {q["hot_seat_id"] for q in _qs if q.get("hot_seat_id", 0) > 0}
        )
        # player_count is the roster paid here, so it matches what the 24h
        # sweep records for the same session (game_roster._ama); it used to be
        # the asker count alone on this path (social-prompt-40).
        await end_game(self.db, self.game_id, player_count=len(participants), round_count=total_q, payload=payload,
                       bot=self.bot, player_ids=participants, reason=reason)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class HotSeatSelect(discord.ui.Select):
    """Picker restricted to members who volunteered (are in the AMA queue),
    so the host can rotate the hot seat only among people who opted in."""

    def __init__(self, game_id: str, db, ama_view: AMAView, candidates: list[discord.Member]):
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in candidates[:25]
        ]
        super().__init__(
            placeholder="Select new hot seat", min_values=1, max_values=1, options=options
        )
        self.game_id = game_id
        self.db = db
        self.ama_view = ama_view

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        guild = interaction.guild
        new_member = guild.get_member(uid) if guild else None
        if new_member is None:
            await interaction.response.send_message(
                "That member is no longer available.", ephemeral=True
            )
            return
        # Remove from queue if they were queued
        if new_member.id in self.ama_view.queue:
            self.ama_view.queue.remove(new_member.id)

        await interaction.response.defer()
        await interaction.edit_original_response(content="Hot seat updated.", view=None)
        await self.ama_view._set_hot_seat(new_member, interaction.channel, announce=True)

        if guild is not None:
            await audit_anonymous(
                interaction.client, self.db, guild,
                game_type="ama", user=interaction.user,
                event=EVENT_HOT_SEAT_CHANGED,
                target_id=new_member.id, game_id=self.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
            )


class AskTargetSelect(discord.ui.Select):
    """Panel-format picker: choose which panelist to send an anonymous question to."""

    def __init__(self, ama_view: AMAView, candidates: list[discord.Member]):
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in candidates[:25]
        ]
        super().__init__(
            placeholder="Choose who to ask", min_values=1, max_values=1, options=options
        )
        self.ama_view = ama_view

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        if self.ama_view._closed:
            await interaction.response.send_message("This game is closing — no new questions.", ephemeral=True)
            return
        if not is_panel_target(self.ama_view.panel, target_id):
            await interaction.response.send_message(
                "That person left the panel — please pick someone else.", ephemeral=True
            )
            return
        modal = AskQuestionModal(
            self.ama_view.game_id, self.ama_view.db, interaction.channel,
            self.ama_view.mode, self.ama_view.host_id, target_id, self.ama_view,
        )
        await interaction.response.send_modal(modal)


class AMABottomView(discord.ui.View):
    """Persistent one-line bar at the bottom of chat for AMA."""

    def __init__(self, game_id: str, db, ama_view: AMAView, game_msg_url: str):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.db = db
        self.ama_view = ama_view
        self.add_item(discord.ui.Button(
            label="Jump to Answers",
            style=discord.ButtonStyle.link,
            url=game_msg_url,
        ))
        # "Notify Me" pings on hot-seat changes, which never happen in panel
        # format — drop it so the bottom bar has no dead button there.
        if ama_view.game_format == AMA_FORMAT_PANEL:
            for item in list(self.children):
                if getattr(item, "custom_id", None) == "ama_notify_toggle":
                    self.remove_item(item)

    @discord.ui.button(label="Ask a Question", style=discord.ButtonStyle.primary, custom_id="ama_bottom_ask")
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self.ama_view._begin_ask(interaction)

    @discord.ui.button(label="🔔 Notify Me", style=discord.ButtonStyle.secondary, custom_id="ama_notify_toggle")
    async def toggle_notify(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        user_id = interaction.user.id
        if user_id in self.ama_view._ping_subscribers:
            self.ama_view._ping_subscribers.discard(user_id)
            await interaction.response.send_message("🔕 You'll no longer be pinged when the hot seat changes.", ephemeral=True)
        else:
            self.ama_view._ping_subscribers.add(user_id)
            await interaction.response.send_message("🔔 You'll be pinged whenever the hot seat changes!", ephemeral=True)

    @discord.ui.button(label="🙋 Volunteer", style=discord.ButtonStyle.success, custom_id="ama_bottom_volunteer")
    async def volunteer(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self.ama_view._handle_volunteer(interaction)


class AMACog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self._active_channels: dict[int, str] = {}
        self._question_maintenance_task: asyncio.Task | None = None
        #: game_id → the panel keeping that AMA's bottom bar at the bottom
        #: of its channel. One per live AMA, not one per guild: two AMAs can
        #: be running in two channels of the same server, each following its
        #: own conversation. The holder is the same one the nine round games'
        #: boards live in, minus the dial read they open through — the bar is
        #: deliberately not on the Live Game Boards dial.
        self._bars = LivePanels()

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self) -> None:
        register_handler(ANSWER_BATCH_KIND, _deliver_answer_batch)

    async def cog_unload(self) -> None:
        unregister_handler(ANSWER_BATCH_KIND)
        if self._question_maintenance_task and not self._question_maintenance_task.done():
            self._question_maintenance_task.cancel()
        self._bars.unload()

    def _open_bar(
        self, game_id: str, view: AMAView, bottom_view: "AMABottomView"
    ) -> StickyPanel:
        """Register the panel that keeps one AMA's bar at the channel bottom.

        Unconditional, unlike the Live Game Boards dial the nine round games
        sit behind: a bar that stops following the conversation is a bar
        nobody can reach the Ask a Question button on, so there was never a
        version of this to turn off.
        """
        panel = make_bar_panel(self.bot, game_id, view, bottom_view)
        self._bars.add(game_id, panel)
        view._bar = panel
        return panel

    async def _retire_bar(self, game_id: str, channel) -> None:
        """Take one AMA's bar down for good, through the shared teardown.

        The body moved to ``game_bottom_bar_service.retire_bar`` on
        2026-09-20, when Truth or Dare's bar made it the second identical
        copy. What it does and why is documented there.

        It replaced three hand-rolled fallbacks: delete the cached handle,
        else fetch the persisted id and delete that, else edit the bar to
        read "🛑 AMA ended". All three existed because the first delete was
        unlocked and had no retry; the shared one retries over ~40s, so a
        message that still will not go is one the bot could not have edited
        either.
        """
        await retire_bar(
            self._bars, game_id, getattr(channel, "guild", None), label="ama",
        )

    async def _resolve_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:  # noqa: BLE001 — discord.HTTPException/InvalidData is the real raise set, but test doubles standing in for the bot can miss the method entirely; either way a failed lookup means "treat as gone"
            return None

    async def cleanup_ended_game(self, channel_id: int, game_id: str, channel=None):
        """Remove AMA tracking + bottom bar artifacts for a finished game."""
        if channel is None:
            channel = await self._resolve_channel(channel_id)

        ama_view_raw = self.bot.active_views.get(game_id)
        ama_view = ama_view_raw if isinstance(ama_view_raw, AMAView) else None
        bottom_view_raw = self.bot.active_views.get(f"{game_id}_bottom")
        bottom_view = bottom_view_raw if isinstance(bottom_view_raw, AMABottomView) else None

        if ama_view:
            try:
                ama_view.stop()
            except Exception:
                log.exception("ama: failed to stop AMA view during cleanup")

        if bottom_view:
            try:
                bottom_view.stop()
                disable_all_items(bottom_view)
            except Exception:
                log.exception("ama: failed to stop bottom view during cleanup")

        await self._retire_bar(game_id, channel)
        if ama_view:
            ama_view._bar = None

        self.bot.active_views.pop(f"{game_id}_bottom", None)
        self.bot.active_views.pop(game_id, None)
        self._active_channels.pop(channel_id, None)

    async def _prune_question_message_view(
        self, channel, message_id: int, footer_text: str | None = None, *, question: dict | None = None,
    ) -> bool:
        return await prune_question_card(channel, message_id, footer_text, question=question, bot=self.bot)

    async def end_with_recap(self, channel, game_id: str) -> bool:
        """``/games end`` on an AMA: the same recap ending as the host's
        🏁 End AMA, instead of the red Force-Closed card (social-prompt-33).
        False when no live view holds the game (after a restart that failed
        to recover it) so the caller falls back to the force-close path."""
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, AMAView) or view._closed:
            return False
        await view.close_now(channel, reason=REASON_HOST_ENDED)
        return True

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild an in-flight AMA's persistent views after a bot restart.

        Registered in ``bot.game_recoverers["ama"]`` and driven by the shared
        startup sweep, which hands us the game's main embed *message*. On a
        restart ``bot.active_views`` is bare and discord.py's persistent-view
        registry is empty, so every AMA button (Volunteer / Ask / New Hot Seat,
        the sticky bottom bar, and each open question card) is dead and the
        channel stays "in progress".

        We rebuild the top control panel (``AMAView``) bound to the main embed,
        the sticky bottom bar (``AMABottomView``) rebound to its persisted
        message so it can keep re-sticking, and every unresolved question card
        (``QuestionView``) — crucially wired to the live ``ama_view`` so Reply /
        Pass refresh the main embed — and repopulate ``_active_channels`` so the
        ``on_message`` re-stick loop re-arms. Expired cards are pruned in passing.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        game_format = normalize_format(payload.get("format"))
        mode = payload.get("mode", "unfiltered")
        guild = getattr(message, "guild", None) or getattr(channel, "guild", None)

        # Top control panel — bound to the main embed message. The dials
        # ride in the payload so a restart keeps the launch's settings.
        ping_role = payload.get("ping_role_id")
        view = AMAView(
            game_id, host_id, mode, self.db, self.bot, game_format=game_format,
            ping_role_id=int(ping_role) if ping_role else None,
            per_turn=resolve_questions_per_turn(payload.get("questions_per_turn")),
            host_name=await seat_name(self.bot, guild, host_id) if guild else "Host",
        )
        view._game_msg = message
        hot_seat_id = payload.get("hot_seat_id")
        if hot_seat_id:
            view.hot_seat_id = int(hot_seat_id)
            member = guild.get_member(int(hot_seat_id)) if guild else None
            if member:
                view._hot_seat_name = member.display_name
            # Re-arm the hour with whatever is left of it (social-prompt-42):
            # a recovered seat used to sit until questions or a mod moved it.
            view._start_hot_seat_timer(
                channel,
                hot_seat_seconds_remaining(
                    payload.get("hot_seat_started_at"), datetime.now(timezone.utc)
                ),
            )
        self.bot.active_views[game_id] = view
        # Rebuild BEFORE registering: discord.py's view store keeps the items
        # present at ``add_view`` time, and ``_rebuild`` clears and replaces
        # them, so the old order left a recovered board's buttons unanswered
        # until something else redrew it (ship review, 2026-10-01). WYR, NHIE
        # and MLT already recover in this order.
        await view.refresh_names()
        view._rebuild(payload)
        self.bot.add_view(view, message_id=int(message.id))
        # A board that was still an embed when the restart brought this
        # code is redrawn as the card now: the recovered view has nothing to
        # answer its old Skip / New Hot Seat / End AMA buttons with.
        await redraw_embed_era_lobby(view, message)

        # Sticky bottom bar. Its panel reads the persisted id straight out of
        # the payload, so there is no fetch here any more — and a stored id
        # whose message has since gone is no longer fatal: the next re-stick
        # posts a fresh bar and the delete of the old one 404s, which is the
        # ordinary case. Before this, a failed fetch left the bar with no
        # handle and it quietly stopped following the conversation for the
        # rest of the AMA.
        bottom_view = AMABottomView(game_id, self.db, view, message.jump_url)
        bottom_id = payload.get(BAR_ID_KEY)
        if bottom_id:
            self.bot.add_view(bottom_view, message_id=int(bottom_id))
            self._open_bar(game_id, view, bottom_view)
        else:
            # No bar was ever posted (a launch that lacked Send Messages), so
            # there is nothing to keep at the bottom. The view is registered
            # bare, as it always was, in case the message does exist.
            self.bot.add_view(bottom_view)
        self.bot.active_views[f"{game_id}_bottom"] = bottom_view

        # Screened questions still waiting in the host's DMs — their Approve /
        # Reject buttons are persistent, keyed on the DM message id we stored
        # at send time (social-prompt-35).
        questions = payload.get("questions", [])
        for idx, dm_msg_id in pending_screened_dms(questions):
            question = questions[idx]
            approve_view = ScreenedQuestionView(
                game_id=game_id,
                question_text=question.get("text", ""),
                question_idx=idx,
                db=self.db,
                channel=channel,
                hot_seat_id=int(question.get("hot_seat_id") or 0),
                asker_id=int(question.get("asker_id") or 0),
                ama_view=view,
                target_name=await seat_name(self.bot, guild, int(question.get("hot_seat_id") or 0)),
            )
            self.bot.add_view(approve_view, message_id=dm_msg_id)

        # Per-question cards — rebuilt with the live ama_view; expired cards pruned.
        now = datetime.now(timezone.utc)
        changed = False
        for idx, question in enumerate(questions):
            msg_id = question_message_id(question)
            if not msg_id:
                continue
            status = (question.get("status") or "").lower()
            if is_resolved_status(status):
                await self._prune_question_message_view(channel, msg_id, question=question)
                continue
            asked_at = parse_iso_ts(
                question.get("asked_at")
                or question.get("created_at")
                or question.get("posted_at")
            )
            if should_expire(asked_at, now):
                pruned = await self._prune_question_message_view(
                    channel, msg_id, footer_text=STATE_EXPIRED, question=question,
                )
                if pruned:
                    mark_question_expired(question)
                    changed = True
                continue
            q_hot_seat = question.get("hot_seat_id") or payload.get("hot_seat_id")
            if not q_hot_seat:
                continue
            qview = QuestionView(
                game_id=game_id,
                hot_seat_id=q_hot_seat,
                db=self.db,
                question_idx=idx,
                asker_id=question.get("asker_id", 0),
                ama_view=view,
                question_text=question.get("text", ""),
                guild=guild,
                seat_name=await seat_name(self.bot, guild, int(q_hot_seat)),
            )
            self.bot.add_view(qview, message_id=int(msg_id))

        if changed:
            recompute_totals(payload)
            await update_game_payload(self.db, game_id, payload)

        self._active_channels[channel.id] = game_id
        log.info("Recovered ama game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True

    async def _prune_stale_question_views(self):
        rows = await self.db.fetchall(
            "SELECT game_id, channel_id FROM games_active_games WHERE game_type = 'ama'"
        )
        if not rows:
            return

        now = datetime.now(timezone.utc)

        for row in rows:
            game_id = row["game_id"]
            channel = await self._resolve_channel(row["channel_id"])
            if channel is None:
                continue

            payload = await get_game_payload(self.db, game_id)
            questions = payload.get("questions", [])
            changed = False

            for question in questions:
                msg_id = question_message_id(question)
                if not msg_id:
                    continue

                status = (question.get("status") or "").lower()
                if is_resolved_status(status):
                    continue

                asked_at = parse_iso_ts(
                    question.get("asked_at")
                    or question.get("created_at")
                    or question.get("posted_at")
                )
                if not should_expire(asked_at, now):
                    continue

                pruned = await self._prune_question_message_view(
                    channel, msg_id, footer_text=STATE_EXPIRED, question=question,
                )
                if not pruned:
                    continue
                mark_question_expired(question)
                changed = True

            if changed:
                recompute_totals(payload)
                await update_game_payload(self.db, game_id, payload)

    async def _question_maintenance_loop(self):
        while not self.bot.is_closed():
            await asyncio.sleep(21600)  # every 6 hours
            try:
                await self._prune_stale_question_views()
            except Exception as e:  # noqa: BLE001 — loop guard; one bad iteration must not kill the recurring prune
                log.debug("AMA stale question prune loop error: %s", e)

    @commands.Cog.listener()
    async def on_ready(self):
        # View re-registration now runs through the shared startup recovery
        # sweep (bot.game_recoverers["ama"] -> recover_game); on_ready only
        # (re)arms the periodic stale-question prune loop.
        if self._question_maintenance_task is None or self._question_maintenance_task.done():
            self._question_maintenance_task = asyncio.create_task(self._question_maintenance_loop())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        game_id = self._active_channels.get(message.channel.id)
        if not game_id:
            return
        panel = self._bars.get(game_id)
        if panel is None:
            # No bar to keep at the bottom — a launch that could not post one,
            # or a game recovered without a persisted bar id. Stop watching
            # the channel rather than paying a row read per message forever.
            self._active_channels.pop(message.channel.id, None)
            return
        active_row = await self.db.fetchone(
            "SELECT game_id FROM games_active_games WHERE game_id = ? AND game_type = 'ama'",
            (game_id,),
        )
        if not active_row:
            await self.cleanup_ended_game(message.channel.id, game_id, channel=message.channel)
            return
        await panel.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_bar_channel(self, channel):
        await self._bars.on_channel_delete(channel)

    @commands.Cog.listener("on_thread_delete")
    async def _forget_deleted_bar_thread(self, thread):
        # A game can be launched in a thread, so the bar's channel kinds
        # include them — and Discord dispatches a different event for a
        # thread, which is why core's on_channel_delete cannot cover both.
        await self._bars.on_channel_delete(thread)

    @app_commands.command(name="ama", description=play_description("ama"))
    @app_commands.describe(
        mode="screened = host approves questions first, unfiltered = posts immediately",
        format="hot seat = one person at a time, open panel = ask anyone who's opted in",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Unfiltered", value="unfiltered"),
            app_commands.Choice(name="Screened", value="screened"),
        ],
        format=[
            app_commands.Choice(name="Hot Seat (one at a time)", value=AMA_FORMAT_HOT_SEAT),
            app_commands.Choice(name="Open Panel (ask anyone opted in)", value=AMA_FORMAT_PANEL),
        ],
    )
    async def ama(self, interaction: discord.Interaction, mode: str = "unfiltered", format: str = AMA_FORMAT_HOT_SEAT):
        log.info("%s used /games play ama in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "ama")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"mode": mode, "format": format},
        )
        await finish_launch_response(interaction, game_id)
        # Skip, New Hot Seat and End AMA aren't on the board, so hand the
        # host their controls now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for an AMA: ⏭️ Skip, 🔄 New Hot Seat (hot-seat
        format) and 🏁 End AMA. The host always gets it; anyone else needs
        the same mod standing the controls check (``is_host_or_mod``)."""
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, AMAView) or view._closed:
            await host_reply(interaction, GAME_ENDED_REPLY)
            return
        if not is_host_or_mod(interaction, view.host_id):
            await host_reply(interaction, NOT_HOST_REPLY)
            return
        panel = GameHostPanel(
            self.bot, game_id, build_host_panel_layout(view.game_format),
            host_panel_handlers(view.game_format),
            view_type=AMAView,
        )
        await send_panel(interaction, panel, label="ama host panel")

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
        mode = options.get("mode", "unfiltered")
        game_format = normalize_format(options.get("format"))
        # Per-server dials from the AMA dashboard panel: the role pinged on a
        # new hot seat (none by default) and how many answers make a turn.
        # A guild that has never answered the ping dial (OPT_PING_ROLE) and
        # already has an @AMA role — the one the retired by-name lookup
        # pinged — has it seeded once here; a cleared dial stays cleared.
        game_opts = await get_game_options(self.db, "ama", guild_id)
        ping_role_id = await resolve_ping_role_id(
            self.db, guild_id, game_opts, getattr(channel, "guild", None)
        )
        per_turn = resolve_questions_per_turn(game_opts.get(OPT_QUESTIONS_PER_TURN))
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ama",
            state="open",
            payload={
                "mode": mode,
                "format": game_format,
                "questions": [],
                "hot_seat_id": None,
                "hot_seat_rotations": 0,
                "total_passed": 0,
                "total_answered": 0,
                "ping_role_id": ping_role_id,
                "questions_per_turn": per_turn,
            },
        )

        launch_guild = getattr(channel, "guild", None)

        log.info("Game %s (ama) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = AMAView(
            game_id, host_id, mode, self.db, self.bot, game_format=game_format,
            ping_role_id=ping_role_id, per_turn=per_turn, host_name=host_name,
        )
        self.bot.active_views[game_id] = view

        try:
            msg = await retry_transient(lambda: channel.send(view=view), label="ama launch")
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ama launch lacked send perms in channel %s", channel.id)
            return None
        view._game_msg = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])

        # The persistent bottom bar, posted and thereafter kept at the bottom
        # by the shared sticky placer (best-effort — the game is already
        # live). Nothing to replace on this first placement: the payload has
        # no bar id yet, so place() posts, records the id where recovery
        # reads it, and deletes nothing.
        bottom_view = AMABottomView(game_id, self.db, view, msg.jump_url)
        bottom_msg = None
        try:
            bar = self._open_bar(game_id, view, bottom_view)
            if launch_guild is not None:
                bottom_msg = await bar.place(launch_guild, channel)
        except Exception:  # noqa: BLE001 — the bottom bar is optional chrome; a failed placement must not stop the AMA launching
            log.warning(
                "ama launch: failed to post bottom bar in channel %s",
                channel.id, exc_info=True,
            )
        if bottom_msg is None:
            # No bar. Drop the panel again so the listener stops watching for
            # one; the AMA's own card carries the same Ask a Question button.
            self._bars.release(game_id)
            view._bar = None
        else:
            self.bot.active_views[f"{game_id}_bottom"] = bottom_view
        self._active_channels[channel.id] = game_id
        return game_id


async def setup(bot: "Bot"):
    cog = AMACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ama")
    play.add_command(cog.ama, override=True)
    bot.game_launchers["ama"] = cog.launch
    bot.game_recoverers["ama"] = cog.recover_game
