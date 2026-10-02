"""The ephemeral DM settings panel — the only member-facing DM surface.

``/dm_help``, ``/dm_set_mode``, ``/dm_status`` and ``/dm_revoke`` were removed
on 2026-07-28 and folded into ``DmSettingsView``, reached from the request
panel's "Settings" button. With no slash commands left, every guard that used
to sit on a command signature now lives in the view, so it is tested here
rather than assumed.

Scope: what the panel *keeps and reports* across a press — the state bugs this
view has actually had. Which control reaches which handler is in
``tests/dm/test_dm_perms_cog.py``; what the card looks like is in
``tests/dm/test_dm_perms_layouts.py``; the DB writes are in
``tests/dm/test_dm_perms_service.py``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.dm.perms.cog import DmSettingsView
from bot_modules.dm.perms.layouts import (
    MODE_SELECT_ID,
    NEXT_PAGE_ID,
    REMOVE_BUTTON_ID_BASE,
)


def _member(user_id: int = 1, *, roles=None) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.display_name = f"User{user_id}"
    member.roles = roles or []
    guild = MagicMock()
    guild.id = 99
    guild.icon = None
    member.guild = guild
    return member


def _cog() -> MagicMock:
    cog = MagicMock()
    cog.bot.ctx.db_path = ":memory:"
    cog._mode_roles_for.return_value = {}
    cog.revoke_connection = AsyncMock(return_value=True)
    return cog


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user = _member(1)
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    # Fresh interaction: _redraw picks edit_message over edit_original_response
    # — until a defer, after which only the original response can be edited.
    interaction.response.is_done.return_value = False

    async def _defer(*_args, **_kwargs) -> None:
        interaction.response.is_done.return_value = True

    interaction.response.defer = AsyncMock(side_effect=_defer)
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.original_response = AsyncMock(return_value=MagicMock())
    return interaction


def _panel(mode: str = "ask", connections=()) -> DmSettingsView:
    return DmSettingsView(
        _cog(),
        _member(1),
        mode=mode,
        connections=list(connections),
        name_fn=lambda uid: f"User{uid}",
    )


def _choose(view: DmSettingsView, mode: str) -> discord.ui.Select:
    """Set the dial the way a member picking an option does."""
    select = view.find_item(MODE_SELECT_ID)
    select._values = [mode]
    return select


def _card_text(view: DmSettingsView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


@pytest.fixture(autouse=True)
def audit_rows(monkeypatch):
    """Capture ``write_audit_log`` instead of writing it.

    A mode change records itself since 2026-08-18, and ``_cog`` hands the view a
    ``:memory:`` path with no schema behind it — so without this every mode test
    dies on "no such table: dm_audit_log" rather than on anything it asserts.
    Returning the list lets one test check the row is actually written.
    """
    import bot_modules.dm.perms.cog as mod

    rows: list[tuple] = []
    monkeypatch.setattr(
        mod, "write_audit_log",
        lambda *args, **kwargs: rows.append((args, kwargs)),
    )
    return rows


@pytest.fixture
def set_mode(monkeypatch):
    """Stub the role write the dial performs; the roles aren't under test."""
    import bot_modules.dm.perms.cog as mod

    stub = AsyncMock()
    monkeypatch.setattr(mod, "set_member_dm_mode", stub)
    return stub


# ── the mode dial ────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["open", "ask", "closed"])
@pytest.mark.asyncio
async def test_setting_a_mode_writes_it_and_says_so(set_mode, mode):
    view = _panel(mode="open" if mode != "open" else "closed")
    _choose(view, mode)

    await view._on_mode(_interaction())

    assert set_mode.await_args.args[1] == mode
    text = _card_text(view)
    assert "Changed to" in text
    assert "-# Changed" not in text


@pytest.mark.asyncio
async def test_mode_change_redraws_with_the_new_mode(set_mode):
    """Regression: add_roles/remove_roles go out over REST and don't update the
    cached member, so re-reading roles right after made the panel show the old
    mode while the confirmation line claimed the new one."""
    view = _panel(mode="ask")
    _choose(view, "closed")

    await view._on_mode(_interaction())

    assert view._mode == "closed"
    select = view.find_item(MODE_SELECT_ID)
    assert [o.value for o in select.options if o.default] == ["closed"]


@pytest.mark.asyncio
async def test_failed_mode_change_does_not_move_the_dial(monkeypatch):
    """A Forbidden must not leave the panel claiming a mode that was never set."""
    import bot_modules.dm.perms.cog as mod

    monkeypatch.setattr(
        mod,
        "set_member_dm_mode",
        AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope")),
    )
    view = _panel(mode="ask")
    _choose(view, "open")
    interaction = _interaction()

    await view._on_mode(interaction)

    assert view._mode == "ask"
    # The dial deferred first, so the refusal has to travel by followup.
    interaction.followup.send.assert_awaited_once()
    assert "permission" in interaction.followup.send.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_mode_change_defers_before_the_role_writes(set_mode):
    """set_member_dm_mode is up to five REST calls before the redraw could
    answer; Discord's 3-second window closes under load and the roles would
    move while the member saw "This interaction failed"."""
    interaction = _interaction()
    defers_seen: list[int] = []
    set_mode.side_effect = lambda *_a, **_k: defers_seen.append(
        interaction.response.defer.await_count
    )
    view = _panel(mode="ask")
    _choose(view, "open")

    await view._on_mode(interaction)

    assert defers_seen == [1]
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_mode_change_records_itself_in_the_audit_log(set_mode, audit_rows):
    """Without this row, only Discord's own audit log knew a mode had changed."""
    view = _panel()
    _choose(view, "closed")

    await view._on_mode(_interaction())

    assert len(audit_rows) == 1
    args, kwargs = audit_rows[0]
    assert args[2] == "mode_set"
    assert kwargs["notes"] == "mode=closed"
    assert kwargs["actor_id"] == 1


@pytest.mark.asyncio
async def test_second_mode_change_uses_live_roles_not_the_panel_snapshot(set_mode):
    """Regression (2026-08-18): the view cached its member at panel-open time.

    A member who changed mode twice without closing the panel had the second
    change computed against the *first* change's stale role set, so the previous
    mode's role was never removed — leaving two DM roles on and handing the dedup
    listener a choice it then got wrong, silently reverting the pick.
    """
    ask_role = MagicMock(spec=discord.Role)
    ask_role.id, ask_role.name, ask_role.position = 20, "ASK TO DM", 9
    live = _member(1, roles=[ask_role])   # what the member holds by the 2nd click

    view = _panel()                        # opened with no roles
    _choose(view, "closed")
    interaction = _interaction()
    interaction.user = live

    await view._on_mode(interaction)

    assert set_mode.await_args.args[0] is live


@pytest.mark.asyncio
async def test_an_empty_dial_press_keeps_the_current_mode(set_mode):
    """A select that dispatches with nothing resolved must not blank the mode."""
    view = _panel(mode="closed")
    view.find_item(MODE_SELECT_ID)._values = []

    await view._on_mode(_interaction())

    assert view._mode == "closed"


# ── the connection list ──────────────────────────────────────────────────


def _connection(user_id: int) -> dict[str, object]:
    return {"user_id": user_id, "type": "dm", "reason": "", "created_at": 1_750_000_000}


@pytest.mark.asyncio
async def test_next_page_binds_remove_to_the_eleventh_connection():
    """Prod had members with eleven and twelve connections when the ten-row
    cut shipped, and their oldest rows were unreachable from the one surface
    that can remove them. The arrow reaches them, and the first Remove on
    page two is the eleventh connection — not the first one over again."""
    connections = [_connection(i) for i in range(1, 13)]
    view = _panel(connections=connections)

    turn = _interaction()
    await view.find_item(NEXT_PAGE_ID).callback(turn)

    assert view._page == 1
    turn.response.edit_message.assert_awaited_once()
    assert "Page 2 of 2 • 12 connections" in _card_text(view)

    press = _interaction()
    await view.find_item(REMOVE_BUTTON_ID_BASE).callback(press)
    confirm = press.response.send_message.await_args.kwargs["view"]
    assert confirm.connection["user_id"] == connections[10]["user_id"]


@pytest.mark.asyncio
async def test_a_page_turn_drops_the_changed_to_lead():
    """The page is the answer to that press; "Changed to" was the answer to
    the last. The lead lives on the view as a flag the next redraw clears,
    so this is the test that the flag really is cleared."""
    view = _panel(connections=[_connection(i) for i in range(1, 13)])
    view._changed = True
    view._render()
    assert "Changed to" in _card_text(view)

    await view.find_item(NEXT_PAGE_ID).callback(_interaction())

    assert "Changed to" not in _card_text(view)
    assert "You're on" in _card_text(view)
    assert "Page 2 of 2" in _card_text(view)


@pytest.mark.asyncio
async def test_reload_pulls_the_page_back_when_its_last_row_goes(monkeypatch):
    """Removing the only row on page two must land on page one, not on an
    empty page with a Next arrow pointing nowhere."""
    import bot_modules.dm.perms.cog as mod

    view = _panel(connections=[_connection(i) for i in range(1, 12)])
    view._page = 1
    view._render()
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    monkeypatch.setattr(
        mod,
        "load_member_connections",
        lambda *_a: [_connection(i) for i in range(1, 11)],
    )

    await view.reload(note="Removed your connection with User11.")

    assert view._page == 0
    assert view.find_item(NEXT_PAGE_ID) is None


@pytest.mark.asyncio
async def test_reload_rereads_the_connections_and_redraws_the_stored_message(
    monkeypatch,
):
    """A removal is confirmed on its *own* ephemeral message, so there is no
    response left to edit — the panel redraws the message it kept."""
    import bot_modules.dm.perms.cog as mod

    view = _panel(connections=[_connection(2), _connection(3)])
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    monkeypatch.setattr(
        mod, "load_member_connections", lambda *_a: [_connection(3)]
    )

    await view.reload(note="Removed your connection with User2.")

    view.message.edit.assert_awaited_once()
    assert view.message.edit.await_args.kwargs["view"] is view
    text = _card_text(view)
    assert "User3" in text and "User2" not in text.split("-#")[0]
    assert "-# Removed your connection with User2." in text


@pytest.mark.asyncio
async def test_reload_survives_a_message_that_has_gone(monkeypatch):
    """The ephemeral panel can be dismissed while the confirm card is open."""
    import bot_modules.dm.perms.cog as mod

    view = _panel(connections=[_connection(2)])
    view.message = MagicMock()
    view.message.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=404), "gone")
    )
    monkeypatch.setattr(mod, "load_member_connections", lambda *_a: [])

    await view.reload(note="Removed your connection with User2.")

    assert view._connections == []


@pytest.mark.asyncio
async def test_a_removal_that_found_nothing_says_so(monkeypatch):
    """``revoke_connection`` returning False must read as "no connection", not
    as a successful removal — the other member may have got there first."""
    import bot_modules.dm.perms.cog as mod
    from bot_modules.dm.perms.cog import RemoveConfirmView
    from bot_modules.dm.perms.layouts import CONFIRM_REMOVE_ID

    view = _panel(connections=[_connection(2)])
    view.cog.revoke_connection = AsyncMock(return_value=False)
    view.message = MagicMock()
    view.message.edit = AsyncMock()
    monkeypatch.setattr(mod, "load_member_connections", lambda *_a: [])

    confirm = RemoveConfirmView(view, _connection(2))
    interaction = _interaction()
    await confirm.find_item(CONFIRM_REMOVE_ID).callback(interaction)

    card = interaction.edit_original_response.await_args.kwargs["view"]
    text = "\n".join(
        item.content
        for item in card.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "Nothing to Remove" in text
    assert "no longer connected" in _card_text(view)
