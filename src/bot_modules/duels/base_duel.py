"""BaseDuel — fixed 2-player special case of BaseGame.

Adds the single-opponent challenge/accept/decline flow and the pairwise winner
resolution (winner = the player who isn't the loser). All shared machinery —
lifecycle, the expiry/auto-revert sweep, the nickname-stake flow, rate limiting, and
the abstract hooks — lives in `BaseGame`.

Two stake modes are supported on the duel path:
  * **Nickname mode** (no custom stakes, no wager): the winner renames the
    loser for the guild's ``sentence_hours``.
  * **Custom stakes** (free-text stakes given): the loser owes the agreed-upon
    stakes; the bot enforces nothing and never renames anyone. A coin wager
    with no stakes text lands here too — the pot *is* the stake (a wager label
    is recorded at creation via `resolve_stakes_text`), so no rename either.
"""
from __future__ import annotations

import json
import time
from typing import Any

import discord

from bot_modules.core.db_utils import sql_identifier
from bot_modules.core.utils import allow_pings
from bot_modules.games.utils.game_manager import sign_off_game_chore
from bot_modules.games.utils.timer import now_plus
from bot_modules.no_contact.logic import SURFACE_DUEL_CHALLENGE


from . import db as duels_db
from .db import CHALLENGE_RESPONSE_SECONDS
from .base_game import BaseGame, _fmt_coins
from .filters import (
    game_is_nick_stake,
    resolve_nick_stake,
    resolve_stakes_text,
    validate_stakes,
)
from .layouts import build_challenge_layout, build_duel_notice_layout
from .views import CHALLENGE_TIMED_OUT_TEXT, ChallengeView


class BaseDuel(BaseGame):
    """Abstract base for 2-player nickname-duel games.

    Subclasses must define GAME_KEY / GAME_DISPLAY_NAME and implement the abstract
    hooks declared on BaseGame.
    """

    # ── Restart recovery ──────────────────────────────────────────────────────

    async def _db_fetch_pending_games(self) -> list:
        """Challenges still inside their response window, for cog_load.

        Every duel keeps its rows in ``<GAME_KEY>_games``; ids are read here
        and rehydrated through the cog's own ``_db_get_game`` so the row
        shape stays the game's business.
        """
        table = sql_identifier(f"{self.GAME_KEY}_games")
        rows = await self.db.fetchall(
            f"SELECT id FROM {table} WHERE state = 'PENDING' AND created_at > ?",
            (time.time() - CHALLENGE_RESPONSE_SECONDS,),
        )
        games = []
        for row in rows:
            game = await self._db_get_game(int(row["id"]))
            if game is not None:
                games.append(game)
        return games

    def _build_challenge_view(
        self, game: Any, *, deadline: float
    ) -> discord.ui.View | discord.ui.LayoutView | None:
        return ChallengeView(
            game_id=game.id,
            target_id=game.target_id,
            on_accept=self._handle_accept,
            on_decline=self._handle_decline,
            deadline=deadline,
        )

    # ── Shared challenge entrypoint ───────────────────────────────────────────

    async def _base_challenge(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        stakes_text: str | None,
        wager: int | None = None,
        nickname: bool | None = None,
        *,
        stakes_prevalidated: bool = False,
    ) -> int | None:
        """Run all pre-game checks and create a challenge embed. Called by subclass command.

        Returns the new game's id, or None when the challenge was refused.

        ``wager`` makes it a coin duel: the amount is *declared* now but no
        money moves until the target accepts, so a decline or a timeout costs
        nothing. Both antes are taken at accept, and the winner takes the pot.
        ``stakes_prevalidated`` is the Run It Back path handing back text that
        already went through ``validate_stakes`` (and its markdown escape).
        """
        if not interaction.guild:
            await self._refuse(interaction, "This command only works in a server.")
            return None

        challenger = interaction.user  # type: ignore[assignment]
        guild: discord.Guild = interaction.guild

        if target.id == challenger.id:
            await self._refuse(interaction, "You can't challenge yourself.")
            return None
        if target.bot:
            await self._refuse(interaction, "You can't challenge a bot.")
            return None

        cfg = await duels_db.get_config(self.db, guild.id, self.GAME_KEY)
        refusal = await self._launch_refusal(guild.id, interaction.channel_id, cfg)
        if refusal:
            await self._refuse(interaction, refusal)
            return None

        # No-contact gate (see BaseGame._blocked_pair). After the guild-wide
        # checks so the refusal is believable — "game in progress" where the
        # game is switched off or not allowed here would read as a bug — and
        # before the rate limit, so an ordinary refusal costs no strike.
        # The copy is the existing pair-already-playing line: an outcome
        # the challenger has seen before and cannot pin on anyone.
        if await self._blocked_pair(
            guild.id, challenger.id, target.id, record_surface=SURFACE_DUEL_CHALLENGE
        ):
            await self._refuse(interaction, self._IN_PROGRESS_COPY)
            return None

        limit = self._challenge_limit(cfg)
        if self._check_rate_limit(challenger.id, limit):
            await self._refuse(
                interaction,
                f"You've issued too many challenges recently — the limit here is "
                f"{limit} an hour. Try again a little later.",
            )
            return None

        # Validate and normalise the stakes text *before* deciding whether this
        # is a nickname game. Whitespace-only stakes clean to None, and reading
        # the raw string would answer "something else is staked" for a game
        # that ends up staking nothing — skipping the preflights below and then
        # falling through to nickname mode at settlement anyway.
        if stakes_text and not stakes_prevalidated:
            stakes_result = validate_stakes(
                stakes_text,
                max_length=cfg["max_stakes_length"],
                denylist=json.loads(cfg.get("nick_denylist") or "[]"),
            )
            if not stakes_result.ok:
                await self._refuse(interaction, f"Stakes rejected: {stakes_result.reason}")
                return None
            stakes_text = stakes_result.value or None

        # Nickname-mode preflight only applies when the loser is actually going
        # to be renamed. Non-nickname games never rename anyone, so they don't
        # need the Manage Nicknames permission or a clear nickname slate.
        nick_stake = resolve_nick_stake(stakes_text, wager, nickname)
        if not nick_stake and stakes_text is None and wager is None:
            # nickname:False with nothing else on the table would be a duel
            # with no stake at all, which every downstream reader would then
            # have to guess about. Say so instead.
            await self._refuse(
                interaction,
                "Turning the nickname stake off means you need to stake "
                "something else — add `wager:` or `stakes:`.",
            )
            return None
        nick_notice: str | None = None
        if nick_stake:
            perm_error = await self._check_bot_can_nick(guild)
            if perm_error:
                await self._refuse(interaction, perm_error)
                return None

            nick_error = await self._check_no_active_nick(guild, [challenger, target])  # type: ignore[list-item]
            if nick_error:
                await self._refuse(interaction, nick_error)
                return None

            # The rematch cooldown (the panels' "Wait Before a Rematch"). It
            # was offered on all three duel panels and read by nothing until
            # 2026-09-04 (duels-party-116); like the group games' per-player
            # cooldown it only guards the nickname stake — a wagered or
            # custom-stakes rematch is always allowed.
            cd = await duels_db.check_cooldown(
                self.db, guild.id, self.GAME_KEY, challenger.id, target.id,
                int(cfg["cooldown_hours"]),
            )
            if cd is not None:
                await self._refuse(interaction, self._rematch_cooldown_copy(cd))
                return None

            # A player outranking the bot doesn't block the challenge — warn,
            # then continue; the rename is skipped if that player loses.
            nick_notice = self._rename_warning(
                guild, [challenger, target]  # type: ignore[list-item]
            )

        existing = await self._db_get_active_game_for_pair(guild.id, challenger.id, target.id)
        superseded = None
        if existing is not None and existing.state == "RESOLVED":
            # The last game's winner never named the loser. The *winner*
            # starting a new game between the pair ends that window rather
            # than blocking for the rest of it — the naming window is long
            # now (duels-party-118). The loser can't end it for them: a lost
            # nickname duel followed by a quick Run It Back would otherwise
            # wipe the rename the winner was about to apply.
            if challenger.id != existing.winner_id:
                await self._refuse(interaction, self._UNNAMED_YET_COPY)
                return None
            superseded, existing = existing, None
        if existing:
            await self._refuse(interaction, self._IN_PROGRESS_COPY)
            return None

        if wager is not None:
            err = await self._wager_precheck(guild.id, challenger.id, wager)
            if err:
                await self._refuse(interaction, err)
                return None

        if superseded is not None:
            # Past every refusal: the new game is going to be made, so the
            # old window closes now rather than on a challenge that failed.
            await self._conclude_unnamed(
                superseded, duels_db.NICK_REASON_SUPERSEDED,
                card=build_duel_notice_layout(
                    "🔁 Nickname Not Set",
                    "The winner started a new game against the loser before "
                    "naming them. No rename applied.",
                ),
            )

        # Every live stake goes into the persisted text, so the "📋 Stakes"
        # section on every downstream card lists all of them — the coins used
        # to be visible only on the challenge card and then again at
        # settlement ("Oh there were 2 stakes 👀").
        settings = await self._econ_settings(guild.id) if wager is not None else None
        wager_line = None
        if wager is not None:
            each = _fmt_coins(settings, wager) if settings else f"**{wager:,}**"
            takes = _fmt_coins(settings, wager * 2) if settings else f"**{wager * 2:,}**"
            wager_line = f"💰 {each} each — winner takes {takes}."
        sentence_hours = int(cfg["sentence_hours"])
        stakes_text = resolve_stakes_text(
            stakes_text, wager, nick_stake=nick_stake, wager_line=wager_line,
            nick_line=self._nick_stakes_line(sentence_hours),
        )

        game_id = await self._db_create_game(
            guild_id=guild.id,
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            challenger_id=challenger.id,
            target_id=target.id,
            stakes_text=stakes_text,
            nick_stake=nick_stake,
        )
        self._record_challenge(challenger.id)

        if wager is not None:
            await self._declare_wager(guild.id, game_id, challenger.id, wager)

        # The game's card starts here, framed as it stays through the result.
        art = await self._load_card_art(game_id, interaction.channel_id)
        items = build_challenge_layout(
            self.GAME_DISPLAY_NAME, challenger.id, target.id,
            stakes_text or self.nick_forfeit_copy(sentence_hours),
            wager=wager, expires_at=now_plus(CHALLENGE_RESPONSE_SECONDS),
            name_fn=self._name_fn(guild),
            titled=bool(art.header and art.title_in_header),
        )
        view = self._frame(
            ChallengeView(
                game_id=game_id,
                target_id=target.id,
                on_accept=self._handle_accept,
                on_decline=self._handle_decline,
            ),
            game_id,
        )
        view.render(items)
        await interaction.response.send_message(
            view=view, allowed_mentions=allow_pings(users=[target]),
        )
        msg = await interaction.original_response()
        view.message = msg
        await self._db_set_state(game_id, "PENDING", message_id=msg.id)
        if nick_notice:
            await interaction.followup.send(nick_notice, ephemeral=True)
        return game_id

    #: The pair-already-playing refusal. Also what the no-contact gate says,
    #: so the two can never drift apart (docs/specs/no_contact_spec.md).
    _IN_PROGRESS_COPY = "You two already have a game in progress."
    #: The loser of an unnamed nickname game trying to start the next one.
    _UNNAMED_YET_COPY = (
        "The winner of your last game hasn't named you yet — a new game between "
        "you two opens up once they have, or once their naming window closes."
    )

    def _rematch_cooldown_copy(self, remaining: float) -> str:
        return (
            f"You two played for your nicknames recently — the rematch cooldown "
            f"here means you can stake them against each other again in "
            f"**{self._remaining(remaining)}**. A wager or custom stakes "
            f"(`nickname: False`) can run right now."
        )

    async def _record_rematch_cooldown(self, game: Any) -> None:
        """A settled duel starts the pair's rematch clock (every stake mode:
        the dial only *blocks* nickname games, but the clock runs from
        whatever the pair last played)."""
        await duels_db.set_cooldown(
            self.db, game.guild_id, self.GAME_KEY,
            int(game.challenger_id), int(game.target_id),
        )

    # ── View callbacks ────────────────────────────────────────────────────────

    #: Why a challenge is no longer acceptable, in the words of what actually
    #: happened. "This challenge is no longer active" left a player who clicked
    #: a second too late with no idea whether they'd been beaten to it, blocked,
    #: or hit a bug ("LMAO that did not let me accept" — game night 2026-08-21).
    _STALE_CHALLENGE_REASONS = {
        "EXPIRED_PENDING": CHALLENGE_TIMED_OUT_TEXT,
        "DECLINED": "❌ That challenge was already declined.",
        "ACTIVE": "▶️ That challenge has already been accepted — the game is running.",
    }
    _STALE_CHALLENGE_FALLBACK = "This challenge has already finished."

    def _stale_challenge_message(self, game: Any) -> str:
        if game is None:
            return "That challenge is gone — it may have been cleaned up."
        return self._STALE_CHALLENGE_REASONS.get(
            game.state, self._STALE_CHALLENGE_FALLBACK
        )

    async def _handle_accept(self, interaction: discord.Interaction, game_id: int) -> None:
        #: Set once the duel is really ACTIVE, so the chore is signed off
        #: even if a guard below returns out before the view is built.
        started: tuple[int, int] | None = None
        try:
            game = await self._db_get_game(game_id)
            if not game or game.state != "PENDING":
                await interaction.response.send_message(
                    self._stale_challenge_message(game), ephemeral=True
                )
                return

            ante = await self._game_ante(game_id)
            if ante > 0:
                settings = await self._econ_settings(game.guild_id)
                ante_text = _fmt_coins(settings, ante) if settings else f"**{ante:,}**"
                # Both antes land at accept — no money moves while a challenge is
                # merely pending, so a decline or a timeout needs no refund. If
                # either side can't cover it now, the challenge is called off
                # rather than started half-funded.
                for uid, who in (
                    (game.target_id, "you"),
                    (game.challenger_id, "the challenger"),
                ):
                    err = await self._take_stake(game.guild_id, game_id, uid, ante)
                    if err is None:
                        continue
                    await self._db_set_state(game_id, "DECLINED")  # refunds + drops
                    note = err if who == "you" else (
                        f"The challenger can no longer cover the {ante_text} wager — "
                        "challenge called off."
                    )
                    # Terminal (DECLINED): this challenge message is never
                    # edited again — the live challenge card (itself a
                    # layout now) is replaced by the called-off notice, a
                    # whole next LayoutView rather than a view=None strip
                    # (guide § Retiring a card).
                    await interaction.response.edit_message(
                        content=None,
                        embed=None,
                        attachments=[],
                        view=self._framed_card(
                            game_id,
                            build_duel_notice_layout("❌ Challenge Called Off", note),
                        ),
                    )
                    return

            await self._db_set_state(game_id, "ACTIVE")
            await self.on_game_start(game)
            # Assigned only once on_game_start has returned: the row is
            # ACTIVE either way, but a start that raised is not a game
            # anyone ran, and crediting it would also put a REST board
            # repaint in front of the error the player is waiting for.
            started = (game.guild_id, game.challenger_id)

            # Re-fetch after on_game_start (subclass may have set additional fields)
            game = await self._db_get_game(game_id)
            if not game:
                return

            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            await self._load_card_art(game.id, game.channel_id)
            view = self.build_game_view(game.id)
            rendered = self.render_game_state(game, guild)
            self.bot.add_view(view, message_id=game.message_id)
            await interaction.response.edit_message(
                **self._active_game_edit_kwargs(view, rendered)
            )

        finally:
            # AFTER the interaction is answered, never before, and in a
            # finally so a vanished row can't skip a chore the game has
            # already earned. An accepted challenge is two humans playing,
            # which is the clearest "a multiplayer game ran" the bot ever
            # sees — but signing off can repaint the todo board, and a
            # repaint is a REST edit discord.py sleeps through under
            # per-channel rate limiting, long enough to burn the
            # three-second window and fail an accept whose game is already
            # ACTIVE (the hazard todo_cog.add_todo documents).
            #
            # Credited to the challenger: they ran a game, where the
            # acceptor answered an invitation rather than issuing one.
            if started is not None:
                await sign_off_game_chore(self.bot, *started)

    async def _handle_decline(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await self._db_get_game(game_id)
        if not game or game.state != "PENDING":
            await interaction.response.send_message(
                self._stale_challenge_message(game), ephemeral=True
            )
            return
        await self._db_set_state(game_id, "DECLINED")
        # Terminal, same reasoning as the ante-failure branch above.
        await interaction.response.edit_message(
            content=None,
            embed=None,
            attachments=[],
            view=self._framed_card(
                game_id,
                build_duel_notice_layout(
                    "❌ Challenge Declined",
                    f"{discord.utils.escape_markdown(interaction.user.display_name)} "
                    "declined the challenge.",
                ),
            ),
        )

    async def _handle_game_button(self, interaction: discord.Interaction, game_id: int) -> None:
        """Entry point for all in-game button presses. Subclass build_game_view passes this."""
        await interaction.response.defer()
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game:
                await interaction.followup.send("Game not found.", ephemeral=True)
                return
            if game.state != "ACTIVE":
                await interaction.followup.send("This game is no longer active.", ephemeral=True)
                return

            status, loser_id = await self.handle_interaction(interaction, game)

            if status == "rejected":
                return

            if status == "done":
                assert loser_id is not None
                winner_id = (
                    game.challenger_id if loser_id != game.challenger_id else game.target_id
                )
                await self._post_result(interaction, game, winner_id, loser_id)
            else:  # "continue"
                guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                await self._edit_continue_state(interaction, game, guild)

    async def _post_result(
        self,
        interaction: discord.Interaction,
        game: Any,
        winner_id: int,
        loser_id: int,
    ) -> None:
        """Resolve from a button interaction."""
        await self._finalize_result(game, winner_id, loser_id)
        await self.on_game_resolved(game.id)
        self._game_locks.pop(game.id, None)

    async def _finalize_result(
        self,
        game: Any,
        winner_id: int,
        loser_id: int,
    ) -> None:
        """Render the result into the game's card and set the terminal state.

        The same edit whether a player's click or a timeout resolved the
        game (:meth:`BaseGame._show_result`). Caller handles timer
        cancellation / lock cleanup (it differs between the interaction and
        timer paths).
        """
        guild = self.bot.get_guild(game.guild_id)

        # Two modes: nickname (no custom stakes → winner renames the loser) and
        # custom stakes (loser owes the agreed-upon stakes, no bot enforcement).
        nick_mode = game_is_nick_stake(game)
        sentence_hours = await self._sentence_hours(game.guild_id) if nick_mode else None

        result_state = self.render_result_state(
            game, guild, sentence_hours=sentence_hours,  # type: ignore[arg-type]
        )

        # Both modes carry a view now: Name the Loser only in nickname mode,
        # Run It Back on every result. winner/loser ride along with the
        # terminal write: the economy hook re-reads the row, and not every
        # cog persists them before this point.
        if getattr(game, "resolved_at", None) is None:
            game.resolved_at = time.time()
        result_message_id = await self._show_result(
            game, winner_id, loser_id, result_state
        )
        await self._db_set_state(
            game.id, "RESOLVED" if nick_mode else "RESOLVED_NO_NICK",
            result_message_id=result_message_id,
            winner_id=winner_id,
            loser_id=loser_id,
        )
