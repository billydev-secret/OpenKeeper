"""Which games keep their host-only controls on an ephemeral host panel,
and the mechanics every such panel shares.

Truth or Dare moved Ask Question / Write My Own / End Game off its board
onto one on 2026-09-29, and the party games followed as they converted to
Components V2 (Ben, 2026-09-30: everything host-only moves, except where a
voter can press it). Every door that should hand the host their
controls reads this table: ``/games host`` (``games_config_cog``) and the
``/games help`` panel's Start Here, which launches through the headless
``launch()`` and so never passes the slash command's own hand-off. Looked
up by cog name, the way ``/games end``'s tables are, so a cog mid-reload
reads as "no panel" rather than raising.
"""

from __future__ import annotations

import contextvars
import logging
import time
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from bot_modules.core.layout import ExpiringLayoutView, bind
from bot_modules.games.utils.send_retry import retry_transient

log = logging.getLogger(__name__)

#: game_type -> the cog whose ``open_host_panel(interaction, game_id)``
#: sends that game's host panel.
HOST_PANEL_COGS = {
    "traditional": "TraditionalCog",
    "rushmore": "RushmoreCog",
    "wyr": "WYRCog",
    "nhie": "NHIECog",
    "mlt": "MLTCog",
    "legitlibs": "LegitLibsCog",
    "ama": "AMACog",
    "price": "PriceCog",
    "hottakes": "HotTakesCog",
    "fantasies": "FantasiesCog",
    "ttl": "TTLCog",
    "ffa": "FFACog",
}

#: Games whose live table or round is **not** a ``games_active_games`` row,
#: so ``/games host`` cannot find them by ``get_active_game``: Meadow Mahjong
#: keeps ``mahjong_tables`` and Risky Rolls keeps rounds in memory
#: (``risky_roll.state``). Writing them into the shared table would hand
#: every sweep over it a game it cannot close, so instead ``/games host``
#: asks each cog here when the channel has no shared row:
#: ``open_channel_host_panel(interaction)`` returns True when the channel
#: holds one of its games (whether or not it then refused the presser),
#: False to let the next cog look.
CHANNEL_HOST_PANEL_COGS = {
    "mahjong": "MahjongCog",
    "risky_roll": "RiskyRollCog",
}

HostPanelOpener = Callable[[Any, str], Awaitable[None]]

#: The slash command that opens the host panel, named wherever a card
#: points the host at it.
HOST_COMMAND = "/games host"

#: A board's one pointer at the host's controls. A game started from
#: ``/games help`` or the scheduler never hands the host their panel the
#: way the slash command does, and a board carries nothing host-only,
#: so without this line such a host had no way to reach them (ship review,
#: 2026-09-29).
HOST_HINT = f"Hosting? {HOST_COMMAND} opens your controls."

#: How long a host panel stays pressable: 14 minutes, just inside the 15
#: Discord allows for editing an ephemeral message through its interaction.
#: Past that the grey-out edit fails silently and the panel would look live
#: while every press fails with "This interaction failed" (ship review,
#: 2026-09-29). Measured from when the panel was SENT, not from the last
#: press (:class:`HostPanelView`): the panel's message handle rides the
#: sending interaction's token, which a later press does not renew. Once it
#: greys out, ``/games host`` fetches a fresh one, which the panel says.
HOST_PANEL_TIMEOUT = 14 * 60

GAME_ENDED_REPLY = "This game has ended."
NOT_HOST_REPLY = "❌ Only the host or a mod can use the host controls."


def host_panel_opener(bot: Any, game_type: str) -> HostPanelOpener | None:
    """The loaded cog's ``open_host_panel`` for *game_type*, or ``None``
    when the game has no host panel or its cog isn't loaded."""
    cog_name = HOST_PANEL_COGS.get(game_type)
    get_cog = getattr(bot, "get_cog", None)
    cog = get_cog(cog_name) if cog_name and get_cog else None
    return getattr(cog, "open_host_panel", None) if cog is not None else None


#: Set while :func:`open_host_panels` dispatches ``/games host``: a list
#: that :func:`reply` appends a refusal to instead of sending it. One
#: channel can hold a shared-row game, a Mahjong table and a Risky round at
#: once, and the presser should get every panel they may open — not a
#: refusal from whichever game was asked first (ship review, 2026-10-01).
_HELD_REFUSALS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "host_panel_held_refusals", default=None,
)


async def open_channel_host_panel(bot: Any, interaction: discord.Interaction) -> bool:
    """Offer the channel to each cog in :data:`CHANNEL_HOST_PANEL_COGS`;
    True if any of them answered the presser (a panel or a refusal). Every
    cog is asked — a channel can hold a Mahjong table and a Risky round."""
    answered = False
    get_cog = getattr(bot, "get_cog", None)
    for cog_name in CHANNEL_HOST_PANEL_COGS.values():
        cog = get_cog(cog_name) if get_cog else None
        opener = getattr(cog, "open_channel_host_panel", None) if cog is not None else None
        if opener is not None and await opener(interaction):
            answered = True
    return answered


async def open_host_panels(
    bot: Any, interaction: discord.Interaction, row: Any | None,
    *, no_game: str, no_panel: str,
) -> None:
    """``/games host``: every host panel the presser may open in this channel.

    The channel's shared-row game (*row*, from ``games_active_games``) and
    each :data:`CHANNEL_HOST_PANEL_COGS` game are all offered the press,
    with their refusals held back (:data:`_HELD_REFUSALS`); each panel one
    of them opens goes out (the first as the reply, the rest as
    follow-ups). Only when none opened does the presser get a line: the
    shared game's refusal first, else a channel game's, else *no_panel* for
    a shared game without a panel, else *no_game*. Before this, a channel
    with a shared-row game never reached Mahjong or Risky at all, so their
    host-only buttons — on the panel and nowhere else — were unreachable.
    """
    held: list[str] = []
    token = _HELD_REFUSALS.set(held)
    try:
        if row is not None:
            opener = host_panel_opener(bot, row["game_type"])
            if opener is None:
                held.append(no_panel)
            else:
                await opener(interaction, row["game_id"])
        await open_channel_host_panel(bot, interaction)
    finally:
        _HELD_REFUSALS.reset(token)
    if not interaction.response.is_done():
        await reply(interaction, held[0] if held else no_game)


async def reply(interaction: discord.Interaction, text: str) -> None:
    """An ephemeral line on a fresh interaction or an answered one alike —
    ``open_host_panel`` is reached from ``/games host`` (fresh) and from
    Help's Start Here (already deferred)."""
    held = _HELD_REFUSALS.get()
    if held is not None:
        held.append(text)
        return
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def send_panel(
    interaction: discord.Interaction, panel: ExpiringLayoutView, *, label: str,
) -> None:
    """Send *panel* to the presser, ephemerally, and hand it its message.

    Works on a fresh interaction (``/games host``) and on one already
    answered (the launch, whose deferred reply ``finish_launch_response``
    has deleted) alike. *label* names the send in the retry log.
    """
    if isinstance(panel, HostPanelView):
        panel.start_clock_at(getattr(interaction, "created_at", None))
    if interaction.response.is_done():
        panel.message = await retry_transient(
            lambda: interaction.followup.send(view=panel, ephemeral=True, wait=True),
            label=label,
        )
    else:
        await interaction.response.send_message(view=panel, ephemeral=True)
        panel.message = await interaction.original_response()


class HostPanelView(ExpiringLayoutView):
    """An ephemeral host panel whose timeout counts from when it was sent.

    discord.py restarts a view's timeout on every press, but the panel's
    grey-out edits the message through the *sending* interaction's token,
    which Discord honours for 15 minutes and a later press never renews. So
    a panel pressed at minute 10 used to time out at minute 24, its grey-out
    edit failed silently, and it sat there looking live while every press
    failed (ship review, 2026-10-01). Each press now shrinks ``timeout`` to
    what is left of the original window — ``interaction_check`` runs before
    discord.py pushes the expiry forward, and that push reads ``timeout``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("timeout", HOST_PANEL_TIMEOUT)
        super().__init__(*args, **kwargs)
        self._deadline = time.monotonic() + float(self.timeout or HOST_PANEL_TIMEOUT)

    def start_clock_at(self, created_at: Any) -> None:
        """Count the window from when the sending interaction was created,
        if that is earlier: a panel sent as a follow-up on a deferred press
        (Start Here, a slow launch) edits through a token whose 15 minutes
        began at the press, not at this build (ship review, 2026-10-01)."""
        if not isinstance(created_at, datetime):
            return
        waited = max(0.0, time.time() - created_at.timestamp())
        self._deadline = min(self._deadline, time.monotonic() - waited + HOST_PANEL_TIMEOUT)
        self.timeout = max(1.0, self._deadline - time.monotonic())

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        self.timeout = max(1.0, self._deadline - time.monotonic())
        return await super().interaction_check(interaction)


class GameHostPanel(HostPanelView):
    """An ephemeral host panel that holds no state of its own.

    Each button hands off to a method on **whichever view is the game's
    live one when it is pressed** — looked up in ``bot.active_views`` by
    game id, not captured — because a round game swaps in a new view every
    round and the panel outlives them. The method keeps its own gate: the
    panel is only ever sent to someone who passed one, but a gate on the
    action is the one that counts. A press with no live view of
    *view_type* gets :data:`GAME_ENDED_REPLY` rather than acting on a dead
    game.

    *handlers* maps a button's ``custom_id`` to the name of the view method
    that takes the interaction.
    """

    def __init__(
        self,
        bot: Any,
        game_id: str,
        items: list[discord.ui.Item[Any]],
        handlers: dict[str, str],
        *,
        view_type: type | tuple[type, ...],
    ) -> None:
        super().__init__()
        self.bot = bot
        self.game_id = game_id
        self.view_type = view_type
        for item in items:
            self.add_item(item)
        for custom_id, method in handlers.items():
            bind(self, custom_id, self._handler(method))

    def _handler(self, method: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _press(interaction: discord.Interaction) -> None:
            view = self.bot.active_views.get(self.game_id)
            action = getattr(view, method, None) if isinstance(view, self.view_type) else None
            if action is None:
                await interaction.response.send_message(GAME_ENDED_REPLY, ephemeral=True)
                return
            await action(interaction)

        return _press


class CallbackHostPanel(HostPanelView):
    """An ephemeral host panel whose buttons call straight into a cog.

    :class:`GameHostPanel` finds the game's live *view* in
    ``bot.active_views``; Mahjong and Risky Rolls have none (tables live in
    their own rows, rounds in ``risky_roll.state``), so their buttons name an
    async ``(interaction, panel)`` callback instead. The callback keeps the
    action's own gate, as every host panel does, and ``panel.stop()``s the
    panel when it replaces its message with a finished card
    (``ExpiringLayoutView`` contract). Same :data:`HOST_PANEL_TIMEOUT`,
    counted from when the panel was sent (:class:`HostPanelView`).
    """

    def __init__(
        self,
        items: list[discord.ui.Item[Any]],
        handlers: dict[str, Callable[[discord.Interaction, CallbackHostPanel], Awaitable[None]]],
    ) -> None:
        super().__init__()
        for item in items:
            self.add_item(item)
        for custom_id, callback in handlers.items():
            bind(self, custom_id, self._handler(callback))

    def _handler(
        self, callback: Callable[[discord.Interaction, CallbackHostPanel], Awaitable[None]],
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _press(interaction: discord.Interaction) -> None:
            await callback(interaction, self)

        return _press


async def hand_over(
    bot: Any, interaction: discord.Interaction, opener: HostPanelOpener, game_id: str | None,
) -> None:
    """The slash launch's hand-off: send the host their panel right after
    the game starts, rather than make them go find :data:`HOST_COMMAND`.
    Nothing is sent for a launch that produced no live game, and a failure
    is logged, not raised — the game is already running."""
    if not game_id or game_id not in bot.active_views:
        return
    try:
        await opener(interaction, game_id)
    except discord.HTTPException:
        log.info("host panel not delivered for %s", game_id)
