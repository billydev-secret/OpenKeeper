"""Cog-level: Guess button + picker flow.

Since the Components V2 conversion (2026-09-30) the pickers are
``WhisperGuessNativeView`` (in a guild — Discord's member select) and
``WhisperGuessListView`` (from a DM — the opt-in pool, paged), and every
answer edits the picker into a card: assertions read the ``view=`` kwarg's
text, never ``content=``. The exhausted and solved outcomes redraw the DM
through ``_redraw_dm``, which fetches it by ``dm_msg_id``.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
import pytest

from bot_modules.whisper.layouts import GUESS_PICK_ID
from bot_modules.whisper.models import Whisper, WhisperConfig
from bot_modules.whisper.service import GuessOutcome
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")

SENDER, TARGET = 1001, 2001
FEED = 8001


def _w(*, solved: bool = False, guesses_left: int = 3) -> Whisper:
    return Whisper(
        id=42, guild_id=9001, sender_id=SENDER, target_id=TARGET, message="hi",
        created_at=time.time(), state="pending", solved=solved, exposed=False,
        guesses_left=guesses_left, channel_msg_id=88888, dm_msg_id=99999,
    )


ROLE = 7001


def _cfg(role_id: int = ROLE, *, sender_feedback: bool = False) -> WhisperConfig:
    return WhisperConfig(
        guild_id=9001, role_id=role_id, channel_id=FEED, log_channel_id=8002,
        sender_feedback=sender_feedback,
    )


def _pool_member(uid: int, **kw) -> FakeMember:
    """A member holding the Whisper role — what the native picker must accept."""
    return FakeMember(id=uid, roles=[FakeRole(id=ROLE)], **kw)


def _bot():
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    # The DM redraw brands the card for the guild; None keeps it unstamped.
    bot.get_guild = MagicMock(return_value=None)
    return bot


def _make_guess_button(whisper_id: int = 42):
    from bot_modules.whisper.cog import WhisperGuessButton
    return WhisperGuessButton(_bot(), whisper_id)


def _make_members(n: int, exclude_id: int = TARGET) -> list[FakeMember]:
    return [
        FakeMember(id=5000 + i, display_name=f"Member{i:03d}")
        for i in range(n)
        if (5000 + i) != exclude_id
    ]


def _card_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def _button_ids(view: discord.ui.LayoutView) -> list[str | None]:
    return [b.custom_id for b in view.walk_children() if isinstance(b, discord.ui.Button)]


def _select(view: discord.ui.LayoutView):
    return next(i for i in view.walk_children() if getattr(i, "custom_id", None) == GUESS_PICK_ID)


def _fetched_dm(bot) -> MagicMock:
    """Route ``_redraw_dm``'s fetch path to a DM message; returns it."""
    target = FakeMember(id=TARGET)
    dm_msg = target.create_dm.return_value.fetch_message.return_value
    bot.get_user = MagicMock(return_value=target)
    return dm_msg


# ── Button-level: pre-checks ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_button_non_target_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "recipient" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_already_solved_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(solved=True)):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_guess_button_no_guesses_left_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(guesses_left=0)):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_guess_button_no_guild_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_guild_context_uses_native_picker():
    """In a guild interaction the native avatar picker (UserSelect) is shown
    instead of the pool list, and the view keeps its message for the
    expiry edit."""
    from bot_modules.whisper.cog import WhisperGuessNativeView
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()):
        await button.callback(interaction)

    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    view = kwargs["view"]
    assert isinstance(view, WhisperGuessNativeView)
    assert isinstance(_select(view), discord.ui.UserSelect)
    assert view.message is interaction.original_response.return_value


# The pool list (with role/member lookup + pagination) is the DM fallback now —
# auto-populated user selects can't resolve guild members from a DM. These
# tests run in DM context (interaction.guild is None; the guild is resolved
# server-side via bot.get_guild).


@pytest.mark.asyncio
async def test_guess_button_role_not_configured_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    button.bot.get_guild = MagicMock(return_value=MagicMock())
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg(role_id=0)):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "role" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_role_missing_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    guild = MagicMock()
    guild.get_role = MagicMock(return_value=None)
    button.bot.get_guild = MagicMock(return_value=guild)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "role" in args[0].lower()


@pytest.mark.asyncio
async def test_guess_button_empty_member_list_rejected():
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    guild = MagicMock()
    role = MagicMock()
    role.members = [FakeMember(id=TARGET)]  # only the target themselves
    guild.get_role = MagicMock(return_value=role)
    button.bot.get_guild = MagicMock(return_value=guild)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "no other" in args[0].lower()


# ── Button-level: happy path (DM pool list) ──────────────────────────────────

@pytest.mark.asyncio
async def test_guess_button_dm_context_sends_the_pool_list():
    """From a DM the picker is the opt-in pool minus the target, as a string
    select; with a page or fewer there are no arrows, only Filter."""
    from bot_modules.whisper.cog import WhisperGuessListView
    button = _make_guess_button()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = None
    guild = MagicMock()
    role = MagicMock()
    role.members = [*_make_members(5), FakeMember(id=TARGET, display_name="Me")]
    guild.get_role = MagicMock(return_value=role)
    button.bot.get_guild = MagicMock(return_value=guild)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await button.callback(interaction)

    _, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    view = kwargs["view"]
    assert isinstance(view, WhisperGuessListView)
    select = _select(view)
    assert isinstance(select, discord.ui.Select)
    assert [o.value for o in select.options] == [str(5000 + i) for i in range(5)]
    assert "◀" not in [b.label for b in view.walk_children() if isinstance(b, discord.ui.Button)]


# ── Native avatar picker (UserSelect) callback ───────────────────────────────

def _make_native_view():
    from bot_modules.whisper.cog import WhisperGuessNativeView
    return WhisperGuessNativeView(_bot(), _w(), invoker_id=TARGET)


@pytest.mark.asyncio
async def test_user_select_rejects_bot_without_consuming_guess():
    view = _make_native_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    picked_bot = FakeMember(id=4242, bot=True)

    with patch.object(discord.ui.UserSelect, "values", new_callable=PropertyMock,
                      return_value=[picked_bot]), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_record_guess") as rec:
        await view._on_pick(interaction)

    rec.assert_not_called()  # a stray bot pick must not burn an attempt
    answer = interaction.response.edit_message.call_args.kwargs["view"]
    assert "bot" in _card_text(answer).lower()
    assert _button_ids(answer) == []


@pytest.mark.asyncio
async def test_user_select_rejects_member_outside_pool_without_consuming_guess():
    """rotation-rooms-167: the native picker offers every member of the
    server, so a pick with no Whisper role cannot be the sender. It is
    refused as a free pass, not a burned third of the target's guesses."""
    view = _make_native_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()
    outsider = FakeMember(id=4343, roles=[FakeRole(id=1)])

    with patch.object(discord.ui.UserSelect, "values", new_callable=PropertyMock,
                      return_value=[outsider]), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_record_guess") as rec:
        await view._on_pick(interaction)

    rec.assert_not_called()
    answer = interaction.response.edit_message.call_args.kwargs["view"]
    assert _card_text(answer) == "❌ They aren't in the Whisper pool — that one's free."


@pytest.mark.asyncio
async def test_user_select_correct_records_and_posts_to_feed():
    view = _make_native_view()
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.response.edit_message = AsyncMock()
    dm_msg = _fetched_dm(view.bot)

    with patch.object(discord.ui.UserSelect, "values", new_callable=PropertyMock,
                      return_value=[_pool_member(SENDER)]), \
         patch("bot_modules.whisper.cog._do_load_whisper",
               side_effect=[_w(), _w(solved=True)]), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_record_guess") as rec:
        await view._on_pick(interaction)

    rec.assert_called_once_with(":memory:", whisper_id=42, guessed_id=SENDER, correct=True)
    feed_channel.send.assert_awaited_once()
    assert "Whisper Solved" in _card_text(feed_channel.send.call_args.kwargs["view"])
    answer = interaction.response.edit_message.call_args.kwargs["view"]
    assert "solved" in _card_text(answer).lower()
    # Solved: the DM redraws without its Guess button.
    dm_msg.edit.assert_awaited_once()
    assert "whisper:guess:42" not in _button_ids(dm_msg.edit.call_args.kwargs["view"])


# ── Navigation ────────────────────────────────────────────────────────────────

def _make_list_view(page: int = 0, members=None):
    from bot_modules.whisper.cog import WhisperGuessListView
    if members is None:
        members = _make_members(30)
    return WhisperGuessListView(_bot(), _w(), members, invoker_id=TARGET, page=page)


@pytest.mark.asyncio
async def test_guess_select_next_advances_page():
    view = _make_list_view()
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    await view._on_next(interaction)

    assert view._page == 1
    assert interaction.response.edit_message.call_args.kwargs["view"] is view
    assert [o.value for o in _select(view).options] == [str(5000 + i) for i in range(25, 30)]


@pytest.mark.asyncio
async def test_guess_select_prev_retreats_page():
    view = _make_list_view(page=1)
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    await view._on_prev(interaction)

    assert view._page == 0


@pytest.mark.asyncio
async def test_guess_select_filter_narrows_the_pool_and_clear_restores_it():
    view = _make_list_view()
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    view.apply_filter("Member02")
    labels = [o.label for o in _select(view).options]
    # Prefix matches rank first; the subsequence matches (Member002 …) trail.
    assert labels[:10] == [f"Member02{i}" for i in range(10)]
    assert len(labels) < 25
    assert view._filter_query == "Member02"

    await view._on_clear_filter(interaction)
    assert view._filter_query == ""
    assert len(_select(view).options) == 25


# ── Pool list callback: outcomes ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_select_correct_posts_to_feed_and_edits_message():
    view = _make_list_view(members=_make_members(3))
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.response.edit_message = AsyncMock()
    _fetched_dm(view.bot)

    with patch.object(discord.ui.Select, "values", new_callable=PropertyMock,
                      return_value=[str(SENDER)]), \
         patch("bot_modules.whisper.cog._do_load_whisper",
               side_effect=[_w(), _w(solved=True)]), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_record_guess") as rec:
        await view._on_pick(interaction)

    rec.assert_called_once_with(":memory:", whisper_id=42, guessed_id=SENDER, correct=True)
    feed_channel.send.assert_awaited_once()
    answer = interaction.response.edit_message.call_args.kwargs["view"]
    text = _card_text(answer).lower()
    assert "solved" in text or "right" in text
    assert view.is_finished()


@pytest.mark.asyncio
async def test_guess_select_wrong_shows_remaining_count():
    view = _make_list_view(members=_make_members(3))
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.edit_message = AsyncMock()

    with patch.object(discord.ui.Select, "values", new_callable=PropertyMock,
                      return_value=["9999"]), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w(guesses_left=3)), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._redraw_dm", new_callable=AsyncMock) as redraw, \
         patch("bot_modules.whisper.cog._do_record_guess"):
        await view._on_pick(interaction)

    answer = interaction.response.edit_message.call_args.kwargs["view"]
    assert "2 guesses left" in _card_text(answer)
    assert _button_ids(answer) == []
    # The DM card carries the guesses-left count, so a wrong guess redraws it
    # too, not only a solve or the last guess.
    redraw.assert_awaited_once()


@pytest.mark.asyncio
async def test_guess_select_exhausted_removes_guess_button_from_dm():
    view = _make_list_view(members=_make_members(3))
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.guild = MagicMock()
    interaction.response.edit_message = AsyncMock()
    dm_msg = _fetched_dm(view.bot)

    with patch.object(discord.ui.Select, "values", new_callable=PropertyMock,
                      return_value=["9999"]), \
         patch("bot_modules.whisper.cog._do_load_whisper",
               side_effect=[_w(guesses_left=1), _w(guesses_left=0)]), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_record_guess"):
        await view._on_pick(interaction)

    dm_msg.edit.assert_awaited_once()
    edit_kwargs = dm_msg.edit.call_args.kwargs
    # A pre-conversion embed DM becomes a V2 card on this edit.
    assert edit_kwargs["embed"] is None
    ids = _button_ids(edit_kwargs["view"])
    assert "whisper:share:42" in ids
    assert "whisper:delete:42" in ids
    assert "whisper:guess:42" not in ids

    answer = interaction.response.edit_message.call_args.kwargs["view"]
    assert "out of guesses" in _card_text(answer).lower()


@pytest.mark.asyncio
async def test_guess_select_race_lost_answers_without_recording():
    view = _make_list_view(members=_make_members(3))
    interaction = fake_interaction(user=FakeMember(id=TARGET))
    interaction.response.edit_message = AsyncMock()

    with patch.object(discord.ui.Select, "values", new_callable=PropertyMock,
                      return_value=["9999"]), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_w()), \
         patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_record_guess", return_value=False):
        await view._on_pick(interaction)

    answer = interaction.response.edit_message.call_args.kwargs["view"]
    assert "another tab" in _card_text(answer)


# ── Sender feedback DM (2026-09 review, rotation-rooms-159) ──────────────────
#
# The copy is pinned in tests/whisper/test_whisper_logic.py; these cover the cog's
# gates around it: the dial, the sender's pool membership, the no-contact
# degrade, and that the outcome path actually calls it.

WRONG = GuessOutcome(correct=False, attempts_remaining=2, exhausted=False)


def _guild_with(*members: FakeMember) -> FakeGuild:
    return FakeGuild(id=9001, members={m.id: m for m in members})


async def _notify(db_path, guild, cfg, *, guesses=((4444, WRONG),), blocked=False):
    """Make the guesses, then let their batch fall due and go out — the note is
    queued on dm_batch at guess time and delivered by the kind's handler."""
    from bot_modules.whisper.cog import _deliver_guess_batch, _notify_sender_of_guess
    from bot_modules.core.db_utils import open_db
    from bot_modules.dm.batch.store import claim_due

    bot = MagicMock()
    bot.ctx.db_path = db_path
    with patch("bot_modules.whisper.cog.send_branded_dm", AsyncMock()) as dm, \
         patch("bot_modules.whisper.cog.no_contact_service.is_no_contact_conn",
               return_value=blocked), \
         patch("bot_modules.whisper.cog._load_config", return_value=cfg), \
         patch("bot_modules.whisper.cog.build_name_fn",
               AsyncMock(return_value=lambda uid: f"User{uid}")):
        for guessed_id, outcome in guesses:
            await _notify_sender_of_guess(
                bot, guild, cfg, _w(), guessed_id=guessed_id, outcome=outcome,
            )
        with open_db(db_path) as conn:
            batches = claim_due(conn, now=10**12)
        for batch in batches:
            await _deliver_guess_batch(bot, guild, guild.get_member(batch[0].user_id), batch)
    return dm


def _digest_text(dm) -> str:
    return _card_text(dm.call_args.kwargs["layout"])


@pytest.mark.asyncio
async def test_sender_is_dmed_with_the_guessed_name_when_the_dial_is_on(sync_db_path):
    sender = _pool_member(SENDER)
    dm = await _notify(sync_db_path, _guild_with(sender), _cfg(sender_feedback=True))

    dm.assert_awaited_once()
    assert dm.call_args.args[0] is sender
    assert _digest_text(dm) == (
        "## 📬 Guesses on Whisper #42\nThey guessed User4444. Wrong, 2 left."
    )


@pytest.mark.parametrize(
    ("cfg", "sender"),
    [
        pytest.param(_cfg(sender_feedback=False), _pool_member(SENDER), id="dial-off"),
        pytest.param(_cfg(sender_feedback=True), FakeMember(id=SENDER), id="sender-opted-out"),
        pytest.param(_cfg(sender_feedback=True), None, id="sender-left-the-server"),
    ],
)
@pytest.mark.asyncio
async def test_sender_is_not_dmed(cfg, sender, sync_db_path):
    """Ships dark, and honours ``/whisper optout`` — a sender who dropped the
    role has left the game and its DMs with it."""
    guild = _guild_with(sender) if sender is not None else _guild_with()
    dm = await _notify(sync_db_path, guild, cfg)
    dm.assert_not_called()


@pytest.mark.asyncio
async def test_no_contact_pair_degrades_the_guessed_name_to_someone(sync_db_path):
    """Naming a blocked party to the other side is contact. The degraded
    line reads like any other wrong guess, so the sender can't tell — and the
    row is not dropped at send, which would leave a gap in the countdown."""
    dm = await _notify(
        sync_db_path, _guild_with(_pool_member(SENDER)), _cfg(sender_feedback=True),
        blocked=True,
    )
    text = _digest_text(dm)
    assert text == "## 📬 Guesses on Whisper #42\nThey guessed someone. Wrong, 2 left."
    assert "4444" not in text


@pytest.mark.asyncio
async def test_guesses_inside_one_window_arrive_as_one_dm(sync_db_path):
    last = GuessOutcome(correct=False, attempts_remaining=0, exhausted=True)
    dm = await _notify(
        sync_db_path, _guild_with(_pool_member(SENDER)), _cfg(sender_feedback=True),
        guesses=((4444, WRONG), (5555, last)),
    )
    dm.assert_awaited_once()
    assert _digest_text(dm) == (
        "## 📬 Guesses on Whisper #42\n"
        "They guessed User4444. Wrong, 2 left.\n"
        "They guessed User5555. Wrong — they're out of guesses. You're safe."
    )


@pytest.mark.asyncio
async def test_turning_the_dial_off_mid_window_drops_the_batch(sync_db_path):
    from bot_modules.whisper.cog import _deliver_guess_batch, _notify_sender_of_guess
    from bot_modules.core.db_utils import open_db
    from bot_modules.dm.batch.store import claim_due

    sender = _pool_member(SENDER)
    guild = _guild_with(sender)
    bot = MagicMock()
    bot.ctx.db_path = sync_db_path
    await _notify_sender_of_guess(
        bot, guild, _cfg(sender_feedback=True), _w(), guessed_id=4444, outcome=WRONG,
    )
    with open_db(sync_db_path) as conn:
        (batch,) = claim_due(conn, now=10**12)
    with patch("bot_modules.whisper.cog.send_branded_dm", AsyncMock()) as dm, \
         patch("bot_modules.whisper.cog._load_config",
               return_value=_cfg(sender_feedback=False)):
        await _deliver_guess_batch(bot, guild, sender, batch)
    dm.assert_not_called()


@pytest.mark.asyncio
async def test_guess_outcome_notifies_the_sender_after_answering_the_target():
    """One wiring assertion: the outcome helper reaches the notifier with the
    recorded guess, and only after the target's own reply has gone out."""
    from bot_modules.whisper.cog import _handle_guess_outcome

    bot = _bot()
    guild = _guild_with(_pool_member(SENDER))
    interaction = fake_interaction(user=FakeMember(id=TARGET), guild=guild)
    order: list[str] = []
    interaction.response.edit_message = AsyncMock(side_effect=lambda **_: order.append("target"))
    notify = AsyncMock(side_effect=lambda *a, **k: order.append("sender"))
    cfg = _cfg(sender_feedback=True)
    whisper = _w()

    with patch("bot_modules.whisper.cog._do_record_guess", return_value=True), \
         patch("bot_modules.whisper.cog._load_config", return_value=cfg), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=whisper), \
         patch("bot_modules.whisper.cog._redraw_dm", new_callable=AsyncMock), \
         patch("bot_modules.whisper.cog._notify_sender_of_guess", notify):
        await _handle_guess_outcome(interaction, bot, whisper, 4444)

    notify.assert_awaited_once_with(bot, guild, cfg, whisper, guessed_id=4444, outcome=WRONG)
    assert order == ["target", "sender"]
