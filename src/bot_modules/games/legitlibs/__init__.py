import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.core.utils import is_host_or_mod
from bot_modules.games.constants import play_description
from bot_modules.games.utils.game_manager import (
    channel_name, finish_launch_response, end_game, sign_off_game_chore,
)
from bot_modules.games.utils.host_panel import (
    GAME_ENDED_REPLY,
    NOT_HOST_REPLY,
    GameHostPanel,
    hand_over,
    reply as host_reply,
    send_panel,
)
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.command_groups import play
from .classic_logic import clamp_tier, tier_clamp_note
from .data import SEED_PATH, get_channel_max_tier, seed_templates_from_file
from .layouts import ANOTHER_ID, HOST_CANCEL_ID, HOST_START_ID, another_row, build_host_panel_layout
from .modes.quiplash import run_quiplash
from .modes.classic import run_classic
from .views import LibsCard

log = logging.getLogger(__name__)

class LegitLibsCog(commands.Cog, name="LegitLibsCog"):
    def __init__(self, bot):
        self.bot = bot
        self._game_canceled: set[str] = set()

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self):
        await seed_templates_from_file(self.db, SEED_PATH, author_id=0)
        log.info("LegitLibsCog loaded.")

    # ── /games play legitlibs ─────────────────────────────────────────────────────────────
    @app_commands.command(name="legitlibs", description=play_description("legitlibs"))
    @app_commands.describe(
        mode="Game mode: classic (default) or quiplash",
        tier="Heat tier 1–4 (1=Flirty, 2=Spicy, 3=Filthy, 4=Unhinged). Default: 2",
        template_id="Optional: use a specific template by ID",
        tag="Optional: filter templates by tag",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Classic (sequential fill)", value="classic"),
        app_commands.Choice(name="Quiplash (everyone fills, all revealed)", value="quiplash"),
    ])
    @app_commands.choices(tier=[
        app_commands.Choice(name="1 — Flirty 🌶️", value=1),
        app_commands.Choice(name="2 — Spicy 🌶️🌶️", value=2),
        app_commands.Choice(name="3 — Filthy 🌶️🌶️🌶️", value=3),
        app_commands.Choice(name="4 — Unhinged 💀", value=4),
    ])
    async def legitlibs(
        self,
        interaction: discord.Interaction,
        mode: str = "classic",
        tier: int = 2,
        template_id: str | None = None,
        tag: str | None = None,
    ):
        log.info("%s used /games play legitlibs in #%s", interaction.user.display_name, channel_name(interaction.channel))

        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running here — any game, not only another
        # LegitLibs round, which is all the private guard this replaced saw.
        refusal = await refuse_launch(self.db, interaction, "legitlibs")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        # The channel's tier cap, worked out here so the host hears about a
        # clamp — the modes clamp again for the headless doors but only log.
        max_tier = await get_channel_max_tier(self.db, interaction.channel_id or 0)
        note = tier_clamp_note(tier, max_tier)
        tier, _ = clamp_tier(tier, max_tier)

        await interaction.response.defer()
        if note:
            try:
                await interaction.followup.send(note, ephemeral=True)
            except discord.HTTPException:
                pass

        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"mode": mode, "tier": tier, "template_id": template_id, "tag": tag},
        )
        await finish_launch_response(
            interaction, game_id,
            perms_hint="Couldn't start LegitLibs — no published templates for that tier/tag, "
            "or I'm missing permission to post here.",
        )
        # Start and Cancel aren't on the cards, so hand the host their
        # controls now rather than make them go find ``/games host``.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def open_host_panel(self, interaction: discord.Interaction, game_id: str) -> None:
        """``/games host`` for a LegitLibs round: Start and Cancel Round on
        one panel for the whole round. The host always gets it; anyone else
        needs the same mod standing the controls check (``is_host_or_mod``)."""
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, LibsCard):
            await host_reply(interaction, GAME_ENDED_REPLY)
            return
        if not is_host_or_mod(interaction, view.host_id):
            await host_reply(interaction, NOT_HOST_REPLY)
            return
        panel = GameHostPanel(
            self.bot, game_id, build_host_panel_layout(),
            {HOST_START_ID: "start_game", HOST_CANCEL_ID: "cancel_round"},
            view_type=LibsCard,
        )
        await send_panel(interaction, panel, label="legitlibs host panel")

    async def launch(self, *, channel, host_id, host_name, guild_id, options) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        mode = options.get("mode", "classic")
        tier = int(options.get("tier", 2))
        template_id = options.get("template_id") or None
        tag = options.get("tag") or None
        guild = getattr(channel, "guild", None)
        if mode == "quiplash":
            return await run_quiplash(self, channel=channel, guild=guild, host_id=host_id, host_name=host_name, tier=tier, template_id=template_id, tag=tag)
        if mode == "classic":
            return await run_classic(self, channel=channel, guild=guild, host_id=host_id, host_name=host_name, tier=tier, template_id=template_id, tag=tag)
        # hotseat not implemented
        log.info("legitlibs launch: mode %r not available", mode)
        return None

    async def offer_another(
        self, message: discord.Message, items: list, host_id: int, options: dict,
    ) -> None:
        """Put **🔁 Another One** under a finished round's last card.

        Called by both modes once ``end_game`` has run, so the button's launch
        guard sees a free channel. *items* are the card's own items, re-sent
        with the button row under them — a V2 message is its view, so the
        row can't be attached without the card. *options* is the round's
        mode, tier and tag — the next template is the pool's pick, never
        the same id. Host or mod only (trivia-tail-90).
        """
        async def _again(interaction: discord.Interaction) -> None:
            log.info("%s pressed 'Another One' in #%s", interaction.user.display_name, channel_name(interaction.channel))
            if not is_host_or_mod(interaction, host_id):
                await interaction.response.send_message(
                    "❌ Only the host or a mod can start another one.", ephemeral=True)
                return
            await self.another_one(interaction, view, items, options)

        view = LibsCard("", host_id, [*items, another_row()], {ANOTHER_ID: _again})
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass

    async def another_one(
        self, interaction: discord.Interaction, view: LibsCard, items: list, options: dict,
    ) -> None:
        """The recap button: same tier, mode and tag, next template from the pool."""
        refusal = await refuse_launch(self.db, interaction, "legitlibs")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        view.stop()
        # The button has done its job: the card goes back to being the card.
        view.render(items)
        assert interaction.message is not None  # component interactions carry their message
        try:
            await interaction.message.edit(view=view)
        except discord.HTTPException:
            pass
        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options=dict(options),
        )
        if not game_id:
            try:
                await interaction.followup.send(
                    "Couldn't start another one — no published templates left for that "
                    "tier/tag, or I'm missing permission to post here.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
        # A relaunch skips finish_launch_response, so the chore sign-off
        # rides here — only on a launch that produced a game.
        await sign_off_game_chore(self.bot, interaction.guild_id, interaction.user.id)
        # The presser hosts the next round and its lobby carries nothing
        # host-only — hand them their controls.
        await hand_over(self.bot, interaction, self.open_host_panel, game_id)

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Recover a LegitLibs round after a bot restart.

        Both modes (classic + quiplash share the ``legitlibs`` game type) run
        entirely inside a blocking fill/reveal loop that the restart tore down;
        there is no phase view to rebind and no resumable state, so — like the
        Story recovery — we end the round and post a notice. This unblocks the
        channel immediately (the ``games_active_games`` row is deleted) instead
        of leaving dead buttons until the 24h cleanup sweep.
        """
        game_id = row["game_id"]
        label = "Quiplash" if (payload.get("mode") == "quiplash") else "LegitLibs"
        try:
            await channel.send(
                f"🎲 This {label} round was interrupted by a bot restart and can't "
                "be resumed — start a new one with `/games play legitlibs`."
            )
        except discord.HTTPException:
            pass
        await end_game(self.db, game_id)
        self.bot.active_views.pop(game_id, None)
        self._game_canceled.add(game_id)
        log.info("legitlibs game %s ended after restart (mode=%s).", game_id, payload.get("mode"))
        return True


async def setup(bot):
    cog = LegitLibsCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("legitlibs")
    play.add_command(cog.legitlibs)
    bot.game_launchers["legitlibs"] = cog.launch
    bot.game_recoverers["legitlibs"] = cog.recover_game
