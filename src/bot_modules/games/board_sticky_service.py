"""Per-guild dial: keep a live game's board pinned to the channel's bottom.

Off by default (ship-dark). When a guild turns it on, a live game's board
re-posts itself to the bottom of the channel as chat buries it, for as long as
that board is live, then goes dormant the moment the game retires it. One dial
covers every game that wires itself up to it; see
``docs/plans/sticky-panel-extraction.md`` for the site survey and which games
have adopted it so far.

The dial itself follows ``advisor_service``'s ``ADVISOR_CONTEXT_KEY``: a single
boolean in the shared ``config`` table, scoped per guild.

This module also owns the **wiring** every adopting cog needs, so a new game
is a view and two calls rather than another copy of it: the dial read, the
board's location in ``games_active_games``, the ``StickyPanel`` factory, the
open/retire pair (plus ``unstick_game_board``, for a board that comes off the
dial while its game plays on), and — in ``LiveBoards`` at the foot of this
module — the per-cog bookkeeping that sits around them. Mt. Rushmore Draft and Name Your
Price both went through a hand-rolled copy of the functions first; the copies
were identical apart from the view's type name, which is what moved them here.
``LiveBoards`` followed for the same reason when nine more games adopted the
dial on 2026-09-12.

Two rules are load-bearing and easy to lose if this is ever re-inlined:

* ``build`` **raises** once a board is retired rather than returning ``None``.
  ``StickyPanel._place_locked`` does not check its build result, so ``None``
  would explode inside core; raising is the established adapter idiom (see
  ``economy_cog._build_auction_panel``).
* ``forget()`` is called at retire as well, because an ``on_message`` already
  in flight when the game ends can otherwise still read the panel's cached ids
  and schedule a restick that reaches the refusal above — which core logs as an
  ERROR traceback for a perfectly ordinary game ending. See
  ``auction_views._release_panel``, which does the same thing for the same
  reason.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Protocol, cast

import discord

from bot_modules.core.db_utils import get_config_value, parse_bool, set_config_value
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.games.utils.game_manager import update_game_message
from bot_modules.games.utils.send_retry import retry_transient

#: Off by default — a merge (and even a restart) changes nothing until an
#: admin ticks this on Games Global Config.
BOARD_STICKY_KEY = "games_board_sticky_enabled"


def get_board_sticky_enabled(conn: sqlite3.Connection, guild_id: int = 0) -> bool:
    """Whether live game boards should stay pinned to the channel's bottom."""
    return parse_bool(get_config_value(conn, BOARD_STICKY_KEY, "0", guild_id), False)


def set_board_sticky_enabled(
    conn: sqlite3.Connection, enabled: bool, guild_id: int = 0
) -> None:
    set_config_value(conn, BOARD_STICKY_KEY, "1" if enabled else "0", guild_id)


class LiveBoardView(Protocol):
    """What a game's view must expose to be stuck to the channel bottom.

    Both adopters are ``discord.ui.View`` subclasses that already had all of
    this except the last two, which the sticky wiring adds.
    """

    game_id: str
    guild: discord.Guild | None
    #: Set by ``open_game_board`` when the dial is on, so the view's own
    #: redraw seam knows to route through the panel instead of a cached
    #: message that a repost may have orphaned.
    _panel: StickyPanel | None
    #: Set by ``retire_game_board``. Distinct from a game's own ``_closed``,
    #: which is set one *legitimate* render earlier (the disabled frame).
    _board_retired: bool

    def _build_embed(self) -> discord.Embed: ...


class LiveBoardLayoutView(Protocol):
    """The Components V2 sibling of :class:`LiveBoardView`.

    A game whose board is a bare ``discord.ui.LayoutView`` implements this
    instead of the embed Protocol above — same three fields, and
    ``_rebuild`` (clear the view's own items and re-``add_item`` them from
    the game's pure builder) standing in for ``_build_embed``. There is no
    fresh object to hand ``PanelContent``: the view *is* the message body,
    so redrawing it means mutating it in place before it is re-sent.

    ``build_board_content`` and the two ``message.edit`` fallbacks below
    branch on ``isinstance(view, discord.ui.LayoutView)`` at runtime rather
    than widening ``LiveBoardView`` itself to cover both shapes. That keeps
    every one of the twelve cogs still typed against the embed Protocol
    compiling untouched (Components V2 wave 3b converts their boards one
    games group at a time), and a runtime check against a concrete
    discord.py class is simpler and more reliable than trying to make one
    Protocol's structural match conditional on which concrete subclass a
    caller happens to hold. This second Protocol retires once every board
    has converted and ``LiveBoardView``/``_build_embed`` go with it.
    """

    game_id: str
    guild: discord.Guild | None
    _panel: StickyPanel | None
    _board_retired: bool

    def _rebuild(self) -> None: ...


#: Either shape a live board's view may take. ``build_board_content`` and the
#: functions that lead to it accept this rather than ``LiveBoardView`` alone
#: — see :class:`LiveBoardLayoutView` for why the split is two Protocols
#: plus an isinstance check, not one Protocol widened to cover both.
AnyLiveBoardView = LiveBoardView | LiveBoardLayoutView


def _read_dial(bot: Any, guild_id: int) -> bool:
    with bot.ctx.open_db() as conn:
        return get_board_sticky_enabled(conn, guild_id)


async def board_sticky_enabled(bot: Any, guild: discord.Guild | None) -> bool:
    """Whether *guild* wants live boards stuck to the channel bottom.

    A ``None`` guild short-circuits before the DB read: ``StickyPanel.place``
    takes a non-optional ``discord.Guild`` and dereferences ``guild.id``
    immediately, so a ``None`` must never reach it. Callers read this once,
    when a board is first posted, so a mid-game flip of the dial never
    disturbs a board already running.
    """
    if guild is None:
        return False
    return await asyncio.to_thread(_read_dial, bot, guild.id)


def board_location(bot: Any, game_id: str) -> tuple[int, int]:
    """``StickyPanel.load_ids`` for one game: where its board is right now.

    Reads the *live* ``games_active_games`` row rather than a second store, so
    it can never drift from what ``update_game_message`` wrote or what the
    busy-check's jump link and ``/games end`` see. Synchronous because
    ``StickyPanel`` calls its id hooks off-thread; ``game_manager``'s own
    accessors are async, which is why this pair lives here rather than there.
    """
    with bot.ctx.open_db() as conn:
        row = conn.execute(
            "SELECT channel_id, message_id FROM games_active_games WHERE game_id = ?",
            (game_id,),
        ).fetchone()
    if row is None:
        return 0, 0
    return int(row["channel_id"] or 0), int(row["message_id"] or 0)


def set_board_location(
    bot: Any, game_id: str, channel_id: int, message_id: int
) -> None:
    """``StickyPanel.save_ids`` — the same table and columns
    ``update_game_message`` writes, so a repost keeps the jump link live."""
    with bot.ctx.open_db() as conn:
        conn.execute(
            "UPDATE games_active_games SET channel_id = ?, message_id = ? WHERE game_id = ?",
            (channel_id, message_id, game_id),
        )


class TransientBoardLocation:
    """Where each of one cog's boards is, held in memory.

    Most adopters' board *is* the message the game row names, so
    ``board_location`` reads and writes that row and the two can never
    drift. Fantasies & Dealbreakers is the exception: its row names the
    host's control panel, which ``recover_game`` re-registers after a
    restart, so a board id written there would hand the panel's message to
    the wrong view — and its round boards are deliberately untracked
    anyway, abandoned at a restart since long before this dial existed.
    Those keep their ids here instead, which is all the lifetime they need.

    Keyed by ``game_id`` like the panels themselves, so two games in one
    guild never see each other's board.
    """

    def __init__(self) -> None:
        self._by_game: dict[str, tuple[int, int]] = {}

    def load(self, game_id: str) -> tuple[int, int]:
        return self._by_game.get(game_id, (0, 0))

    def save(self, game_id: str, channel_id: int, message_id: int) -> None:
        self._by_game[game_id] = (channel_id, message_id)

    def forget(self, game_id: str) -> None:
        self._by_game.pop(game_id, None)


def build_board_content(name: str, view: AnyLiveBoardView) -> PanelContent:
    """``StickyPanel.build`` for a live board — or a refusal once it retires.

    Raising, not returning ``None``: see the module docstring's first rule.
    ``_board_retired`` is the flag this checks, never a game's own ``_closed``,
    which is set one *legitimate* render earlier (the disabled frame that
    still has to go out).

    A layout board has no fresh object to hand ``PanelContent`` the way a
    fresh embed is one — the view *is* the message body — so it is rebuilt
    in place first (see :class:`LiveBoardLayoutView`) and then handed over
    as-is.
    """
    if view._board_retired:
        raise RuntimeError(f"{name} is retired")
    if isinstance(view, discord.ui.LayoutView):
        cast(LiveBoardLayoutView, view)._rebuild()
        return PanelContent(layout=view)
    embed_view = cast(LiveBoardView, view)
    return PanelContent(embed=embed_view._build_embed(), view=cast(discord.ui.View, view))


def make_board_panel(
    bot: Any,
    name: str,
    game_id: str,
    view: AnyLiveBoardView,
    *,
    replaces_prior_board: bool = True,
    location: TransientBoardLocation | None = None,
) -> StickyPanel:
    """A ``StickyPanel`` for one game's (or one round's) live board.

    One instance per live board, never one shared per guild: ``StickyPanel``
    is keyed per guild, but games run per channel and a guild can have several
    going at once.

    *replaces_prior_board* is what the game row already names. It is true for
    an adopter whose board *moves* — Mt. Rushmore Draft turns its lobby
    message into the draft board, Name Your Price carries one message from
    round to round — so ``place()`` treats that message as the panel's own and
    removes it once the new board is safely up. It is false for an adopter
    that posts a **fresh** board per round and leaves the round that just
    closed on screen as its record (Never Have I Ever and the other timed
    round games): there, the message the row names belongs to the previous
    round, not to this panel, and deleting it would silently eat the game's
    history the moment a guild ticked the dial on.
    """
    #: Flipped by the first ``_save``. After that the game row names *this*
    #: panel's board, so reading it is right again — which matters because
    #: ``StickyPanel`` re-consults ``load_ids`` whenever its per-guild id
    #: cache expires, not only on the first placement.
    placed = False

    def _load(_guild_id: int) -> tuple[int, int]:
        if not replaces_prior_board and not placed:
            return 0, 0
        if location is not None:
            return location.load(game_id)
        return board_location(bot, game_id)

    def _save(_guild_id: int, channel_id: int, message_id: int) -> None:
        nonlocal placed
        placed = True
        if location is not None:
            location.save(game_id, channel_id, message_id)
            return
        set_board_location(bot, game_id, channel_id, message_id)

    async def _build(_guild: discord.Guild) -> PanelContent:
        return build_board_content(name, view)

    return StickyPanel(name, bot, load_ids=_load, save_ids=_save, build=_build)


async def open_game_board(
    bot: Any,
    db: Any,
    boards: dict[str, StickyPanel],
    name: str,
    game_id: str,
    view: AnyLiveBoardView,
    channel: Any,
    guild: discord.Guild | None,
    msg: discord.Message | None = None,
    *,
    location: TransientBoardLocation | None = None,
) -> discord.Message:
    """Post a live board — sticky when the guild's dial is on, the adopter's
    own pre-existing behaviour otherwise.

    *msg* is the message this board takes over: the lobby card Mt. Rushmore
    Draft turns into its draft board, or the one Name Your Price carries from
    round to round. With the dial off it is edited in place, exactly as the
    adopter did before it wired itself up here.

    ``None`` instead, for an adopter that posts a **fresh** board per round
    and leaves the closed one on screen as that round's record: with the dial
    off that is a plain ``channel.send``, and with it on the panel starts with
    no board of its own to move (see ``make_board_panel``).

    No explicit delete of the message being replaced: ``board_location`` reads
    it straight out of the game row, so ``place()`` already treats it as "the
    old panel" and removes it *after* the new board is safely up. Deleting it
    first would invert that — a placement failure (the bot lost Send Messages,
    say) would leave the game with neither the old message nor a board.
    """
    if guild is not None and channel is not None and await board_sticky_enabled(
        bot, guild
    ):
        panel = make_board_panel(
            bot,
            name,
            game_id,
            view,
            replaces_prior_board=msg is not None,
            location=location,
        )
        posted = await panel.place(guild, channel)
        if posted is not None:
            boards[game_id] = panel
            view._panel = panel
            return posted
        # Placement failed — the old message is untouched (place() never got
        # far enough to delete it). Fall through to the non-sticky path
        # rather than leave the game without a board.

    if isinstance(view, discord.ui.LayoutView):
        cast(LiveBoardLayoutView, view)._rebuild()
        if msg is not None:
            try:
                await msg.edit(content=None, embed=None, attachments=[], view=view)
                return msg
            except Exception:
                if channel is None:
                    raise
        if channel is None:
            raise ValueError(f"{name}: no channel to post the board in")
        new_msg = await retry_transient(
            lambda: channel.send(view=view), label=f"{name} board"
        )
        if location is None:
            await update_game_message(db, game_id, new_msg.id)
        return new_msg

    # ``view`` is a Protocol so this module need not import any game's view
    # class; every remaining (embed-shaped) adopter is a real
    # ``discord.ui.View``.
    ui_view = cast(discord.ui.View, view)
    embed = cast(LiveBoardView, view)._build_embed()
    if msg is not None:
        try:
            await msg.edit(embed=embed, view=ui_view)
            return msg
        except Exception:
            if channel is None:
                # Nothing left to try: no channel to post into and the edit
                # just failed. Let the caller's own error handling see it
                # rather than swallow it into a game with no board.
                raise
    if channel is None:
        raise ValueError(f"{name}: no channel to post the board in")
    new_msg = await retry_transient(lambda: channel.send(embed=embed, view=ui_view), label=f"{name} board")
    if location is None:
        # A tracked adopter's board *is* the message the game row names, so a
        # fresh post re-points it — the same write the adopter made itself
        # before it wired up here. An untracked adopter's row names something
        # else entirely (Fantasies' host control panel), and writing a board
        # id over it is exactly what ``TransientBoardLocation`` exists to
        # avoid; its dial-off boards stay untracked, as they always were.
        await update_game_message(db, game_id, new_msg.id)
    return new_msg


async def redraw_board(
    view: AnyLiveBoardView,
    message: discord.Message | discord.PartialMessage | None = None,
) -> None:
    """Redraw a live board wherever it actually is right now.

    The seam every mid-board redraw goes through once a game is on the dial.
    With the dial off this is the plain ``message.edit`` the adopter did
    before. With it on, *message* is a snapshot a restick may already have
    deleted, so the redraw goes through the panel instead — which re-reads
    the board's ids and, if the board has gone missing altogether, reposts
    it.

    The resurrection refusal is swallowed: a redraw that lands after the
    board retired has nothing left to draw.
    """
    if view._panel is not None:
        # Guarded the same way ``open_game_board`` guards its own guild.id
        # read — a panel only ever exists because ``guild`` was truthy at
        # that point, but nothing re-asserts that here.
        if view.guild is not None:
            try:
                await view._panel.refresh(view.guild.id)
            except RuntimeError:
                pass
        return
    if message is None:
        return
    if isinstance(view, discord.ui.LayoutView):
        cast(LiveBoardLayoutView, view)._rebuild()
        try:
            await message.edit(content=None, embed=None, attachments=[], view=view)
        except discord.HTTPException:
            pass
        return
    try:
        await message.edit(
            embed=cast(LiveBoardView, view)._build_embed(),
            view=cast(discord.ui.View, view),
        )
    except discord.HTTPException:
        pass


async def redraw_embed_era_board(
    view: AnyLiveBoardView,
    message: discord.Message | discord.PartialMessage | None,
) -> None:
    """Redraw a recovered board that is still an embed on screen.

    A game's conversion to Components V2 lands with a restart, and a round
    in flight across it comes back as an embed message with the old button
    rows — including host-only buttons the new card no longer carries, which
    the recovered view has nothing to answer. Recovery calls this so that
    board becomes the new card at once instead of on its next vote. A board
    that is already a card is left alone: recovery does not otherwise edit
    the message it re-registers.
    """
    if message is None or not getattr(message, "embeds", None):
        return
    await redraw_board(view, message)


async def redraw_embed_era_lobby(
    view: discord.ui.LayoutView,
    message: discord.Message | discord.PartialMessage | None,
) -> None:
    """:func:`redraw_embed_era_board` for a lobby, which is not on the dial:
    a plain edit that clears the embed the message still carries."""
    if message is None or not getattr(message, "embeds", None):
        return
    try:
        await message.edit(content=None, embed=None, attachments=[], view=view)
    except discord.HTTPException:
        pass


def retire_game_board(
    bot: Any,
    boards: dict[str, StickyPanel],
    view: AnyLiveBoardView,
    channel: Any = None,
    *,
    location: TransientBoardLocation | None = None,
) -> discord.PartialMessage | None:
    """Stop a board reposting, and refuse to render it again.

    Call this at *every* point the board's life ends — natural completion, a
    forced ``/games end``, a host's own End Game button — so the panel is
    released promptly rather than left for a straggling ``on_message`` to
    restick. Idempotent, and a no-op when the dial was never on.

    Returns wherever the board actually ended up, for callers that go on
    editing it (a sticky repost may have moved it since it was opened, so the
    caller's own local message variable can be stale). ``None`` when the dial
    was off, when *channel* is not given, or when the game row no longer names
    a message — in each case the caller's existing reference is still right.
    """
    view._board_retired = True
    return _release_panel(bot, boards, view, channel, location)


def unstick_game_board(
    bot: Any,
    boards: dict[str, StickyPanel],
    view: AnyLiveBoardView,
    channel: Any = None,
    *,
    location: TransientBoardLocation | None = None,
) -> discord.PartialMessage | None:
    """Let a board settle where it is, while the game carries on drawing it.

    ``retire_game_board`` is for a board whose life is over. This is for one
    that stops *following* the channel but keeps rendering: Truth or Dare's
    room board is a single message that lives as long as the room, and it is
    worth pinning to the bottom only while the room is filling up. Once the
    first question is asked, the question cards are the thread of the game and
    a board that went on jumping past them would bury them one at a time.

    ``_board_retired`` is therefore deliberately *not* set — every later
    preference toggle and every later question still redraws this board.
    ``_panel`` is cleared instead, which is what sends ``redraw_board`` back
    to a plain edit, and the returned ``PartialMessage`` says where to aim it:
    a restick may have moved the board since the caller last saw it.

    Idempotent, and a no-op when the dial was never on.
    """
    target = _release_panel(bot, boards, view, channel, location)
    view._panel = None
    return target


def _release_panel(
    bot: Any,
    boards: dict[str, StickyPanel],
    view: AnyLiveBoardView,
    channel: Any,
    location: TransientBoardLocation | None,
) -> discord.PartialMessage | None:
    """Drop one board's panel, and say where that board actually ended up.

    Shared by the two ways a board leaves the dial: letting it go is the same
    work either way, and only whether the view may render again differs.
    """
    panel = boards.pop(view.game_id, None)
    if panel is None:
        return None
    panel.cancel_all()
    # Drop the cached ids too, not just the pending restick — see the module
    # docstring's second rule.
    if view.guild is not None:
        panel.forget(view.guild.id)
    # An untracked board's ids live only here, never in the game row, so
    # this has to run even when the caller has no channel to hand back —
    # skipping it on that path leaked the pair for the rest of the process.
    if location is not None:
        _channel_id, message_id = location.load(view.game_id)
        location.forget(view.game_id)
    elif channel is not None:
        _channel_id, message_id = board_location(bot, view.game_id)
    else:
        return None
    if channel is None or not message_id:
        return None
    return channel.get_partial_message(message_id)


class LivePanels:
    """Every live sticky panel one cog owns, keyed by game id.

    The half of ``LiveBoards`` below that has nothing to do with the Live
    Game Boards dial: the ``game_id -> StickyPanel`` dict, read access to one
    game's panel, the two gateway forwarders and the ``cog_unload`` sweep.

    It is split out because AMA's bottom bar wants exactly this and nothing
    else. The bar is a per-game sticky panel like any other, but it is
    deliberately **not** on the dial — it has followed the bottom of its
    channel since long before that dial existed, so there is no previous
    behaviour for a dial to preserve — and ``LiveBoards.open`` reads the dial
    on the way to posting. Without the split AMA would have re-hand-rolled
    the dict and both forwarders, which is the sixth copy of the shape this
    module exists to have exactly one of.
    """

    def __init__(self) -> None:
        #: For a board this is populated only when the dial is on for that
        #: guild, which is what makes every method here a no-op for a
        #: dial-off game. A panel that is not on the dial at all (AMA's bar)
        #: is registered the moment it is built.
        self._panels: dict[str, StickyPanel] = {}

    def __contains__(self, game_id: str) -> bool:
        return game_id in self._panels

    def __len__(self) -> int:
        return len(self._panels)

    def get(self, game_id: str) -> StickyPanel | None:
        """The live panel for one game, or ``None`` when there is none —
        for a board, because the dial was off when it was posted."""
        return self._panels.get(game_id)

    def __getitem__(self, game_id: str) -> StickyPanel:
        """The live panel for one game. Raises ``KeyError`` when there is
        none, which is the same shape as the dict this replaced."""
        return self._panels[game_id]

    def add(self, game_id: str, panel: StickyPanel) -> None:
        """Take ownership of a panel the caller built itself.

        For a panel that is not posted through ``LiveBoards.open`` — AMA's
        bar, which builds its own through ``make_bar_panel`` because it does
        not consult the dial. Registering it here is what puts it on the
        forwarders and the unload sweep.
        """
        self._panels[game_id] = panel

    def release(self, game_id: str) -> StickyPanel | None:
        """Stop one game's panel reposting and hand it back.

        Dropping it from the dict is what matters: ``on_message`` never
        reaches it again, so its cached ids cannot arm a restick either. The
        panel is returned for a caller with one more thing to do with it —
        AMA's bar is deleted through ``unpost`` afterwards. Idempotent.
        """
        panel = self._panels.pop(game_id, None)
        if panel is not None:
            panel.cancel_all()
        return panel

    async def on_message(self, message: discord.Message) -> None:
        """Forward from the cog's ``on_message`` listener."""
        for panel in list(self._panels.values()):
            await panel.on_message(message)

    async def on_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Forward from the cog's channel- and thread-delete listeners."""
        for panel in list(self._panels.values()):
            await panel.on_channel_delete(channel)

    def unload(self) -> None:
        """Call from ``cog_unload``: a reload must not leave a debounced
        restick armed against a cog (and a Discord state) that is gone."""
        for panel in list(self._panels.values()):
            panel.cancel_all()
        self._panels.clear()


class LiveBoards(LivePanels):
    """Every live board one cog owns, plus the lifecycle the cog forwards in.

    The free functions above are the seam; this is the bookkeeping that sat
    around them. Mt. Rushmore Draft and Name Your Price each hand-rolled the
    same five pieces: a ``game_id -> StickyPanel`` dict, an ``on_message``
    forwarder, an ``on_guild_channel_delete`` forwarder, a ``cog_unload``
    sweep, and an open/retire pair. Two copies were tolerable; the nine games
    that joined on 2026-09-12 would have made eleven, so the dict and the
    forwarding moved here and an adopting cog holds one of these instead.

    Composition rather than a mixin, matching what
    ``docs/plans/sticky-panel-extraction.md`` settled for ``economy_cog``'s
    three panels: ``@commands.Cog.listener`` is collected off the cog class,
    so the two gateway listeners have to be declared there either way and a
    mixin would buy nothing that a two-line forwarder does not.

    *label* names the panels for the log — ``"rushmore board"`` becomes
    ``"rushmore board <game_id>"``, and an adopter with a board per phase
    passes a *suffix* to tell one from the next.
    """

    def __init__(
        self, bot: Any, db: Any, label: str, *, tracked: bool = True
    ) -> None:
        super().__init__()
        self._bot = bot
        self._db = db
        self._label = label
        #: False for a cog whose boards are not the message the game row
        #: names, so their ids cannot live there — see
        #: ``TransientBoardLocation``, which is then what holds them.
        self._location = None if tracked else TransientBoardLocation()

    async def open(
        self,
        game_id: str,
        view: AnyLiveBoardView,
        channel: Any,
        guild: discord.Guild | None,
        msg: discord.Message | None = None,
        *,
        suffix: str = "",
    ) -> discord.Message:
        """Post a board — sticky when the guild's dial is on, the adopter's
        own behaviour otherwise: an in-place edit of *msg*, or a fresh send
        when the adopter posts a new board per round and passes no *msg*.
        The dial is read once, here, so a mid-game flip never disturbs a
        board already running."""
        return await open_game_board(
            self._bot,
            self._db,
            self._panels,
            f"{self._label} {game_id}{suffix}",
            game_id,
            view,
            channel,
            guild,
            msg,
            location=self._location,
        )

    def retire(
        self, view: AnyLiveBoardView, channel: Any = None
    ) -> discord.PartialMessage | None:
        """Stop a board reposting, and refuse to render it again.

        Returns wherever the board actually ended up when *channel* is given,
        for a caller that goes on editing it.
        """
        return retire_game_board(
            self._bot, self._panels, view, channel, location=self._location
        )

    def unstick(
        self, view: AnyLiveBoardView, channel: Any = None
    ) -> discord.PartialMessage | None:
        """Stop a board following the channel without ending it.

        For a board that is sticky only for part of its life — Truth or Dare's
        room board, pinned to the bottom until the first question is asked.
        Returns wherever the board ended up when *channel* is given, so the
        caller can point its own message reference at the live board.
        """
        return unstick_game_board(
            self._bot, self._panels, view, channel, location=self._location
        )

    def abandon(self, game_id: str) -> None:
        """Stop one game's board reposting with its view out of reach.

        ``retire`` is the ordinary path and takes the view, because it also
        has to refuse every later render. This is for the paths that unwind
        *past* a phase's own retire and no longer hold it: a crash inside a
        round (Clapback's ``_cleanup``, reached through ``_cancel_game``) and
        a transient failure re-driving a round that already posted its boards
        (``_survive_hiccup``). Dropping the panel from this dict is what
        matters — ``on_message`` never reaches it again, so its cached ids
        cannot arm anything either. Idempotent, and a no-op after an ordinary
        retire.
        """
        self.release(game_id)
        if self._location is not None:
            self._location.forget(game_id)
