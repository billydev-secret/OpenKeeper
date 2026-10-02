"""Quickdraw cog — slash commands and BaseDuel implementation.

Components V2 (wave 3b): every card is a bare ``LayoutView`` built through
``layouts.py`` — no accent, no ``discord.Embed`` anywhere in this file.
``docs/specs/dk_pvp_games_suite_spec.md`` §9.2 names the paradigms.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from bot_modules.core.background import spawn

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.utils import escape_markdown

from bot_modules.core.utils import allow_pings
from bot_modules.duels.base_duel import BaseDuel
from bot_modules.duels.filters import game_is_nick_stake
from bot_modules.games.command_groups import games

from . import db as qdb
from .game import QuickdrawGame
from .layouts import build_result_items_layout, build_turn_card_layout, build_void_layout
from .views import QuickdrawLayoutView

log = logging.getLogger("dungeonkeeper.quickdraw")


class QuickdrawDuel(BaseDuel, name="QuickdrawCog"):

    GAME_KEY = "quickdraw"
    GAME_DISPLAY_NAME = "Quickdraw"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self._timers: dict[int, asyncio.Task] = {}

    quickdraw = app_commands.Group(
        name="quickdraw",
        description="Quickdraw — first to react wins, false-starters lose",
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
        return await qdb.create_game(
            self.db, guild_id, channel_id, challenger_id, target_id, stakes_text,
            nick_stake,
        )

    async def _db_get_game(self, game_id: int) -> QuickdrawGame | None:
        return await qdb.get_game(self.db, game_id)

    async def _db_get_active_game_for_pair(
        self, guild_id: int, user_a: int, user_b: int
    ) -> QuickdrawGame | None:
        return await qdb.get_active_game_for_pair(self.db, guild_id, user_a, user_b)

    async def _db_get_pending_for_challenger(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> QuickdrawGame | None:
        return await qdb.get_pending_game_for_challenger(self.db, guild_id, channel_id, user_id)

    async def _db_write_state(self, game_id: int, state: str, **kw) -> None:
        await qdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_fetch_active_games(self) -> list[QuickdrawGame]:
        return await qdb.fetch_active_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[QuickdrawGame]:
        return await qdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[QuickdrawGame]:
        return await qdb.fetch_sweepable_games(self.db, now)

    # ── Timer helpers ─────────────────────────────────────────────────────────

    def _cancel_timer(self, game_id: int) -> None:
        task = self._timers.pop(game_id, None)
        # _fire_void reaches here from inside the draw-window task; cancelling
        # the running task would land on its next await and drop the rest.
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _run_game_timer(
        self, game_id: int, delay: float, draw_window: float
    ) -> None:
        """Sleep `delay` seconds then fire the draw signal."""
        try:
            await asyncio.sleep(delay)
            await self._fire_draw(game_id, draw_window)
        except asyncio.CancelledError:
            pass

    async def _run_drawwindow_timer(self, game_id: int, draw_window: float) -> None:
        """Sleep `draw_window` seconds then void if nobody fired."""
        try:
            await asyncio.sleep(draw_window)
            await self._fire_void(game_id)
        except asyncio.CancelledError:
            pass

    async def _fire_draw(self, game_id: int, draw_window: float) -> None:
        """Transition WAITING → DRAW; edit message to show draw signal."""
        fired_at = time.time()
        async with self._get_lock(game_id):
            game = await qdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.qd_state != "WAITING":
                return

            await self._db_set_state(
                game_id, "ACTIVE",
                qd_state="DRAW",
                fired_at=fired_at,
                last_action_at=fired_at,
            )
            game = await qdb.get_game(self.db, game_id)
            if not game:
                return

            guild = self.bot.get_guild(game.guild_id)
            if guild:
                view = self.build_game_view(game_id)
                view.render(self.render_game_state(game, guild))
                await self._edit_message_silent(
                    game.channel_id, game.message_id, layout=view
                )

        # Start the draw window timer outside the lock (no await between lock
        # release and task creation, so no race with concurrent button presses)
        task = asyncio.create_task(self._run_drawwindow_timer(game_id, draw_window))
        self._timers[game_id] = task

    async def _fire_void(self, game_id: int) -> None:
        """Draw window expired. Void if nobody fired; if the winner fired but the
        opponent never did, resolve winner-only (no loser reaction time)."""
        async with self._get_lock(game_id):
            game = await qdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE":
                return

            guild = self.bot.get_guild(game.guild_id)

            if game.qd_state == "DRAW":
                # Nobody fired in time — void, no consequence.
                await self._db_set_state(game_id, "VOID")
                p1_name = self._display_name(guild, game.challenger_id)
                p2_name = self._display_name(guild, game.target_id)
                notice = build_void_layout(p1_name, p2_name)
                await self._edit_message_silent(
                    game.channel_id, game.message_id,
                    layout=self._framed_card(game.id, notice),
                )

            elif game.qd_state == "WINNER_FIRED":
                # Winner drew; the opponent never fired. Resolve with no loser
                # time (loser_fired_at stays NULL → result shows "didn't draw").
                await self._db_set_state(
                    game_id, "ACTIVE",
                    qd_state="COMPLETE",
                    last_action_at=time.time(),
                )
                game.qd_state = "COMPLETE"
                if guild is not None:
                    # The result replaces the board on the game's own card.
                    await self._finalize_result(
                        game, game.winner_id, game.loser_id,  # type: ignore[arg-type]
                    )
                else:
                    # Guild unresolvable — terminalize anyway so the economy
                    # seam still sees the round end (this path used to pay but
                    # leave the row ACTIVE for the sweep to abandon).
                    await self._db_set_state(
                        game.id,
                        "RESOLVED" if game_is_nick_stake(game) else "RESOLVED_NO_NICK",
                        winner_id=game.winner_id,
                        loser_id=game.loser_id,
                    )
            else:
                return

        self._cancel_timer(game_id)
        self._game_locks.pop(game_id, None)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: QuickdrawGame) -> None:
        cfg = await qdb.get_config(self.db, game.guild_id)
        delay = random.uniform(cfg["min_delay"], cfg["max_delay"])
        draw_window = cfg["draw_window"]
        now = time.time()
        await self._db_set_state(
            game.id, "ACTIVE",
            qd_state="WAITING",
            draw_delay=delay,
            last_action_at=now,
        )
        task = asyncio.create_task(self._run_game_timer(game.id, delay, draw_window))
        self._timers[game.id] = task

    async def on_game_resume(self, game: QuickdrawGame) -> None:
        """Called on cog_load for each ACTIVE game. Resume timers."""
        cfg = await qdb.get_config(self.db, game.guild_id)
        draw_window = cfg["draw_window"]

        if game.qd_state == "WAITING":
            # Re-roll delay and restart from scratch; let players know
            delay = random.uniform(cfg["min_delay"], cfg["max_delay"])
            now = time.time()
            await self._db_set_state(
                game.id, "ACTIVE",
                draw_delay=delay,
                last_action_at=now,
            )
            task = asyncio.create_task(self._run_game_timer(game.id, delay, draw_window))
            self._timers[game.id] = task
            await self._send_restart_notice(game)

        elif game.qd_state == "DRAW":
            now = time.time()
            fired_at = game.fired_at or now
            remaining = fired_at + draw_window - now
            if remaining <= 0:
                # Window has already passed — void immediately
                spawn(self._fire_void(game.id), label="quickdraw void", logger=log)
            else:
                task = asyncio.create_task(self._run_drawwindow_timer(game.id, remaining))
                self._timers[game.id] = task
                await self._send_restart_notice(game)

        elif game.qd_state == "WINNER_FIRED":
            # Restarted after the winner fired but before the opponent did. The
            # blind-reaction window can't be resumed fairly — resolve winner-only.
            spawn(self._fire_void(game.id), label="quickdraw void", logger=log)

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timer(game_id)

    async def _send_restart_notice(self, game: QuickdrawGame) -> None:
        channel = self.bot.get_channel(game.channel_id)
        if not channel:
            return
        try:
            await channel.send(  # type: ignore[union-attr]
                f"🔄 Bot restarted — Quickdraw round resuming. "
                f"<@{game.challenger_id}> <@{game.target_id}>",
                allowed_mentions=allow_pings(
                    users=[game.challenger_id, game.target_id],
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @staticmethod
    def _display_name(guild: discord.Guild | None, user_id: int) -> str:
        """A member's display name, escaped for a ``TextDisplay`` — a
        ``<@id>`` fallback would be a raw mention this card never pings, so
        an unresolved member falls back to their bare id instead (matching
        the pre-V2 embed's behavior)."""
        member = guild.get_member(user_id) if guild else None
        raw = member.display_name if member else str(user_id)
        return escape_markdown(raw)

    def render_game_state(
        self, game: QuickdrawGame, guild: discord.Guild
    ) -> list[discord.ui.Item]:
        p1_name = self._display_name(guild, game.challenger_id)
        p2_name = self._display_name(guild, game.target_id)
        stakes = game.stakes_text or "Loser surrenders their nickname."
        return build_turn_card_layout(game.qd_state, p1_name, p2_name, stakes)

    def render_result_state(
        self,
        game: QuickdrawGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        original_name: str | None = None,
        self_apply_nick: str | None = None,
        sentence_hours: int | None = None,
        **_kwargs,
    ) -> list[discord.ui.Item]:
        loser_name = self._display_name(guild, game.loser_id)  # type: ignore[arg-type]
        winner_name = self._display_name(guild, game.winner_id)  # type: ignore[arg-type]
        fired_at = game.fired_at
        clean_draw = fired_at is not None

        winner_reaction: float | None = None
        loser_reaction: float | None = None
        if fired_at is not None:
            if game.resolved_at is not None:
                winner_reaction = game.resolved_at - fired_at
            if game.loser_fired_at is not None:
                loser_reaction = game.loser_fired_at - fired_at

        stakes_text = game.stakes_text or self.nick_forfeit_copy(sentence_hours)

        nick_heading: str | None = None
        nick_body: str | None = None
        if self_apply_nick:
            nick_heading = "🏷️ Nickname — Over To You"
            # Neither is escaped here: BaseGame escapes original_name where it
            # captures it, and the helper escapes the nick at render.
            nick_body = self.nick_self_apply_copy(
                original_name or loser_name, self_apply_nick, sentence_hours,
            )
        elif imposed_nick:
            nick_heading = "🏷️ Nickname Applied"
            nick_body = self.nick_applied_copy(
                original_name or loser_name, imposed_nick, sentence_hours,
            )
        elif game_is_nick_stake(game):
            nick_heading = "⏳ Awaiting Nickname"
            nick_body = self.awaiting_nick_copy(winner_name, sentence_hours)

        return build_result_items_layout(
            clean_draw=clean_draw,
            stakes_text=stakes_text,
            winner_reaction=winner_reaction,
            loser_reaction=loser_reaction,
            nick_heading=nick_heading,
            nick_body=nick_body,
        )

    def build_game_view(self, game_id: int) -> QuickdrawLayoutView:
        return self._frame(QuickdrawLayoutView(game_id, self._handle_game_button), game_id)

    async def handle_interaction(
        self, interaction: discord.Interaction, game: QuickdrawGame
    ) -> tuple[str, int | None]:
        player_id = interaction.user.id

        if player_id not in (game.challenger_id, game.target_id):
            await interaction.followup.send("❌ You're not in this game.", ephemeral=True)
            return ("rejected", None)

        if game.qd_state == "WAITING":
            # False start — the presser loses
            loser_id = player_id
            winner_id = (
                game.challenger_id if player_id == game.target_id else game.target_id
            )
            now = time.time()
            await self._db_set_state(
                game.id, "ACTIVE",
                qd_state="COMPLETE",
                winner_id=winner_id,
                loser_id=loser_id,
                resolved_at=now,
                last_action_at=now,
            )
            game.winner_id = winner_id
            game.loser_id = loser_id
            game.qd_state = "COMPLETE"
            # game.fired_at remains None → signals false start to render.
            # The result replaces the board on this same card
            # (BaseGame._show_result), so there's no greyed board first.
            return ("done", loser_id)

        if game.qd_state == "DRAW":
            # First to click wins — but the round isn't over. Record the winner
            # and move to WINNER_FIRED so the opponent can still fire and log
            # their own reaction time for the result delta.
            winner_id = player_id
            loser_id = (
                game.challenger_id if player_id == game.target_id else game.target_id
            )
            now = time.time()
            await self._db_set_state(
                game.id, "ACTIVE",
                qd_state="WINNER_FIRED",
                winner_id=winner_id,
                loser_id=loser_id,
                resolved_at=now,
                last_action_at=now,
            )
            game.winner_id = winner_id
            game.loser_id = loser_id
            game.qd_state = "WINNER_FIRED"
            game.resolved_at = now
            # game.fired_at is already set (by _fire_draw) → signals clean draw

            reaction = now - game.fired_at if game.fired_at is not None else 0.0
            await interaction.followup.send(
                f"🔫 You drew in **{reaction:.3f}s**! "
                "Hang on — seeing if they can beat it…",
                ephemeral=True,
            )
            # "continue" keeps the FIRE button live (blind) for the opponent.
            return ("continue", None)

        if game.qd_state == "WINNER_FIRED":
            if player_id == game.winner_id:
                await interaction.followup.send(
                    "❌ You already drew — waiting on your opponent.", ephemeral=True
                )
                return ("rejected", None)

            # Opponent fired second: record their reaction time and resolve.
            now = time.time()
            await self._db_set_state(
                game.id, "ACTIVE",
                qd_state="COMPLETE",
                loser_fired_at=now,
                last_action_at=now,
            )
            game.qd_state = "COMPLETE"
            game.loser_fired_at = now
            return ("done", game.loser_id)

        # qd_state == "COMPLETE" — round already fully resolved
        await interaction.followup.send("❌ Too slow — this round is already over.", ephemeral=True)
        return ("rejected", None)

    # ── Slash commands ────────────────────────────────────────────────────────

    @quickdraw.command(name="challenge", description="Challenge someone to a Quickdraw")
    @app_commands.describe(
        user="The player you're challenging",
        stakes="Optional custom stakes text (max 200 chars)",
        wager="Optional coin wager — you both ante this; winner takes the pot",
        nickname="Also stake nicknames? The winner renames the loser (default: only when nothing else is staked)",
    )
    async def quickdraw_challenge(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stakes: str | None = None,
        wager: int | None = None,
        nickname: bool | None = None,
    ) -> None:
        await self._base_challenge(interaction, user, stakes, wager, nickname)


async def setup(bot: Bot) -> None:
    cog = QuickdrawDuel(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("quickdraw")
    games.add_command(cog.quickdraw)
