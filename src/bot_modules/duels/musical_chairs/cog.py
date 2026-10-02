"""Musical Chairs cog — reflex elimination for 3..N players."""
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
from bot_modules.duels.base_game import BaseGame
from bot_modules.duels.db import NAMING_WINDOW_SECONDS
from bot_modules.duels.filters import game_is_nick_stake
from bot_modules.duels.layouts import build_duel_notice_layout
from bot_modules.games.command_groups import games
from bot_modules.shared.name_resolver import NameFn, resolve_name_from

from . import db as mcdb
from .game import MusicalChairsGame, chairs_for, no_sitter_verdict, resolve_round
from .layouts import build_result_layout, build_round_layout
from .views import SitView

log = logging.getLogger("dungeonkeeper.musical_chairs")


class MusicalChairsCog(BaseGame, name="MusicalChairsCog"):

    GAME_KEY = "musical_chairs"
    GAME_DISPLAY_NAME = "Musical Chairs"

    HOW_TO_PLAY = (
        "🎵 While the music plays, **do nothing** — pressing 🪑 Sit early puts "
        "you out.\n"
        "🪑 The moment the music stops the panel flips to **Sit!** — press it "
        "as fast as you can.\n"
        "❌ There's always one chair fewer than there are players, so whoever "
        "misses out is eliminated each round.\n"
        "🏆 Last player seated wins."
    )

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self._timers: dict[int, asyncio.Task] = {}

    musicalchairs = app_commands.Group(
        name="musicalchairs",
        description="Musical Chairs — grab a seat when the music stops!",
    )

    # ── DB hooks ──────────────────────────────────────────────────────────────

    async def _db_get_game(self, game_id: int) -> MusicalChairsGame | None:
        return await mcdb.get_game(self.db, game_id)

    async def _db_write_state(self, game_id: int, state: str, **kw) -> None:
        await mcdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_create_lobby(
        self, guild_id: int, channel_id: int, host_id: int, stakes_text: str | None,
        nick_stake: bool = False,
    ) -> int:
        return await mcdb.create_lobby(
            self.db, guild_id, channel_id, host_id, stakes_text, nick_stake
        )

    async def _db_fetch_active_games(self) -> list[MusicalChairsGame]:
        return await mcdb.fetch_active_games(self.db)

    async def _db_fetch_lobby_games(self) -> list[MusicalChairsGame]:
        return await mcdb.fetch_lobby_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[MusicalChairsGame]:
        return await mcdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[MusicalChairsGame]:
        return await mcdb.fetch_sweepable_games(self.db, now)

    async def get_lobby_params(self, guild_id: int) -> tuple[int, int]:
        cfg = await mcdb.get_config(self.db, guild_id)
        return int(cfg["min_players"]), int(cfg["max_players"])

    # ── Timer helpers ─────────────────────────────────────────────────────────

    def _cancel_timer(self, game_id: int) -> None:
        task = self._timers.pop(game_id, None)
        # A round closed by the scramble timer reaches here from inside that
        # task; cancelling it would land on its next await and drop the rest.
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _run_music_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._start_scramble(game_id)

    async def _run_scramble_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        resolved = False
        async with self._get_lock(game_id):
            game = await mcdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "SCRAMBLE":
                return
            resolved = await self._close_round_locked(game)
        if resolved:
            self._game_locks.pop(game_id, None)

    async def _start_scramble(self, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await mcdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "MUSIC":
                return
            now = time.time()
            cfg = await mcdb.get_config(self.db, game.guild_id)
            await self._db_set_state(
                game_id, "ACTIVE",
                phase="SCRAMBLE",
                seated="[]",
                chairs=chairs_for(len(game.alive)),
                phase_started_at=now,
                phase_duration=float(cfg["scramble_window"]),
                last_action_at=now,
            )
            guild = self.bot.get_guild(game.guild_id)
            if guild and not await self._repost_panel(game_id, guild):
                game2 = await mcdb.get_game(self.db, game_id)
                if game2 and game2.message_id:
                    view = self.build_game_view(game_id)
                    view.render(self.render_game_state(game2, guild))
                    await self._edit_message_silent(
                        game2.channel_id, game2.message_id, layout=view,
                    )
            task = asyncio.create_task(
                self._run_scramble_timer(game_id, float(cfg["scramble_window"]))
            )
            self._timers[game_id] = task

    async def _repost_panel(self, game_id: int, guild: discord.Guild) -> bool:
        """Move the game panel back to the bottom of the channel.

        The panel is edited in place for the whole game, so in a busy channel
        it scrolls away and players cannot find the button at the one moment it
        matters — three people spent the 2026-08-21 game night asking "where do
        I click???". Discord has no reorder API, so the panel moves the only way
        a panel can: post the new one, record its id, then delete the old.

        Post-before-delete, and the delete is best-effort: if the bot has lost
        Send Messages the old panel keeps working instead of being destroyed,
        and a failed delete costs a duplicate rather than a dead game. Returns
        False when the panel could not be moved (caller falls back to editing).
        """
        game = await mcdb.get_game(self.db, game_id)
        if not game:
            return False
        channel = self.bot.get_channel(game.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return False
        view = self.build_game_view(game_id)
        view.render(self.render_game_state(game, guild))
        try:
            new_msg = await channel.send(view=view)
        except (discord.Forbidden, discord.HTTPException):
            return False
        old_message_id = game.message_id
        await self._db_set_state(game_id, "ACTIVE", message_id=new_msg.id)
        if old_message_id and old_message_id != new_msg.id:
            try:
                await channel.get_partial_message(old_message_id).delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        return True

    async def _close_round_locked(self, game: MusicalChairsGame) -> bool:
        """Resolve a SCRAMBLE: seat the fastest, eliminate the rest. Returns True if the
        game ended. Caller holds the lock; any pending scramble timer is cancelled/done."""
        now = time.time()
        chairs = chairs_for(len(game.alive))
        survivors, eliminated = resolve_round(game.alive, game.seated, chairs)
        new_elim = list(game.elimination_order) + eliminated

        guild = self.bot.get_guild(game.guild_id)
        channel = self.bot.get_channel(game.channel_id)

        verdict = no_sitter_verdict(game.alive, survivors, game.reruns)
        if verdict == "void":
            await self._void_no_sitter(game)
            return True
        if verdict == "rerun":
            await self._rerun_round(game, now)
            return False

        if len(survivors) <= 1:
            winner = survivors[0] if survivors else (eliminated[-1] if eliminated else None)
            loser = new_elim[-1] if new_elim else None
            game.alive = survivors
            game.elimination_order = new_elim
            await self._db_set_state(
                game.id, "ACTIVE",
                alive=json.dumps(survivors),
                elimination_order=json.dumps(new_elim),
                seated="[]",
                last_action_at=now,
            )
            if guild and channel and eliminated:
                out = ", ".join(self._name(guild, u) for u in eliminated)
                await self._announce_to_channel(
                    game.id, f"❌ **{out}** didn't find a chair."
                )
            if winner is not None and loser is not None:
                await self._post_group_result(game, winner, loser)
            return True

        cfg = await mcdb.get_config(self.db, game.guild_id)
        music = random.uniform(cfg["min_music"], cfg["max_music"])
        new_chairs = chairs_for(len(survivors))
        await self._db_set_state(
            game.id, "ACTIVE",
            phase="MUSIC",
            round=game.round + 1,
            chairs=new_chairs,
            alive=json.dumps(survivors),
            elimination_order=json.dumps(new_elim),
            seated="[]",
            reruns=0,
            phase_started_at=now,
            phase_duration=music,
            last_action_at=now,
        )
        game.alive = survivors
        game.elimination_order = new_elim
        game.phase = "MUSIC"
        game.chairs = new_chairs

        if guild and channel and eliminated:
            out = ", ".join(self._name(guild, u) for u in eliminated)
            await self._announce_to_channel(
                game.id,
                f"❌ **{out}** didn't find a chair. {len(survivors)} left. "
                f"🎵 the music starts again…",
            )
        game2 = await mcdb.get_game(self.db, game.id)
        if guild and game2 and game2.message_id:
            view = self.build_game_view(game.id)
            view.render(self.render_game_state(game2, guild))
            await self._edit_message_silent(
                game2.channel_id, game2.message_id, layout=view,
            )
        task = asyncio.create_task(self._run_music_timer(game.id, music))
        self._timers[game.id] = task
        return False

    async def _rerun_round(self, game: MusicalChairsGame, now: float) -> None:
        """Nobody sat: play the same round again with fresh music.

        Same players, same chairs, no elimination; the panel is edited back
        to its MUSIC face and the music timer re-armed. Caller holds the lock.
        """
        cfg = await mcdb.get_config(self.db, game.guild_id)
        music = random.uniform(cfg["min_music"], cfg["max_music"])
        await self._db_set_state(
            game.id, "ACTIVE",
            phase="MUSIC",
            seated="[]",
            reruns=game.reruns + 1,
            phase_started_at=now,
            phase_duration=music,
            last_action_at=now,
        )
        game.phase = "MUSIC"
        game.seated = []
        game.reruns += 1
        await self._announce_to_channel(
            game.id,
            "🪑 Nobody sat! Same players, same chairs — 🎵 the music starts again…",
        )
        guild = self.bot.get_guild(game.guild_id)
        game2 = await mcdb.get_game(self.db, game.id)
        if guild and game2 and game2.message_id:
            view = self.build_game_view(game.id)
            view.render(self.render_game_state(game2, guild))
            await self._edit_message_silent(
                game2.channel_id, game2.message_id, layout=view,
            )
        task = asyncio.create_task(self._run_music_timer(game.id, music))
        self._timers[game.id] = task

    async def _void_no_sitter(self, game: MusicalChairsGame) -> None:
        """Nobody sat twice in a row: the game is called off, every stake
        refunded by the terminal seam, and nobody is renamed. Caller holds
        the lock."""
        await self._db_set_state(game.id, "VOID", seated="[]", last_action_at=time.time())
        await self._announce_to_channel(
            game.id,
            "🏳️ Nobody sat two rounds running — the game is called off. "
            "No result, no nickname; any stakes are refunded.",
        )
        if game.message_id:
            notice = build_duel_notice_layout(
                "🏳️ Game Called Off",
                "Nobody sat two rounds running. No result, no nickname — "
                "any stakes are refunded.",
            )
            await self._edit_message_silent(
                game.channel_id, game.message_id, layout=self._framed_card(game.id, notice),
            )
        await self.on_game_resolved(game.id)

    # ── Button handler ────────────────────────────────────────────────────────

    async def _on_sit(self, interaction: discord.Interaction, game_id: int) -> None:
        await interaction.response.defer()
        resolved = False
        async with self._get_lock(game_id):
            game = await mcdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE":
                await interaction.followup.send(
                    "❌ This game is no longer active.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid not in game.alive:
                await interaction.followup.send(
                    "❌ You're not in this game (or you're already out).", ephemeral=True
                )
                return

            if game.phase == "MUSIC":
                cfg = await mcdb.get_config(self.db, game.guild_id)
                if cfg["false_start_elim"]:
                    await interaction.followup.send(
                        "❌ You sat too early — the music was still playing, so "
                        "you're out. Wait for **Sit!** next game.",
                        ephemeral=True,
                    )
                    await self._group_eliminate(
                        game, uid, interaction=interaction,
                        reason="sat before the music stopped",
                    )
                else:
                    await interaction.followup.send(
                        "❌ Not yet — wait for **Sit!**", ephemeral=True
                    )
                return

            if game.phase == "SCRAMBLE":
                if uid in game.seated:
                    await interaction.followup.send(
                        "❌ You already grabbed a chair!", ephemeral=True
                    )
                    return
                new_seated = list(game.seated) + [uid]
                await self._db_set_state(
                    game_id, "ACTIVE",
                    seated=json.dumps(new_seated), last_action_at=time.time(),
                )
                game.seated = new_seated
                chairs = chairs_for(len(game.alive))
                if len(new_seated) >= chairs:
                    self._cancel_timer(game_id)
                    resolved = await self._close_round_locked(game)
                else:
                    guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                    # A press already in flight when the round flipped is
                    # answered against the *old* panel, which _repost_panel has
                    # since deleted. The seat is already recorded above, so a
                    # dead edit costs nothing but must not escape the callback.
                    try:
                        view = self.build_game_view(game_id)
                        view.render(self.render_game_state(game, guild))
                        await interaction.edit_original_response(view=view)
                    except (discord.NotFound, discord.HTTPException):
                        pass
                return
        if resolved:
            self._game_locks.pop(game_id, None)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: MusicalChairsGame) -> None:
        cfg = await mcdb.get_config(self.db, game.guild_id)
        music = random.uniform(cfg["min_music"], cfg["max_music"])
        now = time.time()
        await self._db_set_state(
            game.id, "ACTIVE",
            phase="MUSIC",
            round=1,
            chairs=chairs_for(len(game.alive)),
            seated="[]",
            phase_started_at=now,
            phase_duration=music,
            last_action_at=now,
        )
        task = asyncio.create_task(self._run_music_timer(game.id, music))
        self._timers[game.id] = task

    async def on_game_resume(self, game: MusicalChairsGame) -> None:
        if not game.phase or game.phase_started_at is None or game.phase_duration is None:
            spawn(self._start_scramble(game.id), label="musical chairs scramble", logger=log)
            return
        now = time.time()
        remaining = (game.phase_started_at + game.phase_duration) - now

        channel = self.bot.get_channel(game.channel_id)
        if channel:
            mentions = " ".join(f"<@{u}>" for u in game.alive)
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"🔄 Bot restarted — Musical Chairs resuming. {mentions}",
                    allowed_mentions=allow_pings(users=game.alive),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if game.phase == "MUSIC":
            delay = max(0.0, remaining)
            task = asyncio.create_task(self._run_music_timer(game.id, delay))
            self._timers[game.id] = task
        else:  # SCRAMBLE
            delay = max(0.0, remaining)
            task = asyncio.create_task(self._run_scramble_timer(game.id, delay))
            self._timers[game.id] = task

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timer(game_id)

    def _name(self, guild: discord.Guild, uid: int) -> str:
        m = guild.get_member(uid)
        return discord.utils.escape_markdown(m.display_name) if m else str(uid)

    def _name_fn(self, guild: discord.Guild | None) -> NameFn:
        def fn(uid: int) -> str:
            return resolve_name_from(uid, guild=guild)
        return fn

    def render_game_state(
        self, game: MusicalChairsGame, guild: discord.Guild
    ) -> list[discord.ui.Item[Any]]:
        chairs = game.chairs if game.chairs is not None else chairs_for(len(game.alive))
        stakes = game.stakes_text or "Loser surrenders their nickname."
        return build_round_layout(
            phase=game.phase or "MUSIC",
            round_no=game.round,
            chairs=chairs,
            alive=game.alive,
            stakes=stakes,
            name_fn=self._name_fn(guild),
        )

    def render_result_state(
        self,
        game: MusicalChairsGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        original_name: str | None = None,
        self_apply_nick: str | None = None,
        sentence_hours: int | None = None,
        **_kwargs,
    ) -> list[discord.ui.Item[Any]]:
        # `original_name` keeps its meaning for the base's own embed-arm copy
        # helpers, but the layout arm never re-names the winner or loser
        # after BaseGame._result_layout's own names line — so it's unused here.
        del original_name
        stakes = game.stakes_text or self.nick_forfeit_copy(sentence_hours)
        deadline = int((game.resolved_at or time.time()) + NAMING_WINDOW_SECONDS)
        return build_result_layout(
            players=len(game.roster),
            stakes=stakes,
            nick_stake=game_is_nick_stake(game),
            imposed_nick=imposed_nick,
            self_apply_nick=self_apply_nick,
            nick_span=self._hours_span(sentence_hours),
            naming_deadline=deadline,
        )

    def build_game_view(self, game_id: int) -> SitView:
        # SIT routes through _on_sit (not _handle_group_button) because Musical Chairs'
        # button has phase-dependent behavior (false-start vs seat-claim) and can
        # eliminate multiple players at once on round close.
        return self._frame(SitView(game_id, self._on_sit), game_id)

    # ── Slash commands ────────────────────────────────────────────────────────

    @musicalchairs.command(name="start", description="Open a Musical Chairs lobby")
    @app_commands.describe(
        stakes="Optional custom stakes text (max 200 chars)",
        wager="Optional coin wager — every player antes this; winner takes the pot",
        nickname="Also stake nicknames? The winner renames the loser (default: only when nothing else is staked)",
    )
    async def mc_start(
        self,
        interaction: discord.Interaction,
        stakes: str | None = None,
        wager: int | None = None,
        nickname: bool | None = None,
    ) -> None:
        await self._base_lobby(interaction, stakes, wager, nickname)

async def setup(bot: Bot) -> None:
    cog = MusicalChairsCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("musicalchairs")
    games.add_command(cog.musicalchairs)
