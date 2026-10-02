"""The Help & Settings hub's 💤 row must send a card whose Start button
actually answers.

discord.py looks a press up in the view stored for the pressed message before
it falls back to the persistent views registered with ``bot.add_view``. The
hub used to send a control-free card whose Start button carried the
persistent ``break:start`` id and no callback, so every press from the hub
ran ``Item.callback``'s no-op and a member saw "didn't respond in time"
(2026-09-30). The row now sends ``BreakPanelView`` itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui.view import ViewStore

from bot_modules.breaks.cog import BreakPanelView
from bot_modules.breaks.layouts import START_CUSTOM_ID
from bot_modules.server_features.cog import ServerFeaturesHubView

_KEY = (discord.ComponentType.button.value, START_CUSTOM_ID)


def _start_item(view: discord.ui.LayoutView):
    return next(c for c in view.walk_children() if getattr(c, "custom_id", None) == START_CUSTOM_ID)


def _hub(breaks_cog) -> ServerFeaturesHubView:
    """The posted hub view, with a bot that holds (or lacks) BreaksCog."""
    bot = MagicMock()
    bot.get_cog = lambda name: breaks_cog if name == "BreaksCog" else None
    return ServerFeaturesHubView(SimpleNamespace(bot=bot))


@pytest.mark.asyncio
async def test_hub_break_card_start_button_is_bound():
    breaks_cog = SimpleNamespace(bot=MagicMock())
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    await _hub(breaks_cog).open_break(interaction)

    interaction.response.send_message.assert_awaited_once()
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    assert isinstance(view, BreakPanelView)
    item = _start_item(view)
    assert item.callback.__func__ is not discord.ui.Item.callback
    # Plain, as the spec says: the hub's copy carries no banner and no footer.
    assert not any(isinstance(c, discord.ui.MediaGallery) for c in view.walk_children())


@pytest.mark.asyncio
async def test_hub_break_card_press_reaches_the_break_handler_not_a_no_op():
    """The store lookup discord.py 2.7.1 performs, against the real views:
    the ephemeral card's own item wins over the persistent one, so it must
    carry the handler itself."""
    breaks_cog = SimpleNamespace(bot=MagicMock())
    store = ViewStore(SimpleNamespace(loop=None, store_view=lambda *a, **k: None))
    store.add_view(BreakPanelView(breaks_cog))  # what BreaksCog.cog_load registers
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    await _hub(breaks_cog).open_break(interaction)
    sent = interaction.response.send_message.await_args.kwargs["view"]
    store.add_view(sent, 123456)  # what send_message(ephemeral=True) does

    picked = store._views[123456][_KEY]
    assert picked.callback.__func__ is not discord.ui.Item.callback
    assert picked.callback.__func__ is BreakPanelView.start


@pytest.mark.asyncio
async def test_hub_break_row_says_unavailable_without_the_cog():
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done.return_value = False
    await _hub(None).open_break(interaction)
    args, kwargs = interaction.response.send_message.await_args
    assert "view" not in kwargs and kwargs["ephemeral"] is True
    assert args[0].startswith("❌")
