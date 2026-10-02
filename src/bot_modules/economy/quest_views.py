"""Discord views for economy quests — the ``/bank quests`` claim UI and the
manager sign-off flow.

Components V2 since 2026-09-21: every card this feature sends is a
``LayoutView`` built from ``bot_modules/economy/quest_layouts.py``, not an
embed (``docs/plans/components_v2_migration.md`` wave 3a). Two surfaces
live here:

* ``QuestClaimView`` — the ephemeral ``/bank quests`` card. It combines the
  board list and its Detail/Claim/Reroll selects in one component tree — a
  V2 message has no separate content-vs-view slot to split them across the
  way the old embed + view pair did. Its Claim select claims the picked
  quest at click time (the ``period`` key is computed then, so a claim that
  straddles a day/week roll lands in the right bucket) and either confirms
  an instant payout or files a sign-off claim.

* The sign-off flow — how a manager approves or denies a claim. Pending
  claims are listed on the **todo board** (``todo/board_logic.py``); its
  Sign-Offs button opens :func:`open_signoff_picker`, an ephemeral pick-one
  select that leads to the claim's detail card and an Approve/Deny pair.
  Approve resolves straight away; Deny opens a reason modal first. Every
  handler is fail-safe: a service error surfaces as an ephemeral note,
  never a dead button.

  The Approve/Deny buttons are ``discord.ui.DynamicItem`` subclasses whose
  ``custom_id`` embeds the ``claim_id`` (``econ_claim:approve:<id>`` /
  ``econ_claim:deny:<id>``). Nothing posts a card carrying them any more —
  claims moved to the board — but they stay registered so the cards already
  sitting in bank channels from before the move are still clickable, and a
  click still routes after a restart once the cog re-registers the classes
  with ``bot.add_dynamic_items``. Those legacy cards are still the *embed*
  messages they were created as; the resolution repaint converts one to a
  card in place, exactly the way the style guide's "edit converting embed
  to card" case says (``content=None, embed=None, attachments=[]``, then
  ``view=``).

The claim-credit booster flag is always the *claimant's* boost status at the
moment of the credit — for instant claims that is the clicker, for sign-off it
is read from the claim row's ``user_id`` when a manager approves (not the
manager's own status).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any, cast
from functools import partial

import discord

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.layout import ExpiringLayoutView, card
from bot_modules.economy.logic import is_economy_manager, local_day_for
from bot_modules.economy.quest_layouts import (
    build_quest_board_layout,
    build_quest_claimed_layout,
    build_quest_detail_layout,
    build_signoff_card_layout,
)
from bot_modules.economy.view_helpers import card_name_fn
from bot_modules.economy.view_helpers import unit as _unit
from bot_modules.economy.quests import (
    channel_scope_line,
    quest_period,
    scope_worth_showing,
)
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy.signoff_notice import announce_signoff_outcome
from bot_modules.economy.quests_service import (
    claim_quest,
    deny_history,
    get_quest,
    pending_signoff_rows,
    reroll_board_slot,
    resolve_claim,
)
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
    member_is_booster,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review quest claims."
_CLAIM_VIEW_TIMEOUT = 180  # seconds; the ephemeral claim card is short-lived.
_DENY_REASON_MAX = 300


def can_manage_economy(member: discord.Member, settings: EconSettings) -> bool:
    """True for server admins or holders of the configured manager role.

    Canonical home for the economy-manager gate — the cog imports this so the
    ``/bank`` grant check and the sign-off buttons share one rule (defined here
    rather than in the cog to avoid a views→cog import cycle).
    """
    return is_economy_manager(
        is_admin=member.guild_permissions.administrator,
        role_ids=[r.id for r in member.roles],
        manager_role_id=settings.manager_role_id,
    )


# ── persistent sign-off buttons ───────────────────────────────────────────────


class QuestApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_claim:approve:(?P<cid>\d+)"),
):
    """Persistent Approve button; ``custom_id`` carries the claim id."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_claim:approve:{claim_id}",
            )
        )
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> QuestApproveButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction, self.claim_id, approve=True, deny_reason=None
        )


class QuestDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_claim:deny:(?P<cid>\d+)"),
):
    """Persistent Deny button; opens a reason modal, then resolves."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Deny",
                emoji="✖️",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_claim:deny:{claim_id}",
            )
        )
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> QuestDenyButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # Gate before opening the modal so a non-manager never even sees it.
        settings = await _load_settings(interaction, interaction.guild)
        member = interaction.user
        if settings is None or not isinstance(member, discord.Member):
            await _safe_ephemeral(interaction, "❌ This only works in a server.")
            return
        if not can_manage_economy(member, settings):
            await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
            return
        await interaction.response.send_modal(
            QuestDenyModal(self.claim_id, interaction.message)
        )


def _signoff_buttons_row(claim_id: int) -> "discord.ui.ActionRow[Any]":
    """The persistent Approve/Deny pair, built fresh for one card.

    ``DynamicItem`` is declared ``Item[View | LayoutView]`` in the discord.py
    stubs (it can attach to either), but ``ActionRow`` wants an ``Item``
    parameterized to its own concrete view type — a union isn't a subtype of
    either half, so pyright flags a real runtime-safe call as a type error.
    The two buttons are still verified in ``tests/economy/test_quest_layouts.py`` to
    render inside a ``LayoutView`` and keep their exact ``custom_id``.
    """
    row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    row.add_item(QuestApproveButton(claim_id))  # type: ignore[reportArgumentType]
    row.add_item(QuestDenyButton(claim_id))  # type: ignore[reportArgumentType]
    return row


def _signoff_card_view(
    settings: EconSettings,
    *,
    claim_id: int,
    claimant_id: int,
    name_fn,
    quest_title: str,
    reward: int,
    criteria: str,
    deny_count: int,
    state: str,
    resolver_id: int | None = None,
    deny_reason: str | None = None,
) -> discord.ui.LayoutView:
    """The sign-off card, buttoned while pending and bare once resolved.

    One build per surface: the board's ephemeral detail and the resolution
    repaint each get their own instance, so a view never belongs to two
    messages.
    """
    buttons = _signoff_buttons_row(claim_id) if state == "pending" else None
    return card(
        build_signoff_card_layout(
            settings,
            claimant_id=claimant_id,
            name_fn=name_fn,
            quest_title=quest_title,
            reward=reward,
            criteria=criteria,
            deny_count=deny_count,
            state=state,
            resolver_id=resolver_id,
            deny_reason=deny_reason,
            buttons=buttons,
        )
    )


class QuestDenyModal(discord.ui.Modal, title="Deny Quest Claim"):
    reason: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Reason (shown to the member)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=_DENY_REASON_MAX,
    )

    def __init__(self, claim_id: int, card_message: discord.Message | None) -> None:
        super().__init__()
        self.claim_id = claim_id
        self.card_message = card_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction,
            self.claim_id,
            approve=False,
            deny_reason=self.reason.value.strip(),
            card_message=self.card_message,
        )


# ── resolution plumbing ───────────────────────────────────────────────────────


async def _load_settings(
    interaction: discord.Interaction, guild: discord.Guild | None
) -> EconSettings | None:
    if guild is None:
        return None
    bot = cast("Bot", interaction.client)

    def _read() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild.id)

    return await asyncio.to_thread(_read)


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ quests")


def _read_claim_bundle(ctx: AppContext, claim_id: int) -> dict | None:
    """Load everything the card needs for a claim in one connection."""
    with ctx.open_db() as conn:
        claim = conn.execute(
            "SELECT * FROM econ_quest_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if claim is None:
            return None
        guild_id = int(claim["guild_id"])
        quest = get_quest(conn, guild_id, int(claim["quest_id"]))
        deny_count = len(
            deny_history(conn, int(claim["quest_id"]), int(claim["user_id"]))
        )
        settings = load_econ_settings(conn, guild_id)
    return {
        "claim": claim,
        "quest": quest,
        "deny_count": deny_count,
        "settings": settings,
    }


async def _handle_resolution(
    interaction: discord.Interaction,
    claim_id: int,
    *,
    approve: bool,
    deny_reason: str | None,
    card_message: discord.Message | None = None,
) -> None:
    """Approve/deny a sign-off claim: gate, resolve, repaint, announce.

    Fail-safe throughout — any error becomes an ephemeral note so the button
    never dies. ``card_message`` is supplied by the deny modal (a modal-submit
    interaction has no ``.message``); the approve button uses its own message.
    Either way, an *ephemeral* one means the board flow — see below.

    The announcement is a register post and only for a denial: an approval
    credits ledger kind ``quest``, which the register drain posts on its own.
    Nothing DMs (2026-08-27).
    """
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    card_msg = card_message if card_message is not None else interaction.message
    if card_msg is not None and getattr(card_msg.flags, "ephemeral", False):
        # The board flow. Its detail card *is* a message and arrives here like
        # a legacy card would, but an ephemeral message cannot be edited through
        # the channel-message endpoint — only through the interaction that owns
        # it. Dropping it here routes the repaint down the ``card is None``
        # branch below rather than burning an API call that always 404s.
        card_msg = None

    # Ack immediately: this chain does several awaits (resolve, the repaint,
    # the register post) before it replies, which can blow past the 3s
    # interaction window. Deferring keeps the token alive; _safe_ephemeral is
    # defer-aware.
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("econ quests: failed to defer resolution", exc_info=True)

    try:
        bundle = await asyncio.to_thread(_read_claim_bundle, ctx, claim_id)
    except Exception:
        log.exception("econ quests: failed to load claim %s", claim_id)
        await _safe_ephemeral(interaction, "❌ Couldn't load that claim — try again.")
        return

    if bundle is None or bundle["quest"] is None:
        await _safe_ephemeral(interaction, "❌ That claim no longer exists.")
        return

    settings: EconSettings = bundle["settings"]
    claim = bundle["claim"]
    quest = bundle["quest"]

    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return

    claimant_id = int(claim["user_id"])

    # Already resolved (e.g. from the dashboard, or by a second mod) — repaint
    # to the true state and tell the clicker, rather than double-crediting.
    if claim["state"] != "pending":
        await _refresh_resolved_card(interaction, card_msg, settings, claim, quest, bundle)
        await _safe_ephemeral(
            interaction, "❌ That claim was already resolved — I refreshed it above."
        )
        return

    # Booster flag is the CLAIMANT's status at credit time, not the manager's.
    booster = (
        member_is_booster(bot, guild.id, claimant_id) if approve else False
    )

    def _resolve() -> object:
        with ctx.open_db() as conn:
            return resolve_claim(
                conn,
                settings,
                claim_id,
                approve=approve,
                resolver_id=member.id,
                deny_reason=deny_reason,
                booster=booster,
            )

    try:
        resolution = await asyncio.to_thread(_resolve)
    except ValueError:
        # Lost the race (resolved between our read and write) — refresh + note.
        try:
            fresh = await asyncio.to_thread(_read_claim_bundle, ctx, claim_id)
        except sqlite3.Error:
            fresh = None
        if fresh is not None and fresh["quest"] is not None:
            await _refresh_resolved_card(
                interaction, card_msg, settings, fresh["claim"], fresh["quest"], fresh
            )
        await _safe_ephemeral(
            interaction, "❌ That claim was already resolved — I refreshed it above."
        )
        return
    except Exception:
        log.exception("econ quests: failed to resolve claim %s", claim_id)
        await _safe_ephemeral(interaction, "❌ Couldn't resolve that claim — try again.")
        return

    new_state = "paid" if approve else "denied"
    name_fn = await card_name_fn(ctx, guild, claim, member.id)
    view = _signoff_card_view(
        settings,
        claim_id=claim_id,
        claimant_id=claimant_id,
        name_fn=name_fn,
        quest_title=str(quest["title"]),
        reward=int(quest["reward"]),
        criteria=str(quest["criteria"]),
        deny_count=bundle["deny_count"],
        state=new_state,
        resolver_id=member.id,
        deny_reason=deny_reason,
    )
    if card_msg is not None:
        # A legacy card in a bank channel, clicked directly — still the embed
        # message it was created as, so the edit nulls out its embed slot the
        # way every embed→card conversion does.
        try:
            await card_msg.edit(content=None, embed=None, attachments=[], view=view)
        except discord.HTTPException:
            log.warning("econ quests: failed to edit the sign-off card for %s", claim_id)
    else:
        # The board flow: repaint the ephemeral detail in place, so the outcome
        # appears where the decision was made rather than only in an ack.
        try:
            await interaction.edit_original_response(
                content=None, embed=None, attachments=[], view=view
            )
        except discord.HTTPException:
            log.debug("econ quests: failed to repaint sign-off detail", exc_info=True)

    paid = int(getattr(resolution, "paid", 0))
    # An approval is already public: the credit writes ledger kind "quest" and
    # the register drain posts it. Only a denial needs announcing by hand.
    if not approve:
        await announce_signoff_outcome(
            bot,
            ctx.db_path,
            guild.id,
            state="denied",
            user_id=claimant_id,
            quest_title=str(quest["title"]),
            trigger_kind=quest["trigger_kind"],
            deny_reason=deny_reason,
        )
    await refresh_signoff_board(bot, guild.id)

    if approve:
        ack = (
            f"Approved — paid {settings.currency_emoji} **{paid:,}** "
            f"{_unit(settings, paid)} to <@{claimant_id}>."
        )
    else:
        ack = f"Denied <@{claimant_id}>'s claim."
    await _safe_ephemeral(interaction, ack)


async def _refresh_resolved_card(
    interaction: discord.Interaction,
    card_msg: discord.Message | None,
    settings: EconSettings,
    claim,
    quest,
    bundle: dict,
) -> None:
    """Repaint whatever surface the click came from to the claim's true state.

    Reached when a claim was resolved elsewhere between the read and the write
    — from the dashboard, or by a second mod. ``card_msg`` is a legacy
    bank-channel card; without one the click came from the board's ephemeral
    detail, which is edited in place instead.
    """
    bot = cast("Bot", interaction.client)
    name_fn = await card_name_fn(bot.ctx, interaction.guild, claim)
    claim_id = int(claim["id"])
    view = _signoff_card_view(
        settings,
        claim_id=claim_id,
        claimant_id=int(claim["user_id"]),
        name_fn=name_fn,
        quest_title=str(quest["title"]),
        reward=int(quest["reward"]),
        criteria=str(quest["criteria"]),
        deny_count=bundle["deny_count"],
        state=str(claim["state"]),
        resolver_id=(
            int(claim["resolver_id"]) if claim["resolver_id"] is not None else None
        ),
        deny_reason=claim["deny_reason"],
    )
    try:
        if card_msg is not None:
            await card_msg.edit(content=None, embed=None, attachments=[], view=view)
        else:
            await interaction.edit_original_response(
                content=None, embed=None, attachments=[], view=view
            )
    except discord.HTTPException:
        log.debug("econ quests: failed to refresh resolved card", exc_info=True)


async def refresh_signoff_board(bot: discord.Client, guild_id: int) -> None:
    """Repaint the todo board, where pending sign-offs are listed.

    Called on both edges — a claim filed and a claim resolved — because the
    board's own 60s loop only repaints guilds where a recurring chore spawned
    or was written off, so without this a claim would sit invisible until
    something else happened to move the board. Best effort in the strongest
    sense: by the time this runs the claim is committed and, on the resolving
    side, the member has been paid; a Discord hiccup must never surface as a
    failed claim or a failed payout.
    """
    from bot_modules.todo.cog import repaint_board  # noqa: PLC0415

    await repaint_board(bot, guild_id)


# ── the board's Sign-Offs button ─────────────────────────────────────────────
#
# The todo board is one sticky message and Discord caps the components on a
# message, so Approve/Deny cannot hang off it once per pending claim. The
# board's Complete button already solved the same shape: a button that opens an
# ephemeral select. Sign-offs follow it — pick a claim, read its criteria, act.


class SignoffPickSelect(discord.ui.Select):
    """Ephemeral picker listing the claims waiting on a sign-off."""

    def __init__(self, rows: list[dict], settings: EconSettings) -> None:
        options = []
        for row in rows[:25]:  # Discord caps a select at 25 options
            label = f"{row['claimant_name']} — {row['quest_title']}"
            if len(label) > 100:
                label = label[:99] + "…"
            # The one place the reward and the criteria fit before a mod
            # commits to opening a claim; the full criteria is on the detail
            # card behind it.
            reward = int(row["reward"])
            desc = f"{reward:,} {_unit(settings, reward)} · {row['criteria']}"
            if len(desc) > 100:
                desc = desc[:99] + "…"
            options.append(
                discord.SelectOption(
                    label=label, value=str(row["id"]), description=desc or None
                )
            )
        super().__init__(
            placeholder="Pick a claim to review…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        claim_id = int(self.values[0])
        bot = cast("Bot", interaction.client)
        guild = interaction.guild
        if guild is None:
            await _safe_ephemeral(interaction, "❌ This only works in a server.")
            return
        try:
            bundle = await asyncio.to_thread(_read_claim_bundle, bot.ctx, claim_id)
        except Exception:
            log.exception("econ quests: failed to load claim %s for review", claim_id)
            await _safe_ephemeral(
                interaction, "❌ Couldn't load that claim — try again."
            )
            return
        if bundle is None or bundle["quest"] is None:
            await _safe_ephemeral(interaction, "❌ That claim no longer exists.")
            return

        claim = bundle["claim"]
        quest = bundle["quest"]
        # The same builder the bank-channel card used, in the same states — so
        # a claim someone resolved while this picker was open renders here as
        # resolved, with no buttons, instead of offering a decision already
        # made.
        state = str(claim["state"])
        name_fn = await card_name_fn(bot.ctx, guild, claim)
        view = _signoff_card_view(
            bundle["settings"],
            claim_id=claim_id,
            claimant_id=int(claim["user_id"]),
            name_fn=name_fn,
            quest_title=str(quest["title"]),
            reward=int(quest["reward"]),
            criteria=str(quest["criteria"]),
            deny_count=bundle["deny_count"],
            state=state,
            resolver_id=(
                int(claim["resolver_id"]) if claim["resolver_id"] is not None else None
            ),
            deny_reason=claim["deny_reason"],
        )
        try:
            # This picker message started as plain content + an ordinary
            # Select, so the edit that turns it into a card nulls the content
            # slot explicitly — the same "edit converting embed to card"
            # convention, for a message that never had an embed to null.
            await interaction.response.edit_message(content=None, view=view)
        except discord.HTTPException:
            log.debug("econ quests: failed to open claim detail", exc_info=True)


async def open_signoff_picker(interaction: discord.Interaction) -> None:
    """The todo board's Sign-Offs button: gate, then offer the pending claims.

    Gated on ``can_manage_economy`` rather than on the board's own moderator
    check — approving pays real currency, so this stays the economy's gate even
    though the button that opens it sits on the todo board. In the main guild
    the two roles are the same one, so nobody who could sign off before is shut
    out by the move.
    """
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return

    def _load() -> tuple[EconSettings, list[dict]]:
        with bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild.id)
            rows = [dict(r) for r in pending_signoff_rows(conn, guild.id, limit=25)]
        return settings, rows

    try:
        settings, rows = await asyncio.to_thread(_load)
    except Exception:
        log.exception("econ quests: failed to load pending sign-offs")
        await _safe_ephemeral(
            interaction, "❌ Couldn't load the sign-offs — try again."
        )
        return

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return
    if not rows:
        await _safe_ephemeral(interaction, "No quest sign-offs are waiting. ✨")
        return

    for row in rows:
        claimant = guild.get_member(int(row["user_id"]))
        row["claimant_name"] = claimant.display_name if claimant else "someone"

    view = discord.ui.View(timeout=_CLAIM_VIEW_TIMEOUT)
    view.add_item(SignoffPickSelect(rows, settings))
    await interaction.response.send_message(
        "Which claim do you want to review?", view=view, ephemeral=True
    )


class QuestDetailSelect(discord.ui.Select):
    """Ephemeral select showing one quest's full story on demand.

    The /bank quests list is deliberately terse (one line per quest); this
    select carries the long form — description, how it completes, progress,
    and for a channel-scoped quest the channel it counts in — without the
    list paying for it up front. The scope line lands here rather than on the
    list because the list's rows are a padded monospace table: a channel
    mention renders at whatever width the reader's client makes of the
    channel name, which would pull the reward column out of alignment on
    every row that had one.
    """

    def __init__(self, settings: EconSettings, quests: list[dict]) -> None:
        self.settings = settings
        self._quests = {str(q["id"]): q for q in quests[:25]}
        options = [
            discord.SelectOption(
                label=str(q["title"])[:100],
                value=str(q["id"]),
                description=str(q["qtype"])[:100],
            )
            for q in quests[:25]
        ]
        super().__init__(
            placeholder="ℹ️ Quest details…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        q = self._quests.get(self.values[0])
        if q is None:  # stale view after a reroll — just re-run /bank quests
            await interaction.response.send_message(
                "❌ That quest is no longer on your board — re-run "
                "`/bank quests`.",
                ephemeral=True,
            )
            return
        state = str(q.get("state") or "")
        scope_line = ""
        # Where to do it, for a channel-scoped quest. Whether it is still
        # worth saying is `quests.scope_worth_showing`'s call, not this
        # card's — the same counter pair the bar on the card is drawn from.
        if scope_worth_showing(
            state, int(q.get("current") or 0), int(q.get("target") or 0)
        ):
            scope_line = channel_scope_line(q.get("trigger_channel_id"))
        items = build_quest_detail_layout(self.settings, q, scope_line=scope_line)
        await interaction.response.send_message(view=card(items), ephemeral=True)


class QuestClaimSelect(discord.ui.Select):
    """Ephemeral select of the caller's currently-claimable quests."""

    def __init__(
        self,
        ctx: AppContext,
        settings: EconSettings,
        guild: discord.Guild,
        claimable: list[dict],
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.guild = guild
        # qtype/signoff keyed by quest id for the click-time claim.
        self._meta = {q["id"]: q for q in claimable}
        options = [
            discord.SelectOption(
                label=str(q["title"])[:100],
                value=str(q["id"]),
                description=(f"{q['reward']} {_unit(settings, int(q['reward']))} · {q['qtype']}")[
                    :100
                ],
            )
            for q in claimable
        ]
        super().__init__(
            placeholder="Claim a quest…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = cast("Bot", interaction.client)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("❌ This only works in a server.", ephemeral=True)
            return

        quest_id = int(self.values[0])
        meta = self._meta.get(quest_id)
        if meta is None:
            await interaction.followup.send("❌ That quest is no longer available.", ephemeral=True)
            return
        qtype = str(meta["qtype"])
        booster = member_is_booster(bot, self.guild.id, member.id)
        guild_id = self.guild.id
        settings = self.settings

        def _claim() -> object:
            with self.ctx.open_db() as conn:
                offset = get_tz_offset_hours(conn, guild_id)
                day = local_day_for(time.time(), offset)
                period = quest_period(qtype, day)
                return claim_quest(
                    conn, settings, guild_id, quest_id, member.id,
                    period=period, booster=booster,
                )

        try:
            outcome = await asyncio.to_thread(_claim)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception("econ quests: claim failed for quest %s", quest_id)
            await interaction.followup.send(
                "❌ Something went wrong claiming that quest — try again.", ephemeral=True
            )
            return

        state = getattr(outcome, "state", "")
        if state == "paid":
            paid = int(getattr(outcome, "paid", 0))
            items = build_quest_claimed_layout(
                settings, title=str(meta["title"]), paid=paid
            )
            await interaction.followup.send(view=card(items), ephemeral=True)
            return

        # Sign-off: the claim is filed, so nudge the mods' board, then confirm.
        await refresh_signoff_board(bot, self.guild.id)
        await interaction.followup.send(
            f"Submitted **{meta['title']}** for mod sign-off — "
            "the result will show on your quest board.",
            ephemeral=True,
        )


class QuestRerollSelect(discord.ui.Select):
    """Board reroll: swap an untouched quest for a new one.

    One free reroll per guild-local day, then paid ones up to the daily cap.
    The service picks the replacement (member's own shuffle order, different
    trigger kind preferred), validates everything and charges — this select
    just surfaces the result; on success it confirms old → new and disables
    itself, since the price of the *next* reroll may have changed.
    """

    def __init__(
        self,
        ctx: AppContext,
        settings: EconSettings,
        guild: discord.Guild,
        rerollable: list[dict],
        local_day: str,
        reroll_cost: int | None = 0,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.guild = guild
        self.local_day = local_day
        options = [
            discord.SelectOption(
                label=str(q["title"])[:100],
                value=str(q["id"]),
                description=str(q["qtype"])[:100],
            )
            for q in rerollable[:25]
        ]
        if reroll_cost:
            unit = _unit(settings, int(reroll_cost))
            price = f"{reroll_cost} {unit}"
        else:
            price = "free"
        super().__init__(
            placeholder=f"🎲 Reroll one untouched quest ({price})…"[:150],
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        quest_id = int(self.values[0])
        user_id = interaction.user.id
        guild_id = self.guild.id

        def _do_reroll():
            with self.ctx.open_db() as conn:
                old = get_quest(conn, guild_id, quest_id)
                new, cost = reroll_board_slot(
                    conn, self.settings, guild_id, user_id, quest_id,
                    self.local_day,
                )
                return old, new, cost

        try:
            old, new, cost = await asyncio.to_thread(_do_reroll)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.disabled = True
        try:
            await interaction.edit_original_response(view=self.view)
        except discord.HTTPException:
            pass
        old_title = str(old["title"]) if old else "that quest"
        unit = _unit(self.settings, int(new["reward"]))
        paid = (
            f" Cost {cost} {_unit(self.settings, int(cost))}."
            if cost
            else " That was today's free reroll."
        )
        await interaction.followup.send(
            f"🎲 Swapped **{old_title}** for **{new['title']}** "
            f"({new['reward']} {unit} · {new['qtype']}).{paid} "
            "Run `/bank quests` again to see your refreshed board.",
            ephemeral=True,
        )


class QuestClaimView(ExpiringLayoutView):
    """The ``/bank quests`` card: the board list plus its action selects.

    Components V2 gives a message no separate content-vs-view slot, so the
    board (**paradigm 6 — list**, whose per-row "action" is one of the
    selects below it rather than a per-row button — see
    ``quest_layouts.build_quest_board_layout``) and the Detail/Claim/Reroll
    selects that act on it (**paradigm 11 — menu dropdown**, one per action)
    live in the same component tree, where an embed + view pair used to
    split them.
    """

    def __init__(
        self,
        ctx: AppContext,
        settings: EconSettings,
        guild: discord.Guild,
        claimable: list[dict],
        *,
        rerollable: list[dict] | None = None,
        reroll_cost: int | None = 0,
        local_day: str = "",
        detailable: list[dict] | None = None,
    ) -> None:
        super().__init__(timeout=_CLAIM_VIEW_TIMEOUT)
        for item in build_quest_board_layout(settings, list(detailable or [])):
            self.add_item(item)
        if detailable:
            self.add_item(discord.ui.ActionRow(QuestDetailSelect(settings, detailable)))
        if claimable:
            self.add_item(discord.ui.ActionRow(QuestClaimSelect(ctx, settings, guild, claimable)))
        if rerollable and local_day:
            self.add_item(
                discord.ui.ActionRow(
                    QuestRerollSelect(
                        ctx, settings, guild, rerollable, local_day, reroll_cost
                    )
                )
            )


# ── quest-board "Show my quests" button ──────────────────────────────────────
#
# Out of scope for the quests wave: this button sits on the shared
# leaderboard/quest-board panel, a different feature's card, and posts no
# embed of its own — it only opens the two panels below.

QUEST_BOARD_CUSTOM_ID = "econ:show_my_quests"


async def open_my_quests(interaction: discord.Interaction) -> None:
    """The economy panel's 📋 Show My Quests — the member's own quest list.

    The board is anonymous by design (no member names, no per-member draw), so
    this is the members' door from it into their own private quest list — the
    same ephemeral panel ``/bank quests`` opens. One button serves everyone; the
    per-member state lives entirely in the ephemeral reply, so it carries no id.
    """
    bot = cast("Bot", interaction.client)
    cog = bot.get_cog("EconomyCog")
    if cog is None:  # cog unloaded mid-restart — never a dead button
        await interaction.response.send_message(
            "❌ Quests are unavailable right now — try again in a moment.",
            ephemeral=True,
        )
        return
    await cog.send_quests_panel(interaction)  # type: ignore[attr-defined]


WALLET_CUSTOM_ID = "econ:show_my_wallet"


async def open_wallet(interaction: discord.Interaction) -> None:
    """The economy panel's 💰 Wallet — the member's own wallet, ephemerally.

    Replaced the panel's old trailing "`/bank quests` … `/bank wallet` …"
    explainer: the two things it pointed at are buttons on the panel, so the
    board spends its space on content instead of on telling members which
    commands to type. Carries no id — the ephemeral reply holds all
    per-member state.
    """
    bot = cast("Bot", interaction.client)
    cog = bot.get_cog("EconomyCog")
    if cog is None:  # cog unloaded mid-restart — never a dead button
        await interaction.response.send_message(
            "❌ Your wallet is unavailable right now — try again in a moment.",
            ephemeral=True,
        )
        return
    await cog.send_wallet_panel(interaction)  # type: ignore[attr-defined]
