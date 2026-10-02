"""AMA's one-line bottom bar, kept at the bottom of the channel.

An AMA's questions are asked through a button, so the bar carrying that button
has to stay reachable while the channel fills up with answers. It did that with
its own delete-and-re-post loop: a leading-edge 2 s debounce per channel, a
``_suppress_resend`` flag for the flows that posted a system message and edited
the bar in the same breath, and a live ``discord.Message`` handle on the view as
the only record of where the bar was. ``docs/plans/sticky-panel-extraction.md``
filed it as out of family — the shared ``StickyPanel`` is keyed per guild and
the bar's state is per *game instance*, with its id in a games payload blob and
no ``(guild_id) -> (channel_id, message_id)`` pair to hand a ``load_ids``
callback.

The nine games that joined the Live Game Boards dial answered that: a panel per
*game*, its id hooks closed over the ``game_id`` and the guild id ignored, is
the shape this module uses too. What it does not share with them is the dial —
the bar has always followed the conversation, so keeping it at the bottom is
not a setting and this migration is unconditional.

That per-game shape now lives in ``services/game_bottom_bar_service``, which
Truth or Dare's opt-in strip shares (2026-09-20). What stays here is what is
actually AMA's: the payload key its id is stored under, and the one line of
text the bar renders. The three names below are the module's public surface
and keep their signatures — the cog and this module's tests both use them.

Three things the hand-rolled loop did not have come with the move:

* **post-before-delete** — the bar was deleted first, so a send that failed
  (the bot lost Send Messages mid-AMA) left the game with no bar at all and no
  way back to it;
* **a failure ceiling** — a channel the bot cannot post in stops being retried
  on every burst of chat;
* **placement-registry membership** — the bar no longer trades the bottom slot
  back and forth with another sticky panel in the same channel.

And one thing goes the other way, deliberately: ``_suppress_resend`` is gone.
Its real job was self-loop protection, which ``StickyPanel`` owns properly
(``was_placed``, the at-the-bottom guard, placements atomic to cancellation).
What is left of it was a quiet window around a rotation announcement, and a bar
that stays *above* the "a new host is in the hot seat" it was just edited for
is the wrong outcome for a bottom bar. It now follows that announcement down
like anything else.
"""

from __future__ import annotations

from typing import Any, Protocol

import discord

from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.games.ama.logic import bottom_bar_text
from bot_modules.games.bottom_bar_service import (
    bar_location as _bar_location,
    make_bar_panel as _make_bar_panel,
    set_bar_location as _set_bar_location,
)

#: Where the bar's message id lives in the game payload. Already the key the
#: pre-sticky bar wrote and crash recovery read, so nothing needs migrating.
BAR_ID_KEY = "bottom_message_id"


class AMABarView(Protocol):
    """What the bar's ``build`` reads off the live AMA view.

    Every field is already on ``AMAView`` — the bar's line has always been a
    render of the AMA's in-memory state rather than of anything stored.
    """

    game_format: str
    queue: list[int]
    panel: list[int]
    _hot_seat_name: str | None


def bar_location(bot: Any, game_id: str) -> tuple[int, int]:
    """Where one AMA's bar is right now, for ``StickyPanel.load_ids``.

    The message id is :data:`BAR_ID_KEY` in the game payload — already the
    key the pre-sticky bar wrote and crash recovery read, so nothing needed
    migrating.
    """
    return _bar_location(bot, game_id, BAR_ID_KEY)


def set_bar_location(bot: Any, game_id: str, channel_id: int, message_id: int) -> None:
    """Stamp the bar's new id into the payload, for ``StickyPanel.save_ids``.

    *channel_id* is accepted and ignored — see :func:`bar_location`.
    """
    _set_bar_location(bot, game_id, message_id, BAR_ID_KEY)


def build_bar_content(
    view: AMABarView, bottom_view: discord.ui.View
) -> PanelContent:
    """``StickyPanel.build`` for the bar: one line of text and three buttons.

    Text, not an embed — the bar is meant to read as a control strip under the
    conversation, and an embed would box and indent it. ``signature`` is the
    line itself, which makes a redraw that would change nothing cost no API
    call at all: the bar is refreshed on every volunteer, every queue change
    and every rotation, and most of those leave the line identical.
    """
    label = bottom_bar_text(
        view.game_format,
        hot_seat_name=view._hot_seat_name,
        queue_len=len(view.queue),
        panel_len=len(view.panel),
    )
    return PanelContent(content=label, view=bottom_view, signature=label)


def make_bar_panel(
    bot: Any,
    game_id: str,
    view: AMABarView,
    bottom_view: discord.ui.View,
) -> StickyPanel:
    """A ``StickyPanel`` for one live AMA's bar.

    One instance per AMA, never one per guild: two AMAs can run in two
    channels of the same server, and each bar follows its own conversation.
    The placement, the id hooks and the ``restick_on_bot`` choice all come
    from the shared bar module; what is AMA's is the line of text.
    """

    async def _build(_guild: discord.Guild) -> PanelContent:
        return build_bar_content(view, bottom_view)

    return _make_bar_panel(
        bot,
        game_id,
        name=f"ama bar {game_id}",
        id_key=BAR_ID_KEY,
        build=_build,
    )
