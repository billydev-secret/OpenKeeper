import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.session.layouts import build_session_recap_layout
from bot_modules.games.session.logic import build_highlights, format_duration
from bot_modules.shared.name_resolver import build_name_fn


def _card(items: Sequence[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one send."""
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


class SessionCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(
        name="recap",
        description="Show a recap of the current games session.",
    )
    async def session_recap(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Deliberately naive UTC: ``games_session_tracker.last_game_at`` holds
        # naive ISO strings (written by ``update_session``, defaulted by
        # SQLite's naive ``CURRENT_TIMESTAMP``) and this cutoff is compared
        # against them as a *string* in SQL. An aware value would serialise
        # with a "+00:00" suffix and stop matching.
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
        ).isoformat()
        session_row = await self.db.fetchone(
            """
            SELECT session_id, started_at, last_game_at, game_ids, player_ids
            FROM games_session_tracker
            WHERE channel_id = ? AND last_game_at >= ?
            ORDER BY last_game_at DESC LIMIT 1
            """,
            (interaction.channel_id, cutoff),
        )

        if not session_row:
            await interaction.followup.send(
                "No active session found in this channel within the last 30 minutes."
            )
            return

        game_ids = json.loads(session_row["game_ids"])
        player_ids = json.loads(session_row["player_ids"])
        duration_str = format_duration(
            session_row["started_at"], session_row["last_game_at"]
        )

        # Fetch game history for these game IDs
        game_histories: list[dict] = []
        for gid in game_ids:
            row = await self.db.fetchone(
                "SELECT game_type, player_count, round_count, payload FROM games_game_history WHERE game_id = ?",
                (gid,),
            )
            if row:
                game_histories.append(
                    {
                        "game_type": row["game_type"],
                        "payload": json.loads(row["payload"]),
                    }
                )

        # Resolve display names against the live guild — fed into logic
        # so the per-game highlight builder stays Discord-free.
        name_lookup: dict[str, str] = {}
        if interaction.guild:
            for history in game_histories:
                payload = history["payload"]
                ids_to_resolve: set[str] = set()
                ids_to_resolve.update(payload.get("guilt_scores", {}).keys())
                ids_to_resolve.update(payload.get("scores", {}).keys())
                for uid_str in ids_to_resolve:
                    try:
                        member = interaction.guild.get_member(int(uid_str))
                    except (TypeError, ValueError):
                        continue
                    if member:
                        name_lookup[uid_str] = member.display_name

        highlights = build_highlights(game_histories, name_lookup)

        guild = interaction.guild
        # Built once over the whole roster: only ids the member cache misses
        # (players who have since left) cost a query.
        name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=interaction.guild_id or 0,
            user_ids=[int(uid) for uid in player_ids],
        )
        items = build_session_recap_layout(
            game_count=len(game_ids),
            player_ids=player_ids,
            duration_str=duration_str,
            highlights=highlights,
            name_fn=name_fn,
        )
        await interaction.followup.send(view=_card(items))


async def setup(bot: "Bot"):
    await bot.add_cog(SessionCog(bot))
