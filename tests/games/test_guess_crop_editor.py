"""Tests for CropEditorView button callbacks."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.games.guess.models import BoundingBox

GUILD_ID = 9001
GUESS_CHANNEL_ID = 8001
IMG_W, IMG_H = 500, 500


@pytest.fixture(autouse=True)
def _no_lobby_art(monkeypatch):
    """The public cards read the guild's lobby strip; the mocked bots here
    have no branding table to read it from."""
    from bot_modules.games.utils.lobby_art import NO_ART

    monkeypatch.setattr(
        "bot_modules.games.guess.cog.read_lobby_art", AsyncMock(return_value=NO_ART)
    )

# Minimal fake JPEG header so discord.File doesn't complain
_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20


def _box(x1: float = 100, y1: float = 100, x2: float = 300, y2: float = 300) -> BoundingBox:
    return BoundingBox(x1, y1, x2, y2)


def _make_view(crop_box: BoundingBox | None = None):  # type: ignore[return]
    from bot_modules.games.guess.cog import CropEditorView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    return CropEditorView(
        bot,
        image_bytes=b"fake",
        img_w=IMG_W,
        img_h=IMG_H,
        crop_box=crop_box or _box(),
        guild_id=GUILD_ID,
        guess_channel_id=GUESS_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=1,
    )


@pytest.fixture
def inter() -> MagicMock:
    mock = MagicMock()
    mock.response = MagicMock()
    mock.response.edit_message = AsyncMock()
    mock.response.send_message = AsyncMock()
    mock.response.defer = AsyncMock()
    mock.followup = MagicMock()
    mock.followup.send = AsyncMock()
    mock.edit_original_response = AsyncMock()
    return mock


# ── Direction buttons ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_up_moves_box_up(inter: MagicMock) -> None:
    view = _make_view(_box(100, 200, 300, 400))
    original_y1 = view.crop_box.y1
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_up(inter)
    assert view.crop_box.y1 < original_y1


@pytest.mark.asyncio
async def test_down_moves_box_down(inter: MagicMock) -> None:
    view = _make_view(_box(100, 100, 300, 300))
    original_y1 = view.crop_box.y1
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_down(inter)
    assert view.crop_box.y1 > original_y1


@pytest.mark.asyncio
async def test_left_moves_box_left(inter: MagicMock) -> None:
    view = _make_view(_box(200, 100, 400, 300))
    original_x1 = view.crop_box.x1
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_left(inter)
    assert view.crop_box.x1 < original_x1


@pytest.mark.asyncio
async def test_right_moves_box_right(inter: MagicMock) -> None:
    view = _make_view(_box(100, 100, 300, 300))
    original_x1 = view.crop_box.x1
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_right(inter)
    assert view.crop_box.x1 > original_x1


# ── Zoom buttons ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_zoom_in_shrinks_box(inter: MagicMock) -> None:
    view = _make_view(_box(50, 50, 450, 450))  # 400×400, well above min_px=200
    original_w = view.crop_box.width
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_zoom_in(inter)
    assert view.crop_box.width < original_w


@pytest.mark.asyncio
async def test_zoom_out_expands_box(inter: MagicMock) -> None:
    view = _make_view(_box(100, 100, 300, 300))  # 200×200
    original_w = view.crop_box.width
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_zoom_out(inter)
    assert view.crop_box.width > original_w


# ── Edge clamping ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_up_clamps_at_top_edge(inter: MagicMock) -> None:
    view = _make_view(_box(0, 0, 200, 200))  # already at top
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_up(inter)
    assert view.crop_box.y1 == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_down_clamps_at_bottom_edge(inter: MagicMock) -> None:
    view = _make_view(_box(300, 300, 500, 500))  # already at bottom
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_down(inter)
    assert view.crop_box.y2 == pytest.approx(float(IMG_H))


# ── Cancel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_deletes_the_editor_and_says_nothing(inter: MagicMock) -> None:
    """Cancel is silent: the ephemeral editor goes, with no receipt for a
    submission that never happened (style guide § Words)."""
    inter.delete_original_response = AsyncMock()
    view = _make_view()
    await view._on_cancel(inter)
    inter.response.defer.assert_awaited_once()
    inter.delete_original_response.assert_awaited_once()
    inter.response.edit_message.assert_not_called()
    assert view.is_finished()


# ── Rerender ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rerender_calls_edit_message(inter: MagicMock) -> None:
    view = _make_view()
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._rerender(inter)
    inter.response.edit_message.assert_called_once()
    # The card's gallery shows the preview by name, so the new render has to
    # go up under the name it references.
    kwargs = inter.response.edit_message.call_args.kwargs
    assert [f.filename for f in kwargs["attachments"]] == ["preview.jpg"]
    assert kwargs["view"] is view


# ── Double-post prevention ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_double_post_sends_already_posted(inter: MagicMock) -> None:
    view = _make_view()
    view._posted = True
    await view._on_post(inter)
    inter.response.send_message.assert_called_once()
    msg = inter.response.send_message.call_args.args[0]
    assert "already" in msg.lower()


# ── Auto button ───────────────────────────────────────────────────────────────

def _make_view_with_candidates(*boxes: BoundingBox):  # type: ignore[return]
    from bot_modules.games.guess.cog import CropEditorView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    box_list = list(boxes)
    return CropEditorView(
        bot,
        image_bytes=b"fake",
        img_w=IMG_W,
        img_h=IMG_H,
        crop_box=box_list[0] if box_list else _box(),
        guild_id=GUILD_ID,
        guess_channel_id=GUESS_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=len(box_list),
        candidate_boxes=box_list,
    )


@pytest.mark.asyncio
async def test_auto_first_press_snaps_to_first_candidate(inter: MagicMock) -> None:
    box0 = _box(10, 10, 100, 100)
    box1 = _box(200, 200, 400, 400)
    view = _make_view_with_candidates(box0, box1)
    view.crop_box = _box(300, 300, 490, 490)  # simulate user having panned away
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)
    assert view.crop_box == box0


@pytest.mark.asyncio
async def test_auto_second_press_snaps_to_second_candidate(inter: MagicMock) -> None:
    box0 = _box(10, 10, 100, 100)
    box1 = _box(200, 200, 400, 400)
    view = _make_view_with_candidates(box0, box1)
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)
        await view._on_auto(inter)
    assert view.crop_box == box1


@pytest.mark.asyncio
async def test_auto_wraps_from_last_to_first(inter: MagicMock) -> None:
    box0 = _box(10, 10, 100, 100)
    box1 = _box(200, 200, 400, 400)
    view = _make_view_with_candidates(box0, box1)
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)  # → box0
        await view._on_auto(inter)  # → box1
        await view._on_auto(inter)  # → wraps back to box0
    assert view.crop_box == box0


@pytest.mark.asyncio
async def test_auto_label_is_always_plain_auto(inter: MagicMock) -> None:
    boxes = [_box(10, 10, 100, 100), _box(200, 200, 400, 400)]
    view = _make_view_with_candidates(*boxes)
    with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=_FAKE_JPEG):
        await view._on_auto(inter)
        await view._on_auto(inter)
    assert view._auto_btn.label == " Auto "  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_auto_noop_when_no_candidates(inter: MagicMock) -> None:
    view = _make_view()  # no candidate_boxes
    original_box = view.crop_box
    await view._on_auto(inter)
    assert view.crop_box == original_box
    inter.response.edit_message.assert_not_called()


# ── Failed post rolls back (bug #227) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_post_rolls_back_the_round_and_the_original(
    sync_db_path, tmp_path, monkeypatch, inter: MagicMock
) -> None:
    """The one wiring assertion this bug needs: the rollback is actually
    connected to the post.

    The rollback's own behaviour is covered at the service layer
    (tests/games/test_guess_cleanup_service.py). What can only be proved here
    is that a raising ``send`` reaches it at all — before this fix nothing
    caught it, so the row and the member's photo just stayed.
    """
    import discord

    from bot_modules.games.guess import cog as guess_cog
    from bot_modules.core.db_utils import open_db

    orig_dir = tmp_path / "orig"
    monkeypatch.setattr(guess_cog, "guess_orig_dir", lambda _db: orig_dir)

    view = _make_view()
    view.bot.ctx.db_path = sync_db_path
    view._original_bytes = b"a-members-photo"

    channel = MagicMock(spec=discord.TextChannel)
    channel.mention = "#guess"
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    inter.guild = MagicMock()
    inter.guild.id = GUILD_ID
    inter.guild.get_channel = MagicMock(return_value=channel)

    with (
        patch("bot_modules.games.guess.cog.render_crop", return_value=_FAKE_JPEG),
        patch(
            "bot_modules.economy.game_rewards.fire_member_trigger",
            AsyncMock(),
        ) as quest,
    ):
        await view._on_post(inter)

    with open_db(sync_db_path) as conn:
        rows = conn.execute(
            "SELECT deleted_at, original_path FROM guess_rounds"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["deleted_at"] is not None
    assert rows[0]["original_path"] == ""
    assert list(orig_dir.glob("*")) == []

    # No quest credit for a round that never went up.
    quest.assert_not_called()

    told = inter.edit_original_response.call_args.kwargs
    said = "\n".join(
        item.content
        for item in told["view"].walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "couldn't post" in said.lower() and "nothing was saved" in said.lower()
    # The editor's preview goes with it, and its timeout is retired so it
    # can't grey the editor back over this notice.
    assert told["attachments"] == []
    assert view.is_finished()
