"""Buttons for the ``/info`` panel.

Every button re-enters the flow that already owns its feature — Pen Pals'
``_handle_join``, Whispers' ``_optin_impl``, Guess's consent view, the DM
settings panel, the wellness timezone wizard. **Nothing here grants a role,
writes an opt-in row, or clears one.**

That is deliberate and load-bearing. Each of those flows carries a gate that
is the feature's whole compliance story: Guess and Whispers show consent copy
and only grant the role once it is read and accepted; Pen Pals checks the
guild's opt-in role before pooling anyone; wellness cannot opt someone in
without a timezone. A panel that flipped roles directly would be a second,
ungated door into all of them. Reuse is the safety property, not a shortcut.

If a feature's cog isn't loaded, its button is simply never built — the
status row still renders (``FeatureState.actionable=False``), so the member
sees the truth without a control that would do nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import discord

from bot_modules.core.layout import ExpiringLayoutView
from bot_modules.member_info.layouts import OPTIN_BUTTON_ID_BASE
from bot_modules.member_info.logic import (
    ACTION_JOIN,
    ACTION_OPEN,
    OptInRow,
)

log = logging.getLogger(__name__)

# Pool-event source recorded for a Pen Pals join/leave that came from here,
# so pool history can still answer "where did this join come from?" — the
# panel, a command, a DM button, or now the member's own info card.
PEN_PALS_SOURCE = "info_panel"

async def _run_pen_pals(bot, interaction: discord.Interaction, action: str) -> None:
    from bot_modules.pen_pals.cog import (  # noqa: PLC0415
        _handle_join,
        _handle_leave,
    )

    handler = _handle_join if action == ACTION_JOIN else _handle_leave
    await handler(interaction, bot.ctx.db_path, source=PEN_PALS_SOURCE)


async def _run_whispers(bot, interaction: discord.Interaction, action: str) -> None:
    cog = bot.get_cog("WhisperCog")
    if cog is None:
        await _unavailable(interaction)
        return
    impl = cog._optin_impl if action == ACTION_JOIN else cog._optout_impl
    await impl(interaction)


async def _run_guess(bot, interaction: discord.Interaction, action: str) -> None:
    cog = bot.get_cog("GuessCog")
    if cog is None:
        await _unavailable(interaction)
        return
    impl = cog._optin_impl if action == ACTION_JOIN else cog._optout_impl
    await impl(interaction)


async def _run_dm_mode(bot, interaction: discord.Interaction, action: str) -> None:
    from bot_modules.dm.perms.cog import open_dm_settings  # noqa: PLC0415

    cog = bot.get_cog("DmPermsCog")
    member = interaction.user
    if cog is None or not isinstance(member, discord.Member):
        await _unavailable(interaction)
        return
    await open_dm_settings(cog, interaction, member)


async def _run_wellness(bot, interaction: discord.Interaction, action: str) -> None:
    cog = bot.get_cog("WellnessCog")
    if cog is None:
        await _unavailable(interaction)
        return
    await cog.open_setup(interaction)


async def _run_birthday(bot, interaction: discord.Interaction, action: str) -> None:
    cog = bot.get_cog("BirthdayCog")
    if cog is None:
        await _unavailable(interaction)
        return
    if action == ACTION_JOIN:
        # A modal must be the *first* response to an interaction — which a
        # fresh button click always is, so this is safe from here but would
        # not be after a defer().
        from bot_modules.birthday.cog import _BirthdayModal  # noqa: PLC0415

        await interaction.response.send_modal(_BirthdayModal(bot.ctx))
        return
    await cog.remove_impl(interaction)


async def _run_no_contact(bot, interaction: discord.Interaction, action: str) -> None:
    # The Server Features hub's No Contact card — the one surface that applies
    # ``is_visible_to``, which is why this opens it rather than counting rows.
    cog = bot.get_cog("ServerFeaturesCog")
    if cog is None:
        await _unavailable(interaction)
        return
    await cog.open_no_contact(interaction)


async def _run_my_data(bot, interaction: discord.Interaction, action: str) -> None:
    cog = bot.get_cog("PrivacyCog")
    if cog is None:
        await _unavailable(interaction)
        return
    await cog.open_panel(interaction)


_RUNNERS = {
    "pen_pals": _run_pen_pals,
    "whispers": _run_whispers,
    "guess": _run_guess,
    "dm_mode": _run_dm_mode,
    "wellness": _run_wellness,
    "birthday": _run_birthday,
    "no_contact": _run_no_contact,
    "my_data": _run_my_data,
}


async def _unavailable(interaction: discord.Interaction) -> None:
    message = "❌ That isn't available right now — try again in a minute."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _optin_callback(bot, row: OptInRow):
    async def _callback(interaction: discord.Interaction) -> None:
        runner = _RUNNERS.get(row.key)
        if runner is None:
            await _unavailable(interaction)
            return
        try:
            await runner(bot, interaction, row.action or ACTION_OPEN)
        except Exception:
            # One feature's flow failing must not take the panel with it.
            log.exception("info panel: %s/%s failed", row.key, row.action)
            await _unavailable(interaction)

    return _callback


class MemberInfoView(ExpiringLayoutView):
    """The ``/info`` card and its opt-in buttons.

    Not persistent: the card is ephemeral and its rows are a snapshot of
    state that the buttons themselves change, so it expires (greying out in
    place) rather than lingering with stale labels. Pressing a stale button
    is still safe — the underlying flow re-reads state and refuses on its
    own terms.

    ``items`` is ``layouts.build_member_info_layout(...)`` for the same
    ``rows``; each actionable row's button is found by the numeric id the
    builder gave it.
    """

    def __init__(
        self,
        bot,
        items: Sequence[discord.ui.Item],
        rows: Sequence[OptInRow],
        *,
        timeout: float = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        for item in items:
            self.add_item(item)
        for index, row in enumerate(rows):
            if not row.has_action:
                continue
            button = self.find_item(OPTIN_BUTTON_ID_BASE + index)
            if button is not None:
                button.callback = _optin_callback(bot, row)  # type: ignore[attr-defined]
