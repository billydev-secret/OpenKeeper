"""Pressure Cooker cog — slash commands and BaseDuel implementation."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import logging
import random

import discord
from discord import app_commands

from bot_modules.duels.base_duel import BaseDuel
from bot_modules.duels.db import NAMING_WINDOW_SECONDS
from bot_modules.duels.filters import game_is_nick_stake
from bot_modules.games.command_groups import games
from bot_modules.shared.name_resolver import resolve_name_from

from . import db as pdb
from .game import PressureGame, apply_pump
from .layouts import build_boom_layout, build_pump_layout
from .views import GameView

log = logging.getLogger("dungeonkeeper.pressure")


class PressureCookerDuel(BaseDuel, name="PressureCookerCog"):

    GAME_KEY = "pressure"
    GAME_DISPLAY_NAME = "Pressure Cooker"

    pressure = app_commands.Group(
        name="pressure",
        description="Pressure Cooker — a high-stakes nickname duel",
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
        return await pdb.create_game(
            self.db, guild_id, channel_id, challenger_id, target_id, stakes_text,
            nick_stake,
        )

    async def _db_get_game(self, game_id: int) -> PressureGame | None:
        return await pdb.get_game(self.db, game_id)

    async def _db_get_active_game_for_pair(
        self, guild_id: int, user_a: int, user_b: int
    ) -> PressureGame | None:
        return await pdb.get_active_game_for_pair(self.db, guild_id, user_a, user_b)

    async def _db_get_pending_for_challenger(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> PressureGame | None:
        return await pdb.get_pending_game_for_challenger(self.db, guild_id, channel_id, user_id)

    async def _db_write_state(self, game_id: int, state: str, **kw) -> None:
        await pdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_fetch_active_games(self) -> list[PressureGame]:
        return await pdb.fetch_active_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[PressureGame]:
        return await pdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[PressureGame]:
        return await pdb.fetch_sweepable_games(self.db, now)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: PressureGame) -> None:
        first_player = random.choice([game.challenger_id, game.target_id])
        await self._db_set_state(game.id, "ACTIVE", active_player=first_player)

    def render_game_state(
        self, game: PressureGame, guild: discord.Guild
    ) -> list[discord.ui.Item[Any]]:
        def name_fn(user_id: int) -> str:
            return resolve_name_from(user_id, guild=guild)

        return build_pump_layout(
            game.challenger_id,
            game.target_id,
            game.gauge,
            game.pumps,
            game.active_player,
            game.state,
            name_fn=name_fn,
        )

    def render_result_state(
        self,
        game: PressureGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        original_name: str | None = None,
        self_apply_nick: str | None = None,
        sentence_hours: int | None = None,
        **_kwargs,
    ) -> list[discord.ui.Item[Any]]:
        # original_name/winner/loser identities aren't threaded into any of
        # these copy strings — the names line BaseGame._result_layout puts
        # ahead of this card's items already names both players (style
        # guide § Mentions: "name a person once ... never both").
        del original_name
        esc = discord.utils.escape_markdown
        forfeit_copy = self.nick_forfeit_copy(sentence_hours)

        self_apply_copy = imposed_copy = awaiting_copy = None
        if self_apply_nick:
            self_apply_copy = (
                "Discord won't let me rename the server owner, so the "
                f"nickname has to be set by hand: **{esc(self_apply_nick)}**, "
                f"for {self._hours_span(sentence_hours)}."
            )
        elif imposed_nick:
            imposed_copy = (
                f"Now going by **{esc(imposed_nick)}**, for "
                f"{self._hours_span(sentence_hours)}."
            )
        elif game_is_nick_stake(game):
            awaiting_copy = (
                f"Press **Name the Loser** within {self._span(NAMING_WINDOW_SECONDS)}. "
                f"The nickname lasts {self._hours_span(sentence_hours)}."
            )

        return build_boom_layout(
            game.gauge,
            len(game.pumps),
            game.stakes_text,
            forfeit_copy,
            imposed_copy=imposed_copy,
            self_apply_copy=self_apply_copy,
            awaiting_copy=awaiting_copy,
        )

    def build_game_view(self, game_id: int) -> GameView:
        return self._frame(GameView(game_id, self._handle_game_button), game_id)

    async def handle_interaction(
        self, interaction: discord.Interaction, game: PressureGame
    ) -> tuple[str, int | None]:
        if interaction.user.id != game.active_player:
            await interaction.followup.send("It's not your turn.", ephemeral=True)
            return ("rejected", None)

        result = apply_pump(game, interaction.user.id)
        await pdb.save_pump(self.db, game)

        if result.busted:
            # No greyed bust board first: the result replaces the board on
            # this same card (BaseGame._show_result).
            return ("done", game.loser_id)

        return ("continue", None)

    # ── Slash commands ────────────────────────────────────────────────────────

    @pressure.command(name="challenge", description="Challenge someone to Pressure Cooker")
    @app_commands.describe(
        user="The player you're challenging",
        stakes="Optional custom stakes text (max 200 chars)",
        wager="Optional coin wager — you both ante this; winner takes the pot",
        nickname="Also stake nicknames? The winner renames the loser (default: only when nothing else is staked)",
    )
    async def pressure_challenge(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stakes: str | None = None,
        wager: int | None = None,
        nickname: bool | None = None,
    ) -> None:
        await self._base_challenge(interaction, user, stakes, wager, nickname)

async def setup(bot: Bot) -> None:
    cog = PressureCookerDuel(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("pressure")
    games.add_command(cog.pressure)
