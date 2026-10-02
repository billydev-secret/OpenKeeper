"""The Components V2 render-hook seam wave 3b's six mini-game cogs convert
through (``docs/specs/dk_pvp_games_suite_spec.md`` "Card format").

Mostly plumbing: this drives two throwaway subclasses of real cogs
(``HotPotatoDuel`` for the duel path, ``HotPotatoGroupGameCog`` for the
lobby/group path) whose hooks are swapped to the layout arm, everything
else — DB, game rules, ``handle_interaction`` — left exactly as the real
cog behaves. They predate the real cogs' own conversion and stay because
they pin the seam, not any one game's card. All six mini-game cogs are on
the layout arm now (wave 3b), so no real cog proves the embed arm any
more: the one cog-load reattachment case below borrows a third throwaway
(``PressureCookerDuel`` with an embed-arm ``build_game_view``) to prove
the base still picks the embed twin for a game that answers that way —
the arm the plan retires in the cleanup commit after the last games group
converts.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from bot_modules.duels.hot_potato import db as hpdb
from bot_modules.duels.hot_potato.cog import HotPotatoDuel
from bot_modules.duels.hot_potato_group import db as hpgdb
from bot_modules.duels.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.duels.pressure_cooker import db as pcdb
from bot_modules.duels.pressure_cooker.cog import PressureCookerDuel
from bot_modules.core.layout import check_budget
from bot_modules.duels.views import DuelLayoutView, ResultLayoutView
from bot_modules.core.branding_service import PanelArt
from bot_modules.games.db import GamesDb
from tests.fakes import FakeEconGamesBot, fake_interaction

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


class RecordingBot(FakeEconGamesBot):
    """Records every ``add_view`` call, like
    ``test_duels_challenge_reattach.py``'s fake."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.views: list[tuple[object, int | None]] = []

    def add_view(self, view, *, message_id=None) -> None:
        self.views.append((view, message_id))


# ── layout-arm control views (frozen custom_ids, like a real mini-game's) ──


class PassLayoutView(discord.ui.LayoutView):
    """The layout twin a duel-shaped mini-game's ``build_game_view`` would
    return: one persistent PASS button in its own control row, content
    reset by :meth:`render`."""

    def __init__(self, game_id: int, on_pass) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        btn: discord.ui.Button = discord.ui.Button(
            label="🥔 PASS", style=discord.ButtonStyle.primary,
            custom_id=f"pass:{game_id}",
        )

        async def _cb(interaction: discord.Interaction) -> None:
            await on_pass(interaction, game_id)

        btn.callback = _cb
        self._control_row = discord.ui.ActionRow(btn)
        self.add_item(self._control_row)

    def render(self, items) -> None:
        for item in list(self.children):
            self.remove_item(item)
        for item in items:
            self.add_item(item)
        self.add_item(self._control_row)


class PassGroupLayoutView(discord.ui.LayoutView):
    """The group-shaped twin of :class:`PassLayoutView`."""

    def __init__(self, game_id: int, on_press) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        btn: discord.ui.Button = discord.ui.Button(
            label="Pass", style=discord.ButtonStyle.danger, emoji="🤲",
            custom_id=f"hpg_pass:{game_id}",
        )

        async def _cb(interaction: discord.Interaction) -> None:
            await on_press(interaction, game_id)

        btn.callback = _cb
        self._control_row = discord.ui.ActionRow(btn)
        self.add_item(self._control_row)

    def render(self, items) -> None:
        for item in list(self.children):
            self.remove_item(item)
        for item in items:
            self.add_item(item)
        self.add_item(self._control_row)


# ── fake mini-games: real DB/rules, layout-arm hooks ────────────────────────


class LayoutHotPotatoDuel(HotPotatoDuel):
    """Same DB and game rules as the real cog; only the three render/view
    hooks are swapped to the layout arm, and ``on_game_start`` drops the
    real one's background detonation timer (irrelevant to this seam and a
    leak in a test process)."""

    async def on_game_start(self, game) -> None:
        now = time.time()
        await self._db_set_state(
            game.id, "ACTIVE",
            holder_id=game.challenger_id,
            timer_seconds=9999.0,
            started_at=now,
            pass_log=json.dumps(
                [{"holder_id": game.challenger_id, "received_at": now, "passed_at": None}]
            ),
            last_action_at=now,
        )

    def render_game_state(self, game, guild) -> list[discord.ui.Item]:
        return [discord.ui.TextDisplay(f"🥔 held by {game.holder_id}")]

    def render_result_state(self, game, guild, **kwargs) -> list[discord.ui.Item]:
        return [discord.ui.TextDisplay(f"🥔 {game.winner_id} beat {game.loser_id}")]

    def build_game_view(self, game_id: int) -> PassLayoutView:
        return PassLayoutView(game_id, self._handle_game_button)


class LayoutHotPotatoGroup(HotPotatoGroupGameCog):
    """The lobby/group-path twin of :class:`LayoutHotPotatoDuel`."""

    async def on_game_start(self, game) -> None:
        now = time.time()
        holder = game.alive[0]
        await self._db_set_state(
            game.id, "ACTIVE",
            round=1, holder_id=holder, fuse_seconds=9999.0, phase_started_at=now,
            pass_log=json.dumps(
                [{"holder_id": holder, "received_at": now - 9999, "passed_at": None}]
            ),
            last_action_at=now,
        )

    def render_game_state(self, game, guild) -> list[discord.ui.Item]:
        return [discord.ui.TextDisplay(f"🥔 held by {game.holder_id}")]

    def render_result_state(self, game, guild, **kwargs) -> list[discord.ui.Item]:
        return [discord.ui.TextDisplay(f"🥔 {game.winner_id} beat {game.loser_id}")]

    def build_game_view(self, game_id: int) -> PassGroupLayoutView:
        return PassGroupLayoutView(game_id, self._handle_group_button)


# ── duel path: accept, continue, result ─────────────────────────────────────


async def test_accept_converts_the_challenge_message_to_v2(db, sync_db_path):
    """``_handle_accept`` (base_duel ~465): content=None/embed=None/
    attachments=[] beside a rendered, registered layout view."""
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = LayoutHotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, "loser buys pizza")
    await hpdb.set_game_state(db, gid, "PENDING", message_id=555)
    interaction = fake_interaction(guild=bot.guild)

    await cog._handle_accept(interaction, gid)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["content"] is None
    assert kwargs["embed"] is None
    assert kwargs["attachments"] == []
    view = kwargs["view"]
    assert isinstance(view, PassLayoutView)
    texts = [c.content for c in view.children if isinstance(c, discord.ui.TextDisplay)]
    assert any("held by 1" in t for t in texts)  # challenger holds first
    # The registered view is the same instance the edit carried — one card,
    # one persistent responder, not two independent builds.
    (registered_view, message_id), = bot.views
    assert registered_view is view
    assert message_id == 555


async def test_continue_edit_after_accept_sends_a_fresh_view_only(db, sync_db_path):
    """``_handle_game_button``'s "continue" branch (base_duel ~532, through
    ``_edit_continue_state``): ``view=`` alone, a fresh instance, no
    content/embed/attachments — the message is already V2."""
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = LayoutHotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, "loser buys pizza")
    now = time.time()
    await hpdb.set_game_state(
        db, gid, "ACTIVE", holder_id=1, started_at=now, timer_seconds=9999.0,
        last_action_at=now,
        pass_log=json.dumps(
            [{"holder_id": 1, "received_at": now - 9999, "passed_at": None}]
        ),
    )
    interaction = fake_interaction(guild=bot.guild)
    interaction.user = SimpleNamespace(id=1, display_name="U1")

    await cog._handle_game_button(interaction, gid)

    interaction.response.defer.assert_awaited_once()
    assert interaction.edit_original_response.await_args.kwargs.keys() == {"view"}
    view = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(view, PassLayoutView)
    texts = [c.content for c in view.children if isinstance(c, discord.ui.TextDisplay)]
    # Holder flipped to 2 inside handle_interaction — the re-render reflects it.
    assert any("held by 2" in t for t in texts)
    # No explicit add_view for a continue edit: discord.py's own
    # edit_original_response(view=...) re-registers it.
    assert bot.views == []


async def _duel_at_result(bot, db, *, message_id=8000):
    """A nickname-mode Hot Potato duel on the layout arm, its winner and
    loser set the way a real ``handle_interaction`` sets them before it
    returns "done" — ``_finalize_result`` renders off the game object."""
    cog = LayoutHotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None)
    now = time.time()
    await hpdb.set_game_state(
        db, gid, "ACTIVE", holder_id=1, started_at=now, timer_seconds=9999.0,
        last_action_at=now, message_id=message_id,
        pass_log=json.dumps([{"holder_id": 1, "received_at": now, "passed_at": None}]),
    )
    game = await hpdb.get_game(db, gid)
    game.winner_id, game.loser_id = 1, 2
    return cog, game


async def test_the_result_replaces_the_board_in_the_same_card(db, sync_db_path):
    """``_finalize_result`` → ``_show_result`` (spec "One card"): the result
    is an edit of the game's own message — names line, the mini-game's
    items, the result ``ActionRow`` — with the one-way V2 clear, no ping
    (an edit notifies nobody, so a mention would only be noise), and the
    buttons registered against that same message."""
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog, game = await _duel_at_result(bot, db)
    edit = AsyncMock(return_value=True)
    cog._edit_card = edit  # type: ignore[method-assign]

    await cog._finalize_result(game, winner_id=1, loser_id=2)

    channel_id, message_id = edit.await_args.args
    assert (channel_id, message_id) == (CH, 8000)
    kwargs = edit.await_args.kwargs
    assert kwargs["content"] is None and kwargs["embed"] is None and kwargs["attachments"] == []
    assert "allowed_mentions" not in kwargs
    view = kwargs["view"]
    assert isinstance(view, ResultLayoutView)
    check_budget(view)  # the composed card never overflows a bare game line
    children = list(view.children)
    assert isinstance(children[0], discord.ui.TextDisplay)
    assert children[0].content == "🏆 **Winner:** U1\n💀 **Loser:** U2"
    assert isinstance(children[1], discord.ui.TextDisplay)
    assert "1 beat 2" in children[1].content
    assert isinstance(children[-1], discord.ui.ActionRow)
    button_ids = {b.custom_id for b in children[-1].children}
    assert button_ids == {f"set_nick:{game.id}", f"rematch:{game.id}"}
    assert not any(
        "<@" in c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )
    (registered_view, registered_id), = bot.views
    assert registered_view is view and registered_id == 8000
    stored = await hpdb.get_game(db, game.id)
    assert stored.result_message_id == 8000


async def test_the_result_card_keeps_the_games_art(db, sync_db_path):
    """The art read at challenge time frames the result too: header first,
    footer last, the buttons just above the footer."""
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog, game = await _duel_at_result(bot, db)
    cog._card_art[game.id] = PanelArt(
        None, "https://dash.example/f.webp", header="https://dash.example/h.webp",
        title_in_header=True,
    )
    edit = AsyncMock(return_value=True)
    cog._edit_card = edit  # type: ignore[method-assign]

    await cog._finalize_result(game, winner_id=1, loser_id=2)

    children = list(edit.await_args.kwargs["view"].children)
    assert [c.items[0].media.url for c in (children[0], children[-1])] == [  # type: ignore[attr-defined]
        "https://dash.example/h.webp", "https://dash.example/f.webp",
    ]
    assert isinstance(children[-2], discord.ui.ActionRow)


async def test_a_card_that_cannot_be_edited_registers_nothing(db, sync_db_path):
    """A deleted board: no result message id, no view registered against a
    message that isn't there."""
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog, game = await _duel_at_result(bot, db)
    cog._edit_card = AsyncMock(return_value=False)  # type: ignore[method-assign]

    assert await cog._show_result(game, 1, 2, [discord.ui.TextDisplay("x")]) is None
    assert bot.views == []


# ── group/lobby path: start, continue, result, eliminate ──


async def test_lobby_start_converts_the_lobby_message_to_v2(db, sync_db_path):
    """``_start_lobby_locked`` (~1813): the same conversion shape as accept,
    for the game that starts from a lobby instead of a challenge."""
    bot = RecordingBot(db, sync_db_path, [1, 2, 3])
    cog = LayoutHotPotatoGroup(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser buys pizza")
    await hpgdb.set_game_state(
        db, gid, "LOBBY", message_id=555, roster=json.dumps([1, 2, 3]),
        alive=json.dumps([1, 2, 3]),
    )
    game = await hpgdb.get_game(db, gid)
    interaction = fake_interaction(guild=bot.guild)

    result = await cog._start_lobby_locked(interaction, game, bot.guild)

    assert result is None
    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["content"] is None and kwargs["embed"] is None and kwargs["attachments"] == []
    view = kwargs["view"]
    assert isinstance(view, PassGroupLayoutView)
    (registered_view, message_id), = bot.views
    assert registered_view is view and message_id == 555


async def test_group_continue_edit_view_only(db, sync_db_path):
    """``_handle_group_button``'s continue branch (~2019, through
    ``_edit_continue_state``)."""
    bot = RecordingBot(db, sync_db_path, [1, 2, 3])
    cog = LayoutHotPotatoGroup(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser buys pizza")
    now = time.time()
    await hpgdb.set_game_state(
        db, gid, "ACTIVE", roster=json.dumps([1, 2, 3]), alive=json.dumps([1, 2, 3]),
        holder_id=1, fuse_seconds=9999.0, phase_started_at=now, last_action_at=now,
        pass_log=json.dumps([{"holder_id": 1, "received_at": now - 9999, "passed_at": None}]),
    )
    interaction = fake_interaction(guild=bot.guild)
    interaction.user = SimpleNamespace(id=1, display_name="U1")

    await cog._handle_group_button(interaction, gid)

    assert interaction.edit_original_response.await_args.kwargs.keys() == {"view"}
    view = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(view, PassGroupLayoutView)
    assert bot.views == []  # no explicit re-registration on a continue edit


async def test_group_result_edits_the_board_into_the_result(db, sync_db_path):
    """``_post_group_result``: no greyed-out board and no second post — the
    board message itself becomes the result card, and nobody is pinged."""
    bot = RecordingBot(db, sync_db_path, [1, 2, 3], with_channel=True)
    cog = LayoutHotPotatoGroup(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)  # nickname mode
    now = time.time()
    await hpgdb.set_game_state(
        db, gid, "ACTIVE", roster=json.dumps([1, 2, 3]), alive=json.dumps([1]),
        elimination_order=json.dumps([3, 2]), holder_id=1, fuse_seconds=9999.0,
        phase_started_at=now, last_action_at=now, message_id=555,
        pass_log=json.dumps([{"holder_id": 1, "received_at": now, "passed_at": None}]),
    )
    game = await hpgdb.get_game(db, gid)
    edit = AsyncMock(return_value=True)
    cog._edit_card = edit  # type: ignore[method-assign]

    await cog._post_group_result(game, winner_id=1, loser_id=2)

    assert edit.await_count == 1
    assert edit.await_args.args == (CH, 555)
    result_view = edit.await_args.kwargs["view"]
    assert isinstance(result_view, ResultLayoutView)
    assert result_view.children[0].content == "🏆 **Winner:** U1\n💀 **Loser:** U2"
    assert bot.channel.sent == []  # type: ignore[union-attr]
    assert (await hpgdb.get_game(db, gid)).result_message_id == 555


async def test_group_eliminate_continue_edit(db, sync_db_path):
    """``_group_eliminate``'s non-terminal branch (~2075, through
    ``_edit_continue_state``): the game keeps going, so it's a "continue"
    edit, not a result post."""
    bot = RecordingBot(db, sync_db_path, [1, 2, 3])
    cog = LayoutHotPotatoGroup(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser buys pizza")
    now = time.time()
    await hpgdb.set_game_state(
        db, gid, "ACTIVE", roster=json.dumps([1, 2, 3]), alive=json.dumps([1, 2, 3]),
        holder_id=1, fuse_seconds=9999.0, phase_started_at=now, last_action_at=now,
        pass_log=json.dumps([{"holder_id": 1, "received_at": now, "passed_at": None}]),
    )
    game = await hpgdb.get_game(db, gid)
    interaction = fake_interaction(guild=bot.guild)

    await cog._group_eliminate(game, 2, interaction=interaction)

    assert interaction.edit_original_response.await_args.kwargs.keys() == {"view"}
    view = interaction.edit_original_response.await_args.kwargs["view"]
    assert isinstance(view, PassGroupLayoutView)


# ── cog_load reattach ────────────────────────────────────────────────────────


async def test_cog_load_reattaches_a_layout_shaped_active_game(db, sync_db_path, monkeypatch):
    """``cog_load``'s active-game reattach (~154-159) never branches on the
    hook shape — it only calls ``build_game_view`` + ``add_view`` — so it
    already works unmodified for a layout-shaped game. This proves it: the
    re-registered view is a real ``LayoutView`` whose control row's
    custom_id matches what a press on the live card would carry."""
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = LayoutHotPotatoDuel(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog._expire_loop, "start", lambda: None)
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, "loser buys pizza")
    now = time.time()
    await hpdb.set_game_state(
        db, gid, "ACTIVE", message_id=555, holder_id=1, started_at=now,
        timer_seconds=9999.0, last_action_at=now,
        pass_log=json.dumps([{"holder_id": 1, "received_at": now, "passed_at": None}]),
    )

    await cog.cog_load()

    active_views = [v for v, mid in bot.views if mid == 555]
    assert len(active_views) == 1
    view = active_views[0]
    assert isinstance(view, discord.ui.LayoutView)
    assert isinstance(view, PassLayoutView)
    ids = {getattr(item, "custom_id", None) for item in view.walk_children()}
    assert f"pass:{gid}" in ids


async def test_cog_load_reattaches_a_layout_shaped_resolved_game(db, sync_db_path, monkeypatch):
    """The resolved-game reattach (``_reattach_result_view``) picks the arm
    by what ``build_game_view`` returns: a layout-shaped mini-game's result
    card comes back as a ``ResultLayoutView`` carrying both result buttons
    under the ids a press on the posted card sends, never the embed-arm
    ``ResultView`` (the wrong shape for a message whose V2 flag is set)."""
    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = LayoutHotPotatoDuel(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog._expire_loop, "start", lambda: None)
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None)
    now = time.time()
    await hpdb.set_game_state(
        db, gid, "RESOLVED", message_id=555, result_message_id=777,
        winner_id=1, loser_id=2, resolved_at=now, last_action_at=now,
    )

    await cog.cog_load()

    result_views = [v for v, mid in bot.views if mid == 777]
    assert len(result_views) == 1
    view = result_views[0]
    assert isinstance(view, ResultLayoutView)
    assert not isinstance(view, discord.ui.View)
    ids = {getattr(item, "custom_id", None) for item in view.walk_children()}
    assert {f"set_nick:{gid}", f"rematch:{gid}"} <= ids


class EmbedArmPressureCooker(PressureCookerDuel):
    """A throwaway back on the embed arm: only ``build_game_view`` differs,
    and that is the one hook ``_reattach_result_view`` reads the arm off."""

    def build_game_view(self, game_id: int) -> discord.ui.View:
        return discord.ui.View(timeout=None)


async def test_cog_load_keeps_the_embed_arm_result_view_for_an_embed_game(db, sync_db_path, monkeypatch):
    """The embed twin: a duel-shaped cog whose ``build_game_view`` returns a
    plain ``View`` gets its result card re-registered as the embed-arm
    ``ResultView`` exactly as before the seam. No real cog answers that way
    since Pressure Cooker converted (the last of the six), so a throwaway
    subclass stands in — real DB, real rules, one hook swapped back."""
    from bot_modules.duels.views import ResultView

    bot = RecordingBot(db, sync_db_path, [1, 2])
    cog = EmbedArmPressureCooker(bot)  # type: ignore[arg-type]
    monkeypatch.setattr(cog._expire_loop, "start", lambda: None)
    gid = await pcdb.create_game(db, GUILD, CH, 1, 2, None)
    now = time.time()
    # pressure_games keeps last_pump_at, not last_action_at (migration 028).
    await pcdb.set_game_state(
        db, gid, "RESOLVED", message_id=555, result_message_id=777,
        winner_id=1, loser_id=2, resolved_at=now, last_pump_at=now,
    )

    await cog.cog_load()

    result_views = [v for v, mid in bot.views if mid == 777]
    assert len(result_views) == 1
    assert isinstance(result_views[0], ResultView)


# ── the DuelLayoutView Protocol is what build_game_view promises ───────────


def test_pass_layout_view_satisfies_the_duel_layout_view_protocol():
    """A structural check, not a runtime isinstance (Protocol, not ABC): the
    one hook ``build_game_view``'s docstring promises exists and is callable
    the way ``BaseGame`` uses it."""
    view = PassLayoutView(1, AsyncMock())
    assert hasattr(view, "render") and callable(view.render)
    view.render([discord.ui.TextDisplay("x")])
    assert any(
        isinstance(c, discord.ui.TextDisplay) and c.content == "x"
        for c in view.children
    )
    _: DuelLayoutView = view  # static: PassLayoutView structurally satisfies it


def _real_game_views():
    from bot_modules.duels.chicken.views import ChickenView
    from bot_modules.duels.hot_potato.views import PassLayoutView as HotPotatoView
    from bot_modules.duels.hot_potato_group.views import PassGroupLayoutView
    from bot_modules.duels.musical_chairs.views import SitView
    from bot_modules.duels.pressure_cooker.views import GameView as PumpView
    from bot_modules.duels.quickdraw.views import QuickdrawLayoutView

    return [
        ChickenView, HotPotatoView, PassGroupLayoutView,
        SitView, PumpView, QuickdrawLayoutView,
    ]


@pytest.mark.parametrize("view_cls", _real_game_views(), ids=lambda c: c.__name__)
def test_game_views_have_no_disable(view_cls):
    """A board is never greyed out: the result replaces it in the same
    message (``BaseGame._show_result``, spec "One card"). A ``disable()``
    reappearing here would be a hook nothing calls."""
    view = view_cls(1, AsyncMock())
    assert not hasattr(view, "disable")
    view.render([discord.ui.TextDisplay("x")])
    assert all(
        not b.disabled for b in view.walk_children()
        if isinstance(b, discord.ui.Button)
    )
