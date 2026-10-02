import asyncio
import io
import logging
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS, play_description
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.shared.anon_audit_service import (
    EVENT_REPLY_POSTED,
)
from bot_modules.core.layout import V2_EDIT, bind_present, card
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    GameEnd,
    finish_launch_response,
    create_game,
    get_active_game_by_id,
    get_game_payload,
    modify_payload,
    update_game_message,
    update_session,
    end_game,
    channel_name,
)
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.launch_guard import no_tag_match_message, refuse_launch
from bot_modules.games.command_groups import play
from bot_modules.games.utils.host_panel import (
    GAME_ENDED_REPLY,
    NOT_HOST_REPLY,
    GameHostPanel,
    hand_over,
    reply as host_reply,
    send_panel,
)
from bot_modules.games.ffa.layouts import (
    ALIAS_ID,
    CLOSE_LABEL,
    HELP_ID,
    HOST_CLOSE_ID,
    HOST_NEXT_ID,
    REPLY_ID,
    build_host_panel_layout,
    build_prompt_layout,
    build_recap_layout,
)
from bot_modules.games.ffa.logic import note_reply, recap_summary
from bot_modules.games.ffa.prompts import label_for_kind
from bot_modules.games.board_sticky_service import redraw_embed_era_lobby
from bot_modules.games.utils.question_source import (
    get_ffa_prompt,
    has_matching_questions,
    channel_allows_nsfw,
)
from bot_modules.shared.quote_renderer import (
    THEMES,
    card_filename,
    render_animated_quote_card,
)
# Reuse the confession bot's anonymous-identity machinery so replies look and
# behave exactly like confession replies. These live in the confessions DB
# tables, which share the same SQLite file as the games DB.
from bot_modules.confessions.service import (
    init_db as init_confessions_db,
    clear_anon_identities,
    get_or_assign_anon_identity,
    get_ephemeral_anon_identity,
    anon_name_from_index,
    anon_circle_from_index,
    build_anon_reply,
)
from bot_modules.core.utils import is_host_or_mod

log = logging.getLogger(__name__)

CARD_STEM = "ffa"

# Theme per prompt type on the banner card — truth reads cool/blue, dare
# reads hot/pink. The prompt *card* carries no colour: the label says which.
_THEME_FOR_LABEL = {"TRUTH": "midnight", "DARE": "rose"}

# Embed mode serves truths unless the host asks for dares: a third of the
# bank's dares want a voice note, a photo or a nickname change, none of which
# an anonymous text box can deliver (anon-tail-70). ``/games play ffa
# kind:dare`` and the banner card (open chat, no box) still draw them.
EMBED_DEFAULT_KIND = "truth"

REPLY_HELP = (
    "🎭 **Replying to a Truth or Dare**\n"
    "Your reply is posted by the bot with no name attached.\n\n"
    "• **Reply** — you keep the *same* anonymous nickname for this "
    "prompt, so people can follow your back-and-forth.\n"
    "• **New Alias** — you get a *fresh* nickname every time, so "
    "even your own replies can't be linked together.\n"
    "• The host (or a mod) has **Next** and **Close** on their private host "
    "panel (`/games host`): Next pulls another prompt; Close ends the game "
    "with a recap of which prompts drew the most replies, and pays everyone "
    "who replied.\n\n"
    "Mods can still see who actually sent a reply (logged for safety)."
)


async def _resolve_card_image(guild: discord.Guild | None, bot, host_id: int) -> bytes | None:
    """Bytes for the card background — the server avatar, host avatar fallback.

    The card *is* the deliverable, so this tries hard to return something:
    guild icon first, then the host's avatar if the server has no icon.
    Returns None only if everything fails.
    """
    if guild is not None and guild.icon is not None:
        try:
            return await guild.icon.replace(size=512).read()
        except discord.HTTPException:
            log.warning("ffa: failed to read guild icon for %s", getattr(guild, "id", "?"))
    member = guild.get_member(host_id) if guild else None
    user = member
    if user is None:
        try:
            user = await bot.fetch_user(host_id)
        except discord.HTTPException:
            user = None
    if user is not None:
        try:
            return await user.display_avatar.with_size(512).read()
        except discord.HTTPException:
            log.warning("ffa: failed to read host avatar for %s", host_id)
    return None


async def finish_ffa_game(bot, db, game_id: str, channel) -> GameEnd | None:
    """The host's Close: retire every prompt's buttons, post the recap, drop
    the prompts' anonymous-alias rows, and pay the repliers.

    Until 2026-09-04 an embed-mode game never resolved — no button, no recap,
    and ``ffa`` sat in ``NO_ROSTER_TYPES`` so even the 24h sweep paid nobody
    (anon-tail-70). Each ``prompts`` entry now records its ``repliers``; that
    list is the roster here and the one ``game_roster`` rebuilds for the
    sweep and ``/games end``. Returns what ``end_game`` archived, or None
    when another path already ended the game.
    """
    bot.active_views.pop(game_id, None)
    payload = await get_game_payload(db, game_id)
    entries = [e for e in (payload.get("prompts") or []) if isinstance(e, dict)]

    # Every prompt message carries its own live view; each becomes its
    # closed frame — the prompt and its count, no buttons — so a late reply
    # can't land on a closed game. Best-effort — a prompt deleted mid-game
    # is simply skipped. The frame is rebuilt from the entry, so a prompt
    # still an embed on screen closes as a card too.
    for entry in entries:
        mid = entry.get("message_id")
        if not mid:
            continue
        try:
            msg = await channel.fetch_message(int(mid))
            await msg.edit(view=card(build_prompt_layout(
                str(entry.get("prompt") or ""), str(entry.get("label") or "TRUTH"),
                reply_count=int(entry.get("reply_count") or 0), closed=True,
            )), **V2_EDIT)
        except discord.HTTPException:
            log.debug("ffa: could not retire prompt %s on close", mid, exc_info=True)

    guild = getattr(channel, "guild", None)
    summary = recap_summary(entries)
    roster = list(summary["repliers"]) if summary else []
    if summary is None or summary["total_replies"] == 0:
        await channel.send(
            f"{GAME_ICONS['ffa']} Anonymous Truth or Dare closed — no replies came in, "
            "so there's nothing to recap."
        )
    else:
        payout_line = None
        if guild is not None:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(bot, guild.id, "ffa")
        await channel.send(view=card(build_recap_layout(summary, payout_line=payout_line)))

    # The prompts can no longer be replied to, so their user → alias rows
    # have nothing left to serve (anon-tail-69).
    if guild is not None:
        try:
            clear_anon_identities(
                bot.ctx.db_path, guild.id,
                [int(e["message_id"]) for e in entries if e.get("message_id")],
            )
        except Exception:
            log.exception("ffa: failed to clear anonymous identities for game %s", game_id)

    log.info("Game %s (ffa) closed — %d repliers, %d prompts", game_id, len(roster), len(entries))
    return await end_game(
        db, game_id,
        player_count=len(roster), round_count=len(entries), payload=payload,
        bot=bot, player_ids=roster, reason="ended",
    )


def _find_prompt_entry(payload: dict, message_id: int) -> dict | None:
    """The per-message ``prompts`` entry for ``message_id`` (or None).

    Every posted prompt gets its own entry — each embed message stays
    independently replyable, so its running reply-count lives on the entry
    rather than in one game-wide field.
    """
    for entry in payload.get("prompts") or []:
        if int(entry.get("message_id", 0)) == int(message_id):
            return entry
    return None


# ---------------------------------------------------------------------------
# Card mode (── /games play ffa ──)
# A prompt card with anonymous replies posted back into the channel and a
# live reply-count line. Stateful: the view is bound to the card message
# and re-registered on restart via recover_game. The host's Close (on the
# host panel) ends the game with a recap and pays the repliers
# (finish_ffa_game); the 24h cleanup sweep and /games end archive and pay
# the same roster.
# ---------------------------------------------------------------------------

class FFAEmbedReplyModal(discord.ui.Modal, title="Anonymous Reply"):
    answer = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.paragraph,
        placeholder="Posted anonymously in this channel…",
        max_length=1000,
    )

    def __init__(self, game_view: "FFAEmbedView", *, ephemeral_identity: bool):
        super().__init__()
        self.game_view = game_view
        self.ephemeral_identity = ephemeral_identity

    async def on_submit(self, interaction: discord.Interaction):
        view = self.game_view
        if (
            interaction.guild is None
            or interaction.channel is None
            or isinstance(interaction.channel, (discord.ForumChannel, discord.CategoryChannel))
        ):
            await interaction.response.send_message(
                "These reply buttons only work inside a server channel.", ephemeral=True
            )
            return

        content = str(self.answer.value).strip()
        if not content:
            await interaction.response.send_message("Your reply can't be empty.", ephemeral=True)
            return

        # Extra network hops ahead (post + embed edit + audit) — defer so we
        # never bump into the 3s modal-response deadline.
        await interaction.response.defer(ephemeral=True)

        # The game may have been closed between opening this modal and now;
        # bail so we don't resurrect a closed embed's title/count.
        if not await get_active_game_by_id(view.db, view.game_id):
            await interaction.followup.send("This game has already been closed.", ephemeral=True)
            return

        bot = cast("Bot", interaction.client)
        db_path = bot.ctx.db_path
        guild_id = interaction.guild.id
        # Identity keyed by the EMBED MESSAGE id: stable per-user for
        # "anonymous", fresh each time for "super anonymous".
        root_id = view._game_msg.id if view._game_msg else 0
        if self.ephemeral_identity:
            name_idx, emoji_idx = get_ephemeral_anon_identity(db_path, guild_id, root_id)
        else:
            name_idx, emoji_idx = get_or_assign_anon_identity(
                db_path, guild_id, root_id, interaction.user.id
            )
        body = build_anon_reply(
            content,
            is_op=False,
            circle=anon_circle_from_index(emoji_idx),
            anon_name=anon_name_from_index(name_idx),
        )

        # Post as a Discord reply to the prompt embed so it's clear which prompt
        # this answers (multiple FFAs can share a channel). fail_if_not_exists
        # keeps a deleted embed from erroring the reply.
        assert interaction.channel_id is not None
        try:
            reply_msg = await interaction.channel.send(
                body,
                allowed_mentions=discord.AllowedMentions.none(),
                reference=discord.MessageReference(
                    message_id=root_id,
                    channel_id=interaction.channel_id,
                    guild_id=guild_id,
                    fail_if_not_exists=False,
                ),
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Couldn't post your reply — please try again.", ephemeral=True
            )
            return

        # Bump THIS message's running count and refresh its own card. Each
        # posted prompt tracks replies independently, so a reply to an earlier
        # prompt never disturbs a later one's count. The replier id goes in
        # too — the roster the close pays. It is recorded for payout only:
        # ``ffa`` is in ``game_roster.ANON_ROSTER_TYPES``, so neither this
        # write's session touch nor the close merges it into the games
        # session, and /recap never names a replier.
        replier_id = interaction.user.id

        def _bump(payload):
            entry = _find_prompt_entry(payload, root_id)
            if entry is not None:
                note_reply(entry, replier_id)

        payload = await modify_payload(view.db, view.game_id, _bump)
        try:
            entry = _find_prompt_entry(payload, root_id)
            await view.show_count(int(entry.get("reply_count", 0)) if entry else 0)
        except Exception:  # noqa: BLE001 — best-effort count refresh spans card build + Discord edit; the reply is already recorded and must not be re-reported as failed
            log.debug("ffa: failed to update the reply count", exc_info=True)

        # Audit log records the real user behind the pseudonym.
        try:
            await audit_anonymous(
                bot, bot.games_db, interaction.guild,
                game_type="ffa", user=interaction.user,
                event=EVENT_REPLY_POSTED,
                content=content, label="FFA Anonymous Reply",
                game_id=view.game_id,
                message_id=reply_msg.id, channel_id=interaction.channel_id,
                extra={"root_id": str(root_id)},
            )
        except Exception:  # noqa: BLE001 — audit write spans DB + Discord; the reply is already posted and must not be re-reported as failed
            log.debug("ffa: failed to write audit log", exc_info=True)

        await interaction.followup.send("✅ Your reply has been posted!", ephemeral=True)


class FFAEmbedView(discord.ui.LayoutView):
    """Stateful persistent view bound to a single prompt card message.

    Carries the anonymous-reply buttons (identity keyed by the card's
    message id) and the state needed to keep the reply-count line in sync —
    ``_rebuild`` redraws the card from ``reply_count``. ⏭️ Next and 🏁 Close
    are on the ephemeral host panel, which calls :meth:`next_prompt` and
    :meth:`close_game` on whichever prompt's view is the game's live one
    (the latest). Re-registered after a restart by
    :meth:`FFACog.recover_game`.
    """

    def __init__(self, game_id: str, host_id: int, text: str, label: str,
                 db, bot, kind: str = "random",
                 tags: list[str] | None = None, *, reply_count: int = 0):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.text = text
        self.label = label
        self.db = db
        self.bot = bot
        # Immutable launch filter — replayed by Next to re-roll from the
        # same selected set. Mutable per-game progress (the shown-prompt
        # "seen" set, reply_count) lives in the payload, not on the view;
        # ``reply_count`` here is the card's copy of this message's own.
        self.kind = kind
        self.tags = list(tags or [])
        self.reply_count = reply_count
        self._game_msg: discord.Message | None = None
        self._rebuild()

    def _rebuild(self) -> None:
        """Clear and re-add the card from current state, re-binding its
        buttons — a rebuild adds brand-new ``Button`` instances."""
        self.clear_items()
        for item in build_prompt_layout(self.text, self.label, reply_count=self.reply_count):
            self.add_item(item)
        bind_present(self, {REPLY_ID: self.reply_anon, ALIAS_ID: self.reply_super, HELP_ID: self.reply_help})

    async def show_count(self, reply_count: int) -> None:
        """A reply landed: this card's count, from the payload."""
        self.reply_count = reply_count
        self._rebuild()
        if self._game_msg is None:
            return
        try:
            await self._game_msg.edit(view=self, **V2_EDIT)
        except discord.HTTPException:
            pass

    async def _guard_active(self, interaction: discord.Interaction) -> bool:
        row = await get_active_game_by_id(self.db, self.game_id)
        if not row:
            await interaction.response.send_message(
                "This game is no longer active.", ephemeral=True
            )
            return False
        return True

    async def reply_anon(self, interaction: discord.Interaction) -> None:
        if not await self._guard_active(interaction):
            return
        await interaction.response.send_modal(FFAEmbedReplyModal(self, ephemeral_identity=False))

    async def reply_super(self, interaction: discord.Interaction) -> None:
        if not await self._guard_active(interaction):
            return
        await interaction.response.send_modal(FFAEmbedReplyModal(self, ephemeral_identity=True))

    async def next_prompt(self, interaction: discord.Interaction) -> None:
        """⏭️ Next, from the host panel."""
        log.info("%s pressed Next in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(
                "❌ Only the host or a mod can pull the next prompt.", ephemeral=True
            )
            return
        if not await self._guard_active(interaction):
            return
        await interaction.response.defer()

        allow_nsfw = channel_allows_nsfw(self._game_msg.channel if self._game_msg else interaction.channel)
        payload = await get_game_payload(self.db, self.game_id)
        # Games posted before the Next button existed have no "seen" key — seed
        # it with the current prompt so the first advance can't repeat it.
        seen = list(payload.get("seen") or [self.text])

        picked = await get_ffa_prompt(
            self.db, kind=self.kind, tags=self.tags or None,
            allow_nsfw=allow_nsfw, exclude=seen,
        )
        if picked is None:
            # Selected set exhausted — reset and re-roll, skipping only the
            # current prompt so the reset boundary doesn't immediately repeat.
            seen = [self.text]
            picked = await get_ffa_prompt(
                self.db, kind=self.kind, tags=self.tags or None,
                allow_nsfw=allow_nsfw, exclude=seen,
            )
        if picked is None:  # single-prompt set — nothing else to show
            await interaction.followup.send(
                "That's the only prompt in this set — nothing new to pull.", ephemeral=True
            )
            return

        label, text = picked

        # Post the next prompt as a NEW message and leave every earlier prompt
        # fully interactive — its Reply / New Alias buttons keep working, and its
        # own reply-count line keeps ticking. Each message carries its own view
        # (bound to its own id) and its own `prompts` entry; identity and the
        # reply-reference already key off the per-message id, so replies to
        # different prompts never collide. The game-wide `seen` set still means
        # Next never repeats a prompt across the whole game. The DB anchor stays
        # on the launch message so recovery can walk the whole `prompts` list.
        # Send first: if it fails, nothing about the existing prompts changes.
        channel = self._game_msg.channel if self._game_msg else interaction.channel
        if channel is None or isinstance(
            channel, (discord.ForumChannel, discord.CategoryChannel)
        ):
            await interaction.followup.send(
                "Couldn't post the next prompt in this channel.", ephemeral=True
            )
            return
        new_view = FFAEmbedView(
            self.game_id, self.host_id, text, label, self.db, self.bot,
            kind=self.kind, tags=self.tags,
        )
        try:
            new_msg = await channel.send(view=new_view)
        except discord.HTTPException:
            log.debug("ffa: failed to post next prompt", exc_info=True)
            await interaction.followup.send(
                "Couldn't post the next prompt — please try again.", ephemeral=True
            )
            return

        new_view._game_msg = new_msg
        self.bot.active_views[self.game_id] = new_view
        self.bot.add_view(new_view, message_id=new_msg.id)

        def _advance(p):
            p["prompt"] = text
            p["label"] = label
            p["seen"] = [*seen, text]
            p.setdefault("prompts", []).append(
                {"message_id": new_msg.id, "prompt": text, "label": label, "reply_count": 0, "repliers": []}
            )

        await modify_payload(self.db, self.game_id, _advance)

    async def reply_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(REPLY_HELP, ephemeral=True)

    async def close_game(self, interaction: discord.Interaction) -> None:
        """🏁 Close, from the host panel — host or mod, behind the usual
        confirm popup: the ending the game never had (anon-tail-70) — see
        :func:`finish_ffa_game`."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, CLOSE_LABEL, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(
                "❌ Only the host or a mod can close the game.", ephemeral=True
            )
            return
        if not await self._guard_active(interaction):
            return
        channel = self._game_msg.channel if self._game_msg else interaction.channel

        async def _confirmed(confirm: discord.Interaction) -> None:
            if not await get_active_game_by_id(self.db, self.game_id):
                await confirm.followup.send("This game has already been closed.", ephemeral=True)
                return
            await finish_ffa_game(self.bot, self.db, self.game_id, channel)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )


class FFACog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self):
        # Ensure the shared anonymous-identity pool tables exist (idempotent).
        # Embed-mode reply views are per-message and recovered via recover_game;
        # the banner mode has no interactive state.
        init_confessions_db(self.bot.ctx.db_path)

    @app_commands.command(
        name="ffa",
        description=play_description("ffa"),
    )
    @app_commands.describe(
        kind="Truth (default), Dare, or a random pick — dares need a room that can act them out",
        tags="Comma-separated tags to filter the prompt bank",
        prompt="Write your own prompt instead of pulling a random one (optional)",
    )
    async def ffa(
        self,
        interaction: discord.Interaction,
        kind: Literal["random", "truth", "dare"] = EMBED_DEFAULT_KIND,
        tags: str = "",
        prompt: str | None = None,
    ):
        await self.start_ffa(interaction, kind, tags, prompt, banner=False)

    @app_commands.command(
        name="ffa_banner",
        description=play_description("ffa_banner"),
    )
    @app_commands.describe(
        kind="Truth, Dare, or a random pick (default: random)",
        tags="Comma-separated tags to filter the prompt bank",
        prompt="Write your own prompt instead of pulling a random one (optional)",
    )
    async def ffa_banner(
        self,
        interaction: discord.Interaction,
        kind: Literal["random", "truth", "dare"] = "random",
        tags: str = "",
        prompt: str | None = None,
    ):
        await self.start_ffa(interaction, kind, tags, prompt, banner=True)

    async def start_ffa(
        self,
        interaction: discord.Interaction,
        kind: str = "random",
        tags: str = "",
        prompt: str | None = None,
        *,
        banner: bool = False,
    ):
        cmd = "ffa_banner" if banner else "ffa"
        log.info("%s used /games play %s in #%s", interaction.user.display_name, cmd, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "ffa")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        # ffa is not bank-only (a host prompt or an untagged draw always
        # works), so the guard skips its bank; a tag filter with no match is
        # still refused here, with the guard's copy. The kind rides along:
        # ``kind:dare tags:wren`` where every wren row is a truth is a miss
        # here, not a launch that comes back empty and blames permissions
        # (anon-tail-76).
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list and not (prompt or "").strip() and not await has_matching_questions(
            self.db, "ffa", tag_list, allow_nsfw=channel_allows_nsfw(interaction.channel),
            kind=kind if kind in ("truth", "dare") else None,
        ):
            await interaction.response.send_message(no_tag_match_message(tag_list), ephemeral=True)
            return

        await interaction.response.defer()
        launcher = self.launch_banner if banner else self.launch
        game_id = await launcher(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"kind": kind, "tags": tag_list, "prompt": prompt or ""},
        )
        perms = (
            "**View Channel**, **Send Messages**, and **Attach Files**."
            if banner
            else "**View Channel**, **Send Messages**, and **Embed Links**."
        )
        await finish_launch_response(
            interaction, game_id,
            perms_hint=f"I couldn't start the game here. Please grant me {perms}",
        )
        # Nothing host-only is on the card, so hand the host their controls
        # now rather than make them go find ``/games host``. A banner game
        # is archived the moment it posts, so it has no live view and gets
        # no panel.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for an Anonymous Truth or Dare game: ⏭️ Next and
        🏁 Close, acting on the latest prompt's view. The host always gets
        it; anyone else needs the same mod standing the controls check
        (``is_host_or_mod``)."""
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, FFAEmbedView):
            await host_reply(interaction, GAME_ENDED_REPLY)
            return
        if not is_host_or_mod(interaction, view.host_id):
            await host_reply(interaction, NOT_HOST_REPLY)
            return
        panel = GameHostPanel(
            self.bot, game_id, build_host_panel_layout(),
            {HOST_NEXT_ID: "next_prompt", HOST_CLOSE_ID: "close_game"},
            view_type=FFAEmbedView,
        )
        await send_panel(interaction, panel, label="ffa host panel")

    async def _resolve_prompt(self, channel, options: dict, *, default_kind: str = "random"):
        """Resolve (kind, tags, (label, text)) for a launch. (label, text) is None on miss.

        ``default_kind`` is what a launch with no ``kind`` draws — embed mode
        passes :data:`EMBED_DEFAULT_KIND` (truth), the banner stays random.
        """
        kind = (options.get("kind") or default_kind).lower()
        tags = list(options.get("tags") or [])
        custom = (options.get("prompt") or "").strip()
        if custom:
            return kind, tags, (label_for_kind(kind), custom)
        picked = await get_ffa_prompt(
            self.db, kind=kind, tags=tags or None,
            allow_nsfw=channel_allows_nsfw(channel),
        )
        return kind, tags, picked

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Card mode (default). A prompt card + in-channel anonymous replies.

        Interaction-free (slash command + scheduler). Returns game_id, or None.
        A launch that names no ``kind`` draws truths (anon-tail-70).
        """
        kind, tags, picked = await self._resolve_prompt(channel, options, default_kind=EMBED_DEFAULT_KIND)
        if picked is None:
            log.info("ffa embed launch: no prompt for kind=%s tags=%s in channel %s", kind, tags, channel.id)
            return None
        label, text = picked

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ffa",
            state="open",
            payload={
                "prompt": text,
                "label": label,
                "kind": kind,
                "tags": tags,
                "mode": "embed",
                "seen": [text],
                # One entry per posted prompt message — seeded once the launch
                # message id is known (below). Each entry drives an independently
                # replyable card with its own reply-count line.
                "prompts": [],
            },
        )
        view = FFAEmbedView(game_id, host_id, text, label, self.db, self.bot, kind=kind, tags=tags)
        self.bot.active_views[game_id] = view

        try:
            msg = await retry_transient(lambda: channel.send(view=view), label="ffa launch")
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ffa embed launch lacked send perms in channel %s", channel.id)
            return None

        view._game_msg = msg
        await update_game_message(self.db, game_id, msg.id)

        def _seed(p):
            p.setdefault("prompts", []).append(
                {"message_id": msg.id, "prompt": text, "label": label, "reply_count": 0, "repliers": []}
            )

        await modify_payload(self.db, game_id, _seed)
        await update_session(self.db, channel.id, game_id, [host_id])
        log.info("Game %s (ffa/embed) posted by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        return game_id

    async def launch_banner(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Banner mode. Drops a Truth-or-Dare card in the channel. Returns game_id, or None.

        No thread, no buttons — members just chat freely in the channel. The
        anonymous-reply flow lives on the embed command (:meth:`launch`).
        """
        kind, tags, picked = await self._resolve_prompt(channel, options)
        if picked is None:
            log.info("ffa banner launch: no prompt for kind=%s tags=%s in channel %s", kind, tags, channel.id)
            return None
        label, text = picked

        guild = getattr(channel, "guild", None)
        image_bytes = await _resolve_card_image(guild, self.bot, host_id)
        if image_bytes is None:
            log.warning("ffa banner launch could not resolve a card image in channel %s", channel.id)
            return None

        try:
            card_bytes = await asyncio.to_thread(
                render_animated_quote_card,
                text,
                author_name=label,
                avatar_bytes=image_bytes,
                theme=THEMES[_THEME_FOR_LABEL.get(label, "rose")],
                pfp_shape="none",
            )
        except Exception:
            log.exception("ffa banner launch failed to render card in channel %s", channel.id)
            return None

        # Post the card (bare image — no thread, no buttons).
        try:
            msg = await channel.send(file=discord.File(io.BytesIO(card_bytes), filename=card_filename(CARD_STEM, card_bytes)))
        except discord.Forbidden:
            log.warning("ffa banner launch lacked send perms in channel %s", channel.id)
            return None

        # Record the play to history for stats (fire-and-forget: there's no
        # interactive state — people just chat in the channel).
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ffa",
            message_id=msg.id,
            state="open",
            payload={
                "prompt": text,
                "label": label,
                "kind": kind,
                "tags": tags,
                "mode": "banner",
            },
        )
        log.info("Game %s (ffa/banner) posted by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        # A card is not a games session: no roster, no session — the row is
        # archived at once for the play stats and nothing else.
        await end_game(self.db, game_id)
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the stateful card-mode FFA views after a restart.

        A game may have several posted prompts, each an independently replyable
        message. We rebuild one view per ``prompts`` entry (bound to its own
        message id) so every prompt's buttons come back alive, not just the
        latest; a prompt still an embed on screen (posted before the
        conversion) is redrawn as a card at once. Messages that were deleted
        while the bot was down are skipped.

        Banner games are fire-and-forget (ended immediately, never persisted as
        active), so only embed games recover.
        """
        if payload.get("mode") != "embed":
            return False
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        kind = payload.get("kind") or "random"
        tags = list(payload.get("tags") or [])

        entries = list(payload.get("prompts") or [])
        if not entries:
            # Legacy game from before per-message prompts existed: it only ever
            # had the single anchor message. Synthesize its entry and persist it
            # so subsequent replies find a home for their count.
            entries = [{
                "message_id": message.id,
                "prompt": payload.get("prompt", "") or "",
                "label": payload.get("label") or "TRUTH",
                "reply_count": int(payload.get("reply_count", 0)),
            }]

            def _migrate(p):
                p["prompts"] = entries

            await modify_payload(self.db, game_id, _migrate)

        recovered = 0
        latest_view: FFAEmbedView | None = None
        for entry in entries:
            mid = entry.get("message_id")
            if not mid:
                continue
            # The anchor message is already fetched; the rest we fetch by id and
            # skip any that were deleted while the bot was offline.
            if int(mid) == int(message.id):
                msg = message
            else:
                try:
                    msg = await channel.fetch_message(int(mid))
                except discord.HTTPException:
                    continue
            label = entry.get("label") or "TRUTH"
            text = entry.get("prompt", "") or ""
            view = FFAEmbedView(
                game_id, host_id, text, label, self.db, self.bot,
                kind=kind, tags=tags, reply_count=int(entry.get("reply_count") or 0),
            )
            view._game_msg = msg
            self.bot.add_view(view, message_id=msg.id)
            await redraw_embed_era_lobby(view, msg)
            latest_view = view
            recovered += 1

        if latest_view is None:
            return False
        self.bot.active_views[game_id] = latest_view
        log.info(
            "Recovered ffa (embed) game %s (%d prompt message(s)) in #%s",
            game_id, recovered, getattr(channel, "name", channel.id),
        )
        return True


async def setup(bot: "Bot"):
    cog = FFACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ffa")
    bot.tree.remove_command("ffa_banner")
    play.add_command(cog.ffa, override=True)
    play.add_command(cog.ffa_banner, override=True)
    bot.game_launchers["ffa"] = cog.launch
    bot.game_launchers["ffa_banner"] = cog.launch_banner
    bot.game_recoverers["ffa"] = cog.recover_game
