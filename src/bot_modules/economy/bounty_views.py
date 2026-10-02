"""Discord views for Community Bounty — the board card and its buttons.

Components V2 since 2026-09-28: every card this feature sends is a
``LayoutView`` built by ``bot_modules/economy/bounty_layouts.py``, not an
embed (``docs/plans/components_v2_migration.md`` wave 3a).

One **persistent** card per bounty carries three ``discord.ui.DynamicItem``
buttons whose ``custom_id`` embeds the bounty id (``econ_bounty:chip:<id>`` /
``:award:<id>`` / ``:cancel:<id>``), so clicks still route after a restart once
the cog re-registers the classes:

* 💰 **Chip In** — any member; opens an amount modal and escrows into the pot.
* 🏆 **Award** — the poster or a mod; opens a ``UserSelect`` and pays the winner
  minus rake. A poster can't pick themselves (``resolve_denial``).
* **Cancel** — the poster or a mod; refunds every contributor.

Above the cards sits one **hub panel** per guild, stuck to the bottom of the
board channel by ``core.sticky`` (see ``EconomyCog.bounty_panel``). It explains
the mechanic and lists every open bounty, each with its own 💰 **Chip In**
button, plus the board's one card-level entry point, 🎯 **Post Bounty**. It
replaced the ``/bounty`` command on 2026-07-29.

Every handler is fail-safe — a service error becomes an ephemeral note, never a
dead button. Two resolvers at once is settled in the service (the state guard);
the loser gets the card refreshed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.economy.bounty_layouts import (
    AWARD_CUSTOM_ID_TEMPLATE,
    CANCEL_CUSTOM_ID_TEMPLATE,
    CHIP_CUSTOM_ID_TEMPLATE,
    HUB_CHIP_CUSTOM_ID_TEMPLATE,
    HUB_POST_CUSTOM_ID,
    HUB_TITLE,
    build_bounty_card_layout,
    build_bounty_hub_layout,
)
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.notice_batch import queue_notice
from bot_modules.economy.view_helpers import card_name_fn
from bot_modules.economy.view_helpers import safe_ephemeral as _safe_ephemeral
from bot_modules.shared.name_resolver import NameFn
from bot_modules.core.branding_service import PanelArt, resolve_panel_art_conn
from bot_modules.panels.title_service import with_lobby_title
from bot_modules.economy.bounty_service import (
    HUB_LIST_LIMIT,
    BountyBoardEntry,
    award_bounty,
    award_pair_no_contact,
    board_entries,
    cancel_bounty,
    contribute,
    contributor_count,
    get_bounty,
    names_two_members,
    notifiable_contributors,
    open_board_count,
    pot_of,
    resolve_denial,
    set_bounty_card,
    should_notify_winner,
)
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")


# ── modals / selects ───────────────────────────────────────────────────────


class _ChipInModal(discord.ui.Modal, title="Chip In to This Bounty"):
    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="How much?",
        placeholder="A whole number of coins",
        max_length=12,
    )

    def __init__(
        self,
        bounty_id: int,
        card: discord.Message | None,
        *,
        refresh_by_ids: bool = False,
    ) -> None:
        super().__init__()
        self.bounty_id = bounty_id
        self.card = card
        #: Set when the chip-in came from the hub panel, which has no card
        #: message to hand us — refetch the card by its stored ids instead.
        self.refresh_by_ids = refresh_by_ids

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_chip(
            interaction,
            self.bounty_id,
            str(self.amount.value),
            self.card,
            refresh_by_ids=self.refresh_by_ids,
        )


class _AwardSelect(discord.ui.UserSelect):
    def __init__(self, bounty_id: int, card: discord.Message | None) -> None:
        super().__init__(placeholder="Who gets the bounty?", min_values=1, max_values=1)
        self.bounty_id = bounty_id
        self.card = card

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_award(interaction, self.bounty_id, self.values[0], self.card)


class _AwardSelectView(discord.ui.View):
    """Ephemeral member picker shown to the resolver after they click Award."""

    def __init__(self, bounty_id: int, card: discord.Message | None) -> None:
        super().__init__(timeout=300)
        self.add_item(_AwardSelect(bounty_id, card))


# ── persistent card buttons ──────────────────────────────────────────────────


class BountyChipInButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:chip:(?P<bid>\d+)"),
):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Chip In", emoji="💰",
                style=discord.ButtonStyle.success,
                custom_id=CHIP_CUSTOM_ID_TEMPLATE.format(bounty_id=bounty_id),
            )
        )
        self.bounty_id = bounty_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyChipInButton:
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _ChipInModal(self.bounty_id, interaction.message)
        )


class BountyAwardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:award:(?P<bid>\d+)"),
):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Award", emoji="🏆",
                style=discord.ButtonStyle.primary,
                custom_id=AWARD_CUSTOM_ID_TEMPLATE.format(bounty_id=bounty_id),
            )
        )
        self.bounty_id = bounty_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyAwardButton:
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # No winner yet, so the poster passes here and the self-award rule is
        # re-asked in _handle_award once they've picked someone.
        if await _gate_resolve(interaction, self.bounty_id) is None:
            return
        await interaction.response.send_message(
            "Pick who earned this bounty:",
            view=_AwardSelectView(self.bounty_id, interaction.message),
            ephemeral=True,
        )


class BountyCancelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:cancel:(?P<bid>\d+)"),
):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.danger,
                custom_id=CANCEL_CUSTOM_ID_TEMPLATE.format(bounty_id=bounty_id),
            )
        )
        self.bounty_id = bounty_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyCancelButton:
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_cancel(interaction, self.bounty_id)


class BountyBoardView(discord.ui.LayoutView):
    """Persistent (timeout=None) card for one bounty.

    Thin wrapper around ``build_bounty_card_layout`` — Chip In / Award /
    Cancel live in its bottom row while the bounty is open, and nowhere once
    it is awarded, cancelled or expired, which is the whole of what this
    class needs to add over the pure builder.
    """

    def __init__(
        self,
        settings: EconSettings,
        bounty,
        *,
        pot: int,
        contributors: int,
        name_fn: NameFn,
        art: PanelArt | None = None,
    ) -> None:
        super().__init__(timeout=None)
        for item in build_bounty_card_layout(
            settings, bounty, pot=pot, contributors=contributors, name_fn=name_fn,
            art=art,
        ):
            self.add_item(item)


# ── persistent hub buttons ───────────────────────────────────────────────────


class BountyHubPostButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:hub_post"),
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Post Bounty", emoji="🎯",
                style=discord.ButtonStyle.primary,
                custom_id=HUB_POST_CUSTOM_ID,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyHubPostButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        # The modal lives on the cog (it calls back into do_bounty_post), and a
        # modal must be the interaction's FIRST response — so the cog opens it
        # rather than handing one back here.
        cog = cast("Bot", interaction.client).get_cog("EconomyCog")
        # getattr, not a direct call: importing EconomyCog here to type it would
        # be circular (the cog imports this module for its buttons).
        opener = getattr(cog, "open_bounty_post_modal", None)
        if opener is None:  # pragma: no cover — cog is always loaded in practice
            await _safe_ephemeral(interaction, "❌ The economy is unavailable.")
            return
        await opener(interaction)


class BountyHubChipButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:hub_chip:(?P<bid>\d+)"),
):
    """Per-row Chip In on the hub panel — straight to the amount modal.

    A distinct custom_id format from the card's own Chip In button: this
    click's ``interaction.message`` is the hub panel, not the bounty's card,
    so the card to repaint has to be found by its stored ids rather than
    taken from the interaction (``refresh_by_ids=True``). Replaces the old
    bounty-picker select (``_HubChipSelect``/``_HubChipView``) now that every
    open row carries its own button — one press reaches the modal instead of
    picking a bounty first and then seeing it.
    """

    def __init__(self, bounty_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Chip In", emoji="💰",
                style=discord.ButtonStyle.success,
                custom_id=HUB_CHIP_CUSTOM_ID_TEMPLATE.format(bounty_id=bounty_id),
            )
        )
        self.bounty_id = bounty_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyHubChipButton:
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _ChipInModal(self.bounty_id, None, refresh_by_ids=True)
        )


class BountyHubView(discord.ui.LayoutView):
    """Persistent (timeout=None) hub: how-it-works, the open list, Post Bounty.

    ``art`` is the guild's hub art (``branding_service.PanelArt``) — a hub
    with no banner of its own wears the small lobby strip (Ben, 2026-09-28,
    see ``bounty_layouts``' module docstring), drawn through ``art.header``.
    Defaults to no art so a caller building one for a test needn't supply it.
    """

    def __init__(
        self,
        settings: EconSettings,
        guild_id: int,
        entries: list[BountyBoardEntry],
        *,
        open_total: int,
        art: PanelArt | None = None,
    ) -> None:
        super().__init__(timeout=None)
        art = art or PanelArt(banner=None, footer=None)
        for item in build_bounty_hub_layout(
            settings, guild_id, entries, open_total=open_total,
            header_url=art.header, footer_url=art.footer,
            title_in_header=art.title_in_header,
        ):
            self.add_item(item)


async def build_bounty_hub_panel(bot: Bot, guild: discord.Guild) -> BountyHubView:
    """Render the hub for ``guild`` — the cog's StickyPanel ``build`` callback."""

    def _read() -> tuple[EconSettings, list[BountyBoardEntry], int, PanelArt]:
        # One connection for settings, list, count and art: this runs on
        # every sticky repost, so a connect+PRAGMA per read would be the hot
        # waste.
        with bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild.id)
            entries = board_entries(conn, guild.id, limit=HUB_LIST_LIMIT)
            open_total = open_board_count(conn, guild.id)
            art = resolve_panel_art_conn(conn, guild.id)
        # Draws the strip's title on first use, after the connection closes.
        return settings, entries, open_total, with_lobby_title(art, HUB_TITLE)

    settings, entries, open_total, art = await asyncio.to_thread(_read)
    return BountyHubView(settings, guild.id, entries, open_total=open_total, art=art)


# ── helpers ──────────────────────────────────────────────────────────────────


async def _gate_resolve(
    interaction: discord.Interaction, bounty_id: int, *, winner_id: int | None = None
) -> EconSettings | None:
    """The settings if the clicker may award/cancel this bounty; else reply, None.

    Loads the row alongside the settings so :func:`resolve_denial` can see the
    poster — the buttons are on a persistent card and every member can press
    them, so this is the only thing standing between a stranger and the pot.
    Hands the settings back because the award path needs them for the rake and
    would otherwise re-read them a second time.
    """
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return None
    bot = cast("Bot", interaction.client)

    def _load() -> tuple[EconSettings, int | None]:
        with bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild.id)
            row = get_bounty(conn, bounty_id)
            poster = int(row["poster_id"]) if row is not None else None
            return settings, poster

    settings, poster_id = await asyncio.to_thread(_load)
    if poster_id is None:
        # Gone since the card was drawn; the service would say the same thing.
        await _safe_ephemeral(interaction, "❌ That bounty no longer exists.")
        return None
    denial = resolve_denial(
        poster_id=poster_id,
        actor_id=member.id,
        is_manager=can_manage_economy(member, settings),
        winner_id=winner_id,
    )
    if denial is not None:
        await _safe_ephemeral(interaction, denial)
        return None
    return settings


async def _load_settings(bot: Bot, guild_id: int) -> EconSettings:
    def _read() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)

    return await asyncio.to_thread(_read)


async def _card_name_fn(ctx: AppContext, guild: discord.Guild, row) -> NameFn:
    """Names for a card's poster and winner — either may have left by now.

    Goes through the shared ``card_name_fn`` for its never-raises guarantee:
    a repaint after an award has already paid out must degrade to ``<@id>``,
    not abandon the card over a database hiccup.

    One exception, and it is the whole point of this wrapper: an awarded card
    whose poster and winner are a no-contact pair prints both as a bare
    ``User <id>`` (``award_pair_no_contact``). The card is public and says
    "Posted by X / Won by Y" in the bot's own voice, which is the association
    the list exists to prevent — the same call ``games.guess.layouts.reveal_names``
    makes. A number is the *correct* output there; don't "fix" it. Sitting in
    the name function rather than in ``build_bounty_card_layout`` means every
    repaint path gets it, including ones added later.
    """
    def _degraded() -> bool:
        if not names_two_members(row):
            return False  # nobody to name the poster beside
        try:
            with ctx.open_db() as conn:
                return award_pair_no_contact(conn, guild.id, row)
        except sqlite3.Error:
            # Fail toward the degrade: the pair could not be ruled out, and a
            # bare id is recoverable where naming them together is not. Only
            # an awarded card reaches here, so an ordinary repaint during a
            # database hiccup still falls back to ``<@id>`` as it always did.
            log.debug("econ bounty: no-contact check failed for a card", exc_info=True)
            return True

    if await asyncio.to_thread(_degraded):
        return lambda uid: f"User {uid}"
    return await card_name_fn(
        ctx, guild, None, int(row["poster_id"]), int(row["winner_id"] or 0)
    )


async def _refresh_card(
    bot: Bot, card: discord.Message | None, guild: discord.Guild, bounty_id: int
) -> None:
    """Re-render a card from the current row (after any state move)."""
    if card is None:
        return
    settings = await _load_settings(bot, guild.id)

    def _read():
        with bot.ctx.open_db() as conn:
            row = get_bounty(conn, bounty_id)
            if row is None:
                return None
            return (
                row, pot_of(conn, bounty_id), contributor_count(conn, bounty_id),
                resolve_panel_art_conn(conn, guild.id),
            )

    data = await asyncio.to_thread(_read)
    if data is None:
        return
    row, pot, contributors, art = data
    name_fn = await _card_name_fn(bot.ctx, guild, row)
    view = BountyBoardView(
        settings, row, pot=pot, contributors=contributors, name_fn=name_fn, art=art
    )
    try:
        await card.edit(content=None, embed=None, attachments=[], view=view)
    except discord.HTTPException:
        log.debug("econ bounty: failed to edit card", exc_info=True)


# ── handlers ─────────────────────────────────────────────────────────────────


async def _refresh_card_by_stored_ids(
    bot: Bot, guild: discord.Guild, bounty_id: int
) -> None:
    """Repaint a bounty's card when the caller has no message to edit.

    The hub's Chip In button is one interaction removed from the card, so the
    card's ids are the only handle on it. Best-effort like every other refresh:
    the coins are already escrowed by the time this runs.
    """

    def _ids() -> tuple[int, int] | None:
        with bot.ctx.open_db() as conn:
            row = get_bounty(conn, bounty_id)
            if row is None:
                return None
            return int(row["card_channel_id"]), int(row["card_message_id"])

    ids = await asyncio.to_thread(_ids)
    if ids is None:
        return
    await refresh_card_by_id(bot, guild, ids[0], ids[1], bounty_id)


async def refresh_bounty_hub(bot: discord.Client, guild: discord.Guild) -> None:
    """Nudge the board's hub panel to repaint after a pot changed.

    Takes a bare ``Client`` like :func:`refresh_card_by_id` so the economy loop
    can call it with the bot it holds. Best-effort and never raises: a stale pot
    on the hub is cosmetic (the card is authoritative), so this must not turn a
    successful chip-in into an error.
    """
    cog = cast("Bot", bot).get_cog("EconomyCog")
    refresh = getattr(cog, "refresh_bounty_hub_panel", None)
    if refresh is None:
        return
    try:
        await refresh(guild)
    except Exception:  # noqa: BLE001 — best-effort cosmetic repaint after the chip-in already succeeded; panel refresh's raise set (discord send + DB) isn't fully caught internally
        log.debug("econ bounty: hub refresh failed", exc_info=True)


async def _handle_chip(
    interaction: discord.Interaction,
    bounty_id: int,
    raw_amount: str,
    card: discord.Message | None,
    *,
    refresh_by_ids: bool = False,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)

    try:
        amount = int(raw_amount.strip())
    except ValueError:
        await _safe_ephemeral(interaction, "❌ Enter a whole number of coins.")
        return
    if amount <= 0:
        await _safe_ephemeral(interaction, "❌ Enter a positive amount.")
        return

    settings = await _load_settings(bot, guild.id)

    def _contribute() -> int:
        with bot.ctx.open_db() as conn:
            return contribute(conn, settings, guild.id, bounty_id, member.id, amount)

    try:
        pot = await asyncio.to_thread(_contribute)
    except ValueError as exc:
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ bounty: chip-in failed for %s", bounty_id)
        await _safe_ephemeral(interaction, "❌ Couldn't add that — try again.")
        return

    if refresh_by_ids:
        await _refresh_card_by_stored_ids(bot, guild, bounty_id)
    else:
        await _refresh_card(bot, card, guild, bounty_id)
    await refresh_bounty_hub(bot, guild)
    await _safe_ephemeral(
        interaction, f"💰 Chipped in {amount:,} — the pot is now {pot:,}."
    )


async def _handle_award(
    interaction: discord.Interaction,
    bounty_id: int,
    winner: discord.User | discord.Member,
    card: discord.Message | None,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)

    settings = await _gate_resolve(interaction, bounty_id, winner_id=winner.id)
    if settings is None:
        return

    def _award():
        with bot.ctx.open_db() as conn:
            return award_bounty(
                conn, settings, guild.id, bounty_id,
                winner_id=winner.id, resolver_id=member.id,
            )

    try:
        result = await asyncio.to_thread(_award)
    except ValueError as exc:
        await _refresh_card(bot, card, guild, bounty_id)
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ bounty: award failed for %s", bounty_id)
        await _safe_ephemeral(interaction, "❌ Couldn't award that — try again.")
        return

    await _refresh_card(bot, card, guild, bounty_id)
    # The bounty just left the open list the hub renders.
    await refresh_bounty_hub(bot, guild)

    def _may_dm() -> bool:
        with bot.ctx.open_db() as conn:
            return should_notify_winner(
                conn, guild.id, int(result.bounty["poster_id"]), winner.id
            )

    try:
        # The card is an ordinary message (only the hub is sticky), so its
        # link keeps working — the winner can go read what they were paid for.
        # Unless the two are a no-contact pair: the card names the poster, so
        # the DM is dropped and the award reads as an ordinary success.
        where = f"\n{card.jump_url}" if card is not None else ""
        if await asyncio.to_thread(_may_dm):
            safe_title = discord.utils.escape_markdown(str(result.bounty["title"]))
            await notify_member(
                bot, bot.ctx.db_path, guild.id, winner.id,
                content=(
                    f"🏆 You were awarded the bounty **{safe_title}** — "
                    f"{result.payout:,} coins are in your wallet!{where}"
                ),
            )
    except Exception:  # noqa: BLE001 — best-effort receipt DM after the award already moved money; notify_member's own catch doesn't cover every failure mode of the underlying send
        log.debug("econ bounty: failed to DM winner", exc_info=True)
    tail = f" (house kept {result.rake:,})" if result.rake else ""
    await _safe_ephemeral(
        interaction,
        f"🏆 Awarded to {winner.mention} — {result.payout:,} paid out{tail}.",
    )


async def _handle_cancel(interaction: discord.Interaction, bounty_id: int) -> None:
    if await _gate_resolve(interaction, bounty_id) is None:
        return
    guild = interaction.guild
    member = interaction.user
    assert guild is not None and isinstance(member, discord.Member)
    bot = cast("Bot", interaction.client)
    card = interaction.message
    await interaction.response.defer(ephemeral=True)

    def _cancel():
        with bot.ctx.open_db() as conn:
            return cancel_bounty(conn, guild.id, bounty_id, resolver_id=member.id)

    try:
        row, refunded = await asyncio.to_thread(_cancel)
    except ValueError as exc:
        await _refresh_card(bot, card, guild, bounty_id)
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ bounty: cancel failed for %s", bounty_id)
        await _safe_ephemeral(interaction, "❌ Couldn't cancel that — try again.")
        return

    await _refresh_card(bot, card, guild, bounty_id)
    # The bounty just left the open list the hub renders.
    await refresh_bounty_hub(bot, guild)
    where = f"\n{card.jump_url}" if card is not None else ""

    poster_id = int(row["poster_id"])

    def _tell() -> None:
        with bot.ctx.open_db() as conn:
            for uid in notifiable_contributors(conn, guild.id, poster_id, refunded):
                # Per contributor, as the direct DMs were: one failed insert
                # must not cost the rest of the list their note.
                try:
                    queue_notice(
                        conn, guild_id=guild.id, recipient_id=uid,
                        line=(
                            "A bounty you chipped into was cancelled — your "
                            f"coins are back.{where}"
                        ),
                        actor_id=poster_id, named=False,
                    )
                except Exception:  # noqa: BLE001 — best-effort note; the refund already landed
                    log.warning(
                        "econ bounty: failed to queue refund note for %s on %s",
                        uid, bounty_id, exc_info=True,
                    )

    # Everyone in `refunded` has their coins back; this only decides who hears
    # about it. The note links the card, and the card names the poster — so a
    # contributor under a no-contact rule with them is left out of the telling,
    # not out of the refund (and the poster rides as the note's actor, so a
    # rule added before it goes out still stops it). The resolver's receipt
    # still counts them all. The note waits for the contributor's next batched
    # economy DM (economy.notice_batch).
    try:
        await asyncio.to_thread(_tell)
    except Exception:  # noqa: BLE001 — best-effort refund note after the coins already moved back
        log.warning("econ bounty: failed to queue refund notes for %s", bounty_id, exc_info=True)
    await _safe_ephemeral(
        interaction, f"✖️ Cancelled — refunded {len(refunded)} contributor(s)."
    )


async def refresh_card_by_id(
    bot: discord.Client,
    guild: discord.Guild,
    channel_id: int,
    message_id: int,
    bounty_id: int,
) -> bool:
    """Fetch a board card by ids and re-render it — used by the expiry sweep.

    Returns whether the card was actually found and repainted. The expiry
    sweep DMs refunded contributors a link to that card, and a mod who tidied
    the board channel has already deleted it — so "did the fetch work" is the
    only signal available that the permalink would 404. False means link
    nothing rather than send a dead one.
    """
    if not channel_id or not message_id:
        return False
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return False
    try:
        card = await channel.fetch_message(message_id)
    except discord.HTTPException:
        return False
    await _refresh_card(cast("Bot", bot), card, guild, bounty_id)
    return True


async def post_bounty_card(
    bot: Bot,
    ctx: AppContext,
    guild: discord.Guild,
    settings: EconSettings,
    bounty_id: int,
) -> None:
    """Best-effort: post the board card to the bounty channel and record its ids."""
    channel = guild.get_channel(int(settings.bounty_channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        return

    def _read():
        with ctx.open_db() as conn:
            row = get_bounty(conn, bounty_id)
            if row is None:
                return None
            return (
                row, pot_of(conn, bounty_id), contributor_count(conn, bounty_id),
                resolve_panel_art_conn(conn, guild.id),
            )

    try:
        data = await asyncio.to_thread(_read)
        if data is None:
            return
        row, pot, contributors, art = data
        name_fn = await _card_name_fn(ctx, guild, row)
        view = BountyBoardView(
            settings, row, pot=pot, contributors=contributors, name_fn=name_fn, art=art
        )
        message = await channel.send(view=view)
    except discord.HTTPException:
        log.warning("econ bounty: failed to post card for %s", bounty_id)
        return
    except Exception:
        log.exception("econ bounty: unexpected error posting card %s", bounty_id)
        return

    def _record() -> None:
        with ctx.open_db() as conn:
            set_bounty_card(conn, bounty_id, channel.id, message.id)

    try:
        await asyncio.to_thread(_record)
    except sqlite3.Error:
        log.debug("econ bounty: failed to record card ids", exc_info=True)
