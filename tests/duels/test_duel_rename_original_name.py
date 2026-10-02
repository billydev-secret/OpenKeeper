"""The result card's nickname line must name the loser's OLD name.

Regression for "after the rename this message shows <new name> is <new name>":
``render_result_state`` runs *after* ``loser.edit(nick=...)`` has applied, so a
render that reads the loser's live ``display_name`` prints the new nick on both
sides of "is now known as". Every nickname-stake game shared the bug, so the fix
(thread the pre-edit ``original_name`` through) is checked across all six.

``GAMES`` also records which arm of the render-hooks seam each game is on
(Components V2 wave 3b, ``docs/specs/dk_pvp_games_suite_spec.md`` §1): the legacy
``discord.Embed`` with a "Nickname Applied" field, or a list of layout items
whose text is searched instead. A converted card that still writes the loser's
resolved name into the sentence keeps the regression in full; one that never
states the loser's name at all — the base's ``<@winner> <@loser>`` ping line
already does, and the style guide names a person once — can't reproduce the
bug, so the check there is only that no name has crept back onto the line.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

WINNER_ID = 111
LOSER_ID = 222
OLD_NAME = "OldName"
NEW_NICK = "NewNick"


def _guild_after_rename() -> MagicMock:
    """A guild whose loser already shows the NEW nick — i.e. post-edit state.

    This is the crux: if the loser resolved to the OLD name here, a buggy and a
    fixed render would read identically and the test would pass on the bug.
    """
    winner = SimpleNamespace(display_name="Winner", id=WINNER_ID)
    loser = SimpleNamespace(display_name=NEW_NICK, id=LOSER_ID)
    members = {WINNER_ID: winner, LOSER_ID: loser}
    guild = MagicMock()
    guild.get_member = MagicMock(side_effect=lambda uid: members.get(uid))
    return guild


def _quickdraw_game():
    # fired_at=None takes the "false start" path and skips reaction-time math.
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        fired_at=None, resolved_at=None, loser_fired_at=None,
    )


def _pressure_game():
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        gauge=50, pumps=[], active_player=None,
    )


def _hot_potato_game():
    # started_at=None skips the style-points block.
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        pass_log=[], started_at=None, timer_seconds=0,
    )


def _musical_chairs_game():
    # resolved_at anchors the naming-window countdown on the layout card.
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None, roster=[],
        resolved_at=1_700_000_000.0,
    )


def _hot_potato_group_game():
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        roster=[], pass_log=[], resolved_at=1_700_000_000.0,
    )


def _chicken_game():
    return SimpleNamespace(
        winner_id=WINNER_ID, loser_id=LOSER_ID, stakes_text=None,
        alive=[], bail_log=[],
        # The crash card names the meter reading at the blow (migration 208).
        crash_at=15.0, climb_duration=25.0,
    )


def _load(module: str, cls: str):
    import importlib

    mod = importlib.import_module(module)
    klass = getattr(mod, cls)
    # render_result_state only touches self._name (guild lookups), never
    # self.bot/db — so skip __init__ and its heavy wiring.
    return klass.__new__(klass)


# (module, class, game fixture, arm). ``arm`` is how the game's result card
# states the nickname sentence — see the module docstring:
#   "embed"           legacy discord.Embed, "Nickname Applied" field
#   "layout-named"    Components V2 items that still name the loser
#   "layout-nameless" Components V2 items that never name the loser
GAMES = [
    ("bot_modules.duels.quickdraw.cog", "QuickdrawDuel", _quickdraw_game, "layout-named"),
    ("bot_modules.duels.pressure_cooker.cog", "PressureCookerDuel", _pressure_game, "layout-nameless"),
    ("bot_modules.duels.hot_potato.cog", "HotPotatoDuel", _hot_potato_game, "layout-nameless"),
    ("bot_modules.duels.musical_chairs.cog", "MusicalChairsCog", _musical_chairs_game, "layout-nameless"),
    ("bot_modules.duels.hot_potato_group.cog", "HotPotatoGroupGameCog", _hot_potato_group_game, "layout-nameless"),
    ("bot_modules.duels.chicken.cog", "ChickenCog", _chicken_game, "layout-nameless"),
]
GAME_IDS = [g[0].split(".")[-2] for g in GAMES]

# Every arm opens its nickname line with the same marker — the applied /
# self-apply sentence with 🏷️, the awaiting-a-nickname one with ⏳ — so the
# layout arm's sentence is the card's text from that marker on.
NICK_MARK = "🏷️"
AWAITING_MARK = "⏳"


def _items_text(items) -> str:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return "\n".join(
        child.content
        for child in view.walk_children()
        if isinstance(child, discord.ui.TextDisplay)
    )


def _nick_field(rendered, *, awaiting: bool = False) -> str:
    """The nickname sentence of a result card on either arm: the
    "Nickname Applied" (or "Awaiting Nickname") field of an embed, or the
    text from the matching marker to the end of a layout card."""
    if isinstance(rendered, list):
        mark = AWAITING_MARK if awaiting else NICK_MARK
        text = _items_text(rendered)
        if mark not in text:
            raise AssertionError(f"no {mark!r} nickname line in the result card:\n{text}")
        return text[text.rindex(mark):]
    wanted = "Awaiting Nickname" if awaiting else "Nickname Applied"
    for field in rendered.fields:
        if wanted in (field.name or ""):
            return field.value or ""
    raise AssertionError(f"no {wanted!r} field in the result embed")


@pytest.mark.parametrize("module, cls, make_game, arm", GAMES, ids=GAME_IDS)
def test_result_card_names_old_name_not_new(module, cls, make_game, arm):
    cog = _load(module, cls)
    rendered = cog.render_result_state(
        make_game(), _guild_after_rename(),
        imposed_nick=NEW_NICK, original_name=OLD_NAME,
    )
    value = _nick_field(rendered)
    sentence = f"**{OLD_NAME}** is now known as **{NEW_NICK}** for 24 hours."
    if arm == "embed":
        assert value == sentence
    elif arm == "layout-named":
        assert sentence in value
    else:
        assert f"**{NEW_NICK}**" in value
        assert OLD_NAME not in value
    # The bug's signature: the new nick on both sides.
    assert f"**{NEW_NICK}** is now known as **{NEW_NICK}**" not in value


@pytest.mark.parametrize("module, cls, make_game, arm", GAMES, ids=GAME_IDS)
def test_falls_back_to_live_name_without_original(module, cls, make_game, arm):
    """No original_name (e.g. an older resolved game) → the live name, not a
    crash — or, on a nameless card, still no name at all."""
    cog = _load(module, cls)
    rendered = cog.render_result_state(
        make_game(), _guild_after_rename(), imposed_nick=NEW_NICK,
    )
    value = _nick_field(rendered)
    if arm == "layout-nameless":
        assert f"**{NEW_NICK}**" in value
        assert OLD_NAME not in value
    else:
        assert f"**{NEW_NICK}** is now known as **{NEW_NICK}**" in value


class _FlippingLoser:
    """A member whose ``display_name`` becomes the new nick once ``edit`` runs —
    exactly the live-Discord behavior that made the render read the new name
    on both sides. If the base handler captured the name *after* the edit, the
    spy below would see the new nick instead of the old.
    """

    def __init__(self) -> None:
        self.id = LOSER_ID
        self.nick = OLD_NAME
        self._renamed = False

    @property
    def display_name(self) -> str:
        return NEW_NICK if self._renamed else OLD_NAME

    async def edit(self, *, nick, reason=None):
        self._renamed = True


async def test_base_handler_captures_name_before_the_edit(monkeypatch):
    """The pre-edit display name is what reaches render_result_state."""
    from bot_modules.duels import base_game as bg

    cog = bg.BaseGame.__new__(bg.BaseGame)
    cog.GAME_KEY = "test"
    cog.GAME_DISPLAY_NAME = "Test"
    cog.bot = MagicMock()  # self.db reads self.bot.games_db

    loser = _FlippingLoser()
    winner_id = WINNER_ID
    game = SimpleNamespace(
        id=1, state="RESOLVED", winner_id=winner_id, loser_id=LOSER_ID,
        challenger_id=winner_id,
    )

    guild = MagicMock()
    guild.owner_id = 999  # loser is not the owner
    guild.members = []
    guild.get_member = MagicMock(side_effect=lambda uid: loser if uid == LOSER_ID else MagicMock(id=uid))

    interaction = MagicMock()
    interaction.user.id = winner_id
    interaction.guild = guild
    interaction.response.edit_message = _async_noop()
    interaction.followup.send = _async_noop()

    async def _get_config(db, gid, gtype):
        return {"max_nick_length": 32, "nick_denylist": "[]", "sentence_hours": 24}

    monkeypatch.setattr(bg.duels_db, "get_config", _get_config)
    monkeypatch.setattr(bg.duels_db, "apply_nick", _async_noop())
    monkeypatch.setattr(
        bg, "validate_nickname",
        lambda *a, **k: SimpleNamespace(ok=True, value=NEW_NICK, reason=None),
    )

    cog._db_get_game = _async_return(game)
    cog._check_bot_can_nick = _async_return(None)
    cog._check_no_active_nick = _async_return([])
    cog._refuse_rename_across_pair = _async_return(False)  # not a no-contact pair
    cog._unrenameable_members = MagicMock(return_value=[])  # loser is renameable
    cog._db_set_state = _async_noop()
    cog._disabled_result_view = MagicMock(return_value=None)

    spy = MagicMock(return_value=MagicMock())
    cog.render_result_state = spy

    await cog._handle_nick_submit_locked(interaction, game.id, NEW_NICK)

    spy.assert_called_once()
    assert spy.call_args.kwargs["original_name"] == OLD_NAME
    # And the loser really was renamed (the flip fired), proving the capture
    # genuinely preceded the edit rather than the flip just never happening.
    assert loser.display_name == NEW_NICK


def _async_noop():
    async def _fn(*a, **k):
        return None
    return _fn


def _async_return(value):
    async def _fn(*a, **k):
        return value
    return _fn
