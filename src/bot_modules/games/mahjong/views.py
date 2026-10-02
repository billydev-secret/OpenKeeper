"""Meadow Mahjong views — buttons and selects, no rules (stage 6, spec §6).

Components V2: every surface is a ``LayoutView`` carrying its own text. The
public table card (:class:`TableView`) takes its body from
``layouts.build_table_panel`` and adds ``layouts.phase_controls`` — custom
ids ``mmj:{table_id}:{action}``, stable across restarts. Every
member-private surface is a :class:`RackPanel`: the rack body from
``layouts.build_rack_panel`` with the phase's picker under it, which the
table's Open Rack button can always re-summon — that is the §6.4 "Refresh
Rack" fallback for expired interactions. All callbacks route to cog
handlers; the cog routes to the service; the service routes to the engine.
Nothing here decides anything.

The ephemeral views inherit ``core.layout.ExpiringLayoutView``: past their
timeout they grey out in place when the cog handed them their message. A
press that swaps a fresh view onto the same message stops the one it
replaces first (that class's contract).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.layout import ExpiringLayoutView, bind_present
from bot_modules.games.mahjong.game_logic import GameState, Phase
from bot_modules.games.mahjong.layouts import (
    PANEL_CARD_ID,
    PANEL_CREATE_ID,
    PANEL_HELP_ID,
    PANEL_SETTINGS_ID,
    PANEL_STATS_ID,
    TABLE_ACTIONS,
    phase_controls,
    table_cid,
)
from bot_modules.games.mahjong.tile_render import chip, load_emoji_map
from bot_modules.games.mahjong.tiles import FULL_RANK, Tile, sort_rack

if TYPE_CHECKING:
    from bot_modules.games.mahjong.cog import MahjongCog

Items = Sequence[discord.ui.Item[Any]]


def _cid(table_id: int, action: str) -> str:
    return table_cid(table_id, action)


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _grey(label: str, emoji: str | None = None, *, disabled: bool = False) -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label=label, style=discord.ButtonStyle.secondary, emoji=emoji, disabled=disabled,
    )


class TableView(discord.ui.LayoutView):
    """The persistent view on the sticky table message, built per phase:
    the card's body (``layouts.build_table_panel``) and the phase's row of
    buttons, each bound to the cog's table-button dispatch."""

    ACTIONS = TABLE_ACTIONS

    def __init__(self, cog: "MahjongCog", table_id: int, phase: Phase | None = None,
                 *, register_all: bool = False, add_bot: bool = False,
                 body: Items = ()):
        super().__init__(timeout=None)
        self.cog = cog
        self.table_id = table_id
        if register_all:
            # resume-time registration only (never posted): the sticky message
            # on disk may carry any phase's buttons until its next repost, and
            # an unregistered custom_id dies as "interaction failed" instead of
            # routing to the stale-table copy.
            row: list[discord.ui.Button[Any]] = []
            for action in self.ACTIONS:
                b = discord.ui.Button(label=action, custom_id=_cid(table_id, action))
                b.callback = self._make_callback(action)
                row.append(b)
                if len(row) == 5:
                    self.add_item(discord.ui.ActionRow(*row))
                    row = []
            if row:
                self.add_item(discord.ui.ActionRow(*row))
            return
        for item in body:
            self.add_item(item)
        for item in phase_controls(table_id, phase, add_bot=add_bot):
            self.add_item(item)
        bind_present(self, {
            _cid(table_id, action): self._make_callback(action) for action in self.ACTIONS
        })

    def _make_callback(self, action: str):
        async def cb(interaction: discord.Interaction):
            await self.cog.handle_table_button(interaction, self.table_id, action)
        return cb


def tile_emoji(tile: Tile) -> discord.PartialEmoji | None:
    """The registered tile face for a menu option, or None before the
    one-command emoji registration has run (§7.2's launch state) — callers
    fall back to the text chip, so a menu can never render blank."""
    emoji_id = load_emoji_map().get(tile.code)
    if not emoji_id:
        return None
    return discord.PartialEmoji(name=f"mm_{tile.code}", id=emoji_id)


def _tile_label(tile: Tile) -> tuple[str, discord.PartialEmoji | None]:
    """(label, emoji) for one tile: the face carries the picture and the
    name stays readable ("5 Bam"); without registered art the chip goes
    back in the label so the option is still identifiable."""
    face = tile_emoji(tile)
    return (tile.label if face else f"{chip(tile)} — {tile.label}"), face


def _tile_options(tiles: list[Tile]) -> list[discord.SelectOption]:
    """One option per rack slot; duplicate kinds get indexed values so a
    member can pick two of the same tile."""
    counts: dict[str, int] = {}
    options = []
    for t in sort_rack(tiles):
        n = counts.get(t.code, 0)
        counts[t.code] = n + 1
        label, face = _tile_label(t)
        options.append(discord.SelectOption(
            label=label, value=f"{t.code}#{n}", emoji=face,
        ))
    return options[:25]


def parse_tile_values(values: list[str]) -> list[Tile]:
    return [Tile(v.split("#")[0]) for v in values]


class RackPanel(ExpiringLayoutView):
    """An ephemeral member panel: the rack body first, then whatever picker
    the moment calls for (a subclass adds it in ``controls``). ``body`` is
    optional so a picker can be built and inspected on its own."""

    def __init__(self, body: Items = (), *, timeout: float | None = 600):
        super().__init__(timeout=timeout)
        for item in body:
            self.add_item(item)


class CharlestonPickView(RackPanel):
    """Pass picker: pick 3 (jokers pre-filtered), or blind some slots on
    the final pass of each Charleston."""

    def __init__(self, cog: "MahjongCog", table_id: int, seat_rack: list[Tile],
                 *, final_pass: bool, blind_n: int = 0, to_label: str | None = None,
                 body: Items = ()):
        super().__init__(body)
        self.cog = cog
        self.table_id = table_id
        pool = [t for t in seat_rack if t is not Tile.JOKER]
        picks = 3 - blind_n
        if picks > 0:
            select = discord.ui.Select(
                placeholder=f"Pick {picks} tile{'s' if picks != 1 else ''}"
                + (f" for {to_label}" if to_label else " to pass")
                + (f" ({blind_n} blind)" if blind_n else ""),
                min_values=picks, max_values=picks,
                options=_tile_options(pool),
            )

            async def on_pick(interaction: discord.Interaction):
                await cog.handle_charleston_pick(
                    interaction, table_id, parse_tile_values(select.values), blind_n
                )
            select.callback = on_pick
            self.add_item(discord.ui.ActionRow(select))
        if final_pass:
            buttons: list[discord.ui.Button[Any]] = []
            for n in range(1 if picks > 0 else 3, 4):
                if n == blind_n:
                    continue
                b = _grey(f"Blind ×{n}")

                async def on_blind(interaction: discord.Interaction, n=n):
                    if n == 3:
                        await cog.handle_charleston_pick(interaction, table_id, [], 3)
                    else:
                        self.stop()
                        fresh = CharlestonPickView(
                            cog, table_id, seat_rack,
                            final_pass=True, blind_n=n, to_label=to_label, body=body,
                        )
                        await interaction.response.edit_message(view=fresh)
                        fresh.message = await interaction.original_response()
                b.callback = on_blind
                buttons.append(b)
            if buttons:
                self.add_item(discord.ui.ActionRow(*buttons))


class CourtesyProposeView(RackPanel):
    def __init__(self, cog: "MahjongCog", table_id: int, body: Items = ()):
        super().__init__(body)
        buttons: list[discord.ui.Button[Any]] = []
        for n in range(4):
            b = _grey(f"Offer {n}")

            async def cb(interaction: discord.Interaction, n=n):
                await cog.handle_courtesy_propose(interaction, table_id, n)
            b.callback = cb
            buttons.append(b)
        self.add_item(discord.ui.ActionRow(*buttons))


class CourtesyGiveView(RackPanel):
    def __init__(self, cog: "MahjongCog", table_id: int,
                 seat_rack: list[Tile], owed: int, body: Items = ()):
        super().__init__(body)
        pool = [t for t in seat_rack if t is not Tile.JOKER]
        select = discord.ui.Select(
            placeholder=f"Give {owed} tile{'s' if owed != 1 else ''}",
            min_values=owed, max_values=owed, options=_tile_options(pool),
        )

        async def cb(interaction: discord.Interaction):
            await cog.handle_courtesy_give(
                interaction, table_id, parse_tile_values(select.values)
            )
        select.callback = cb
        self.add_item(discord.ui.ActionRow(select))


class TurnView(RackPanel):
    """Your-turn panel: discard select, Redeem Joker when legal, Mahjong."""

    def __init__(self, cog: "MahjongCog", table_id: int, state: GameState,
                 seat: int, body: Items = ()):
        super().__init__(body)
        rack = state.seats[seat].rack
        select = discord.ui.Select(
            placeholder="Discard a tile…", min_values=1, max_values=1,
            options=_tile_options(rack),
        )

        async def on_discard(interaction: discord.Interaction):
            await cog.handle_discard(
                interaction, table_id, parse_tile_values(select.values)[0]
            )
        select.callback = on_discard
        self.add_item(discord.ui.ActionRow(select))

        redeemable = _redeemable(state, seat)
        redeem = _grey("Redeem Joker", "🃏", disabled=not redeemable)

        async def on_redeem(interaction: discord.Interaction):
            self.stop()
            fresh = RedeemView(cog, table_id, state, seat, body=body)
            await interaction.response.edit_message(view=fresh)
            fresh.message = await interaction.original_response()
        redeem.callback = on_redeem

        mahjong = _grey("Mahjong", "🀄")

        async def on_mahjong(interaction: discord.Interaction):
            await cog.handle_self_mahjong(interaction, table_id)
        mahjong.callback = on_mahjong
        self.add_item(discord.ui.ActionRow(redeem, mahjong))


def _redeemable(state: GameState, seat: int) -> list[tuple[int, Tile]]:
    """(exposure_id, natural) pairs this seat could redeem right now."""
    rack = set(state.seats[seat].rack)
    out = []
    for s in state.seats:
        for e in s.exposures:
            if e.jokers > 0 and e.natural in rack:
                out.append((e.exposure_id, e.natural))
    return out


class RedeemView(RackPanel):
    def __init__(self, cog: "MahjongCog", table_id: int, state: GameState,
                 seat: int, body: Items = ()):
        super().__init__(body)
        options = [
            discord.SelectOption(
                label=(f"Swap your {_tile_label(natural)[0]} "
                       f"into exposure #{exposure_id}"),
                value=f"{exposure_id}:{natural.code}",
                emoji=tile_emoji(natural),
            )
            for exposure_id, natural in _redeemable(state, seat)[:25]
        ]
        select = discord.ui.Select(
            placeholder="Redeem which joker?", options=options,
        )

        async def cb(interaction: discord.Interaction):
            exposure_id, code = select.values[0].split(":")
            await cog.handle_redeem(
                interaction, table_id, int(exposure_id), Tile(code)
            )
        select.callback = cb
        self.add_item(discord.ui.ActionRow(select))


class CallTilesView(RackPanel):
    """After ✋ Call: pick the 2–5 rack tiles completing the exposure. Its
    own ephemeral card, so it carries its one line of text."""

    def __init__(self, cog: "MahjongCog", table_id: int,
                 seat_rack: list[Tile], discard_tile: Tile):
        super().__init__([_text("Complete the exposure:")], timeout=120)
        pool = [t for t in seat_rack if t is discard_tile or t is Tile.JOKER]
        # the cog never builds this with fewer than 2 matching tiles (it
        # routes such taps straight to the engine's private downgrade), so
        # min<=max<=len(options) always holds
        select = discord.ui.Select(
            placeholder=(
                f"Expose with your {_tile_label(discard_tile)[0]}s/jokers…"),
            min_values=2, max_values=min(5, len(pool)),
            options=_tile_options(pool),
        )

        async def cb(interaction: discord.Interaction):
            await cog.handle_call_submit(
                interaction, table_id, parse_tile_values(select.values),
                for_discard=discard_tile,
            )
        select.callback = cb
        self.add_item(discord.ui.ActionRow(select))


class MemberPanelView(ExpiringLayoutView):
    """The /mahjong panel: ``layouts.build_member_panel``'s card with its
    five buttons bound to the cog's handlers."""

    def __init__(self, cog: "MahjongCog", body: Items = ()):
        super().__init__(timeout=600)
        for item in body:
            self.add_item(item)
        bind_present(self, {
            PANEL_CREATE_ID: cog.handle_create_start,
            PANEL_CARD_ID: cog.handle_card_viewer,
            PANEL_STATS_ID: cog.handle_my_stats,
            PANEL_SETTINGS_ID: cog.handle_my_settings,
            PANEL_HELP_ID: cog.handle_how_to_play,
        })


ASSIST_CHOICES = (
    ("off", "Off", "Pure card-reading — no readout."),
    ("target", "Target", "Closest hands and how far away each is."),
    ("gap", "Target + gap", "…plus the tiles you still need."),
    ("coach", "Coach", "…plus dead weight and a suggested discard."),
)

SETTINGS_TEXT = (
    "**My Settings** — how much help the table gives you. Your pick "
    "applies to every game you play here."
)


class MySettingsView(ExpiringLayoutView):
    """The /mahjong My Settings menu (plans/mahjong-assist.md A10) — an
    ephemeral container for per-player preferences; assistance is its first
    tenant. One select today; future settings join this view, not the panel."""

    def __init__(self, cog: "MahjongCog", current: str, *, note: str | None = None):
        super().__init__(timeout=600)
        self.add_item(_text(note or SETTINGS_TEXT))
        select = discord.ui.Select(
            placeholder="Assistance level",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=label, value=value, description=desc,
                    default=value == current,
                )
                for value, label, desc in ASSIST_CHOICES
            ],
        )

        async def on_pick(interaction: discord.Interaction):
            await cog.handle_assist_pick(interaction, select.values[0], replaced=self)
        select.callback = on_pick
        self.add_item(discord.ui.ActionRow(select))


class CreateTableView(ExpiringLayoutView):
    """Size picker → stake select with escrow labels → open. Practice rows
    (bots plan B5) skip the stake select entirely — there is none."""

    def __init__(self, cog: "MahjongCog", stakes: tuple[int, ...],
                 escrow_for, seat_count: int | None = None,
                 *, practice_open: bool = False, short_rank: int = 0,
                 max_rank: int = FULL_RANK, note: str | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        if seat_count is None:
            self.add_item(_text("Table size?"))
            sizes = [("Duel (2)", 2, FULL_RANK), ("Full Table (4)", 4, FULL_RANK)]
            if short_rank:
                # A shorter deck is the length dial, so these are labelled by
                # what a player notices — a faster game — with the numbers in
                # play spelled out rather than left to be discovered mid-hand.
                sizes += [
                    (f"Quick Duel (1–{short_rank})", 2, short_rank),
                    (f"Quick Table (1–{short_rank})", 4, short_rank),
                ]
            row: list[discord.ui.Button[Any]] = []
            for label, count, rank in sizes:
                b = _grey(label)

                async def pick_size(interaction: discord.Interaction,
                                    count=count, rank=rank):
                    note = ("" if rank == FULL_RANK else
                            f" (quick deck: numbers 1–{rank} only)")
                    self.stop()
                    fresh = CreateTableView(
                        cog, stakes, escrow_for, count, max_rank=rank,
                        note=f"Pick the stake — escrow locks when you sit{note}:",
                    )
                    await interaction.response.edit_message(view=fresh)
                    fresh.message = await interaction.original_response()
                b.callback = pick_size
                row.append(b)
            self.add_item(discord.ui.ActionRow(*row))
            if practice_open:
                # The quick deck leads when the house has opened it
                # (mahjong-147): practice is where beginners are sent, and
                # the full deck is the hour-long form. Its own row, so the
                # staked sizes and the free ones never interleave.
                practice = [("Practice Duel", 2, FULL_RANK),
                            ("Practice Table", 4, FULL_RANK)]
                if short_rank:
                    practice = [
                        (f"Quick Practice Duel (1–{short_rank})", 2, short_rank),
                        (f"Quick Practice Table (1–{short_rank})", 4, short_rank),
                    ] + practice
                row = []
                for label, count, rank in practice:
                    b = _grey(label, "🌱")

                    async def cb(interaction: discord.Interaction,
                                 count=count, rank=rank):
                        await cog.handle_create_practice(interaction, count, rank)
                    b.callback = cb
                    row.append(b)
                self.add_item(discord.ui.ActionRow(*row))
            return
        self.add_item(_text(note or "Pick the stake — escrow locks when you sit:"))
        options = [
            discord.SelectOption(
                label=f"{s} coin{'s' if s != 1 else ''} per point",
                description=f"{escrow_for(seat_count, s)} coins escrow per seat",
                value=str(s),
            )
            for s in stakes[:25]
        ]
        select = discord.ui.Select(placeholder="Stake…", options=options)

        async def on_stake(interaction: discord.Interaction):
            stake = int(select.values[0])
            self.stop()
            fresh = OpenTableView(
                cog, seat_count, stake, max_rank,
                text=(
                    f"{'Duel' if seat_count == 2 else 'Full Table'} at "
                    f"{stake}/point — {escrow_for(seat_count, stake)} coins "
                    "escrow locks when you open it."
                ),
            )
            await interaction.response.edit_message(view=fresh)
            fresh.message = await interaction.original_response()
        select.callback = on_stake
        self.add_item(discord.ui.ActionRow(select))


class OpenTableView(ExpiringLayoutView):
    def __init__(self, cog: "MahjongCog", seat_count: int, stake: int,
                 max_rank: int = FULL_RANK, *, text: str = ""):
        super().__init__(timeout=300)
        self.max_rank = max_rank
        if text:
            self.add_item(_text(text))
        b = _grey("Open Table")

        async def cb(interaction: discord.Interaction):
            await cog.handle_create_open(
                interaction, seat_count, stake, max_rank
            )
        b.callback = cb
        self.add_item(discord.ui.ActionRow(b))
