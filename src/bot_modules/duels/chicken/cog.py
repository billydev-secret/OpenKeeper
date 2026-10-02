"""Chicken cog — brinkmanship for 2..N players. Hold your nerve or bail before the crash."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import asyncio
import json
import logging
import random
import time

import discord
from discord import app_commands

from bot_modules.core.background import spawn
from bot_modules.core.utils import allow_pings
from bot_modules.duels import db as duels_db
from bot_modules.duels.base_game import BaseGame
from bot_modules.duels.filters import game_is_nick_stake
from bot_modules.games.command_groups import games
from bot_modules.shared.name_resolver import GuildNames

from . import db as chdb
from . import layouts as chicken_layouts
from .game import (
    ChickenGame,
    bravest_bailer,
    crash_pct,
    meter_pct,
    resolve_crash,
    roll_crash_at,
)
from .views import ChickenView

log = logging.getLogger("dungeonkeeper.chicken")

_TICK_INTERVAL = 2.0


class ChickenCog(BaseGame, name="ChickenCog"):

    GAME_KEY = "chicken"
    GAME_DISPLAY_NAME = "Chicken"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        # Chicken runs TWO concurrent tasks per game: the crash deadline + the meter
        # ticker that edits the embed as the bar climbs.
        self._timers: dict[int, list[asyncio.Task]] = {}

    chicken = app_commands.Group(
        name="chicken",
        description="Chicken — hold your nerve, or bail before the meter crashes!",
    )

    # ── DB hooks ──────────────────────────────────────────────────────────────

    async def _db_get_game(self, game_id: int) -> ChickenGame | None:
        return await chdb.get_game(self.db, game_id)

    async def _db_write_state(self, game_id: int, state: str, **kw) -> None:
        await chdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_create_lobby(
        self, guild_id: int, channel_id: int, host_id: int, stakes_text: str | None,
        nick_stake: bool = False,
    ) -> int:
        return await chdb.create_lobby(
            self.db, guild_id, channel_id, host_id, stakes_text, nick_stake
        )

    async def _db_fetch_active_games(self) -> list[ChickenGame]:
        return await chdb.fetch_active_games(self.db)

    async def _db_fetch_lobby_games(self) -> list[ChickenGame]:
        return await chdb.fetch_lobby_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[ChickenGame]:
        return await chdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[ChickenGame]:
        return await chdb.fetch_sweepable_games(self.db, now)

    async def get_lobby_params(self, guild_id: int) -> tuple[int, int]:
        cfg = await chdb.get_config(self.db, guild_id)
        return int(cfg["min_players"]), int(cfg["max_players"])

    # ── Timer helpers (multiple tasks per game) ───────────────────────────────

    def _cancel_timers(self, game_id: int) -> None:
        # _crash reaches here from inside the crash task; cancelling the
        # running task would land on its next await and drop the rest.
        current = asyncio.current_task()
        for task in self._timers.pop(game_id, []):
            if task is not current and not task.done():
                task.cancel()

    def _add_timer(self, game_id: int, task: asyncio.Task) -> None:
        self._timers.setdefault(game_id, []).append(task)

    async def _run_crash_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._crash(game_id)

    async def _run_ticker(self, game_id: int, interval: float, total: float) -> None:
        elapsed = 0.0
        try:
            while elapsed < total:
                await asyncio.sleep(interval)
                elapsed += interval
                async with self._get_lock(game_id):
                    game = await chdb.get_game(self.db, game_id)
                    if not game or game.state != "ACTIVE" or game.phase != "CLIMBING":
                        return
                    guild = self.bot.get_guild(game.guild_id)
                    if guild and game.message_id:
                        # No embed-only arm for a layout card: every full re-render
                        # here re-sends the view too, which can invalidate an
                        # in-flight BAIL click ("interaction failed") — a known,
                        # accepted tradeoff of the V2 conversion (no equivalent to
                        # the old embed-only edit exists on this seam).
                        dv = self.build_game_view(game.id)
                        dv.render(self.render_game_state(game, guild))
                        await self._edit_message_silent(
                            game.channel_id, game.message_id, layout=dv,
                        )
        except asyncio.CancelledError:
            return

    async def _crash(self, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await chdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "CLIMBING":
                return
            crashers = list(game.alive)
            # The tie-break among crashers is a seeded draw, and the seed is
            # logged: whoever eats the nick is random, not the oldest account,
            # and a disputed draw can be replayed from the log.
            seed = random.getrandbits(32)
            winner, loser = resolve_crash(crashers, game.bail_log, random.Random(seed))
            log.info(
                "chicken game %s crashed at %.1fs: crashers=%s seed=%s loser=%s",
                game_id, game.crash_at or 0.0, crashers, seed, loser,
            )
            if loser is not None and winner is not None:
                await self._post_group_result(game, winner, loser)
            else:
                # total wipeout (nobody bailed) → cosmetic, no nick
                await self._resolve_cosmetic(game, winner)
        self._cancel_timers(game_id)
        self._game_locks.pop(game_id, None)

    async def _resolve_cosmetic(self, game: ChickenGame, winner_id: int | None) -> None:
        """Resolve with no nickname stake (everyone bailed, or total wipeout)."""
        now = time.time()
        game.winner_id = winner_id
        guild = self.bot.get_guild(game.guild_id)
        for uid in game.roster:
            await duels_db.set_group_cooldown(self.db, game.guild_id, self.GAME_KEY, uid)

        # The result replaces the board on the game's own card, framed like
        # every other render of it; an edit, so nobody is pinged.
        result_message_id = None
        if guild:
            items = self.render_result_state(game, guild)
            if await self._edit_card(
                game.channel_id, game.message_id,
                view=self._framed_card(game.id, items),
                content=None, embed=None, attachments=[],
            ):
                result_message_id = game.message_id

        await self._db_set_state(
            game.id, "RESOLVED_NO_NICK",
            winner_id=winner_id,
            result_message_id=result_message_id,
            resolved_at=now,
            last_action_at=now,
        )
        await self.on_game_resolved(game.id)

    # ── Button handler ────────────────────────────────────────────────────────

    async def _on_bail(self, interaction: discord.Interaction, game_id: int) -> None:
        await interaction.response.defer()
        resolved = False
        async with self._get_lock(game_id):
            game = await chdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "CLIMBING":
                await interaction.followup.send(
                    "This game is no longer active.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid not in game.alive:
                await interaction.followup.send(
                    "You've already bailed (or you're not in this game).", ephemeral=True
                )
                return

            now = time.time()
            pct = meter_pct(now, game.climb_started_at, game.climb_duration)
            new_alive = [u for u in game.alive if u != uid]
            new_bail = list(game.bail_log) + [
                {"player_id": uid, "bail_ts": now, "meter_pct": pct}
            ]
            await self._db_set_state(
                game_id, "ACTIVE",
                alive=json.dumps(new_alive),
                bail_log=json.dumps(new_bail),
                last_action_at=now,
            )
            game.alive = new_alive
            game.bail_log = new_bail

            if not new_alive:
                # everyone blinked → the last to bail (this presser) wins, no nick
                await self._resolve_cosmetic(game, winner_id=uid)
                resolved = True
            else:
                guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                dv = self.build_game_view(game_id)
                dv.render(self.render_game_state(game, guild))
                await interaction.edit_original_response(view=dv)
        if resolved:
            self._cancel_timers(game_id)
            self._game_locks.pop(game_id, None)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: ChickenGame) -> None:
        cfg = await chdb.get_config(self.db, game.guild_id)
        # The meter is drawn over max_climb; the crash lands somewhere in
        # [min_climb, max_climb] and is never shown, so the bar can blow at
        # 60% and nobody can count it out (duels-party-113). Seeded and logged
        # for the same reason as the tie-break — the seed alone, since INFO
        # reaches the dashboard's live log stream and an admin at the table
        # could read the crash point off it; the post-crash line says where.
        min_climb = float(cfg["min_climb"])
        max_climb = max(min_climb, float(cfg["max_climb"]))
        seed = random.getrandbits(32)
        crash_at = roll_crash_at(min_climb, max_climb, random.Random(seed))
        log.info("chicken game %s climb: max=%.1fs seed=%s", game.id, max_climb, seed)
        now = time.time()
        await self._db_set_state(
            game.id, "ACTIVE",
            phase="CLIMBING",
            climb_started_at=now,
            climb_duration=max_climb,
            crash_at=crash_at,
            bail_log="[]",
            last_action_at=now,
        )
        self._schedule(game.id, crash_at, max_climb)

    def _schedule(self, game_id: int, crash_in: float, total: float) -> None:
        crash = asyncio.create_task(self._run_crash_timer(game_id, crash_in))
        ticker = asyncio.create_task(self._run_ticker(game_id, _TICK_INTERVAL, total))
        self._add_timer(game_id, crash)
        self._add_timer(game_id, ticker)

    async def on_game_resume(self, game: ChickenGame) -> None:
        if game.climb_started_at is None or game.climb_duration is None:
            spawn(self._crash(game.id), label="chicken crash", logger=log)
            return
        now = time.time()
        # Rows from before migration 208 have no crash_at: they crashed at the
        # end of the drawn span, so keep that for them.
        crash_at = game.crash_at if game.crash_at is not None else game.climb_duration
        remaining = (game.climb_started_at + crash_at) - now
        meter_left = max(0.0, (game.climb_started_at + game.climb_duration) - now)

        channel = self.bot.get_channel(game.channel_id)
        if channel:
            mentions = " ".join(f"<@{u}>" for u in game.alive)
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"🔄 Bot restarted — Chicken resuming. {mentions}",
                    allowed_mentions=allow_pings(users=game.alive),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if remaining <= 0:
            spawn(self._crash(game.id), label="chicken crash", logger=log)
        else:
            self._schedule(game.id, remaining, meter_left)

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timers(game_id)

    def render_game_state(
        self, game: ChickenGame, guild: discord.Guild,
    ) -> list[discord.ui.Item[Any]]:
        pct = meter_pct(time.time(), game.climb_started_at, game.climb_duration)
        name_fn = GuildNames(guild=guild)
        return chicken_layouts.build_round_layout(game, pct=pct, name_fn=name_fn)

    def render_result_state(
        self,
        game: ChickenGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        original_name: str | None = None,
        self_apply_nick: str | None = None,
        sentence_hours: int | None = None,
        **_kwargs,
    ) -> list[discord.ui.Item[Any]]:
        span = self._hours_span(sentence_hours)
        if game.loser_id is not None:
            # crash with a nick loser — the base's result-card names line
            # already names both winner and loser, so nothing here does.
            best = bravest_bailer(game.bail_log)
            blew_at = crash_pct(game.crash_at, game.climb_duration)
            stakes = (
                discord.utils.escape_markdown(game.stakes_text)
                if game.stakes_text else self.nick_forfeit_copy(sentence_hours)
            )
            nick_section: str | None = None
            if self_apply_nick:
                nick_section = chicken_layouts.nick_self_apply_section(
                    self_apply_nick, span
                )
            elif imposed_nick:
                nick_section = chicken_layouts.nick_applied_section(imposed_nick, span)
            elif game_is_nick_stake(game):
                deadline = time.time() + duels_db.NAMING_WINDOW_SECONDS
                nick_section = chicken_layouts.awaiting_nick_section(deadline, span)
            return chicken_layouts.build_crash_result_layout(
                blew_at_pct=blew_at,
                n_alive=len(game.alive),
                bravest_pct=best["meter_pct"] if best else None,
                stakes=stakes,
                nick_section=nick_section,
            )

        # cosmetic: everyone bailed, or total wipeout
        if game.winner_id is not None:
            name_fn = GuildNames(guild=guild)
            # A name, not a mention: this card is an edit of the game's own
            # card (BaseGame._show_result's rule), so it notifies nobody.
            winner_ref = name_fn(game.winner_id)
            ranked = sorted(game.bail_log, key=lambda b: b["meter_pct"], reverse=True)
            ranking = [
                (name_fn(b["player_id"]), b["meter_pct"])
                for b in ranked if b["player_id"] != game.winner_id
            ]
            return chicken_layouts.build_everyone_blinked_layout(
                winner_ref=winner_ref, ranking=ranking,
            )

        blew_at = crash_pct(game.crash_at, game.climb_duration)
        return chicken_layouts.build_total_wipeout_layout(blew_at_pct=blew_at)

    def build_game_view(self, game_id: int) -> ChickenView:
        return self._frame(ChickenView(game_id, self._on_bail), game_id)

    # ── Slash commands ────────────────────────────────────────────────────────

    @chicken.command(name="start", description="Open a Chicken lobby")
    @app_commands.describe(
        stakes="Optional custom stakes text (max 200 chars)",
        wager="Optional coin wager — every player antes this; winner takes the pot",
        nickname="Also stake nicknames? The winner renames the loser (default: only when nothing else is staked)",
    )
    async def ch_start(
        self,
        interaction: discord.Interaction,
        stakes: str | None = None,
        wager: int | None = None,
        nickname: bool | None = None,
    ) -> None:
        await self._base_lobby(interaction, stakes, wager, nickname)

async def setup(bot: Bot) -> None:
    cog = ChickenCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("chicken")
    games.add_command(cog.chicken)
