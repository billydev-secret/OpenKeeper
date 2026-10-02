"""Discord UI views shared across all duel game types."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Protocol, Sequence, cast

import discord

from bot_modules.core.layout import ExpiringLayoutView, disable_all
from bot_modules.core.utils import disable_all_items
from bot_modules.duels.db import CHALLENGE_RESPONSE_SECONDS, REMATCH_WINDOW_SECONDS
from bot_modules.duels.layouts import card_art
from bot_modules.core.branding_service import PanelArt

log = logging.getLogger("dungeonkeeper.duels")


class DuelLayoutView(Protocol):
    """The Components V2 shape ``BaseGame.build_game_view`` returns once a
    mini-game's hooks return items instead of an embed — the seam wave 3b's
    six mini-game cogs convert through (``docs/specs/dk_pvp_games_suite_spec.md``
    "Card format").

    A concrete implementation is a persistent ``discord.ui.LayoutView``
    (``timeout=None``, re-attached across a restart the same way a plain
    ``discord.ui.View`` is today) whose ``__init__`` builds the mini-game's
    control row — its persistent buttons, carrying the *same* frozen
    ``custom_id``s the embed-arm view uses, in **one**
    ``discord.ui.ActionRow`` (a bare ``Button`` cannot sit at the top level
    of a Components V2 message — see the style guide § Anatomy). The base
    never builds that row itself; it only ever calls :meth:`render`.

    ``render(items)`` resets the view to *items* — the mini-game's own
    ``TextDisplay``/``Section``/``Separator``/``MediaGallery`` content, no
    controls — then re-adds the control row last, so a re-render can never
    duplicate or lose the buttons. The base calls it every time a card is
    (re)posted: at accept, at a lobby's start, and on every "continue" edit.

    There is no ``disable()``: a game board is never greyed out, because the
    result replaces it in the same message (``BaseGame._show_result``,
    spec "One card").
    """

    def render(self, items: Sequence[discord.ui.Item[Any]]) -> None: ...


class FramedCard:
    """The one ``render`` every PvP card view shares: the guild's lobby
    header, the card's *items*, the control row, then the footer — so a
    game stays one framed card from challenge or lobby to result
    (``docs/specs/dk_pvp_games_suite_spec.md`` "One card").

    A mixin, like :class:`_ResultButtons`, for the same reason: the views it
    serves sit on :class:`discord.ui.LayoutView` and on
    :class:`ExpiringLayoutView`, and both expose ``add_item``/``remove_item``
    the same way. A view sets ``_control_row`` in ``__init__``; the owning
    cog calls :meth:`frame` with the game's art (``BaseGame._frame``).
    Without it the card renders bare, exactly as before.
    """

    _control_row: discord.ui.ActionRow[Any] | None = None
    _art: PanelArt | None = None
    _art_title: str = ""

    def frame(self, art: PanelArt | None, title: str) -> Any:
        """Set the art :meth:`render` draws around every re-render; returns
        the view so a builder can ``return View(...).frame(...)``."""
        self._art, self._art_title = art, title
        return self

    def render(self, items: Sequence[discord.ui.Item[Any]]) -> None:
        """Reset to *items* inside the frame. The control row goes last
        before the footer — unless *items* already placed it (the lobby's
        ``build_lobby_layout(controls=...)``, which draws its own art)."""
        view = cast(discord.ui.LayoutView, self)
        for item in list(view.children):
            view.remove_item(item)
        top, bottom = card_art(self._art, self._art_title)
        for item in (*top, *items):
            view.add_item(item)
        row = self._control_row
        if row is not None and not any(item is row for item in items):
            view.add_item(row)
        for item in bottom:
            view.add_item(item)

#: What a late presser is told. One literal for the two places that say it —
#: the stale-state reply in ``BaseDuel`` and the deadline check on a view
#: re-attached after a restart — so the number can't drift between them.
CHALLENGE_TIMED_OUT_TEXT = (
    "⏱️ That challenge timed out before you pressed — challenges expire "
    f"{CHALLENGE_RESPONSE_SECONDS // 60} minutes after they're posted. "
    "Ask them to send another one."
)


class ChallengeView(FramedCard, ExpiringLayoutView):
    """Accept/Decline card — target only. Implements
    :class:`DuelLayoutView`: :meth:`render` lays the challenge card's items
    (:func:`duels.layouts.build_challenge_layout`) then appends the
    Accept/Decline row last, so a re-render (there is none today, but the
    Protocol requires it) can never duplicate the buttons.

    Fresh from a challenge it is NOT persistent: the timeout is
    :data:`CHALLENGE_RESPONSE_SECONDS`, the same number the card counts down
    to and the expiry sweep uses — :class:`ExpiringLayoutView`'s own
    ``on_timeout`` greys the row out in place once it fires, which needs the
    caller to set ``view.message`` right after sending (see
    ``base_duel.py``'s ``_base_challenge``). Re-attached after a restart
    (``deadline`` given) it is persistent — ``bot.add_view`` accepts nothing
    else — and enforces the card's original deadline itself, refusing a
    press past it with :data:`CHALLENGE_TIMED_OUT_TEXT`.
    """

    def __init__(
        self,
        game_id: int,
        target_id: int,
        on_accept: Callable[[discord.Interaction, int], Awaitable[None]],
        on_decline: Callable[[discord.Interaction, int], Awaitable[None]],
        *,
        deadline: float | None = None,
    ) -> None:
        super().__init__(timeout=None if deadline is not None else CHALLENGE_RESPONSE_SECONDS)
        self.game_id = game_id
        self.target_id = target_id
        self.deadline = deadline
        self._on_accept = on_accept
        self._on_decline = on_decline
        self._resolved = False

        accept_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"challenge_accept:{game_id}",
        )
        accept_btn.callback = self._accept_callback

        decline_btn: discord.ui.Button[Any] = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"challenge_decline:{game_id}",
        )
        decline_btn.callback = self._decline_callback

        self._control_row = discord.ui.ActionRow(accept_btn, decline_btn)
        self.add_item(self._control_row)

    def _resolve(self) -> None:
        """Retire the response window without going deaf.

        The obvious move here is ``stop()``, and it was — but discord.py's
        ``stop()`` also calls the store's ``remove_view``, which deletes
        ``challenge_accept:``/``challenge_decline:`` from the message's entry
        in ``ViewStore``. The accept handler is awaited *after* that, so for
        the second or so it takes to go ACTIVE and repaint, a second press —
        an impatient player, or one whose client hasn't repainted yet — is
        dropped by ``dispatch_view`` with no log line and no reply, and
        Discord shows a red "This interaction failed". That is what
        "It's not letting me accept" looks like from the other side (The
        Golden Meadow, 2026-09-28, pressure game 98).

        ``base_duel`` already knows what to say to a press that lost the race
        (``_stale_challenge_message`` — "That challenge has already been
        accepted — the game is running"); it just never got the chance. So
        retire the timer instead of the registration: assigning ``timeout``
        makes discord.py's timer task exit on its next wake, and its
        ``timeout is None`` check comes *before* the expiry check, so it
        exits cleanly rather than firing ``on_timeout``. The view stays in
        the store for the life of the process — one dict entry per game,
        alongside the game view that is already permanent.
        """
        self._resolved = True
        self.timeout = None

    async def on_timeout(self) -> None:
        # Belt and braces for the above: a resolved challenge must never
        # repaint its greyed-out Accept/Decline row over the live game board
        # that replaced it on this same card.
        if self._resolved:
            return
        await super().on_timeout()

    def _disable_all(self) -> None:
        disable_all(self)

    async def _refuse_if_past_deadline(self, interaction: discord.Interaction) -> bool:
        """True (and answered) when a re-attached view's deadline has passed."""
        if self.deadline is None or time.time() <= self.deadline:
            return False
        await interaction.response.send_message(CHALLENGE_TIMED_OUT_TEXT, ephemeral=True)
        return True

    async def _accept_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s accepted challenge (game %d)", interaction.user.display_name, self.game_id)
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "❌ Only the challenged player can accept.", ephemeral=True
            )
            return
        if await self._refuse_if_past_deadline(interaction):
            return
        self._resolve()
        self._disable_all()
        await self._on_accept(interaction, self.game_id)

    async def _decline_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s declined challenge (game %d)", interaction.user.display_name, self.game_id)
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "❌ Only the challenged player can decline.", ephemeral=True
            )
            return
        if await self._refuse_if_past_deadline(interaction):
            return
        self._resolve()
        self._disable_all()
        await self._on_decline(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("ChallengeView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


#: What a late Run It Back presser is told.
REMATCH_EXPIRED_TEXT = (
    "❌ That result is too old to run back — the button works for "
    f"{REMATCH_WINDOW_SECONDS // 60} minutes after a game ends. "
    "Start a fresh one with the command."
)


class _ResultButtons:
    """Button construction, callbacks and error handling shared by
    :class:`ResultView` (embed arm) and :class:`ResultLayoutView` (layout
    arm), so wave 3b's two result-card shapes can never drift on what their
    buttons do — the guide's requirement for the layout twin.

    Not a view itself: ``discord.ui.View`` and ``discord.ui.LayoutView``
    share no public base worth building on, but both expose ``add_item``
    the same way, so a plain mixin that calls ``self.add_item`` works
    unmodified whichever concrete view it ends up on.
    """

    game_id: int
    winner_id: int
    loser_id: int
    rematch_deadline: float | None
    _on_set_nick: Callable[[discord.Interaction, int], Awaitable[None]] | None
    _on_rematch: Callable[[discord.Interaction, int], Awaitable[None]] | None

    def _init_result_state(
        self,
        game_id: int,
        winner_id: int,
        loser_id: int,
        on_set_nick: Callable[[discord.Interaction, int], Awaitable[None]] | None,
        *,
        on_rematch: Callable[[discord.Interaction, int], Awaitable[None]] | None = None,
        rematch_deadline: float | None = None,
    ) -> None:
        self.game_id = game_id
        self.winner_id = winner_id
        self.loser_id = loser_id
        self._on_set_nick = on_set_nick
        self._on_rematch = on_rematch
        self.rematch_deadline = rematch_deadline

    def _build_result_buttons(self) -> list[discord.ui.Button[Any]]:
        """``📝 Name the Loser`` (winner-only, nickname games only) and
        ``🔁 Run It Back`` (either duelist, or the lobby host — the handler
        decides), in that order — the same two buttons, same ``custom_id``s,
        on either arm. ``rematch_deadline`` of ``None`` omits the rematch
        button (a card re-attached after the window has already closed, or
        the disabled twin of one built past it)."""
        buttons: list[discord.ui.Button[Any]] = []
        if self._on_set_nick is not None:
            nick_btn: discord.ui.Button[Any] = discord.ui.Button(
                label="Name the Loser",
                style=discord.ButtonStyle.primary,
                emoji="📝",
                custom_id=f"set_nick:{self.game_id}",
            )
            nick_btn.callback = self._set_nick_callback
            buttons.append(nick_btn)

        if self._on_rematch is not None and self.rematch_deadline is not None:
            rematch_btn: discord.ui.Button[Any] = discord.ui.Button(
                label="Run It Back",
                style=discord.ButtonStyle.secondary,
                emoji="🔁",
                custom_id=f"rematch:{self.game_id}",
            )
            rematch_btn.callback = self._rematch_callback
            buttons.append(rematch_btn)
        return buttons

    async def _set_nick_callback(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s pressed Name the loser (game %d)", interaction.user.display_name, self.game_id
        )
        if interaction.user.id != self.winner_id:
            await interaction.response.send_message(
                "❌ Only the winner can name the loser.", ephemeral=True
            )
            return
        assert self._on_set_nick is not None
        await self._on_set_nick(interaction, self.game_id)

    async def _rematch_callback(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s pressed Run It Back (game %d)", interaction.user.display_name, self.game_id
        )
        if self.rematch_deadline is not None and time.time() > self.rematch_deadline:
            await interaction.response.send_message(REMATCH_EXPIRED_TEXT, ephemeral=True)
            return
        assert self._on_rematch is not None
        await self._on_rematch(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item, /
    ) -> None:
        log.exception(
            "%s error (game %d)", type(self).__name__, self.game_id, exc_info=error
        )
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


class ResultView(discord.ui.View, _ResultButtons):
    """Post-game embed-arm buttons — persistent (timeout=None). See
    :class:`_ResultButtons` for what the buttons do; :class:`ResultLayoutView`
    is the Components V2 twin, for a mini-game whose ``render_result_state``
    returns items instead of an embed.
    """

    def __init__(
        self,
        game_id: int,
        winner_id: int,
        loser_id: int,
        on_set_nick: Callable[[discord.Interaction, int], Awaitable[None]] | None,
        *,
        on_rematch: Callable[[discord.Interaction, int], Awaitable[None]] | None = None,
        rematch_deadline: float | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._init_result_state(
            game_id, winner_id, loser_id, on_set_nick,
            on_rematch=on_rematch, rematch_deadline=rematch_deadline,
        )
        for button in self._build_result_buttons():
            self.add_item(button)

    def disable(self) -> None:
        disable_all_items(self)


class ResultLayoutView(discord.ui.LayoutView, _ResultButtons):
    """The Components V2 twin of :class:`ResultView` — same ``custom_id``s,
    same callbacks (:class:`_ResultButtons`), same ``disable()`` contract,
    persistent (timeout=None). Paradigm 5, decision card: facts (the
    winner/loser line + the mini-game's own result items), then 1-2 decision buttons in
    the bottom row; ``disable()`` is this card's version of the paradigm's
    "edits into a receipt" step (greyed rather than removed, since Name the
    Loser/Run It Back can still be worth reading after they stop working).

    Unlike :class:`ResultView`, the card's own content lives inside this
    view rather than beside it in an embed: *content* is the composed body
    (the winner/loser line, then the mini-game's items — see
    ``BaseGame._result_layout``), added first, with the button row — one
    ``discord.ui.ActionRow``, since a bare ``Button`` cannot sit at a V2
    message's top level — after it, and omitted entirely when there is
    nothing left to press (a NICKED card past its rematch window). ``art``
    and ``title`` frame the whole card (:func:`duels.layouts.card_art`): the
    result is edited into the game's own card, so it keeps its header and
    footer, the footer below the buttons. Built
    fresh per result post or edit; never mutated in place, the same choice
    Auctions' live board made for a card this cheap to rebuild whole.
    """

    def __init__(
        self,
        game_id: int,
        winner_id: int,
        loser_id: int,
        on_set_nick: Callable[[discord.Interaction, int], Awaitable[None]] | None,
        *,
        on_rematch: Callable[[discord.Interaction, int], Awaitable[None]] | None = None,
        rematch_deadline: float | None = None,
        content: Sequence[discord.ui.Item[Any]] = (),
        art: PanelArt | None = None,
        title: str = "",
    ) -> None:
        super().__init__(timeout=None)
        self._init_result_state(
            game_id, winner_id, loser_id, on_set_nick,
            on_rematch=on_rematch, rematch_deadline=rematch_deadline,
        )
        top, bottom = card_art(art, title)
        for item in (*top, *content):
            self.add_item(item)
        buttons = self._build_result_buttons()
        if buttons:
            self.add_item(discord.ui.ActionRow(*buttons))
        for item in bottom:
            self.add_item(item)

    def disable(self) -> None:
        disable_all(self)
