"""Quests and the passive faucets: triggers, photo posts, boosts, staff comp."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.layout import card
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    revoke_role_perks,
)
from bot_modules.economy.quest_layouts import build_quest_board_layout
from bot_modules.economy.quest_views import (
    QuestClaimView,
    refresh_signoff_board,
)
from bot_modules.economy.quests_service import (
    fire_trigger_inline,
    fire_trigger_quests,
    load_member_quest_board,
    reroll_quote,
)
from bot_modules.economy import photo_service as photo_svc
from bot_modules.economy import quests_service as quests_svc
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
)

if TYPE_CHECKING:
    pass

from bot_modules.economy.cog.base import (
    EconomyBase,
)
from bot_modules.economy.cog.common import (
    _TRIGGER_CACHE_TTL,
    _has_image_attachment,
)

log = logging.getLogger("dungeonkeeper.economy")


class QuestsMixin(EconomyBase):
    """Quests and the passive faucets: triggers, photo posts, boosts, staff comp.

    Mixed into ``EconomyCog`` (economy_cog.py), which was split by area in
    standards audit row 4.29. Moved verbatim: the attributes it reads
    (``self.bot``, the StickyPanels, the caches) are set in the cog's
    ``__init__``.
    """

    # Set in EconomyCog.__init__.
    _trigger_cache: dict[int, tuple[float, list[quests_svc.TriggerQuest]]]
    _photo_opts: dict[int, tuple[float, int]]

    _PHOTO_OPTS_TTL = 60.0  # channel-id cache staleness bound (seconds)

    async def send_quests_panel(self, interaction: discord.Interaction) -> None:
        """The member's private quest list + claim UI.

        Shared by the ``/bank quests`` command and the leaderboard panel's
        "Show my quests" button, so both open the exact same ephemeral view.
        """
        assert interaction.guild is not None
        guild = interaction.guild

        settings, quests_state, board_meta = await asyncio.to_thread(
            self._load_quests_state, guild.id, interaction.user.id
        )
        if await self._refuse_disabled(interaction, settings):
            return

        if not quests_state:
            items = build_quest_board_layout(settings, [])
            await interaction.response.send_message(view=card(items), ephemeral=True)
            return

        claimable = [q for q in quests_state if q["state"] == "claimable"]
        rerollable = board_meta.get("rerollable") or []
        show_reroll = bool(board_meta.get("reroll_ok") and rerollable)
        view = QuestClaimView(
            self.bot.ctx, settings, guild, claimable,
            rerollable=rerollable if show_reroll else None,
            reroll_cost=board_meta.get("reroll_cost"),
            local_day=str(board_meta.get("local_day") or ""),
            detailable=quests_state,
        )
        await interaction.response.send_message(view=view, ephemeral=True)
        # ExpiringLayoutView.on_timeout needs the sent message to edit; without
        # this it's a no-op, and the card would sit with live buttons forever.
        view.message = await interaction.original_response()


    def _load_quests_state(
        self, guild_id: int, user_id: int
    ) -> tuple[EconSettings, list[dict], dict]:
        """Load active quests with the caller's per-period claim state.

        Community quests carry their running total (no self-claim); daily/weekly
        carry ``claimable``/``pending``/``done`` for this period's key.
        """
        with self.bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return settings, [], {}
            offset = get_tz_offset_hours(conn, guild_id)
            day = local_day_for(time.time(), offset)
            out = load_member_quest_board(conn, settings, guild_id, user_id, day)
            # Reroll offer: board quests untouched this period (no claim, no
            # counted progress). One free swap per guild-local day, then paid
            # ones up to the daily cap — `reroll_cost` is 0/price/None, and
            # None is the only state that hides the select.
            cost = reroll_quote(conn, settings, guild_id, user_id, day)
            meta = {
                "local_day": day,
                "reroll_cost": cost,
                "reroll_ok": cost is not None,
                "rerollable": [
                    {"id": q["id"], "title": q["title"], "qtype": q["qtype"]}
                    for q in out
                    if q["qtype"] in ("daily", "weekly", "monthly")
                    and q["state"] not in ("done", "pending")
                    and not q.get("progress_current")
                ],
            }
        return settings, out, meta


    @commands.Cog.listener("on_message")
    async def _on_trigger_message(self, message: discord.Message) -> None:
        """Auto-claim trigger-word quests when a member says the phrase.

        The message is the verification: an instant quest pays on the spot
        (reply + ✅), a sign-off quest files the pending claim and puts it on
        the mods' todo board. Repeats inside the period fall out silently via
        ``claim_trigger_word``'s ValueError.

        The compiled-pattern index is TTL-cached here because this fires for
        every message in the guild; the matching and the claim rules live in
        ``economy_quests_service`` beside the trigger-*kind* mechanic they
        share a board gate with.
        """
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        content = message.content or ""
        if not content:
            return

        guild_id = message.guild.id
        now = time.monotonic()
        cached = self._trigger_cache.get(guild_id)
        if cached is None or cached[0] <= now:

            def _load() -> list[quests_svc.TriggerQuest]:
                with self.bot.ctx.open_db() as conn:
                    return quests_svc.load_trigger_index(conn, guild_id)

            try:
                triggers = await asyncio.to_thread(_load)
            except Exception:
                log.exception("econ trigger: failed to load quests for %s", guild_id)
                return
            self._trigger_cache[guild_id] = (now + _TRIGGER_CACHE_TTL, triggers)
        else:
            triggers = cached[1]
        if not triggers:
            return

        channel = message.channel
        # Threads count as their parent channel.
        scope = (channel.id, getattr(channel, "parent_id", None))
        for trig in quests_svc.matching_triggers(triggers, content, scope):
            await self._complete_trigger_quest(message, member, trig)


    async def _complete_trigger_quest(
        self,
        message: discord.Message,
        member: discord.Member,
        trig: quests_svc.TriggerQuest,
    ) -> None:
        """Claim a matched trigger quest for the message author, best-effort."""
        guild = message.guild
        assert guild is not None
        booster = member.premium_since is not None

        def _claim():
            with self.bot.ctx.open_db() as conn:
                return quests_svc.claim_trigger_word(
                    conn, guild.id, member.id, trig,
                    booster=booster, now=time.time(),
                )

        try:
            settings, outcome = await asyncio.to_thread(_claim)
        except ValueError:
            # Already claimed this period, quest window closed, deactivated
            # since the cache load, or off-board — every repeat message would
            # hit this, so stay quiet rather than spam the channel.
            return
        except Exception:
            log.exception(
                "econ trigger: claim failed for quest %s", trig.quest_id
            )
            return

        await self._announce_quest_claim(message, member, settings, outcome)


    async def _announce_quest_claim(
        self,
        message: discord.Message,
        member: discord.Member,
        settings: EconSettings,
        outcome,
    ) -> None:
        """React for an auto-claimed quest (trigger phrase or photo).

        Silent otherwise — no channel reply, no DM. Wallet/quest log carries
        the news, same as every other trigger kind.
        """
        guild = message.guild
        assert guild is not None

        if outcome.state == "paid":
            reaction = "✅"
        else:
            # Sign-off trigger quest: the phrase files the claim; a mod still
            # approves the payout from the todo board's sign-off section.
            await refresh_signoff_board(self.bot, guild.id)
            reaction = "📝"

        try:
            await message.add_reaction(reaction)
        except discord.HTTPException:
            log.debug("econ trigger: failed to react", exc_info=True)


    async def _photo_channel(self, guild_id: int) -> int:
        """TTL-cached ``read_photo_channel`` — one DB read per guild per TTL.

        The cache stays here rather than in the service: it exists because
        ``on_message`` fires for every message in the guild, which is a
        listener concern, not a rule about how photo posts pay. Only image
        posts in the configured channel go past this to the service.
        """
        now = time.monotonic()
        cached = self._photo_opts.get(guild_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        def _read() -> int:
            with self.bot.ctx.open_db() as conn:
                return photo_svc.read_photo_channel(conn, guild_id)

        try:
            channel_id = await asyncio.to_thread(_read)
        except Exception:
            log.exception("econ photo: channel read failed in guild %s", guild_id)
            return 0
        self._photo_opts[guild_id] = (now + self._PHOTO_OPTS_TTL, channel_id)
        return channel_id


    @commands.Cog.listener("on_message")
    async def _on_photo_post(self, message: discord.Message) -> None:
        """Pay for an image posted in the Photo Challenge channel.

        Guards cheapest-first: guild/bot check, image check, TTL-cached
        channel gate, then a DB eligibility pre-check so a channel with
        nothing to pay never opens a write transaction. The payout split
        (flat participation award + stacked photo_post quest bonus, each
        once per guild-local day) lives in ``economy_photo_service``.
        """
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not _has_image_attachment(message):
            return
        channel_id = await self._photo_channel(message.guild.id)
        if channel_id == 0:
            return
        # A photo posted in a thread of the Photo Challenge channel should earn
        # too — match on the parent like the trigger-quest / games siblings.
        parent_id = getattr(message.channel, "parent_id", None)
        if channel_id not in (message.channel.id, parent_id):
            return

        guild_id = message.guild.id

        def _possible() -> bool:
            with self.bot.ctx.open_db() as conn:
                return photo_svc.payout_possible(conn, guild_id)

        try:
            eligible = await asyncio.to_thread(_possible)
        except Exception:
            log.exception(
                "econ photo: eligibility check failed in guild %s", guild_id
            )
            return
        if not eligible:
            return

        booster = member.premium_since is not None

        def _claim():
            with self.bot.ctx.open_db() as conn:
                return photo_svc.award_photo_post(
                    conn, guild_id, member.id,
                    channel_id=channel_id, booster=booster, now=time.time(),
                )

        try:
            result = await asyncio.to_thread(_claim)
        except Exception:
            log.exception("econ photo: claim failed in guild %s", guild_id)
            return
        if result is None:
            return
        settings, participation, fired = result
        if fired:
            # A quest outcome carries its own ✅ (paid) / 📝 (sign-off) react.
            for _quest, outcome in fired:
                await self._announce_quest_claim(message, member, settings, outcome)
        elif participation:
            # Flat award only (no quest fired) — acknowledge the post.
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                log.debug("econ photo: participation react failed", exc_info=True)


    @commands.Cog.listener("on_member_update")
    async def _on_staff_comp_changed(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Project the staff perk comp when someone gains or loses mod status.

        The comp is derived from live roles, so the entitlement itself flips the
        instant the role does — but the member's personal ROLE is only rebuilt
        by a reconcile, and nothing else triggers one on a role change. Gaining
        staff applies the perks; losing it re-projects, which reverts the
        perk-set nickname and deletes the personal role when no real rental
        remains behind the comp.

        Cheap-exit first: role objects are identical on the vast majority of
        member updates (nick, avatar, timeout, presence), and this listener sees
        every one of them.
        """
        if before.roles == after.roles:
            return
        was_staff = self.bot.ctx.member_is_mod(before)
        now_staff = self.bot.ctx.member_is_mod(after)
        if was_staff == now_staff:
            return

        def _comp_on() -> bool:
            with self.bot.ctx.open_db() as conn:
                settings = load_econ_settings(conn, after.guild.id)
            return settings.enabled and settings.mod_perk_comp

        if not await asyncio.to_thread(_comp_on):
            return
        if now_staff:
            await apply_role_perks(
                self.bot, self.bot.ctx.db_path, after.guild.id, after.id
            )
        else:
            await revoke_role_perks(
                self.bot, self.bot.ctx.db_path, after.guild.id, after.id
            )


    @commands.Cog.listener("on_member_update")
    async def _on_boost_started(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Fire the boost trigger when a member starts boosting.

        Occurrence is the boost start timestamp, so one boost pays an event
        quest once even across gateway replays; boosting again later is a new
        occurrence. Nothing in the codebase watched premium_since transitions
        before this.
        """
        if before.premium_since is not None or after.premium_since is None:
            return
        guild_id = after.guild.id
        occurrence = str(int(after.premium_since.timestamp()))

        def _fire():
            with self.bot.ctx.open_db() as conn:
                return fire_trigger_inline(
                    conn,
                    guild_id,
                    "boost",
                    after.id,
                    occurrence=occurrence,
                    booster=True,
                )

        await asyncio.to_thread(_fire)


    @commands.Cog.listener("on_message")
    async def _on_media_post(self, message: discord.Message) -> None:
        """Fire the media-post trigger for any image a member posts.

        Channel scoping happens per quest (``trigger_channel_id``) inside
        ``fire_trigger_quests``, so an unscoped media quest counts every
        channel while a scoped one (e.g. #art) only counts there. Occurrence
        is the message, so event quests pay per image message — the sane
        cadence for this kind is daily/weekly, as the dashboard hint says.
        """
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not _has_image_attachment(message):
            return

        guild_id = message.guild.id
        channel = message.channel
        parent_id = getattr(channel, "parent_id", None)
        channel_ids = tuple(
            c for c in (channel.id, parent_id) if c is not None
        )
        booster = member.premium_since is not None

        def _claim():
            with self.bot.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                if not settings.enabled:
                    return None
                offset = get_tz_offset_hours(conn, guild_id)
                day = local_day_for(time.time(), offset)
                fired = fire_trigger_quests(
                    conn,
                    settings,
                    guild_id,
                    "media_post",
                    member.id,
                    local_day=day,
                    occurrence=str(message.id),
                    booster=booster,
                    channel_ids=channel_ids,
                )
                return settings, fired

        try:
            result = await asyncio.to_thread(_claim)
        except Exception:
            log.exception("econ media: claim failed in guild %s", guild_id)
            return
        if result is None:
            return
        settings, fired = result
        for _quest, outcome in fired:
            await self._announce_quest_claim(message, member, settings, outcome)
