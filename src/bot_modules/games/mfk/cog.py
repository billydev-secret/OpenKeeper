"""Marry/Fornicate/Kiss cog.

Components V2 since 2026-09-21: every card this feature sends is a
``LayoutView`` built by ``bot_modules/games/mfk/layouts.py``, not an embed
(``docs/plans/components_v2_migration.md`` wave 3b).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.layout import disable_all
from bot_modules.core.utils import allow_pings, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.start_ping_service import (
    extract_start_epoch,
    resolve_start_epoch,
)
from bot_modules.shared.name_resolver import build_name_fn
from bot_modules.no_contact.service import no_contact_pairs_among
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    create_game,
    update_game_message,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_names,
    channel_name,
)
from bot_modules.games.utils.send_retry import retry_transient
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.lobby_art import NO_ART, read_lobby_art
from bot_modules.core.branding_service import PanelArt
from bot_modules.games.mfk.layouts import (
    CLOSE_ASSIGN_CUSTOM_ID,
    HOW_TO_PLAY_CUSTOM_ID,
    JOIN_CUSTOM_ID,
    LOBBY_TITLE,
    build_assignments_layout,
    build_lobby_action_row,
    build_lobby_layout,
)
from bot_modules.games.mfk.logic import (
    DEFAULT_LABELS,
    MAX_PARTICIPANTS,
    assign_targets,
    parse_labels,
    serialize_assignments,
    toggle_participant,
)

log = logging.getLogger(__name__)


def _bind(
    view: discord.ui.LayoutView,
    custom_id: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
) -> None:
    """Attach ``callback`` to the persistent item carrying ``custom_id``.

    Layout builders return components and no behaviour (guide § Code
    conventions), so the view wires itself up after adding them. Persistent
    items are found by ``custom_id`` rather than ``find_item`` — that one
    takes the numeric ``id``, which is a different field (matches
    ``dm_perms_cog._bind``).
    """
    for item in view.walk_children():
        if getattr(item, "custom_id", None) == custom_id:
            item.callback = callback  # type: ignore[method-assign]
            return
    raise KeyError(f"no component with custom_id {custom_id!r} in {view!r}")


def _card(items: Sequence[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one send.

    The assignments reveal carries no buttons, so nothing dispatches to it
    and nothing has to expire.
    """
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


class MFKView(discord.ui.LayoutView):
    """The lobby card + its three buttons — persistent across restarts.

    Rebuilt **in place** (``_redraw`` clears and re-adds items on this same
    instance) on every Join toggle rather than replaced, since the
    persistent registration (``bot.active_views[game_id]``,
    ``bot.add_view(view, message_id=...)`` on restart) depends on the same
    Python object staying valid for the game's lifetime.

    ``card=None`` (the ``recover_game`` path) registers only the button row
    — mirrors ``dm_perms``' ``AskConsentView``: the recovered instance's job
    is only to make the buttons on the already-sent message work again, not
    to resend the card, so it carries no re-derived participant list.
    """

    def __init__(
        self,
        game_id: str,
        host_id: int,
        db,
        bot,
        labels: list[str] | None = None,
        *,
        card: Sequence[discord.ui.Item[Any]] | None = None,
        art: PanelArt = NO_ART,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.art = art
        self.labels = labels or DEFAULT_LABELS
        for item in card if card is not None else [build_lobby_action_row()]:
            self.add_item(item)
        self._bind_buttons()

    def _bind_buttons(self) -> None:
        _bind(self, JOIN_CUSTOM_ID, self.join_pool)
        _bind(self, CLOSE_ASSIGN_CUSTOM_ID, self.close_assign)
        _bind(self, HOW_TO_PLAY_CUSTOM_ID, self.how_to_play)

    def _redraw(
        self, host_name: str, participants: list[str], start_at: int | None
    ) -> None:
        """Rebuild the lobby card in place with the current pool."""
        self.clear_items()
        for item in build_lobby_layout(
            host_name, participants, labels=self.labels, start_at=start_at,
            header_url=self.art.header, footer_url=self.art.footer,
            title_in_header=self.art.title_in_header,
        ):
            self.add_item(item)
        self._bind_buttons()

    async def join_pool(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Join' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        user_id = interaction.user.id
        action_holder: dict[str, str] = {}

        def _toggle(payload):
            action_holder["action"] = toggle_participant(payload, user_id)

        payload = await modify_payload(self.db, self.game_id, _toggle)
        action = action_holder["action"]
        log.info("%s %s game %s in #%s", interaction.user.display_name, action, self.game_id, channel_name(interaction.channel))

        if action == "full":
            await interaction.response.send_message(
                f"❌ This pool is full — Marry/Fornicate/Kiss supports up to "
                f"{MAX_PARTICIPANTS} players.",
                ephemeral=True,
            )
            return

        host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
        names = resolve_names(interaction.guild, payload.get("participants", []))
        self._redraw(
            host_member.display_name if host_member else "Host",
            names,
            # Re-read from the payload, not the view: the countdown must
            # survive a restart that rebuilt this view from the DB.
            extract_start_epoch(payload),
        )
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"You've {action} the pool.", ephemeral=True
        )

    async def close_assign(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Close & Assign' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can assign roles.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        participants = payload.get("participants", [])
        guild = interaction.guild

        # Each player gets 3 random names from the pool (not themselves, and
        # not anyone the no-contact list keeps them apart from — a blocked
        # pair is dropped from each other's sample in both directions). A
        # pool that can't give everyone three names after that draws {} and
        # gets the same refusal a three-player pool gets, so the protected
        # member can't tell which one fired.
        assignments: dict[int, list[int]] = {}
        if len(participants) >= 4:
            forbidden: set[tuple[int, int]] = set()
            if guild is not None:
                forbidden = await asyncio.to_thread(
                    no_contact_pairs_among, self.bot.ctx.db_path, guild.id, participants
                )
            assignments = assign_targets(participants, forbidden_pairs=forbidden)
        if not assignments:
            await interaction.response.send_message(
                "Need at least 4 players in the pool!", ephemeral=True
            )
            return

        await interaction.response.defer()

        # The card names each player's targets through the resolver — a
        # TextDisplay mention renders from the *reading* client's cache, so
        # <@id> there is a bare number to anyone who hasn't seen that
        # member. A V2 send has no content= left to carry the reveal ping,
        # so build_assignments_layout puts it in the card body itself, one
        # raw mention per player's own line.
        name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=guild.id if guild is not None else 0,
            user_ids=list(participants),
        )
        payout_line = None
        if guild:
            from bot_modules.economy.game_rewards import payout_footer_line
            payout_line = await payout_footer_line(self.bot, guild.id, "mfk")
        card = _card(
            build_assignments_layout(
                assignments, labels=self.labels, name_fn=name_fn, payout_line=payout_line
            )
        )

        self.stop()
        disable_all(self)

        await interaction.edit_original_response(view=self)

        # Everybody who drew targets, each pinged once in their own line of
        # the card (a Components V2 message has no content= to carry the
        # ping instead). The send allow-lists exactly the players the card
        # names — the bot pings nobody by default.
        pinged = list(dict.fromkeys(assignments))
        try:
            await interaction.followup.send(
                view=card,
                allowed_mentions=allow_pings(users=pinged),
            )
        except Exception:
            # The assignments are already computed and the lobby is closed,
            # so a send failure (e.g. a 400 from an oversized card) must
            # NOT skip end_game — otherwise the row stays "joining" and
            # recover_game re-registers the dead lobby every restart. Log,
            # try to tell the host, then fall through to end the game.
            log.exception("mfk: failed to post assignments for game %s", self.game_id)
            try:
                await interaction.followup.send(
                    "❌ The game finished but I couldn't post the results "
                    "(the pool may be too large). The game has been ended.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(
            self.db,
            self.game_id,
            player_count=len(participants),
            payload={"assignments": serialize_assignments(assignments)},
            bot=self.bot, player_ids=list(participants),
        )
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]

    async def how_to_play(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["mfk"], ephemeral=True)


class MFKCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="mfk", description=play_description("mfk"))
    @app_commands.describe(
        options='Custom categories (comma-separated, exactly 3). e.g. "Cruise, Wedding, Vacation"',
        start_in="Countdown before the start, in minutes",
    )
    async def mfk(
        self,
        interaction: discord.Interaction,
        options: str | None = None,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info("%s used /games play mfk in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "mfk")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        # Parse custom labels
        labels, label_error = parse_labels(options)
        if label_error is not None:
            await interaction.response.send_message(label_error, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"options": options, "start_in": start_in},
        )
        await finish_launch_response(interaction, game_id)

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        labels, _ = parse_labels(options.get("options"))
        start_epoch = resolve_start_epoch(options)

        payload: dict = {"labels": labels or DEFAULT_LABELS}
        if start_epoch:
            payload["start_epoch"] = start_epoch
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "mfk",
            state="joining",
            payload=payload,
        )

        log.info("Game %s (mfk) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        view = MFKView(
            game_id, host_id, self.db, self.bot, labels=labels,
            art=await read_lobby_art(self.bot, channel, LOBBY_TITLE),
        )
        view._redraw(host_name, [], start_epoch)
        self.bot.active_views[game_id] = view

        try:
            msg = await retry_transient(lambda: channel.send(view=view), label="mfk launch")
        except (discord.Forbidden, discord.DiscordServerError):
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("mfk launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the MFK view after a restart so its buttons work again."""
        game_id = row["game_id"]
        view = MFKView(
            game_id, int(row["host_id"]), self.db, self.bot, labels=payload.get("labels"),
            art=await read_lobby_art(self.bot, channel, LOBBY_TITLE),
        )
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered mfk game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = MFKCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("mfk")
    play.add_command(cog.mfk, override=True)
    bot.game_launchers["mfk"] = cog.launch
    bot.game_recoverers["mfk"] = cog.recover_game
