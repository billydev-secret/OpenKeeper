"""Hot Potato cog — pass-the-bomb nickname duel."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bot_modules.core.background import spawn

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import asyncio
import json
import logging
import random
import time

import discord
from discord import app_commands

from bot_modules.core.utils import allow_pings
from bot_modules.duels.base_duel import BaseDuel
from bot_modules.duels.filters import game_is_nick_stake
from bot_modules.games.command_groups import games
from bot_modules.shared.name_resolver import NameFn, build_name_fn, mention

from . import db as hpdb
from .game import HotPotatoGame, compute_style_points, hold_remaining
from .layouts import build_hot_potato_card, build_hot_potato_result
from .views import PassLayoutView

log = logging.getLogger("dungeonkeeper.hot_potato")


class HotPotatoDuel(BaseDuel, name="HotPotatoCog"):

    GAME_KEY = "hot_potato"
    GAME_DISPLAY_NAME = "Hot Potato"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self._timers: dict[int, asyncio.Task] = {}
        # A name_fn resolved once per game and reused across every render;
        # a game id missing from here falls back to the plain `mention`
        # default (render_game_state never raises over a lookup miss).
        self._name_fns: dict[int, NameFn] = {}

    hot_potato = app_commands.Group(
        name="hotpotato",
        description="Hot Potato — pass the bomb before it blows!",
    )

    # ── DB hooks ──────────────────────────────────────────────────────────────

    async def _db_create_game(
        self,
        guild_id: int,
        channel_id: int,
        challenger_id: int,
        target_id: int,
        stakes_text: str | None,
        nick_stake: bool = False,
    ) -> int:
        return await hpdb.create_game(
            self.db, guild_id, channel_id, challenger_id, target_id, stakes_text,
            nick_stake,
        )

    async def _db_get_game(self, game_id: int) -> HotPotatoGame | None:
        return await hpdb.get_game(self.db, game_id)

    async def _db_get_active_game_for_pair(
        self, guild_id: int, user_a: int, user_b: int
    ) -> HotPotatoGame | None:
        return await hpdb.get_active_game_for_pair(self.db, guild_id, user_a, user_b)

    async def _db_get_pending_for_challenger(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> HotPotatoGame | None:
        return await hpdb.get_pending_game_for_challenger(self.db, guild_id, channel_id, user_id)

    async def _db_write_state(self, game_id: int, state: str, **kw) -> None:
        await hpdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_fetch_active_games(self) -> list[HotPotatoGame]:
        return await hpdb.fetch_active_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[HotPotatoGame]:
        return await hpdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[HotPotatoGame]:
        return await hpdb.fetch_sweepable_games(self.db, now)

    # ── Timer helpers ─────────────────────────────────────────────────────────

    def _cancel_timer(self, game_id: int) -> None:
        task = self._timers.pop(game_id, None)
        # _explode reaches here from inside the explode task; cancelling the
        # running task would land on its next await and drop the rest.
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _run_explode_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._explode(game_id)
        except asyncio.CancelledError:
            pass

    async def _explode(self, game_id: int) -> None:
        """Timer callback: determine loser, award style points, post result. All inside lock."""
        async with self._get_lock(game_id):
            game = await hpdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.holder_id is None:
                return

            loser_id = game.holder_id
            winner_id = (
                game.challenger_id if loser_id == game.target_id else game.target_id
            )
            now = time.time()

            new_log = list(game.pass_log)
            if new_log and new_log[-1]["passed_at"] is None:
                new_log[-1] = {**new_log[-1], "passed_at": now}

            style_totals: dict[int, int] = {}
            if game.started_at and game.timer_seconds:
                style_pts = compute_style_points(
                    new_log, game.started_at, game.timer_seconds, loser_id, winner_id
                )
                for uid, pts in style_pts.items():
                    if pts > 0:
                        await hpdb.add_style_points(self.db, game.guild_id, uid, pts)
                        style_totals[uid] = await hpdb.get_style_total(
                            self.db, game.guild_id, uid
                        )

            game.winner_id = winner_id
            game.loser_id = loser_id
            game.pass_log = new_log
            game.resolved_at = now

            guild = self.bot.get_guild(game.guild_id)

            # Same two stake modes as _finalize_result: nickname (no custom
            # stakes → rename button) vs announce-only. The timer path used to
            # skip this gate and always post the rename view.
            nick_mode = game_is_nick_stake(game)
            result_message_id = None
            if guild:
                sentence_hours = (
                    await self._sentence_hours(game.guild_id) if nick_mode else None
                )
                result_state = self.render_result_state(
                    game, guild, sentence_hours=sentence_hours,
                    style_totals=style_totals,
                )
                # Name the Loser in nickname mode, Run It Back in every mode —
                # the same card _finalize_result shows, edited over the BOOM
                # board rather than posted under it.
                result_message_id = await self._show_result(
                    game, winner_id, loser_id, result_state,
                )

            await self._db_set_state(
                game_id, "RESOLVED" if nick_mode else "RESOLVED_NO_NICK",
                winner_id=winner_id,
                loser_id=loser_id,
                pass_log=json.dumps(new_log),
                result_message_id=result_message_id,
                resolved_at=now,
                last_action_at=now,
            )

        self._cancel_timer(game_id)
        self._game_locks.pop(game_id, None)
        await self.on_game_resolved(game_id)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def _prime_names(self, game: HotPotatoGame) -> None:
        """Resolve & cache this game's two-player name_fn once; reused
        across every render for the game's whole active lifetime
        (challenger_id/target_id never change). Never raises into a game
        path — a failed resolve leaves the entry unset, and
        render_game_state falls back to the plain ``mention`` default
        rather than losing the game over a lookup."""
        if game.id in self._name_fns:
            return
        try:
            self._name_fns[game.id] = await build_name_fn(
                guild=self.bot.get_guild(game.guild_id),
                db_path=self.bot.ctx.db_path,
                guild_id=game.guild_id,
                user_ids=[game.challenger_id, game.target_id],
            )
        except Exception:
            log.exception("Failed to prime hot potato names (game %d)", game.id)

    async def on_game_start(self, game: HotPotatoGame) -> None:
        await self._prime_names(game)
        cfg = await hpdb.get_config(self.db, game.guild_id)
        timer = random.uniform(cfg["min_timer"], cfg["max_timer"])
        now = time.time()
        initial_log = json.dumps(
            [{"holder_id": game.challenger_id, "received_at": now, "passed_at": None}]
        )
        await self._db_set_state(
            game.id, "ACTIVE",
            holder_id=game.challenger_id,
            timer_seconds=timer,
            started_at=now,
            pass_log=initial_log,
            last_action_at=now,
        )
        task = asyncio.create_task(self._run_explode_timer(game.id, timer))
        self._timers[game.id] = task

    async def on_game_resume(self, game: HotPotatoGame) -> None:
        await self._prime_names(game)
        if not game.started_at or not game.timer_seconds:
            spawn(self._explode(game.id), label="hot potato explode", logger=log)
            return

        now = time.time()
        remaining = (game.started_at + game.timer_seconds) - now

        channel = self.bot.get_channel(game.channel_id)
        if channel:
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"🔄 Bot restarted — Hot Potato game resuming. "
                    f"<@{game.challenger_id}> <@{game.target_id}>",
                    allowed_mentions=allow_pings(
                        users=[game.challenger_id, game.target_id],
                    ),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if remaining <= 0:
            spawn(self._explode(game.id), label="hot potato explode", logger=log)
        else:
            task = asyncio.create_task(self._run_explode_timer(game.id, remaining))
            self._timers[game.id] = task

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timer(game_id)
        self._name_fns.pop(game_id, None)

    def render_game_state(
        self, game: HotPotatoGame, guild: discord.Guild
    ) -> list[discord.ui.Item[Any]]:
        name_fn = self._name_fns.get(game.id, mention)
        return build_hot_potato_card(game, name_fn)

    def render_result_state(
        self,
        game: HotPotatoGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        # Accepted for signature compatibility with BaseGame's callers
        # (base_game.py passes the loser's pre-rename display name here) —
        # unused: the result card names nobody, "The Loser"/"The Winner"
        # role words stand in for both players since the names line above
        # already names them (guide § Mentions).
        original_name: str | None = None,
        self_apply_nick: str | None = None,
        sentence_hours: int | None = None,
        style_totals: dict[int, int] | None = None,
        **_kwargs,
    ) -> list[discord.ui.Item[Any]]:
        pass_count = max(0, len(game.pass_log) - 1)
        stakes_line = game.stakes_text or self.nick_forfeit_copy(sentence_hours)

        style_lines: list[str] = []
        if (
            game.started_at
            and game.timer_seconds
            and game.winner_id is not None
            and game.loser_id is not None
        ):
            # Per-game points are the tie flavour; the running total is the
            # only place the cumulative table is ever read (duels-party-119).
            style_pts = compute_style_points(
                game.pass_log,
                game.started_at,
                game.timer_seconds,
                game.loser_id,
                game.winner_id,
            )
            for uid, pts in style_pts.items():
                if pts <= 0:
                    continue
                role = "🏆 Winner" if uid == game.winner_id else "💀 Loser"
                total = (style_totals or {}).get(uid)
                line = f"**{role}**: +{pts} pts"
                if total is not None:
                    line += f" — now has {total:,} style points"
                style_lines.append(line)

        nick_heading: str | None = None
        nick_body: str | None = None
        if self_apply_nick:
            nick_heading = "🏷️ Nickname — Over To You"
            nick_body = self.nick_self_apply_copy(
                "The Loser", self_apply_nick, sentence_hours
            )
        elif imposed_nick:
            nick_heading = "🏷️ Nickname Applied"
            nick_body = self.nick_applied_copy(
                "The Loser", imposed_nick, sentence_hours
            )
        elif game_is_nick_stake(game):
            nick_heading = "⏳ Awaiting Nickname"
            nick_body = self.awaiting_nick_copy("The Winner", sentence_hours)

        return build_hot_potato_result(
            pass_count, stakes_line,
            style_lines=style_lines or None,
            nick_heading=nick_heading, nick_body=nick_body,
        )

    def build_game_view(self, game_id: int) -> PassLayoutView:
        return self._frame(PassLayoutView(game_id, self._handle_game_button), game_id)

    async def handle_interaction(
        self, interaction: discord.Interaction, game: HotPotatoGame
    ) -> tuple[str, int | None]:
        player_id = interaction.user.id

        if player_id not in (game.challenger_id, game.target_id):
            await interaction.followup.send("You're not in this game.", ephemeral=True)
            return ("rejected", None)

        if player_id != game.holder_id:
            await interaction.followup.send(
                "You're not holding the potato!", ephemeral=True
            )
            return ("rejected", None)

        now = time.time()
        cfg = await hpdb.get_config(self.db, game.guild_id)
        wait = hold_remaining(game.pass_log, player_id, now, float(cfg["min_hold"]))
        if wait > 0:
            await interaction.followup.send(
                f"Hold it a moment — you can pass after {float(cfg['min_hold']):.0f}s.",
                ephemeral=True,
            )
            return ("rejected", None)
        new_holder = (
            game.challenger_id if player_id == game.target_id else game.target_id
        )
        new_log = list(game.pass_log)
        if new_log and new_log[-1]["passed_at"] is None:
            new_log[-1] = {**new_log[-1], "passed_at": now}
        new_log.append({"holder_id": new_holder, "received_at": now, "passed_at": None})

        await self._db_set_state(
            game.id, "ACTIVE",
            holder_id=new_holder,
            pass_log=json.dumps(new_log),
            last_action_at=now,
        )
        game.holder_id = new_holder
        game.pass_log = new_log
        return ("continue", None)

    # ── Slash commands ────────────────────────────────────────────────────────

    @hot_potato.command(name="challenge", description="Challenge someone to Hot Potato")
    @app_commands.describe(
        user="The player you're challenging",
        stakes="Optional custom stakes text (max 200 chars)",
        wager="Optional coin wager — you both ante this; winner takes the pot",
        nickname="Also stake nicknames? The winner renames the loser (default: only when nothing else is staked)",
    )
    async def hp_challenge(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stakes: str | None = None,
        wager: int | None = None,
        nickname: bool | None = None,
    ) -> None:
        await self._base_challenge(interaction, user, stakes, wager, nickname)

async def setup(bot: Bot) -> None:
    cog = HotPotatoDuel(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("hotpotato")
    games.add_command(cog.hot_potato)
