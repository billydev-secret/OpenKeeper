"""The ``/games help`` panel: a select menu of games, a detail card, and a
Start button that goes through the shared launch guard.

The panel is ephemeral and short-lived (no persistent ``custom_id``): a
member who wants it again types the command again. Everything that decides
*what* to show lives in :mod:`bot_modules.games.help.logic` and
:mod:`bot_modules.games.help.layouts`; everything that decides *whether a
launch may go ahead* lives in :mod:`bot_modules.games.utils.launch_guard` —
this module only wires the two to Discord, and :func:`start_from_help` is
the one seam the wiring test exercises.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import discord

from bot_modules.core.layout import ExpiringLayoutView
from bot_modules.games.constants import GAME_NAMES
from bot_modules.games.utils.game_manager import (
    DEFAULT_LAUNCH_PERMS_HINT,
    channel_name,
    get_active_game,
    sign_off_game_chore,
)
from bot_modules.games.utils.host_panel import host_panel_opener
from bot_modules.games.utils.launch_guard import launch_refusal
from bot_modules.games.utils.question_source import channel_allows_nsfw
from bot_modules.games.help.layouts import build_game_detail_layout, build_help_layout
from bot_modules.games.help.logic import game_detail, select_options

log = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 600


def _card(items: Sequence[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    """Wrap finished items in a control-free ``LayoutView`` for one edit.

    A message that has carried a Components V2 view keeps the V2 flag for
    life, so the panel can only ever be replaced by another card — never
    by bare ``content=`` (paradigm 7, a receipt).
    """
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


async def start_from_help(
    bot,
    *,
    game_type: str,
    channel,
    channel_id: int | None,
    guild_id: int,
    user_id: int,
    user_name: str,
) -> tuple[bool, str]:
    """Launch *game_type* the way its slash entry would, from the panel.

    Runs the shared launch guard first (allowed channel, enabled dial, busy
    channel, empty bank) and returns its refusal untouched; otherwise calls
    the cog's registered headless ``launch()`` with default options — the
    same door the scheduler and the recap's Play Again use. Returns
    ``(started, message)``; the message is what the panel shows in place of
    itself. Never raises for a missing launcher: a game the panel lists but
    no cog registered gets the permissions hint, not a traceback.
    """
    db = getattr(bot, "games_db", None)
    launcher = getattr(bot, "game_launchers", {}).get(game_type)
    if db is None or launcher is None:
        return False, DEFAULT_LAUNCH_PERMS_HINT
    refusal = await launch_refusal(
        db, game_type, channel_id, guild_id, allow_nsfw=channel_allows_nsfw(channel),
    )
    if refusal:
        return False, refusal
    game_id = await launcher(
        channel=channel,
        host_id=user_id,
        host_name=user_name,
        guild_id=guild_id,
        options={},
    )
    if game_id is None:
        return False, DEFAULT_LAUNCH_PERMS_HINT
    await sign_off_game_chore(bot, guild_id, user_id)
    return True, f"✅ **{GAME_NAMES.get(game_type, game_type)}** is starting in this channel."


async def hand_over_host_panel(interaction: discord.Interaction, game_type: str) -> None:
    """Send the presser the game's host panel, when it has one.

    Start Here launches through the headless ``launch()``, which hands no
    one anything; the slash command sends the panel itself. Without this a
    host who started Truth or Dare from Help had no Ask Question or End
    Game in reach (ship review, 2026-09-29). A failure is logged, not
    raised: the game is already running and ``/games host`` still works.
    """
    bot = interaction.client
    opener = host_panel_opener(bot, game_type)
    db = getattr(bot, "games_db", None)
    if opener is None or db is None:
        return
    row = await get_active_game(db, interaction.channel_id)
    if not row:
        return
    try:
        await opener(interaction, row["game_id"])
    except discord.HTTPException:
        log.info("host panel not delivered after Start Here for %s", game_type)


class GamePicker(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Pick a game…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key, description=desc)
                for key, label, desc in select_options()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, GameHelpView)
        await view.show_game(interaction, self.values[0])


class GameHelpView(ExpiringLayoutView):
    """The ephemeral panel. ``extra_lines`` are the open rooms for the
    overview.

    Rebuilt whole on every press (``_build``): the overview and a game's
    detail card are different item sets, not one card with a swapped field,
    so there is nothing to mutate in place the way a live board's redraw
    does. The picker and the two buttons are plain instances held on
    ``self`` — the panel is ephemeral and never re-sent from cold storage,
    so there is no persistent ``custom_id`` to bind by; a direct callback
    assignment is enough.
    """

    def __init__(self, *, extra_lines: Sequence[str] = ()) -> None:
        super().__init__(timeout=PANEL_TIMEOUT_SECONDS)
        self.extra_lines = list(extra_lines)
        self.selected: str | None = None
        self.picker = GamePicker()
        self.start_button: discord.ui.Button[Any] = discord.ui.Button(
            label="▶️ Start Here", style=discord.ButtonStyle.primary,
        )
        self.start_button.callback = self._on_start
        self.back_button: discord.ui.Button[Any] = discord.ui.Button(
            label="Back", style=discord.ButtonStyle.secondary,
        )
        self.back_button.callback = self._on_back
        self._build()

    def _build(self) -> None:
        self.clear_items()
        if self.selected is None:
            for item in build_help_layout(extra_lines=self.extra_lines):
                self.add_item(item)
        else:
            for item in build_game_detail_layout(self.selected):
                self.add_item(item)
        self.add_item(discord.ui.ActionRow(self.picker))
        if self.selected is not None:
            buttons: list[discord.ui.Button[Any]] = []
            if game_detail(self.selected).startable:
                buttons.append(self.start_button)
            buttons.append(self.back_button)
            self.add_item(discord.ui.ActionRow(*buttons))

    async def show_game(self, interaction: discord.Interaction, key: str) -> None:
        self.selected = key
        self._build()
        await interaction.response.edit_message(view=self)

    async def _on_start(self, interaction: discord.Interaction) -> None:
        key = self.selected
        if key is None:
            await interaction.response.defer()
            return
        log.info(
            "%s pressed Start Here for %s in #%s",
            interaction.user.display_name, key, channel_name(interaction.channel),
        )
        # The launch posts the game's own board; defer so the panel edit
        # can wait for it without the interaction token expiring.
        await interaction.response.defer()
        started, message = await start_from_help(
            interaction.client,
            game_type=key,
            channel=interaction.channel,
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id or 0,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
        )
        if started:
            self.stop()
            # The panel already carries a V2 view, so the confirmation is
            # another card: a bare ``content=`` edit is refused by Discord.
            await interaction.edit_original_response(
                view=_card([discord.ui.TextDisplay(message)])
            )
            await hand_over_host_panel(interaction, key)
        else:
            # A refusal keeps the panel: the member can pick something else.
            await interaction.followup.send(message, ephemeral=True)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.selected = None
        self._build()
        await interaction.response.edit_message(view=self)
