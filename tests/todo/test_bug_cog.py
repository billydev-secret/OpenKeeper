"""Tests for todo/cog_bug.py — the /bug filing gate and the bug board glue.

What the gate *decides* is pinned in tests/todo/test_todo_service.py
(``bug_filing_refusal``); these check it is asked at every door — the command,
the board button, and again at modal submit — and that the board and its
Complete picker only ever touch the bug list.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.todo.cog_bug import BugAddModal, BugBoardView, BugCog
from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.todo.service import (
    BUG_FILING_KEY,
    LIST_BUG,
    create_todo,
    get_board,
    list_todos,
    save_board,
    save_change_cursors,
)
from tests.db_template import migrated_db

GUILD = 123


def _card_text(view) -> str:
    """Every line of text on a Components V2 card, top to bottom."""
    return "\n".join(
        i.content for i in view.walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _buttons(view) -> list:
    return [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]



class _FakeCtx:
    def __init__(self, db_path):
        self.db_path = db_path

    def open_db(self):
        return open_db(self.db_path)

    def is_mod(self, interaction):
        return bool(getattr(interaction.user.guild_permissions, "administrator", False))


def _member(*, mod: bool, user_id: int = 42) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = user_id
    m.guild_permissions = MagicMock(
        administrator=mod, manage_guild=False, manage_channels=False
    )
    return m


def _interaction(user: MagicMock) -> MagicMock:
    i = MagicMock(spec=discord.Interaction)
    i.user = user
    i.guild = MagicMock(id=GUILD)
    i.response = MagicMock()
    i.response.send_message = AsyncMock()
    i.response.send_modal = AsyncMock()
    i.original_response = AsyncMock()
    i.client = MagicMock()
    return i


@pytest.fixture
def db(tmp_path):
    return migrated_db(tmp_path / "bugs.db")


def _cog(db_path, guild=None) -> BugCog:
    bot = MagicMock()
    bot.ctx = _FakeCtx(db_path)
    bot.get_guild.return_value = guild
    return BugCog(bot)


def _open_filing(db_path) -> None:
    with open_db(db_path) as conn:
        set_config_value(conn, BUG_FILING_KEY, "everyone", GUILD)


def _bugs(db_path) -> list:
    with open_db(db_path) as conn:
        return list_todos(conn, GUILD, list_key=LIST_BUG)


def _reply(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


# ── /bug ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_member_is_refused_while_filing_is_closed(db):
    """The dark launch: until an admin opens the list, /bug is /todo's gate."""
    cog = _cog(db)
    interaction = _interaction(_member(mod=False))
    await cog.bug.callback(cog, interaction, "the shop is broken")
    assert _bugs(db) == []
    assert "moderators" in _reply(interaction)


@pytest.mark.asyncio
async def test_a_member_files_once_filing_is_open_and_lands_on_the_bug_list(db):
    _open_filing(db)
    cog = _cog(db)
    interaction = _interaction(_member(mod=False))
    await cog.bug.callback(cog, interaction, "  the shop is broken  ")
    [row] = _bugs(db)
    assert (row["task"], row["added_by"], row["list"]) == ("the shop is broken", 42, "bug")
    assert f"#{row['id']}" in _reply(interaction)
    with open_db(db) as conn:
        assert list_todos(conn, GUILD) == []


@pytest.mark.asyncio
async def test_a_second_filing_inside_the_cooldown_is_refused(db):
    _open_filing(db)
    cog = _cog(db)
    first, second = _interaction(_member(mod=False)), _interaction(_member(mod=False))
    await cog.bug.callback(cog, first, "one")
    await cog.bug.callback(cog, second, "two")
    assert [r["task"] for r in _bugs(db)] == ["one"]
    assert "file another" in _reply(second)


@pytest.mark.asyncio
async def test_moderators_file_while_closed_and_skip_the_cooldown(db):
    cog = _cog(db)
    for text in ("one", "two"):
        await cog.bug.callback(cog, _interaction(_member(mod=True)), text)
    assert sorted(r["task"] for r in _bugs(db)) == ["one", "two"]


@pytest.mark.asyncio
async def test_the_gate_asks_the_app_context_for_the_moderator_tier(db):
    cog = _cog(db)
    cog.bot.ctx.is_mod = MagicMock(return_value=False)
    interaction = _interaction(_member(mod=True))
    await cog.bug.callback(cog, interaction, "x")
    cog.bot.ctx.is_mod.assert_called_with(interaction)
    assert _bugs(db) == []


# ── the board's buttons ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_file_button_refuses_a_member_while_closed(db):
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=_cog(db)):
        await BugBoardView().add(interaction)
    interaction.response.send_modal.assert_not_awaited()
    assert "moderators" in _reply(interaction)


@pytest.mark.asyncio
async def test_the_file_button_opens_the_modal_for_a_member_once_open(db):
    _open_filing(db)
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=_cog(db)):
        await BugBoardView().add(interaction)
    assert isinstance(interaction.response.send_modal.await_args.args[0], BugAddModal)


@pytest.mark.asyncio
async def test_the_modal_rechecks_the_gate_at_submit(db):
    """Filing closed while the modal sat open: nothing is written."""
    modal = BugAddModal()
    modal.task._value = "the shop is broken"
    modal.notes._value = ""
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=_cog(db)):
        await modal.on_submit(interaction)
    assert _bugs(db) == []
    assert "moderators" in _reply(interaction)


@pytest.mark.asyncio
async def test_the_modal_files_with_details(db):
    _open_filing(db)
    modal = BugAddModal()
    modal.task._value = "the shop is broken"
    modal.notes._value = "clicked buy, nothing happened"
    interaction = _interaction(_member(mod=False))
    cog = _cog(db)
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=cog):
        await modal.on_submit(interaction)
    [row] = _bugs(db)
    assert row["description"] == "clicked buy, nothing happened"


@pytest.mark.asyncio
async def test_the_complete_button_refuses_members_even_when_filing_is_open(db):
    """Anyone can file; only mods close."""
    _open_filing(db)
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=_cog(db)), \
         patch("bot_modules.todo.cog_bug.pending_todos") as pending:
        await BugBoardView().complete(interaction)
    pending.assert_not_called()
    assert "close bugs" in _reply(interaction)


@pytest.mark.asyncio
async def test_closing_touches_only_the_bug_list(db):
    with open_db(db) as conn:
        todo = create_todo(conn, GUILD, 1, "a todo")
        bug = create_todo(conn, GUILD, 1, "a bug", list_key=LIST_BUG)
    cog = _cog(db)
    assert await cog.complete_bugs(GUILD, 7, [todo, bug]) == 1
    with open_db(db) as conn:
        [todo_row] = list_todos(conn, GUILD)
        [bug_row] = list_todos(conn, GUILD, list_key=LIST_BUG)
    assert todo_row["completed_at"] is None
    assert bug_row["completed_by"] == 7


def test_the_board_buttons_have_their_own_custom_ids():
    """Sharing an id with the todo board would route one board's clicks to the other."""
    ids = {item.custom_id for item in _buttons(BugBoardView())}
    assert ids == {"bug_board_add", "bug_board_complete"}


# ── the board ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_board_renders_bugs_and_keeps_its_own_placement(db):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 555
    message = MagicMock(spec=discord.Message)
    message.id = 666
    channel.send = AsyncMock(return_value=message)
    channel.get_partial_message = MagicMock(return_value=message)
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel.return_value = channel
    guild.get_channel_or_thread.return_value = channel
    guild.me = None
    with open_db(db) as conn:
        create_todo(conn, GUILD, 1, "a todo")
        create_todo(conn, GUILD, 1, "the shop is broken", list_key=LIST_BUG)
    cog = _cog(db, guild)
    assert await cog.place_board(guild, channel) is message
    text = _card_text(channel.send.await_args.kwargs["view"])
    assert "the shop is broken" in text
    assert "a todo" not in text
    assert "-# 1 pending bug ·" in text
    with open_db(db) as conn:
        assert get_board(conn, GUILD, list_key=LIST_BUG).message_id == 666
        assert not get_board(conn, GUILD).posted


# ── change cards ─────────────────────────────────────────────────────
#
# What a bug card says is pinned in tests/todo/test_todo_change_cards_logic.py; these
# check the glue — the right board's cursor, the right list's rows, and the
# restick after the card.


def _card_setup(db_path):
    """Both boards posted with live cursors, and one sendable channel."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 555
    channel.send = AsyncMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel = MagicMock(return_value=channel)
    with open_db(db_path) as conn:
        save_board(conn, GUILD, 444, 111)
        save_change_cursors(conn, GUILD, change_cursor_at=1)
        save_board(conn, GUILD, 555, 666, list_key=LIST_BUG)
        save_change_cursors(conn, GUILD, change_cursor_at=1, list_key=LIST_BUG)
    cog = _cog(db_path, guild)
    cog.board.schedule_restick = MagicMock()
    return cog, guild, channel


_SFW_CHANNEL = patch(
    "bot_modules.todo.change_cards.is_age_gated_channel", return_value=False
)


@pytest.mark.asyncio
async def test_a_new_bug_posts_a_card_then_resticks_the_bug_board(db):
    cog, guild, channel = _card_setup(db)
    with open_db(db) as conn:
        create_todo(conn, GUILD, 42, "the shop is broken", list_key=LIST_BUG)
    with _SFW_CHANNEL:
        assert await cog.post_change_cards(guild) is True
    guild.get_channel.assert_called_with(555)
    text = _card_text(channel.send.await_args.kwargs["view"])
    assert text == "## 🐛 1 new bug\n🐛 the shop is broken"
    cog.board.schedule_restick.assert_called_once_with(GUILD)
    with open_db(db) as conn:
        assert get_board(conn, GUILD, list_key=LIST_BUG).change_cursor_at > 1
        # The todo board's watermark is its own.
        assert get_board(conn, GUILD).change_cursor_at == 1


@pytest.mark.asyncio
async def test_a_new_todo_never_reaches_the_bug_board(db):
    cog, guild, channel = _card_setup(db)
    with open_db(db) as conn:
        create_todo(conn, GUILD, 42, "a todo, not a bug")
    with _SFW_CHANNEL:
        assert await cog.post_change_cards(guild) is False
    channel.send.assert_not_awaited()
    cog.board.schedule_restick.assert_not_called()


@pytest.mark.asyncio
async def test_the_boot_pass_banks_the_bug_clock_and_says_nothing(db):
    cog, guild, channel = _card_setup(db)
    with open_db(db) as conn:
        create_todo(conn, GUILD, 42, "filed while the bot was down", list_key=LIST_BUG)
    assert await cog.post_change_cards(guild, announce=False) is False
    channel.send.assert_not_awaited()
    with open_db(db) as conn:
        assert get_board(conn, GUILD, list_key=LIST_BUG).change_cursor_at > 1


@pytest.mark.asyncio
async def test_no_bug_board_means_no_bug_cards(db):
    cog = _cog(db)
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    assert await cog.post_change_cards(guild) is False


# ── screenshots ──────────────────────────────────────────────────────
#
# What is refused, held, cleared and stripped is pinned in
# tests/todo/test_todo_service.py and tests/todo/test_todo_change_cards_logic.py; these
# check each door asks, and that the card and the strip act on what they read.


def _upload(url="https://cdn.example/s.png", filename="s.png", ctype="image/png"):
    a = MagicMock(spec=discord.Attachment)
    a.url, a.filename, a.content_type, a.size = url, filename, ctype, 1000
    return a


def _open_screenshots(db_path) -> None:
    from bot_modules.todo.service import BUG_CHANGE_CARDS_KEY

    with open_db(db_path) as conn:
        set_config_value(conn, BUG_CHANGE_CARDS_KEY, "1", GUILD)
        save_board(conn, GUILD, 555, 666, list_key=LIST_BUG)


def _held(db_path) -> list:
    with open_db(db_path) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT attachment_urls, card_channel_id, card_message_id"
                " FROM todos ORDER BY id"
            )
        ]


@pytest.mark.asyncio
async def test_bug_refuses_a_screenshot_where_screenshots_are_off(db):
    """The option can't be hidden per server, so it is refused before filing."""
    _open_filing(db)
    cog = _cog(db)
    interaction = _interaction(_member(mod=False))
    await cog.bug.callback(cog, interaction, "the shop is broken", _upload())
    assert _bugs(db) == []
    assert "Screenshots aren't being taken" in _reply(interaction)


@pytest.mark.asyncio
async def test_bug_holds_a_screenshot_where_screenshots_are_on(db):
    _open_filing(db)
    _open_screenshots(db)
    cog = _cog(db)
    interaction = _interaction(_member(mod=False))
    await cog.bug.callback(cog, interaction, "the shop is broken", _upload())
    assert _held(db) == [('[{"url": "https://cdn.example/s.png", "ext": "png"}]', None, None)]
    assert "screenshot will be posted" in _reply(interaction)


@pytest.mark.asyncio
async def test_the_file_button_offers_uploads_only_where_screenshots_are_on(db):
    _open_filing(db)
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=_cog(db)):
        await BugBoardView().add(interaction)
        assert interaction.response.send_modal.await_args.args[0].upload is None
        _open_screenshots(db)
        await BugBoardView().add(interaction)
        assert interaction.response.send_modal.await_args.args[0].upload is not None


@pytest.mark.asyncio
async def test_the_modal_files_the_bug_even_when_its_screenshots_are_refused(db):
    """The member typed it out; a bad upload costs the picture, not the report."""
    _open_filing(db)
    _open_screenshots(db)
    modal = BugAddModal(screenshots=True)
    modal.task._value = "the shop is broken"
    modal.notes._value = ""
    assert modal.upload is not None
    modal.upload._values = [_upload(ctype="video/mp4")]
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=_cog(db)):
        await modal.on_submit(interaction)
    assert _held(db) == [(None, None, None)]
    assert "filed without them" in _reply(interaction)


@pytest.mark.asyncio
async def test_a_bug_card_posts_the_screenshots_and_remembers_where(db):
    cog, guild, channel = _card_setup(db)
    guild.filesize_limit = 10 * 1024 * 1024
    channel.send = AsyncMock(return_value=MagicMock(id=777))
    cog.bot.http.get_from_cdn = AsyncMock(return_value=b"png-bytes")
    with open_db(db) as conn:
        create_todo(
            conn, GUILD, 42, "the shop is broken", list_key=LIST_BUG,
            screenshots=[{"url": "https://cdn.example/s.png", "ext": "png"}],
        )
    with _SFW_CHANNEL:
        assert await cog.post_change_cards(guild) is True
    [file] = channel.send.await_args.kwargs["files"]
    assert file.filename.startswith("bug-") and file.filename.endswith("-1.png")
    # A V2 message shows no upload a component doesn't reference.
    [gallery] = [
        i for i in channel.send.await_args.kwargs["view"].walk_children()
        if isinstance(i, discord.ui.MediaGallery)
    ]
    [shot] = gallery.items
    assert shot.media.url == f"attachment://{file.filename}"
    assert shot.spoiler is False
    # The link is gone once posted; the card is what closing will edit.
    assert _held(db) == [(None, 555, 777)]


@pytest.mark.asyncio
async def test_closing_a_bug_edits_only_its_screenshots_off_the_card(db):
    from bot_modules.todo.service import settle_bug_screenshots

    with open_db(db) as conn:
        fixed = create_todo(conn, GUILD, 42, "a", list_key=LIST_BUG)
        other = create_todo(conn, GUILD, 42, "b", list_key=LIST_BUG)
        settle_bug_screenshots(
            conn, GUILD, upto_ts=0, posted_bug_ids=[fixed, other],
            channel_id=555, message_id=777,
        )
    mine, theirs = MagicMock(filename=f"bug-{fixed}-1.png"), MagicMock(
        filename=f"bug-{other}-1.png"
    )
    # An embed-era card: posted before the Components V2 conversion, its
    # screenshots plain attachments. Those are still in the channel.
    card = MagicMock(attachments=[mine, theirs])
    card.flags.components_v2 = False
    card.edit = AsyncMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=card)
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel = MagicMock(return_value=channel)
    cog = _cog(db, guild)
    await cog.complete_bugs(GUILD, 1, [fixed])

    await cog.strip_closed_screenshots()

    channel.fetch_message.assert_awaited_once_with(777)
    card.edit.assert_awaited_once_with(attachments=[theirs])
    assert _held(db) == [(None, None, None), (None, 555, 777)]


@pytest.mark.asyncio
async def test_a_screenshot_that_wont_download_costs_the_picture_not_the_card(db):
    cog, guild, channel = _card_setup(db)
    guild.filesize_limit = 10 * 1024 * 1024
    channel.send = AsyncMock(return_value=MagicMock(id=777))
    cog.bot.http.get_from_cdn = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "expired")
    )
    with open_db(db) as conn:
        create_todo(
            conn, GUILD, 42, "the shop is broken", list_key=LIST_BUG,
            screenshots=[{"url": "https://cdn.example/s.png", "ext": "png"}],
        )
    with _SFW_CHANNEL:
        assert await cog.post_change_cards(guild) is True
    assert "files" not in channel.send.await_args.kwargs
    assert _held(db) == [(None, None, None)]


@pytest.mark.parametrize(
    "error, forgotten",
    [
        pytest.param(discord.Forbidden(MagicMock(status=403), "no"), True, id="refused-gives-up"),
        pytest.param(discord.NotFound(MagicMock(status=404), "gone"), True, id="card-gone"),
        pytest.param(discord.HTTPException(MagicMock(status=500), "oops"), False, id="transient-retries"),
    ],
)
@pytest.mark.asyncio
async def test_a_strip_that_fails_gives_up_only_when_retrying_cannot_help(
    db, error, forgotten
):
    from bot_modules.todo.service import settle_bug_screenshots

    with open_db(db) as conn:
        fixed = create_todo(conn, GUILD, 42, "a", list_key=LIST_BUG)
        settle_bug_screenshots(
            conn, GUILD, upto_ts=0, posted_bug_ids=[fixed],
            channel_id=555, message_id=777,
        )
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(side_effect=error)
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel = MagicMock(return_value=channel)
    cog = _cog(db, guild)
    await cog.complete_bugs(GUILD, 1, [fixed])
    await cog.strip_closed_screenshots()
    assert _held(db) == [(None, None, None) if forgotten else (None, 555, 777)]


# ── screenshots on a Components V2 card ──────────────────────────────
#
# A fetched V2 message reports no attachments, so the strip rebuilds the
# gallery: what stays is downloaded from the card and uploaded again, and the
# edit replaces the message's files with exactly those.


def _v2_card(filenames):
    """A posted V2 bug card as ``fetch_message`` hands it back, plus the view
    ``LayoutView.from_message`` would rebuild from it."""
    from bot_modules.core.layout import card as as_view
    from bot_modules.todo.layouts import build_change_card_layout

    items = build_change_card_layout("🐛 2 new bugs", "🐛 a\n🐛 b")
    if filenames:
        items.append(
            discord.ui.MediaGallery(
                *(
                    discord.MediaGalleryItem(
                        f"https://cdn.discordapp.com/attachments/5/6/{name}?ex=1&hm=2",
                        description=f"alt {name}",
                    )
                    for name in filenames
                )
            )
        )
    message = MagicMock(attachments=[], id=777)
    message.flags.components_v2 = True
    message.edit = AsyncMock()
    return message, as_view(items)


async def _strip(db, message, view, bug_ids_to_close, *, fetch):
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=message)
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel = MagicMock(return_value=channel)
    cog = _cog(db, guild)
    cog.bot.http.get_from_cdn = fetch
    await cog.complete_bugs(GUILD, 1, bug_ids_to_close)
    with patch.object(discord.ui.LayoutView, "from_message", return_value=view):
        await cog.strip_closed_screenshots()


def _two_bugs_on_one_card(db):
    from bot_modules.todo.service import settle_bug_screenshots

    with open_db(db) as conn:
        fixed = create_todo(conn, GUILD, 42, "a", list_key=LIST_BUG)
        other = create_todo(conn, GUILD, 42, "b", list_key=LIST_BUG)
        settle_bug_screenshots(
            conn, GUILD, upto_ts=0, posted_bug_ids=[fixed, other],
            channel_id=555, message_id=777,
        )
    return fixed, other


def _gallery(view):
    return [i for i in view.walk_children() if isinstance(i, discord.ui.MediaGallery)]


@pytest.mark.asyncio
async def test_closing_a_bug_rebuilds_a_v2_cards_gallery_without_its_screenshots(db):
    fixed, other = _two_bugs_on_one_card(db)
    message, view = _v2_card([f"bug-{fixed}-1.png", f"bug-{other}-1.png"])
    fetch = AsyncMock(return_value=b"kept-bytes")

    await _strip(db, message, view, [fixed], fetch=fetch)

    # Only the survivor is downloaded, and it goes back up under its own name
    # so closing *its* bug later can still find it.
    fetch.assert_awaited_once()
    assert f"bug-{other}-1.png" in fetch.await_args.args[0]
    kwargs = message.edit.await_args.kwargs
    [file] = kwargs["attachments"]
    assert file.filename == f"bug-{other}-1.png"
    [gallery] = _gallery(kwargs["view"])
    [shot] = gallery.items
    assert shot.media.url == f"attachment://bug-{other}-1.png"
    assert shot.description == f"alt bug-{other}-1.png"
    # The card's words are never touched.
    assert _card_text(kwargs["view"]) == "## 🐛 2 new bugs\n🐛 a\n🐛 b"
    assert _held(db) == [(None, None, None), (None, 555, 777)]


@pytest.mark.asyncio
async def test_a_v2_card_left_with_no_screenshots_loses_its_gallery(db):
    fixed, other = _two_bugs_on_one_card(db)
    message, view = _v2_card([f"bug-{fixed}-1.png", f"bug-{fixed}-2.png"])
    fetch = AsyncMock()

    await _strip(db, message, view, [fixed], fetch=fetch)

    fetch.assert_not_awaited()
    kwargs = message.edit.await_args.kwargs
    assert kwargs["attachments"] == []
    assert _gallery(kwargs["view"]) == []


@pytest.mark.asyncio
async def test_a_survivor_that_wont_download_is_lost_rather_than_blocking_the_removal(db):
    """The removal is the promise: a closed bug's screenshot comes down even
    when carrying another bug's picture over fails."""
    fixed, other = _two_bugs_on_one_card(db)
    message, view = _v2_card([f"bug-{fixed}-1.png", f"bug-{other}-1.png"])
    fetch = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "expired"))

    await _strip(db, message, view, [fixed], fetch=fetch)

    kwargs = message.edit.await_args.kwargs
    assert kwargs["attachments"] == []
    assert _gallery(kwargs["view"]) == []
    assert _held(db)[0] == (None, None, None)


@pytest.mark.asyncio
async def test_a_v2_card_whose_gallery_holds_none_of_the_bugs_images_is_left_alone(db):
    fixed, other = _two_bugs_on_one_card(db)
    message, view = _v2_card([f"bug-{other}-1.png"])

    await _strip(db, message, view, [fixed], fetch=AsyncMock())

    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bug_cards_screenshots_are_spoilered_in_an_age_gated_channel(db):
    cog, guild, channel = _card_setup(db)
    guild.filesize_limit = 10 * 1024 * 1024
    channel.send = AsyncMock(return_value=MagicMock(id=777))
    cog.bot.http.get_from_cdn = AsyncMock(return_value=b"png-bytes")
    with open_db(db) as conn:
        create_todo(
            conn, GUILD, 42, "the shop is broken", list_key=LIST_BUG,
            screenshots=[{"url": "https://cdn.example/s.png", "ext": "png"}],
        )
    with patch("bot_modules.todo.change_cards.is_age_gated_channel", return_value=True):
        assert await cog.post_change_cards(guild) is True
    [gallery] = _gallery(channel.send.await_args.kwargs["view"])
    assert all(shot.spoiler for shot in gallery.items)


# ── the Complete picker ──────────────────────────────────────────────


def _picked(view, values):
    """Discord fills ``values`` from the interaction; a test sets it directly."""
    view.select._values = [str(v) for v in values]
    return view


@pytest.mark.asyncio
async def test_the_picker_closes_the_picked_bugs_and_redraws_into_the_outcome(db):
    from bot_modules.todo.cog_bug import BugCompleteView

    with open_db(db) as conn:
        bug_id = create_todo(conn, GUILD, 42, "the shop is broken", list_key=LIST_BUG)
    cog = _cog(db)
    cog.refresh_board = AsyncMock()
    view = _picked(BugCompleteView([{"id": bug_id, "task": "the shop is broken"}]), [bug_id])
    interaction = _interaction(_member(mod=True))
    interaction.response.edit_message = AsyncMock()
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=cog):
        await view.picked(interaction)

    assert _bugs(db)[0]["completed_at"] is not None
    outcome = interaction.response.edit_message.await_args.kwargs["view"]
    assert _card_text(outcome) == "## ✅ Bugs Fixed\nMarked **1** bug fixed."
    # Stopped, so its timeout can't edit the greyed picker back over the outcome.
    assert view.is_finished()
    cog.refresh_board.assert_awaited_once_with(GUILD)


@pytest.mark.asyncio
async def test_the_picker_rechecks_the_moderator_gate(db):
    from bot_modules.todo.cog_bug import BugCompleteView

    with open_db(db) as conn:
        bug_id = create_todo(conn, GUILD, 42, "the shop is broken", list_key=LIST_BUG)
    view = _picked(BugCompleteView([{"id": bug_id, "task": "x"}]), [bug_id])
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.todo.cog_bug._resolve_cog", return_value=_cog(db)):
        await view.picked(interaction)
    assert "Only moderators" in _reply(interaction)
    assert _bugs(db)[0]["completed_at"] is None


def test_the_picker_expires_inside_an_ephemeral_messages_editable_window():
    """Past 15 minutes Discord refuses the grey-out edit, silently."""
    from bot_modules.todo import cog as todo_cog, cog_bug

    assert 0 < cog_bug.PICKER_TIMEOUT < 15 * 60
    assert 0 < todo_cog.PICKER_TIMEOUT < 15 * 60
