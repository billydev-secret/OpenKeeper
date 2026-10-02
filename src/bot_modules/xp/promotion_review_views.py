"""Discord surface for promotion-review cards.

Persistent (``timeout=None``) buttons rendered as
:class:`discord.ui.DynamicItem` so a card posted before a restart stays
actionable after it. Three button classes, all registered in ``__main__`` via
``bot.add_dynamic_items(...)``:

* :class:`GrantAccessButton` / :class:`DismissButton` — on the pruned-return and
  sleeper cards; ``custom_id`` carries the ``promotion_review_cards`` row id.
* :class:`Level5GrantButton` — retrofitted onto the existing Level 5 card;
  ``custom_id`` carries the target member id (that card is not ledger-backed).

Gating logic and the ledger live in
:mod:`bot_modules.xp.promotion_review_service`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, cast
from functools import partial

import discord

from bot_modules.core.role_provision import ensure_config_role
from bot_modules.core.role_safety import role_block_reason
from bot_modules.shared.feature_roles import PROMOTION_REVIEW_PING
from bot_modules.core.branding import DEFAULT_ACCENT_COLOR, code_safe, safe_resolve_accent
from bot_modules.core.utils import (
    get_bot_member,
    get_guild_channel_or_thread,
    role_ping_kwargs,
)
from bot_modules.inactive.apply import reactivate_member
from bot_modules.xp import promotion_review_service as svc
from bot_modules.moderation.service import write_audit
from bot_modules.shared.name_resolver import NameFn, build_name_fn
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.core.branding import apply_section_spacing

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot
    from bot_modules.core.utils import GuildTextLike

log = logging.getLogger(__name__)


def ping_send_kwargs(role_ids) -> dict:
    """``content``/``allowed_mentions`` for a review-card post.

    A thin alias for :func:`core.utils.role_ping_kwargs`, kept because this
    module's name for the idea is the one the card paths read. Accepts a bare
    id as well as a list, since a single role is still the common case.
    """
    if isinstance(role_ids, int):
        role_ids = [role_ids]
    return role_ping_kwargs(role_ids)


# audit_log action for a successful Grant press (Level 5 or review card).
GRANT_AUDIT_ACTION = "promotion_review_grant"

_GRANT_CID = re.compile(r"promo_review:grant:(?P<cid>\d+)")
_DISMISS_CID = re.compile(r"promo_review:dismiss:(?P<cid>\d+)")
_LEVEL5_CID = re.compile(r"promo_review:l5grant:(?P<uid>\d+)")


# ---------------------------------------------------------------------------
# Embed (pure)
# ---------------------------------------------------------------------------


def format_prune_lines(
    guild: discord.Guild, pruned_roles: list[tuple[int, float | None]]
) -> list[str]:
    """Human lines for what a sweep took, e.g. ``@NSFW — removed <t:…:D>``."""
    lines: list[str] = []
    for role_id, pruned_at in pruned_roles:
        role = guild.get_role(role_id)
        label = role.mention if role is not None else f"role `{role_id}`"
        when = f"removed <t:{int(pruned_at)}:D>" if pruned_at else "removed (date unknown)"
        lines.append(f"{label} — {when}")
    return lines


def build_review_embed(
    accent: discord.Color,
    *,
    kind: str,
    member_id: int,
    member_display: str,
    level: int,
    prune_lines: list[str],
    action_hint: str,
    name_fn: NameFn,
    resolved: tuple[str, int] | None = None,
) -> discord.Embed:
    """Card body for a pruned-return / sleeper card.

    ``resolved`` = ``(resolution, resolver_id)`` once actioned. Members are
    named through ``name_fn``, never mentioned — an embed ``<@id>`` shows as
    digits to a reviewer who hasn't seen the returning member lately.
    """
    member_name = name_fn(member_id)
    if kind == svc.KIND_SLEEPER:
        title = "😴 Sleeper is stirring"
        desc = f"{member_name} is inactive-held and just posted in the sleeper channel."
    else:
        title = "🔙 Member returned"
        desc = f"{member_name} lost access to a sweep and is active again."
    embed = discord.Embed(
        title=title, description=f"{desc}\n{action_hint}", color=accent
    )
    # `member_display` sits inside an inline code span, where markdown does
    # not render -- what it's vulnerable to is a backtick in the member's own
    # username closing the span early. code_safe neutralises the delimiter;
    # escape_markdown would be the wrong tool (nothing here is interpreted).
    embed.add_field(name="Member", value=f"{member_name} (`{code_safe(member_display)}`)", inline=True)
    embed.add_field(name="Level", value=str(level), inline=True)
    if prune_lines:
        embed.add_field(
            name="Access a Sweep Removed", value="\n".join(prune_lines), inline=False
        )
    if resolved is not None:
        resolution, resolver_id = resolved
        verb = {
            svc.RESOLUTION_GRANTED: "✅ Access granted",
            svc.RESOLUTION_REACTIVATED: "✅ Reactivated",
            svc.RESOLUTION_DISMISSED: "🚫 Dismissed",
        }.get(resolution, "Resolved")
        embed.add_field(
            name="Resolved", value=f"{verb} by {name_fn(resolver_id)}", inline=False
        )
    apply_section_spacing(embed)
    return embed


def refresh_spicy_field(
    embed: discord.Embed, has_nsfw: bool, *, on_break: bool = False, lite: bool = False
) -> discord.Embed | None:
    """A copy of a Level 5 card with its "Spicy access" field brought up to date.

    Returns ``None`` when there is nothing to do — the embed has no such field
    (an ordinary level-up post shares this channel, and a card posted while
    ``nsfw_role_id`` was unset never rendered one) or the value is already
    right. The caller then skips the Discord edit entirely.

    Only that one field is rewritten: ``Total XP`` and ``Joined`` are
    deliberately an at-promotion snapshot and must not drift.
    """
    want = svc.spicy_access_value(has_nsfw, on_break=on_break, lite=lite)
    for idx, field in enumerate(embed.fields):
        if field.name == svc.SPICY_FIELD_NAME and field.value != want:
            # deepcopy, not Embed.copy(): that is shallow and its to_dict()
            # hands back the *same* field dicts, so set_field_at would rewrite
            # the caller's embed too.
            updated = deepcopy(embed)
            updated.set_field_at(idx, name=field.name, value=want, inline=bool(field.inline))
            return updated
    return None


# One refresh at a time per member. Two quick role toggles arrive as two
# independent listener tasks, and unserialized they can leave the card on the
# *older* state for good: the first task reads ❌ and starts an edit to ✅ that
# stalls on the edit rate limit, the second reads the still-❌ card, sees its own
# target already matches, no-ops — and the first edit then lands ✅ on a member
# who no longer has the role. Nothing reconciles that. Serializing makes the
# second task read the first's result and correct it.
_refresh_locks: dict[tuple[int, int], asyncio.Lock] = {}
_refresh_lock_users: dict[tuple[int, int], int] = {}


@asynccontextmanager
async def _member_refresh_lock(guild_id: int, user_id: int):
    key = (guild_id, user_id)
    # Safe without its own lock: every line runs on the event loop thread with
    # no await between the lookup and the refcount bump.
    lock = _refresh_locks.setdefault(key, asyncio.Lock())
    _refresh_lock_users[key] = _refresh_lock_users.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = _refresh_lock_users[key] - 1
        if remaining:
            _refresh_lock_users[key] = remaining
        else:  # last one out drops the entry so the dicts don't grow forever
            del _refresh_lock_users[key]
            _refresh_locks.pop(key, None)


async def refresh_level_5_cards(
    ctx: AppContext, member: discord.Member, has_nsfw: bool
) -> None:
    """Re-render every stored Level 5 card for ``member`` to match their roles.

    Driven by the ``on_member_update`` role diff; see
    ``docs/specs/promotion_review_spec.md``. **Never raises**, and one bad card doesn't
    stop the member's others — this runs inside a listener that has other work.
    """
    guild = member.guild
    async with _member_refresh_lock(guild.id, member.id):
        try:
            rows, on_break, lite = await asyncio.to_thread(
                _level_5_state, ctx, guild.id, member.id, has_nsfw
            )
        except Exception:
            log.exception("promo review: failed to load level 5 cards for %s", member.id)
            return

        for row in rows:
            try:
                await _refresh_one_level_5_card(
                    ctx, guild, row, has_nsfw, on_break=on_break, lite=lite
                )
            except Exception:
                log.exception(
                    "promo review: failed to refresh level 5 card %s", row["message_id"]
                )


async def _refresh_one_level_5_card(
    ctx: AppContext,
    guild: discord.Guild,
    row,
    has_nsfw: bool,
    *,
    on_break: bool = False,
    lite: bool = False,
) -> None:
    """Bring a single stored card in line, or forget it if it's gone for good."""
    card_id = int(row["id"])
    message_id = int(row["message_id"])
    channel = await _card_channel(ctx, guild, int(row["channel_id"]), card_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await asyncio.to_thread(_forget_level_5_card, ctx, card_id)
        return
    except discord.HTTPException:
        log.debug("promo review: can't fetch level 5 card %s", message_id, exc_info=True)
        return
    if not message.embeds:
        return
    updated = refresh_spicy_field(message.embeds[0], has_nsfw, on_break=on_break, lite=lite)
    if updated is None:
        return
    try:
        await message.edit(embed=updated)
    except discord.HTTPException:
        log.debug("promo review: failed to refresh card %s", message_id, exc_info=True)


async def _card_channel(
    ctx: AppContext, guild: discord.Guild, channel_id: int, card_id: int
) -> GuildTextLike | None:
    """The card's channel, forgetting the card only if the channel is truly gone.

    A cache miss is ambiguous — a deleted channel and an archived (uncached)
    thread both come back ``None`` — so confirm with a fetch before dropping the
    row.
    """
    channel = get_guild_channel_or_thread(guild, channel_id)
    if channel is not None:
        return channel
    try:
        fetched = await guild.fetch_channel(channel_id)
    except discord.NotFound:
        await asyncio.to_thread(_forget_level_5_card, ctx, card_id)
        return None
    except discord.HTTPException:
        log.debug("promo review: can't fetch channel %s", channel_id, exc_info=True)
        return None
    if isinstance(fetched, discord.TextChannel | discord.Thread):
        return fetched
    # Exists but can't hold the card any more — as unrefreshable as a deletion,
    # so forget it rather than re-fetching it on every future role change.
    await asyncio.to_thread(_forget_level_5_card, ctx, card_id)
    return None


def _level_5_state(ctx: AppContext, guild_id: int, user_id: int, has_nsfw: bool):
    """The member's stored cards, plus ``(on_break, lite)`` for a lost role.

    A break writes its row before it strips anything, so by the time the role
    diff reports Spicy gone the row already says whether Spicy was saved.
    """
    from bot_modules.breaks.store import get_break
    from bot_modules.xp.service import nsfw_grant_role_id

    with ctx.open_db() as conn:
        rows = svc.level_5_cards_for(conn, guild_id, user_id)
        brk = None if has_nsfw else get_break(conn, guild_id, user_id)
        if brk is None:
            return rows, False, False
        cfg = ctx.guild_config(guild_id)
        spicy = nsfw_grant_role_id(cfg.grant_roles, cfg.promotion_review_grant_role_id)
        on_break = spicy > 0 and spicy in brk.stored_roles
        return rows, on_break, on_break and brk.swapped_spicy


def _forget_level_5_card(ctx: AppContext, card_id: int) -> None:
    with ctx.open_db() as conn:
        svc.forget_level_5_card(conn, card_id)


#: Discord truncates a button label past 80 characters.
_LABEL_MAX = 80
GRANT_LABEL_DEFAULT = "Grant access"


def grant_label(role: discord.Role | None) -> str:
    """Button label naming the role the press actually hands out.

    A generic "Grant access" tells a reviewer nothing about what they are about
    to give away — a guild whose ``promotion_review_grant_role_id`` points at
    the wrong role finds out only afterwards, from the audit log. Naming the
    role puts the mistake in front of the press instead of behind it.

    Falls back to the generic wording when no grant role is configured or it has
    since been deleted, and for the sleeper card, whose Grant runs a full
    reactivate rather than adding the configured role.
    """
    if role is None:
        return GRANT_LABEL_DEFAULT
    label = f"Grant {role.name}"
    return label if len(label) <= _LABEL_MAX else label[: _LABEL_MAX - 1] + "…"


#: The Level 5 button while intake Stage 2 is on: the press approves into the
#: greeters' Stage 2 card and grants nothing itself (2026-10-01).
APPROVE_LABEL = "Approve for Stage 2"


def level_5_button_label(role: discord.Role | None, *, stage2_on: bool) -> str:
    """The Level 5 card's button label: what the press will actually do."""
    return APPROVE_LABEL if stage2_on else grant_label(role)


# ---------------------------------------------------------------------------
# Persistent buttons — ledger-backed (pruned-return + sleeper)
# ---------------------------------------------------------------------------


class GrantAccessButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_GRANT_CID,
):
    """Pruned-return: re-add the configured role. Sleeper: full reactivate."""

    def __init__(self, card_id: int, label: str = GRANT_LABEL_DEFAULT) -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"promo_review:grant:{card_id}",
            )
        )
        self.card_id = card_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> GrantAccessButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _resolve_card(interaction, self.card_id, grant=True)


class DismissButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_DISMISS_CID,
):
    """Close the card without granting — e.g. the return isn't approved."""

    def __init__(self, card_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Dismiss",
                emoji="🚫",
                style=discord.ButtonStyle.secondary,
                custom_id=f"promo_review:dismiss:{card_id}",
            )
        )
        self.card_id = card_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> DismissButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _resolve_card(interaction, self.card_id, grant=False)


class ReviewCardView(discord.ui.View):
    """Persistent Grant/Dismiss pair for one ledger-backed card."""

    def __init__(self, card_id: int, label: str = GRANT_LABEL_DEFAULT) -> None:
        super().__init__(timeout=None)
        self.add_item(GrantAccessButton(card_id, label))
        self.add_item(DismissButton(card_id))


# ---------------------------------------------------------------------------
# Persistent button — Level 5 card (not ledger-backed; keyed by member id)
# ---------------------------------------------------------------------------


class Level5GrantButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_LEVEL5_CID,
):
    """Grant the configured access role to the Level 5 promotion candidate."""

    def __init__(self, user_id: int, label: str = GRANT_LABEL_DEFAULT) -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"promo_review:l5grant:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> Level5GrantButton:
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _grant_role_only(interaction, self.user_id)


class Level5PromotionView(discord.ui.View):
    """Persistent single Grant button for a Level 5 promotion card."""

    def __init__(self, user_id: int, label: str = GRANT_LABEL_DEFAULT) -> None:
        super().__init__(timeout=None)
        self.add_item(Level5GrantButton(user_id, label))


# ---------------------------------------------------------------------------
# Posting a card (called from the message hot-path hook)
# ---------------------------------------------------------------------------


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="promo review")


async def post_review_card(
    ctx: AppContext, member: discord.Member, posted_channel_id: int
) -> None:
    """Post a pruned-return / sleeper card for ``member`` if one is owed.

    Runs the authoritative DB gate off-thread, reserves the single open-card
    slot (racing callers lose and skip), posts to the promotion-reviews channel,
    then attaches the message location. Manages the in-memory watch set: drops
    the member once carded or no longer a candidate; keeps a sleeper who posted
    outside the sleeper channel for a later message.
    """
    guild = member.guild
    guild_id = guild.id
    user_id = member.id

    def _prepare():
        with ctx.open_db() as conn:
            kind = svc.evaluate_trigger(conn, guild_id, user_id, posted_channel_id)
            if kind is None:
                return ("skip", svc.still_candidate(conn, guild_id, user_id))
            card_id = svc.reserve_card(conn, guild_id, user_id, kind, time.time())
            if card_id is None:  # lost the race — already carded
                return ("skip", False)
            return (
                "post",
                kind,
                card_id,
                svc.review_channel_id(conn, guild_id),
                svc.pruned_roles_for(conn, guild_id, user_id),
                svc.member_level(conn, guild_id, user_id),
                svc.ping_role_ids(conn, guild_id),
                svc.grant_role_id(conn, guild_id),
            )

    try:
        prepared = await asyncio.to_thread(_prepare)
    except Exception:
        log.exception("promo review: failed to prepare card for %s", user_id)
        return

    if prepared[0] == "skip":
        still = prepared[1]
        if not still:
            svc.discard(guild_id, user_id)
        return

    (
        _, kind, card_id, review_ch_id, pruned_roles, level, fallback_ping, grant_role_id
    ) = prepared

    channel = guild.get_channel(review_ch_id)
    if not isinstance(channel, discord.abc.Messageable):
        await asyncio.to_thread(_delete_card, ctx, card_id)
        log.warning(
            "promo review: review channel %s missing in guild %s", review_ch_id, guild_id
        )
        return

    # Provisions @Promotion Reviewers on a guild that never set a ping role, so
    # the first card has somewhere to shout. A stored "(none)" returns None and
    # the card posts silent, exactly as before.
    provisioned = await ensure_config_role(
        ctx, guild, PROMOTION_REVIEW_PING.key, PROMOTION_REVIEW_PING.spec,
        feature=PROMOTION_REVIEW_PING.feature,
            allow_legacy_fallback=PROMOTION_REVIEW_PING.legacy_fallback,
    )
    # A provisioned/configured @Promotion Reviewers wins. Without one the card
    # falls back to the guild's mod roles rather than posting to nobody — see
    # ``svc.ping_role_ids`` for why a stored 0 here is usually a save artifact
    # rather than a decision to stay silent.
    ping_roles = [provisioned.id] if provisioned is not None else fallback_ping

    accent = await safe_resolve_accent(ctx, guild, log_label="promotion review", default=DEFAULT_ACCENT_COLOR)
    # Only the pruned-return Grant hands out the configured role; the sleeper
    # Grant runs a full reactivate, so naming a role on it would be a lie.
    grant_role = (
        guild.get_role(grant_role_id)
        if kind == svc.KIND_PRUNED_RETURN and grant_role_id > 0
        else None
    )
    label = grant_label(grant_role)
    hint = (
        f"{label} re-adds the role and closes this out."
        if kind == svc.KIND_PRUNED_RETURN
        else f"{label} reactivates them (restores their roles)."
    )
    name_fn = await build_name_fn(
        guild=guild, db_path=ctx.db_path, guild_id=guild_id, user_ids=[user_id]
    )
    embed = build_review_embed(
        accent,
        kind=kind,
        member_id=user_id,
        member_display=str(member),
        level=level,
        prune_lines=format_prune_lines(guild, pruned_roles),
        action_hint=hint,
        name_fn=name_fn,
    )
    try:
        posted = await channel.send(
            embed=embed,
            view=ReviewCardView(card_id, label),
            **ping_send_kwargs(ping_roles),
        )
    except discord.HTTPException:
        log.warning("promo review: failed to post card in guild %s", guild_id)
        await asyncio.to_thread(_delete_card, ctx, card_id)
        return  # keep on watch set — retry on their next message

    await asyncio.to_thread(_attach_message, ctx, card_id, channel.id, posted.id)
    svc.discard(guild_id, user_id)


def _delete_card(ctx: AppContext, card_id: int) -> None:
    with ctx.open_db() as conn:
        svc.delete_card(conn, card_id)


def _attach_message(ctx: AppContext, card_id: int, channel_id: int, message_id: int) -> None:
    with ctx.open_db() as conn:
        svc.set_card_message(conn, card_id, channel_id, message_id)


# ---------------------------------------------------------------------------
# Resolving a card (button callbacks)
# ---------------------------------------------------------------------------


async def _resolve_card(interaction: discord.Interaction, card_id: int, *, grant: bool) -> None:
    """Gate, apply the action (grant/reactivate), close the card, edit. Never raises."""
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("promo review: failed to defer", exc_info=True)

    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not ctx.member_is_mod(actor):
        await _safe_ephemeral(interaction, "❌ You can't action returns here.")
        return

    card = await asyncio.to_thread(lambda: _get_card(ctx, card_id))
    if card is None:
        await _safe_ephemeral(interaction, "❌ That card no longer exists.")
        return
    if card["resolved_at"] is not None:
        await _safe_ephemeral(interaction, "Already resolved.")
        return

    guild_id = guild.id
    member_id = int(card["user_id"])
    kind = str(card["kind"])
    target = guild.get_member(member_id)

    resolution = svc.RESOLUTION_DISMISSED
    if grant:
        if target is None:
            await _safe_ephemeral(interaction, "❌ That member isn't in the server anymore.")
            return
        if kind == svc.KIND_SLEEPER:
            ok, msg = await _do_reactivate(ctx, guild, target, actor)
            if not ok:
                await _safe_ephemeral(interaction, msg)
                return
            resolution = svc.RESOLUTION_REACTIVATED
        else:
            ok, msg = await _do_grant_role(ctx, guild, target, actor)
            if not ok:
                await _safe_ephemeral(interaction, msg)
                return
            resolution = svc.RESOLUTION_GRANTED

    now = time.time()

    def _commit():
        with ctx.open_db() as conn:
            closed = svc.resolve_card(conn, card_id, actor.id, now, resolution)
            if closed and resolution == svc.RESOLUTION_GRANTED:
                svc.mark_prunes_restored(conn, guild_id, member_id, now)
            return closed

    closed = await asyncio.to_thread(_commit)
    svc.discard(guild_id, member_id)
    if not closed:  # someone else resolved it between load and commit
        await _safe_ephemeral(interaction, "Already resolved.")
        return

    await _edit_resolved(interaction, ctx, guild, card, resolution, actor.id)
    await _safe_ephemeral(interaction, _RESULT_MSG.get(resolution, "Done."))


_RESULT_MSG = {
    svc.RESOLUTION_GRANTED: "Access granted.",
    svc.RESOLUTION_REACTIVATED: "Reactivated.",
    svc.RESOLUTION_DISMISSED: "Dismissed.",
}


async def _grant_role_only(interaction: discord.Interaction, user_id: int) -> None:
    """Level 5 card button: approve into intake Stage 2, or grant, no ledger row.

    With Stage 2 on, the press **grants nothing**: it opens the member's
    Stage 2 card, whose built-in *NSFW access* line lets a greeter hand out
    the role (Ben, 2026-10-01: NSFW unlocks only from the greeter's card). If
    the card can't open, nothing is granted and the reply says why. With
    Stage 2 off it adds the configured role itself, as it always did, so the
    button keeps working on a guild that doesn't run intake.
    """
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("promo review: failed to defer l5 grant", exc_info=True)

    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not ctx.member_is_mod(actor):
        await _safe_ephemeral(interaction, "❌ You can't action promotions here.")
        return
    target = guild.get_member(user_id)
    if target is None:
        await _safe_ephemeral(interaction, "❌ That member isn't in the server anymore.")
        return
    # Only this press opens Stage 2 — a role given by hand or through /grant
    # does not (docs/specs/intake_spec.md). post_stage2_card never raises.
    from bot_modules.onboarding.intake.service import STAGE2_DARK, stage2_press_reply
    from bot_modules.onboarding.intake.views import post_stage2_card

    role_id = await asyncio.to_thread(lambda: _grant_role_id(ctx, guild.id))
    live_role_id = role_id if role_id > 0 and guild.get_role(role_id) is not None else 0
    outcome = await post_stage2_card(ctx, target, actor, live_role_id)
    if outcome != STAGE2_DARK:
        await _safe_ephemeral(interaction, stage2_press_reply(outcome))
        return
    _ok, msg = await _do_grant_role(ctx, guild, target, actor)
    await _safe_ephemeral(interaction, msg)


async def _do_grant_role(
    ctx: AppContext, guild: discord.Guild, target: discord.Member, actor: discord.Member
) -> tuple[bool, str]:
    role_id = await asyncio.to_thread(lambda: _grant_role_id(ctx, guild.id))
    role = guild.get_role(role_id) if role_id > 0 else None
    if role is None:
        return False, "❌ No grant role is configured — set one on the dashboard."
    # The dial is config, and a routine approval must never mint staff: on
    # 2026-09-03 it pointed at STAFF and two Level 5 presses handed it out. The
    # presser is a mod, so the reason (it names the role) is safe to show them.
    blocked = role_block_reason(role, get_bot_member(guild))
    if blocked is not None:
        return False, (
            f"❌ I won't grant that: {blocked}. "
            "Pick a different grant role on the dashboard."
        )
    try:
        await target.add_roles(role, reason=f"Promotion review: granted by {actor}")
    except discord.Forbidden:
        return False, "❌ I can't assign that role (check my role position)."
    except discord.HTTPException:
        return False, "❌ Discord hiccup assigning the role — try again."

    def _audit() -> None:
        with ctx.open_db() as conn:
            write_audit(
                conn,
                guild_id=guild.id,
                action=GRANT_AUDIT_ACTION,
                actor_id=actor.id,
                target_id=target.id,
                extra={"role_id": role.id, "role_name": role.name},
            )

    # role_events already logs the change from the gateway, but not who pressed:
    # the Level 5 card has no ledger row, so this is the only record naming them.
    # The role is already granted at this point; a DB hiccup writing the audit
    # row must not raise back into _resolve_card ("Never raises") and strand
    # the card unresolved despite the grant having gone through — log it and
    # still report success, same as the consent-write pattern in guess_cog.
    try:
        await asyncio.to_thread(_audit)
    except Exception:
        log.exception(
            "promo review: failed to write grant audit row for %s in guild %s",
            target.id, guild.id,
        )
    return True, "Access granted."


async def _do_reactivate(
    ctx: AppContext, guild: discord.Guild, target: discord.Member, actor: discord.Member
) -> tuple[bool, str]:
    try:
        result = await reactivate_member(
            ctx, guild, target, reason="Promotion review", actor=actor
        )
    except discord.HTTPException:
        return False, "❌ Discord hiccup reactivating — try again."
    # reactivate_member returns a status string; a leading ❌ marks failure.
    if result.startswith("❌"):
        return False, result
    return True, result


def _get_card(ctx: AppContext, card_id: int):
    with ctx.open_db() as conn:
        return svc.get_card(conn, card_id)


def _grant_role_id(ctx: AppContext, guild_id: int) -> int:
    with ctx.open_db() as conn:
        return svc.grant_role_id(conn, guild_id)


async def _edit_resolved(
    interaction: discord.Interaction,
    ctx: AppContext,
    guild: discord.Guild,
    card,
    resolution: str,
    resolver_id: int,
) -> None:
    message = interaction.message
    if message is None:
        return
    member_id = int(card["user_id"])
    kind = str(card["kind"])
    target = guild.get_member(member_id)
    display = str(target) if target is not None else str(member_id)

    def _fetch():
        with ctx.open_db() as conn:
            return (
                svc.pruned_roles_for(conn, guild.id, member_id),
                svc.member_level(conn, guild.id, member_id),
            )

    pruned_roles, level = await asyncio.to_thread(_fetch)
    accent = await safe_resolve_accent(ctx, guild, log_label="promotion review", default=DEFAULT_ACCENT_COLOR)
    # The member may have left since the card posted, so fall back past the cache.
    name_fn = await build_name_fn(
        guild=guild,
        db_path=ctx.db_path,
        guild_id=guild.id,
        user_ids=[member_id, resolver_id],
    )
    embed = build_review_embed(
        accent,
        kind=kind,
        member_id=member_id,
        member_display=display,
        level=level,
        prune_lines=format_prune_lines(guild, pruned_roles),
        action_hint="",
        name_fn=name_fn,
        resolved=(resolution, resolver_id),
    )
    try:
        await message.edit(embed=embed, view=None)
    except discord.HTTPException:
        log.debug("promo review: failed to edit resolved card", exc_info=True)
