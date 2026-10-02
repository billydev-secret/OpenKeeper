"""The one view a LegitLibs card rides on, and the cancel confirm.

The embed era had five ``View`` subclasses, one per phase, each a set of
decorated buttons calling back into the mode's closures. A Components V2
card is its own view, so the phases share :class:`LibsCard`: the mode
hands it the card's items (``layouts.py``) and a map of ``custom_id`` to
callback, and it binds whatever the card currently shows. Re-rendering a
phase — the counter ticking, the closed frame — is :meth:`LibsCard.render`
with fresh items, then an edit of the message.

The host panel (``host_panel.GameHostPanel``) reaches the round through
:meth:`LibsCard.start_game` and :meth:`LibsCard.cancel_round`, which the
mode wires with ``actions``; a phase without one answers plainly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from bot_modules.core.layout import bind_present
from bot_modules.core.utils import disable_all_items, is_host_or_mod
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.utils.game_manager import channel_name

log = logging.getLogger(__name__)

Callback = Callable[[discord.Interaction], Awaitable[None]]

ALREADY_STARTED_REPLY = "The round has already started."
NOTHING_TO_CANCEL_REPLY = "This round is past the point of cancelling."


class _CancelConfirmView(discord.ui.View):
    """Two-step confirmation to avoid accidental round destruction."""

    def __init__(self, on_confirm):
        super().__init__(timeout=60)
        self._on_confirm = on_confirm

    @discord.ui.button(label="Yes, Cancel Round", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        disable_all_items(self)
        try:
            await interaction.response.edit_message(content="🛑 Cancelling…", view=self)
        except discord.HTTPException:
            pass
        await self._on_confirm(interaction)

    @discord.ui.button(label="Keep Playing", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancel aborted.", view=None)


async def send_help(interaction: discord.Interaction) -> None:
    log.info("%s pressed 'Help' in #%s", interaction.user.display_name, channel_name(interaction.channel))
    await interaction.response.send_message(HOW_TO_PLAY.get("legitlibs", ""), ephemeral=True)


class LibsCard(discord.ui.LayoutView):
    """A LegitLibs phase card: the items from ``layouts.py`` plus the mode's
    callbacks, bound by ``custom_id`` to whatever the card shows.

    ``actions`` carries the host panel's two hand-offs — ``"start"`` while
    the lobby is open, ``"cancel"`` for every phase up to the reveal. Each
    keeps the host/mod gate here, on the action, since the panel only ever
    reaches a member who passed one but the gate on the action is the one
    that counts. Cancel always confirms first: nothing destructive is one
    tap (guide § Controls).
    """

    def __init__(
        self,
        game_id: str,
        host_id: int,
        items: list[discord.ui.Item[Any]],
        callbacks: dict[str, Callback],
        *,
        actions: dict[str, Callback] | None = None,
        cancel_prompt: str = "Cancel this round?",
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self._callbacks = callbacks
        self._actions = actions or {}
        self._cancel_prompt = cancel_prompt
        self.render(items)

    def close_phase(self) -> None:
        """End this card's phase: stop listening and disarm its hand-offs.

        ``stop()`` alone left the view in ``bot.active_views`` with Cancel
        still wired, so the host panel could cancel a round mid-reveal —
        ``end_game`` with no roster, after which the reveal's own paid
        ``end_game`` found the row gone and paid nobody (ship review,
        2026-10-01). Every phase-ending ``stop()`` goes through here.
        """
        self._actions.pop("start", None)
        self._actions.pop("cancel", None)
        self.stop()

    async def _confirmed_cancel(self, interaction: discord.Interaction) -> None:
        """Run Cancel only if it is still armed when "Yes" is pressed — a
        confirm opened during the fill phase can be answered after it."""
        cancel = self._actions.get("cancel")
        if cancel is None:
            try:
                await interaction.followup.send(NOTHING_TO_CANCEL_REPLY, ephemeral=True)
            except discord.HTTPException:
                pass
            return
        await cancel(interaction)

    def render(self, items: list[discord.ui.Item[Any]]) -> None:
        """Replace the card's items and re-bind the callbacks the new items
        carry — a rebuild adds brand-new buttons with nothing wired."""
        self.clear_items()
        for item in items:
            self.add_item(item)
        bind_present(self, self._callbacks)

    async def start_game(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Start' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        start = self._actions.get("start")
        if start is None:
            await interaction.response.send_message(ALREADY_STARTED_REPLY, ephemeral=True)
            return
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start the round.", ephemeral=True)
            return
        await start(interaction)

    async def cancel_round(self, interaction: discord.Interaction) -> None:
        log.info("%s pressed 'Cancel Round' in #%s", interaction.user.display_name, channel_name(interaction.channel))
        cancel = self._actions.get("cancel")
        if cancel is None:
            await interaction.response.send_message(NOTHING_TO_CANCEL_REPLY, ephemeral=True)
            return
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can cancel.", ephemeral=True)
            return
        await interaction.response.send_message(
            self._cancel_prompt, view=_CancelConfirmView(self._confirmed_cancel), ephemeral=True,
        )
