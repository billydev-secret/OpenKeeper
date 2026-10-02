"""The board-sticky dial and the per-cog ``LiveBoards`` bookkeeping.

The dial is off by default, per-guild, and one key for every game wired to it
— never a per-game override. Each adopter's own open/retire call sites are
covered in the ``test_games_*_board_sticky.py`` files; what is pinned here is
the dial itself and the parts of ``LiveBoards`` no adopter's tests reach: the
panel naming an adopter with a board per phase depends on, and the cheap
no-op path a dial-off guild takes on every single message in every channel.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot_modules.games import board_sticky_service as svc


def _config_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    return conn


def test_board_sticky_defaults_off():
    conn = _config_conn()
    assert svc.get_board_sticky_enabled(conn) is False


def test_board_sticky_toggle_round_trips():
    conn = _config_conn()
    svc.set_board_sticky_enabled(conn, True)
    assert svc.get_board_sticky_enabled(conn) is True
    svc.set_board_sticky_enabled(conn, False)
    assert svc.get_board_sticky_enabled(conn) is False


def test_board_sticky_is_per_guild():
    conn = _config_conn()
    svc.set_board_sticky_enabled(conn, True, guild_id=42)
    assert svc.get_board_sticky_enabled(conn, guild_id=42) is True
    # A different guild keeps the (off) default rather than inheriting 42's.
    assert svc.get_board_sticky_enabled(conn, guild_id=99) is False


# ── LivePanels: the same bookkeeping without the dial ───────────────────────


def test_a_panel_registered_by_hand_is_reachable_both_ways():
    """``add`` is for a caller that built its own panel because it does not
    go through ``open`` — AMA's bottom bar, which is deliberately off the
    dial. Once registered it has to look exactly like a board to everything
    else the holder does."""
    panels = svc.LivePanels()
    panel = MagicMock()
    panels.add("g1", panel)

    assert len(panels) == 1
    assert "g1" in panels
    assert panels.get("g1") is panel
    assert panels["g1"] is panel


def test_releasing_a_panel_stops_it_and_hands_it_back():
    """The caller gets the panel because AMA still has to ``unpost`` the bar
    after the holder has stopped it resticking."""
    panels = svc.LivePanels()
    panel = MagicMock()
    panels.add("g1", panel)

    assert panels.release("g1") is panel
    panel.cancel_all.assert_called_once_with()
    assert len(panels) == 0
    # Idempotent: a retire can be reached twice (the game ending, then the
    # host's own button), and the second must not raise.
    assert panels.release("g1") is None


def test_a_released_panel_no_longer_hears_the_channel():
    """Dropping it from the dict is the part that matters — a panel still in
    there would restick a bar the game has already taken down."""
    panels = svc.LivePanels()
    panel = MagicMock()
    panel.on_message = AsyncMock()
    panels.add("g1", panel)
    panels.release("g1")

    asyncio.run(panels.on_message(MagicMock(spec=discord.Message)))
    panel.on_message.assert_not_awaited()


def test_forwarding_reaches_every_live_panel():
    """Two AMAs (or two games) can be live in two channels of one guild, and
    the holder has no idea which channel a message came from — that is the
    panel's own job."""
    panels = svc.LivePanels()
    first, second = MagicMock(), MagicMock()
    for name, panel in (("g1", first), ("g2", second)):
        panel.on_message = AsyncMock()
        panel.on_channel_delete = AsyncMock()
        panels.add(name, panel)

    message = MagicMock(spec=discord.Message)
    channel = MagicMock(spec=discord.TextChannel)
    asyncio.run(panels.on_message(message))
    asyncio.run(panels.on_channel_delete(channel))

    for panel in (first, second):
        panel.on_message.assert_awaited_once_with(message)
        panel.on_channel_delete.assert_awaited_once_with(channel)


def test_unload_cancels_and_clears_everything():
    panels = svc.LivePanels()
    panel = MagicMock()
    panels.add("g1", panel)

    panels.unload()

    panel.cancel_all.assert_called_once_with()
    assert len(panels) == 0


def test_live_boards_is_a_live_panels():
    """The split is what lets AMA compose the holder instead of twinning it;
    a board holder is still one of these, so every adopter above keeps the
    same five methods it had before the split."""
    assert isinstance(_boards(), svc.LivePanels)


# ── LiveBoards: the bookkeeping that used to be copied into each cog ────────


def _boards(label: str = "test board") -> svc.LiveBoards:
    bot = SimpleNamespace(ctx=SimpleNamespace(open_db=lambda: _config_conn()))
    return svc.LiveBoards(bot, MagicMock(), label)


def _view(game_id: str = "g1") -> MagicMock:
    view = MagicMock()
    view.game_id = game_id
    view.guild = None
    view._board_retired = False
    view._panel = None
    return view


class _LayoutBoardView(discord.ui.LayoutView):
    """A minimal Components V2 board: real ``LayoutView`` (so ``isinstance``
    branches route it correctly), rebuilding by dropping in one text item so
    a rebuild is observable without a real game's builder."""

    def __init__(self, game_id: str = "g1") -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.guild: discord.Guild | None = None
        self._panel = None
        self._board_retired = False
        self.rebuild_count = 0
        self._rebuild()

    def _rebuild(self) -> None:
        self.rebuild_count += 1
        self.clear_items()
        self.add_item(discord.ui.TextDisplay(f"## Board {self.rebuild_count}"))


def test_live_boards_starts_empty():
    boards = _boards()
    assert len(boards) == 0
    assert "g1" not in boards
    assert boards.get("g1") is None


def test_open_names_a_panel_after_its_label_game_and_suffix(monkeypatch):
    """The name is what reaches the logs, so an adopter with several boards
    per game can tell one from the next. Name Your Price passes a per-round
    suffix; Mt. Rushmore Draft, with one board per game, passes none."""
    seen: list[str] = []

    async def _capture(
        bot, db, panels, name, game_id, view, channel, guild, msg, *, location=None
    ):
        seen.append(name)
        return msg

    monkeypatch.setattr(svc, "open_game_board", _capture)
    boards = _boards("price board")
    anchor = MagicMock(spec=discord.Message)

    asyncio.run(boards.open("g7", _view("g7"), None, None, anchor, suffix=" round 3"))
    asyncio.run(boards.open("g7", _view("g7"), None, None, anchor))

    assert seen == ["price board g7 round 3", "price board g7"]


def test_forwarding_is_a_no_op_with_nothing_live():
    """The dial-off path. Every message in every channel of every guild runs
    through an adopting cog's listener, so this has to cost nothing when the
    guild never turned the dial on and no panel was ever constructed."""
    boards = _boards()
    message = MagicMock(spec=discord.Message)
    channel = MagicMock(spec=discord.TextChannel)
    asyncio.run(boards.on_message(message))
    asyncio.run(boards.on_channel_delete(channel))
    # Nothing was consulted on the message itself — no guild read, no id cache.
    assert message.method_calls == []
    assert len(boards) == 0


def test_unload_is_safe_with_nothing_live():
    boards = _boards()
    boards.unload()
    boards.unload()
    assert len(boards) == 0


def test_retire_is_a_no_op_for_a_board_that_was_never_stuck():
    """A dial-off game still calls retire at every teardown point; it must
    answer ``None`` (the caller's own message reference is still right) and
    still arm the resurrection refusal."""
    boards = _boards()
    view = _view()
    assert boards.retire(view) is None
    assert view._board_retired is True


def test_unstick_stops_a_board_moving_without_ending_it():
    """Truth or Dare's room board is on the dial for the first stretch of the
    room only. Coming off it is not a retire: the board goes on rendering for
    the rest of the room, so ``_board_retired`` must stay clear — and
    ``_panel`` must not, or every later redraw would still be routed through
    a panel that no longer exists."""
    boards = _boards()
    view = _view()
    panel = MagicMock()
    boards._panels["g1"] = panel
    view._panel = panel

    assert boards.unstick(view) is None  # no channel given: caller's ref stands

    panel.cancel_all.assert_called_once()
    assert len(boards) == 0
    assert view._panel is None
    assert view._board_retired is False


def test_unstick_hands_back_wherever_the_board_ended_up(monkeypatch):
    """A restick may have moved the board since the caller last saw it, so the
    settling board's new home is what the game has to keep editing."""
    monkeypatch.setattr(svc, "board_location", lambda bot, game_id: (10, 555))
    boards = _boards()
    view = _view()
    boards._panels["g1"] = MagicMock()
    channel = MagicMock(spec=discord.TextChannel)

    target = boards.unstick(view, channel)

    channel.get_partial_message.assert_called_once_with(555)
    assert target is channel.get_partial_message.return_value


def test_unstick_is_a_no_op_for_a_board_that_was_never_stuck():
    """The dial-off path: the first question still calls it, and the room's
    own message reference is already right."""
    boards = _boards()
    view = _view()

    assert boards.unstick(view, MagicMock(spec=discord.TextChannel)) is None
    assert view._board_retired is False
    assert view._panel is None


def test_abandon_drops_a_board_whose_view_is_out_of_reach():
    """``retire`` is the ordinary path and needs the view, because it also has
    to refuse every later render. A crash inside a round unwinds *past* that
    phase's retire, and a transient failure re-drives a round that already
    posted its boards — neither still holds the view, and both must leave
    nothing reposting (Clapback's ``_cleanup`` / ``_survive_hiccup``)."""
    boards = _boards()
    panel = MagicMock()
    boards._panels["g1"] = panel

    boards.abandon("g1")
    boards.abandon("g1")  # idempotent: every teardown path goes through it

    panel.cancel_all.assert_called_once()
    assert len(boards) == 0
    assert "g1" not in boards


def test_abandon_clears_an_untracked_boards_stored_ids():
    """Nothing else will: an untracked board's ids live only in the cog's
    in-memory store, and the cog outlives every game it runs."""
    boards = svc.LiveBoards(SimpleNamespace(), MagicMock(), "t", tracked=False)
    boards._location.save("g1", 10, 100)

    boards.abandon("g1")

    assert boards._location.load("g1") == (0, 0)


def test_retire_clears_an_untracked_boards_stored_ids_even_with_no_channel():
    """The ordinary retire path, not just ``abandon``: a caller that has no
    channel to hand back (or already dropped its own reference) must still
    not leak the game's stored (channel_id, message_id) pair — the cog
    outlives every game it runs, so a forgotten pair sits in memory forever."""
    boards = svc.LiveBoards(SimpleNamespace(), MagicMock(), "t", tracked=False)
    boards._location.save("g1", 10, 100)
    boards._panels["g1"] = MagicMock()
    view = _view()

    assert boards.retire(view) is None  # no channel given

    assert boards._location.load("g1") == (0, 0)


# ── where an untracked adopter's boards live ────────────────────────────────


def test_transient_location_keeps_one_pair_per_game():
    """Fantasies' game row names the host's control panel, so its boards
    cannot record themselves there; this holds their ids instead. Keyed by
    game_id like the panels, so two games in one guild never collide."""
    store = svc.TransientBoardLocation()
    assert store.load("g1") == (0, 0)

    store.save("g1", 10, 100)
    store.save("g2", 20, 200)

    assert store.load("g1") == (10, 100)
    assert store.load("g2") == (20, 200)


def test_transient_location_forgets_one_game_only():
    """A cog outlives every game it runs, so retire has to clear the game's
    entry rather than grow the dict one round at a time."""
    store = svc.TransientBoardLocation()
    store.save("g1", 10, 100)
    store.save("g2", 20, 200)

    store.forget("g1")
    store.forget("g1")  # idempotent: every teardown path calls retire

    assert store.load("g1") == (0, 0)
    assert store.load("g2") == (20, 200)


# ── whether a board replaces the message the game row names ────────────────


def _panel(**kwargs) -> svc.StickyPanel:
    """The panel ``make_board_panel`` builds, so its ``load_ids``/``save_ids``
    hooks can be called the way ``StickyPanel`` calls them (off-thread, hence
    sync). The bot is never touched: every DB path is monkeypatched out."""
    return svc.make_board_panel(SimpleNamespace(), "board", "g1", _view(), **kwargs)


def test_a_moving_board_starts_from_the_message_the_row_names(monkeypatch):
    """Rushmore's lobby card and Price's carried board: ``place()`` is meant
    to treat that message as this panel's own and remove it once the
    replacement is safely up."""
    monkeypatch.setattr(svc, "board_location", lambda bot, game_id: (7, 70))
    panel = _panel(replaces_prior_board=True)

    assert panel._load_ids(0) == (7, 70)


def test_a_freshly_posted_board_has_no_prior_board_of_its_own(monkeypatch):
    """A round game posts a new board each round and leaves the closed one on
    screen as that round's record. ``place()`` deletes whatever ``load_ids``
    names, so the first placement must name nothing — otherwise ticking the
    dial on would silently eat the previous round."""
    monkeypatch.setattr(svc, "board_location", lambda bot, game_id: (7, 70))
    saved: list[tuple[int, int, int]] = []
    monkeypatch.setattr(
        svc, "set_board_location",
        lambda bot, game_id, c, m: saved.append((game_id, c, m)),
    )
    panel = _panel(replaces_prior_board=False)

    assert panel._load_ids(0) == (0, 0)

    # Once this panel's own board is up the row names *it*, so reading the
    # row is right again — which matters because StickyPanel re-consults
    # load_ids on every placement, not only the first.
    panel._save_ids(0, 8, 80)
    assert saved == [("g1", 8, 80)]
    assert panel._load_ids(0) == (7, 70)


def test_an_untracked_boards_ids_never_reach_the_game_row(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("an untracked board must not touch the game row")

    monkeypatch.setattr(svc, "board_location", _boom)
    monkeypatch.setattr(svc, "set_board_location", _boom)
    store = svc.TransientBoardLocation()
    panel = _panel(location=store)

    panel._save_ids(0, 9, 90)

    assert store.load("g1") == (9, 90)
    assert panel._load_ids(0) == (9, 90)


# ── the redraw seam ─────────────────────────────────────────────────────────


def test_redraw_goes_through_the_panel_when_one_exists():
    """With the dial on, the message a click came from may have been
    re-posted elsewhere since — so the redraw asks the panel where the board
    is instead of editing the caller's snapshot."""
    view = _view()
    view.guild = SimpleNamespace(id=4242)
    view._panel = MagicMock()
    view._panel.refresh = AsyncMock()
    message = MagicMock(spec=discord.Message)

    asyncio.run(svc.redraw_board(view, message))

    view._panel.refresh.assert_awaited_once_with(4242)
    message.edit.assert_not_called()


def test_redraw_swallows_a_retired_boards_refusal():
    """``build`` refuses a retired board by raising and ``StickyPanel.refresh``
    does not guard its own build call, so the refusal comes straight back
    out. A redraw landing after the round closed (a vote in flight as the
    timer expires) has nothing left to draw — it is not an error."""
    view = _view()
    view.guild = SimpleNamespace(id=4242)
    view._panel = MagicMock()
    view._panel.refresh = AsyncMock(side_effect=RuntimeError("board retired"))

    asyncio.run(svc.redraw_board(view, None))

    view._panel.refresh.assert_awaited_once()


def test_redraw_edits_the_message_when_the_dial_is_off():
    view = _view()
    view._build_embed = MagicMock(return_value=discord.Embed(title="board"))
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()

    asyncio.run(svc.redraw_board(view, message))

    assert message.edit.await_args.kwargs["embed"].title == "board"


def test_redraw_swallows_a_dead_message():
    """The dial-off path's own ordinary race: the round's board was deleted
    (``/games end``, a mod tidying up) between the click and the redraw."""
    view = _view()
    view._build_embed = MagicMock(return_value=discord.Embed(title="board"))
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "unknown message")
    )

    asyncio.run(svc.redraw_board(view, message))


def test_redraw_with_no_panel_and_no_message_does_nothing():
    """Every close path has a window where the caller holds neither."""
    view = _view()
    view._build_embed = MagicMock(side_effect=AssertionError("nothing to draw on"))

    asyncio.run(svc.redraw_board(view, None))


# ── the Components V2 layout arm ─────────────────────────────────────────
# Wave 3b converts one games group at a time, so ``LiveBoardView`` (embed)
# and ``LiveBoardLayoutView`` (layout) run side by side until the last group
# lands. These prove the isinstance branch, not any one game's board.


def test_build_board_content_rebuilds_a_layout_view_in_place():
    """There is no fresh object to hand ``PanelContent`` the way a fresh
    embed is one — the view *is* the message body — so it must be rebuilt
    before being handed back."""
    view = _LayoutBoardView()
    assert view.rebuild_count == 1

    content = svc.build_board_content("board", view)

    assert view.rebuild_count == 2
    assert content.layout is view
    assert content.embed is None


def test_build_board_content_still_raises_for_a_retired_layout_view():
    view = _LayoutBoardView()
    view._board_retired = True

    try:
        svc.build_board_content("board", view)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    # The refusal comes before any rebuild — nothing left to draw.
    assert view.rebuild_count == 1


def test_open_game_board_layout_arm_sends_fresh_with_no_prior_message():
    view = _LayoutBoardView()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(spec=discord.Message, id=99))
    db = MagicMock()
    db.execute = AsyncMock()

    async def _run():
        return await svc.open_game_board(
            SimpleNamespace(), db, {}, "board", "g1", view, channel, None, None,
        )

    result = asyncio.run(_run())

    channel.send.assert_awaited_once_with(view=view)
    assert result.id == 99
    assert view.rebuild_count == 2  # once at __init__, once before sending


def test_open_game_board_layout_arm_edits_the_carried_message():
    view = _LayoutBoardView()
    channel = MagicMock()
    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()

    async def _run():
        return await svc.open_game_board(
            SimpleNamespace(), MagicMock(), {}, "board", "g1", view, channel, None, msg,
        )

    result = asyncio.run(_run())

    msg.edit.assert_awaited_once_with(
        content=None, embed=None, attachments=[], view=view
    )
    assert result is msg


def test_redraw_edits_a_layout_view_message_when_the_dial_is_off():
    view = _LayoutBoardView()
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()

    asyncio.run(svc.redraw_board(view, message))

    message.edit.assert_awaited_once_with(
        content=None, embed=None, attachments=[], view=view
    )
    assert view.rebuild_count == 2


def test_redraw_swallows_a_dead_layout_view_message():
    view = _LayoutBoardView()
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "unknown message")
    )

    asyncio.run(svc.redraw_board(view, message))  # must not raise
