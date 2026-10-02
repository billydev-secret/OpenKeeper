"""One live game's control strip, kept at the bottom of its own channel.

A *bar* is a thin line of text plus a row of buttons that has to stay
reachable while the channel fills up underneath it. It is a ``StickyPanel``
like a game board, with two differences that are the whole reason this module
is separate from ``game_board_sticky_service``:

* **It is keyed per game, not per guild.** Two games can be live in two
  channels of the same server and each bar follows its own conversation, so
  the panel's id hooks close over the ``game_id`` and ignore the guild id
  they are handed.
* **It is not on the Live Game Boards dial.** A board that stops following
  the chat is still a readable board further up; a bar that stops following
  is a row of buttons nobody can reach. There is no version of a bar to turn
  off, so there is nothing for a dial to preserve.

AMA's bar was the first (``games_ama/bottom_bar_service``, which still owns
AMA's own line of text and payload key and calls straight through to here);
Truth or Dare's opt-in strip was the second, and is what moved the
game-agnostic two thirds of that module here rather than copying it. What is
game-specific is exactly three things: the payload key the bar's message id
is stored under, the panel's log name, and how its content is built.

The bar's id lives in the game payload, so a restart finds the bar it left
behind instead of posting a second one. ``set_bar_location`` writes it with a
single ``json_set`` rather than a read-modify-write: the async side of the
payload (``modify_payload``) is a Python read-modify-write on its own
connection, and the narrower this write is, the less of it there is to
clobber. It is not airtight — an async writer that read before us and writes
after still wins — but the consequence is self-healing: a stale id makes the
next re-post delete a message that is already gone, which ``StickyPanel``
treats as the ordinary case.

This module never constructs a :class:`~bot_modules.core.sticky.PanelContent`
itself — *build* is the caller's own function, so it was already agnostic to
whatever shape the bar takes. A Components V2 bar (Components V2 wave 3b)
hands ``make_bar_panel`` a *build* that returns ``PanelContent(layout=...)``
instead of ``embed=`` or ``content=``; nothing here has to change for it to
flow straight through ``StickyPanel.place``/``refresh``. The two
content-only callers that predate any of this (``games_traditional_cog.py``'s
bar and ``games/ama/bottom_bar_service.py``) are untouched either way.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from bot_modules.core.sticky import PanelContent, StickyPanel

log = logging.getLogger(__name__)

#: A game can be launched in a thread as well as a text channel, so a bar
#: follows both. An adopting cog forwards both delete events to match.
BAR_TARGETS = (discord.TextChannel, discord.Thread)


def _payload_of(row: Any) -> dict:
    try:
        return json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}


def bar_location(bot: Any, game_id: str, id_key: str) -> tuple[int, int]:
    """``StickyPanel.load_ids`` for one game's bar: where it is right now.

    The channel is the game's own — a bar cannot be moved to another channel,
    so this reads it off the live row rather than storing a second copy that
    could disagree. A game that has ended has no row, and reports nowhere,
    which is what makes every late re-stick a no-op.

    Synchronous because ``StickyPanel`` calls its id hooks off-thread;
    ``game_manager``'s payload accessors are async, which is why this pair
    lives here rather than there (the same reason ``board_location`` does).
    """
    with bot.ctx.open_db() as conn:
        row = conn.execute(
            "SELECT channel_id, payload FROM games_active_games WHERE game_id = ?",
            (game_id,),
        ).fetchone()
    if row is None:
        return 0, 0
    return int(row["channel_id"] or 0), int(_payload_of(row).get(id_key) or 0)


def set_bar_location(bot: Any, game_id: str, message_id: int, id_key: str) -> None:
    """``StickyPanel.save_ids`` — stamp the bar's new id into the payload.

    A falsy *message_id* stores NULL, which is how taking a bar down clears
    the stored id. The channel a caller is handed is ignored — see
    :func:`bar_location`.
    """
    with bot.ctx.open_db() as conn:
        conn.execute(
            # COALESCE/NULLIF so a row with a NULL or empty payload gets a
            # fresh object instead of json_set returning NULL over the top of
            # it; json_valid so a payload we cannot parse is left alone rather
            # than raising out of a worker thread.
            "UPDATE games_active_games"
            "   SET payload = json_set(COALESCE(NULLIF(payload, ''), '{}'), "
            f"       '$.{id_key}', ?)"
            " WHERE game_id = ?"
            "   AND json_valid(COALESCE(NULLIF(payload, ''), '{}'))",
            (message_id or None, game_id),
        )


def make_bar_panel(
    bot: Any,
    game_id: str,
    *,
    name: str,
    id_key: str,
    build: Callable[[discord.Guild], Awaitable[PanelContent]],
) -> StickyPanel:
    """A ``StickyPanel`` for one live game's bar.

    One instance per game, never one per guild. *name* is what the failure
    ceiling's log line prints, so it names the game instance rather than the
    game type — it is the only place an admin sees which of two live rooms
    the bot cannot post in.

    ``restick_on_bot`` is on because in these games the bot *is* what buries
    the bar: every question card, every rotation announcement is ours. A bar
    that only followed member chat would sink under its own game.
    """

    def _load(_guild_id: int) -> tuple[int, int]:
        return bar_location(bot, game_id, id_key)

    def _save(_guild_id: int, _channel_id: int, message_id: int) -> None:
        set_bar_location(bot, game_id, message_id, id_key)

    return StickyPanel(
        name,
        bot,
        load_ids=_load,
        save_ids=_save,
        build=build,
        restick_on_bot=True,
        target_types=BAR_TARGETS,
    )


async def retire_bar(bars: Any, game_id: str, guild: Any, *, label: str) -> None:
    """Take one game's bar down for good.

    ``unpost`` is the panel's own teardown: it deletes the bar under the
    placement lock — so a re-stick already queued behind it cannot put the bar
    back up — retries a transient delete failure in the background, and zeroes
    the stored id, which is what makes every later refresh and every later
    re-stick a no-op.

    The order matters more than it looks. *bars* is released **first and
    unconditionally**, because that is what cancels the re-sticks already
    queued; only then does a missing *guild* cut the call short. A guild of
    ``None`` means the channel the bar lives in is gone, so there is nothing
    to delete the message through — and the stored id dies with the game row
    anyway.

    A delete that still fails after the shared retry (~40s) is logged, never
    raised: a bar left on screen is a row of dead buttons, but a raise here
    would take the end-of-game path down with it. *label* names the game type
    in that line, since it is the only clue an admin gets about which room the
    bot could not clean up.
    """
    panel = bars.release(game_id)
    if panel is None:
        return
    if guild is None:
        return
    try:
        await panel.unpost(guild)
    except Exception:  # noqa: BLE001 — logged, never raised by design (see docstring): a raise here would take the end-of-game path down with it
        log.warning(
            "%s: could not remove the bottom bar for game %s",
            label, game_id, exc_info=True,
        )
