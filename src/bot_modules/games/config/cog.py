import inspect
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.config.layouts import (
    build_force_end_layout,
    build_game_status_layout,
)

from bot_modules.core.utils import (
    allow_pings,
    has_mod_or_admin_permissions,
    is_mod_or_admin,
)
from bot_modules.shared.name_resolver import build_name_fn
from bot_modules.games.command_groups import games
from bot_modules.event_echo.service import retract_party_game_echo
from bot_modules.games.utils.host_panel import open_host_panels
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    force_end_active_game,
    get_active_game,
    channel_name,
)

log = logging.getLogger(__name__)



# Games whose cog ends itself with a recap (``end_with_recap``): ``/games end``
# hands them the close so the room gets the same game-over card and payout the
# host's own 🏁 End Game posts, instead of the red Force-Closed card
# (vote-games-52 / discovery-3; AMA since social-prompt-33). Looked up by cog
# name, the way AMA's cleanup hook is, so a cog mid-reload degrades to the
# force-close path.
RECAP_ENDING_COGS = {
    "wyr": "WYRCog", "nhie": "NHIECog", "mlt": "MLTCog", "ama": "AMACog",
    # A Compliment round in its wrap-up window finishes on the spot; a lobby
    # answers False and takes the force-close (social-prompt-39).
    "compliment": "ComplimentCog",
}

# Games whose cog owns something outside the game row that ``force_end_active_game``
# cannot reach: a second registered view, or a message of its own still on screen.
# Their ``cleanup_ended_game(channel_id, game_id, channel=)`` runs *before* the
# force-close, because it reads entries out of ``active_views`` that the force-close
# pops. Looked up by cog name like the table above, so a cog mid-reload degrades to
# the force-close rather than raising.
#
# AMA was the only entry until 2026-09-20 and sat here as a bare ``if``; Truth or
# Dare's opt-in bar made it two, and two is a table. Without this, ``/games end``
# and the 24-hour sweep left a bar of live buttons under a room that was over.
PRE_TEARDOWN_COGS = {"ama": "AMACog", "traditional": "TraditionalCog"}

NO_ACTIVE_GAME_REPLY = "There's no active game in this channel."
NO_HOST_PANEL_REPLY = "This game's controls are on its own card — there's no host panel to open."


def _card(items: Sequence[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one send."""
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


class GamesConfigCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def _end_with_recap(self, row, channel) -> bool:
        """Let a game that owns a recap ending close itself; False to fall
        back to the force-close teardown (unknown type, cog not loaded, or a
        lobby with nothing to recap)."""
        cog_name = RECAP_ENDING_COGS.get(row["game_type"])
        if cog_name is None:
            return False
        cog = self.bot.get_cog(cog_name)
        finisher = getattr(cog, "end_with_recap", None)
        if finisher is None or not inspect.iscoroutinefunction(finisher):
            return False
        try:
            return bool(await finisher(channel, row["game_id"]))
        except Exception:
            log.exception("/games end: recap ending failed for %s", row["game_id"])
            return False

    async def _teardown_active_game(self, row, channel) -> None:
        """Force-close *row*'s game and tidy its message. Shared by /games end
        and /games config game-end so both stop loop games cleanly."""
        game_id = row["game_id"]
        # These cogs read entries back out of active_views during cleanup, so
        # let them run BEFORE force_end_active_game pops those entries. No
        # cleanup hook archives, so the force_end call below still records the
        # game to history.
        cog_name = PRE_TEARDOWN_COGS.get(row["game_type"])
        cog = self.bot.get_cog(cog_name) if cog_name else None
        cleanup = getattr(cog, "cleanup_ended_game", None)
        if cleanup is not None:
            try:
                await cleanup(row["channel_id"], game_id, channel=channel)
            except Exception:
                # A bar left on screen is worse than a bar left on screen
                # *and* a game that never archived.
                log.exception("/games end: cleanup failed for %s", game_id)
        await force_end_active_game(self.bot, self.db, game_id)
        # Gray out the live game message's buttons if we can find it.
        if channel and row["message_id"]:
            try:
                msg = await channel.fetch_message(row["message_id"])
                await msg.edit(view=None)
            except discord.HTTPException:
                pass

    # `force` absorbed /games config game-end (2026-07-28): both tore down the
    # channel's active game through _teardown_active_game and announced it with
    # build_force_end_layout, differing only in who may run it and whether it
    # confirms first. That's an option, not a second command.
    @app_commands.command(name="end", description="End the active game in this channel (host or mod).")
    @app_commands.describe(force="Skip the confirmation and close it immediately (mods only).")
    async def games_end(self, interaction: discord.Interaction, force: bool = False):
        log.info(
            "%s used /games end%s in #%s",
            interaction.user.display_name,
            " (force)" if force else "",
            channel_name(interaction.channel),
        )
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.response.send_message(
                "There's no active game in this channel.", ephemeral=True,
            )
            return

        is_host = interaction.user.id == row["host_id"]
        is_mod = bool(interaction.guild) and isinstance(interaction.user, discord.Member) and has_mod_or_admin_permissions(
            interaction.user.guild_permissions
        )
        if not (is_host or is_mod):
            await interaction.response.send_message(
                "❌ Only the game's host or a moderator can end it.", ephemeral=True,
            )
            return
        # The old command was mod-gated; a host skipping their own confirmation
        # would be a new capability, so force stays mod-only.
        if force and not is_mod:
            await interaction.response.send_message(
                "❌ Only a moderator can force-close without confirming.", ephemeral=True,
            )
            return

        channel = interaction.channel
        # Slash commands with an active game always run in a sendable channel.
        assert isinstance(channel, discord.abc.Messageable)

        async def _close() -> None:
            if await self._end_with_recap(row, channel):
                # The recap ending closes the game itself, past
                # force_end_active_game — which is where the other path takes
                # the game's main-chat echo down (bug #273).
                await retract_party_game_echo(
                    self.bot, interaction.guild_id, row["game_id"]
                )
                return
            await self._teardown_active_game(row, channel)
            try:
                await channel.send(view=_card(build_force_end_layout(row["game_type"])))
            except discord.HTTPException:
                pass

        if force:
            await interaction.response.defer(ephemeral=True)
            await _close()
            await interaction.followup.send("Game closed.", ephemeral=True)
            return

        async def _confirmed(confirm_interaction):
            await _close()

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=view, ephemeral=True,
        )

    # Games with no fixed roster — anyone takes part by acting during an open
    # phase, so there's nothing to join or leave.
    OPEN_SUBMISSION_GAMES = {
        "hottakes", "fantasies", "ttl", "wyr", "nhie", "traditional",
    }

    async def _can_manage_others(self, interaction: discord.Interaction, host_id) -> bool:
        """True if the caller may add/remove *other* players: the game's host,
        a mod/admin, or a holder of the configured Game Host role."""
        if interaction.user.id == host_id:
            return True
        if has_mod_or_admin_permissions(getattr(interaction.user, "guild_permissions", None)):
            return True
        row = await self.db.fetchone(
            "SELECT role_id FROM games_editor_role WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        if row:
            user_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
            if int(row["role_id"]) in user_role_ids:
                return True
        return False

    async def _membership_command(self, interaction: discord.Interaction, user, joining: bool):
        verb = "join" if joining else "leave"
        log.info("%s used /games %s in #%s", interaction.user.display_name, verb, channel_name(interaction.channel))
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.response.send_message(
                "There's no active game in this channel.", ephemeral=True,
            )
            return

        target = user or interaction.user
        # Adding or removing someone else requires elevation; self-service is open.
        if target.id != interaction.user.id and not await self._can_manage_others(interaction, row["host_id"]):
            await interaction.response.send_message(
                "❌ Only the game's host, a moderator, or a Game-Host-role holder "
                "can add or remove other players.",
                ephemeral=True,
            )
            return

        game_type = row["game_type"]
        registry = self.bot.game_joiners if joining else self.bot.game_leavers
        handler = registry.get(game_type)
        if handler is None:
            if game_type in self.OPEN_SUBMISSION_GAMES:
                await interaction.response.send_message(
                    f"**{game_type}** is open to everyone — no need to {verb}; "
                    "just take part when a round opens.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"❌ You can't {verb} **{game_type}** mid-game.", ephemeral=True,
                )
            return

        assert isinstance(target, discord.Member)  # game channels are guild channels
        ok, message, *rest = await handler(interaction.channel, row["game_id"], target)
        # A handler's message can name a player with a raw <@id> — Clapback
        # tells the bumped bye player they are back in and must hit Submit.
        # The bot pings nobody unless the send says so, and the text was built
        # in another cog, so the handler hands the ids over as a third element
        # rather than leaving the mention inert here (GameRosterHandler).
        ping_ids: Sequence[int] = rest[0] if rest else ()
        # Announce successes in-channel so the room sees the roster change;
        # keep failures (already in/not in) private to the caller.
        await interaction.response.send_message(
            message,
            ephemeral=not ok,
            allowed_mentions=allow_pings(users=ping_ids),
        )

    @app_commands.command(name="host", description="Open the host controls for the game in this channel.")
    async def games_host(self, interaction: discord.Interaction):
        log.info("%s used /games host in #%s", interaction.user.display_name, channel_name(interaction.channel))
        row = await get_active_game(self.db, interaction.channel_id)
        # Every game in the channel is offered the press — the shared-row
        # game, and Meadow Mahjong tables and Risky Rolls rounds, which are not
        # rows in the shared table — so a channel holding two of them reaches
        # both (host_panel.open_host_panels).
        await open_host_panels(
            self.bot, interaction, row,
            no_game=NO_ACTIVE_GAME_REPLY, no_panel=NO_HOST_PANEL_REPLY,
        )

    @app_commands.command(name="join", description="Join the game running in this channel.")
    @app_commands.describe(user="Add someone else (host/mod/game-host only). Omit to join yourself.")
    async def games_join(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await self._membership_command(interaction, user, joining=True)

    @app_commands.command(name="leave", description="Leave the game running in this channel.")
    @app_commands.describe(user="Remove someone else (host/mod/game-host only). Omit to leave yourself.")
    async def games_leave(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await self._membership_command(interaction, user, joining=False)

    config_group = app_commands.Group(
        name="config",
        description="In-channel game management commands (mods only).",
    )

    @config_group.command(name="game-status", description="Show the active game in this channel.")
    @is_mod_or_admin()
    async def game_status(self, interaction: discord.Interaction):
        log.info("%s used /games config game-status in #%s", interaction.user.display_name, channel_name(interaction.channel))
        from bot_modules.games.utils.game_manager import get_active_game
        row = await get_active_game(self.db, interaction.channel_id)
        guild = interaction.guild
        name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=interaction.guild_id or 0,
            user_ids=[int(row["host_id"])] if row else [],
        )
        view = _card(build_game_status_layout(row, name_fn=name_fn))
        await interaction.response.send_message(view=view, ephemeral=True)

    @game_status.error
    async def mod_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            log.error("Permission denied for %s on mod command in #%s", interaction.user.display_name, channel_name(interaction.channel))
            try:
                await interaction.response.send_message(
                    "❌ You need moderator or admin permissions to use this command.", ephemeral=True
                )
            except discord.NotFound:
                pass
        else:
            log.error("Error in config command: %s", error, exc_info=True)


async def setup(bot: "Bot"):
    cog = GamesConfigCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("config")
    bot.tree.remove_command("end")
    bot.tree.remove_command("join")
    bot.tree.remove_command("leave")
    games.add_command(cog.config_group, override=True)
    games.add_command(cog.games_end, override=True)
    games.add_command(cog.games_join, override=True)
    games.add_command(cog.games_leave, override=True)
    bot.tree.add_command(games, override=True)
