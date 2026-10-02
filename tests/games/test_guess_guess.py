"""Cog-level tests for the Guess guess flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.games.guess.models import BoundingBox, GuessRound
from tests.fakes import FakeMember, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_no_contact")




@pytest.fixture(autouse=True)
def _patch_count_user_guesses():
    """Default per-test guess count to 0; cap tests override via their own patch."""
    with patch("bot_modules.games.guess.cog._do_count_user_guesses", return_value=0) as m:
        yield m


@pytest.fixture(autouse=True)
def _stub_name_resolver(monkeypatch):
    """The cards resolve member ids to display names; build_name_fn reads
    ``known_users``, which a bare ``:memory:`` db_path has no schema for. The
    resolver's own fallback chain is covered in
    tests/shared/test_name_resolver_logic.py, and the builders in
    tests/games/test_guess_layouts.py — here it only has to answer.

    monkeypatch, not mock.patch: a cog fixture whose mock outlives it stubs the
    real helper for a whole xdist worker (see the no-contact stub leak).
    """
    async def _fake_build_name_fn(**_kwargs):
        return lambda uid: f"Member{uid}"

    monkeypatch.setattr(
        "bot_modules.games.guess.cog.build_name_fn", _fake_build_name_fn
    )

GUESS_ROLE_ID = 7001
ROUND_ID = 99


@pytest.fixture(autouse=True)
def _no_lobby_art(monkeypatch):
    """The public cards read the guild's lobby strip; the mocked bots here
    have no branding table to read it from."""
    from bot_modules.games.utils.lobby_art import NO_ART

    monkeypatch.setattr(
        "bot_modules.games.guess.cog.read_lobby_art", AsyncMock(return_value=NO_ART)
    )


def _view_text(view) -> str:
    """Every line of text on a Components V2 card."""
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _said(interaction) -> str:
    """What the picker card says after a pick — it redraws in place."""
    return _view_text(interaction.edit_original_response.call_args.kwargs["view"])


def _game_message(*, embed_era: bool = False):
    """The round's public message. ``embed_era`` is one posted before the
    Components V2 conversion: an embed with the crop as a plain attachment."""
    msg = MagicMock()
    msg.edit = AsyncMock()
    msg.guild = MagicMock()
    if embed_era:
        upload = MagicMock()
        upload.filename = "SPOILER_old_crop.png"
        upload.read = AsyncMock(return_value=b"old-crop-bytes")
        msg.attachments = [upload]
        msg.flags.components_v2 = False
    else:
        msg.attachments = []
        msg.flags.components_v2 = True
    return msg


def _make_round(
    *,
    round_id: int = ROUND_ID,
    submitter_id: int = 1001,
    answer_id: int = 2001,
    solved_at: float | None = None,
) -> GuessRound:
    return GuessRound(
        id=round_id,
        guild_id=9001,
        submitter_id=submitter_id,
        answer_id=answer_id,
        channel_id=8001,
        message_id=12345,
        crop_path="/tmp/fake.jpg",
        crop_url="https://cdn.discord.com/fake.jpg",
        original_path="",
        difficulty="medium",
        candidate_count=1,
        reroll_count=0,
        created_at=1000.0,
        solved_at=solved_at,
        solver_id=None,
        guesses_to_solve=None,
        unique_guessers_to_solve=None,
        answer_optout=False,
        deleted_at=None,
    )


def _make_select_view(
    bot=None,
    guess_members=None,
    game_message=None,
    round_id: int = ROUND_ID,
    cooldown_seconds: int = 0,
    max_guesses_per_round: int = 5,
):
    from bot_modules.games.guess.cog import GuessSelectView

    if bot is None:
        bot = MagicMock()
        bot.ctx.db_path = ":memory:"
    if guess_members is None:
        guess_members = [FakeMember(id=2001, display_name="Alice")]
    if game_message is None:
        game_message = _game_message()

    return GuessSelectView(
        bot, round_id, guess_members, game_message,  # type: ignore[arg-type]
        cooldown_seconds=cooldown_seconds,
        max_guesses_per_round=max_guesses_per_round,
    )


# ── GuessSelectView tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_first_guess_marks_solved_and_edits_message():
    game_msg = _game_message()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()  # not yet solved

    with patch("bot_modules.games.guess.cog._do_count_user_guesses", return_value=0), \
         patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 3, 2)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_called_once()
    assert "correct" in _said(interaction).lower()

    # The one wiring assertion: the reveal edit carries resolved names, not
    # raw <@id>s (tests/games/test_guess_layouts.py owns the rendering contract
    # itself).
    desc = _view_text(game_msg.edit.call_args.kwargs["view"])
    assert "Member2001" in desc and "Member1001" in desc and "Member9999" in desc
    assert "<@" not in desc


@pytest.mark.asyncio
async def test_correct_guess_attaches_full_image_as_spoiler_and_unlinks(tmp_path):
    """First correct guess swaps the message attachment for a SPOILER_-prefixed
    full original, then deletes the on-disk file and clears original_path."""
    orig_file = tmp_path / "1234.png"
    orig_file.write_bytes(b"fake-original-png-bytes")

    game_msg = _game_message()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    round_row.original_path = str(orig_file)

    set_path_calls: list[tuple] = []

    def _capture_set_path(*args, **kwargs):
        set_path_calls.append((args, kwargs))

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch("bot_modules.games.guess.cog._do_set_original_path", side_effect=_capture_set_path), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    edit_kwargs = game_msg.edit.call_args.kwargs
    attachments = edit_kwargs["attachments"]
    assert len(attachments) == 1
    assert attachments[0].filename == "SPOILER_guess_full.jpg"
    # The reveal's gallery shows that upload, spoilered, and the edit clears
    # whatever embed an older round still carried.
    (gallery,) = [
        i for i in edit_kwargs["view"].walk_children()
        if isinstance(i, discord.ui.MediaGallery)
    ]
    assert gallery.items[0].media.url == "attachment://SPOILER_guess_full.jpg"
    assert gallery.items[0].spoiler is True
    assert edit_kwargs["embed"] is None and edit_kwargs["content"] is None
    # File is removed and DB path cleared.
    assert not orig_file.exists()
    assert set_path_calls and set_path_calls[0][0][2] == ""


@pytest.mark.asyncio
async def test_correct_guess_without_original_path_still_solves():
    """Old rounds (pre-migration) have original_path == '' — solve should still
    proceed without trying to attach a missing file."""
    game_msg = _game_message()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()  # original_path defaults to ""

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    # edit() is called without attachments kwarg — no crash, and the existing
    # crop image is preserved rather than wiped: the reveal's gallery keeps
    # pointing at it.
    game_msg.edit.assert_called_once()
    edit_kwargs = game_msg.edit.call_args.kwargs
    assert "attachments" not in edit_kwargs
    (gallery,) = [
        i for i in edit_kwargs["view"].walk_children()
        if isinstance(i, discord.ui.MediaGallery)
    ]
    assert gallery.items[0].media.url == "attachment://SPOILER_guess_crop.jpg"
    assert gallery.items[0].spoiler is True


@pytest.mark.asyncio
async def test_correct_guess_already_solved_does_not_edit_game_message():
    game_msg = _game_message()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round(solved_at=1234.0)

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    assert "someone already solved" in _said(interaction).lower()


@pytest.mark.asyncio
async def test_wrong_guess_bumps_counter_and_sends_not_it_message():
    game_msg = _game_message()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_count_guesses_for_round", return_value=4), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    # Counter-bump edit: a fresh GameView with the new count is attached.
    game_msg.edit.assert_called_once()
    edit_kwargs = game_msg.edit.call_args.kwargs
    assert "Guesses: **4**" in _view_text(edit_kwargs["view"])
    assert "attachments" not in edit_kwargs

    assert "not it" in _said(interaction).lower()


@pytest.mark.asyncio
async def test_wrong_guess_on_solved_round_skips_counter_bump():
    """If the round was already solved when we loaded it, the public message
    already has the solved view — a chip-bump would overwrite it."""
    game_msg = _game_message()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round(solved_at=1234.0)

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_count_guesses_for_round", return_value=8), \
         patch("bot_modules.games.guess.cog._do_insert_guess") as insert, \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    # The round is over: the picker says so and closes, instead of "Keep
    # trying!" on a round nobody can play (design review, 2026-09-30). The
    # guess is still written, as every guess is — the no-contact rule needs
    # each one to run the same course.
    insert.assert_called_once()
    assert "solved this round while you were picking" in _said(interaction)
    assert "Keep trying" not in _said(interaction)
    assert view._select.disabled is True


@pytest.mark.asyncio
async def test_guess_cap_blocks_after_five_attempts():
    """Per-(user, round) guess cap: 5th attempt is rejected, no insert/select effect."""
    view = _make_select_view(cooldown_seconds=0)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    insert_mock = MagicMock()
    with patch("bot_modules.games.guess.cog._do_count_user_guesses", return_value=5), \
         patch("bot_modules.games.guess.cog._do_insert_guess", insert_mock), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_not_called()
    assert "out of guesses" in _said(interaction).lower()
    assert view._select.disabled is True


@pytest.mark.asyncio
async def test_guess_cap_allows_under_limit():
    """4 prior guesses → 5th still allowed."""
    view = _make_select_view(cooldown_seconds=0)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    with patch("bot_modules.games.guess.cog._do_count_user_guesses", return_value=4), \
         patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess") as insert_mock, \
         patch("bot_modules.games.guess.cog._do_count_guesses_for_round", return_value=5), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    insert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_guess_cap_uses_configured_value_not_hardcoded_default():
    """A guild with a lower configured per-round cap blocks before the
    hardcoded default of 5 — proving the view's max_guesses_per_round,
    not the module constant, drives enforcement."""
    view = _make_select_view(cooldown_seconds=0, max_guesses_per_round=2)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    insert_mock = MagicMock()
    with patch("bot_modules.games.guess.cog._do_count_user_guesses", return_value=2), \
         patch("bot_modules.games.guess.cog._do_insert_guess", insert_mock), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_not_called()
    assert "cap: 2" in _said(interaction)


@pytest.mark.asyncio
async def test_correct_guess_loses_race_does_not_edit_message():
    """If _do_mark_solved returns rowcount==0 (someone else won the race),
    the cog must NOT edit the public game message — only ack the guesser."""
    game_msg = _game_message()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()  # solved_at None — but mark_solved will say rowcount=0
    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(0, 5, 3)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    assert "someone already solved" in _said(interaction).lower()


@pytest.mark.asyncio
async def test_select_is_disabled_after_guess():
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    assert view._select.disabled is True


# ── Cooldown tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_select_rejects_guess_within_cooldown():
    import time as _time
    from bot_modules.games.guess.models import GuessGuess

    view = _make_select_view(cooldown_seconds=30)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    recent_guess = GuessGuess(
        id=1, round_id=ROUND_ID, guesser_id=9999,
        guessed_user_id=2001, correct=False,
        created_at=_time.time() - 5,  # 5s ago, within 30s cooldown
    )
    insert_mock = MagicMock()
    with patch("bot_modules.games.guess.cog._do_get_last_guess", return_value=recent_guess), \
         patch("bot_modules.games.guess.cog._do_insert_guess", insert_mock), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_not_called()
    assert "cooldown" in _said(interaction).lower()
    # Still live: the member can pick again once the cooldown is over.
    assert view._select.disabled is False


@pytest.mark.asyncio
async def test_on_select_allows_guess_after_cooldown_expires():
    import time as _time
    from bot_modules.games.guess.models import GuessGuess

    view = _make_select_view(cooldown_seconds=30)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    old_guess = GuessGuess(
        id=1, round_id=ROUND_ID, guesser_id=9999,
        guessed_user_id=2001, correct=False,
        created_at=_time.time() - 60,  # 60s ago, past 30s cooldown
    )
    round_row = _make_round()
    with patch("bot_modules.games.guess.cog._do_get_last_guess", return_value=old_guess), \
         patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess") as insert_mock, \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 2, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_on_select_cooldown_zero_disables_check():
    """cooldown_seconds=0 means no cooldown enforcement."""
    view = _make_select_view(cooldown_seconds=0)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    round_row = _make_round()
    get_last_mock = MagicMock()
    with patch("bot_modules.games.guess.cog._do_get_last_guess", get_last_mock), \
         patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    get_last_mock.assert_not_called()


# ── Rounds posted before the Components V2 conversion ─────────────────────────
#
# Prod had 32 open rounds posted as embeds when the conversion shipped. Each
# converts in place the first time it is edited — a wrong guess's counter
# bump, or the solve — and must keep showing its crop through that edit.


def _gallery_refs(view) -> list[str]:
    return [
        media.media.url
        for item in view.walk_children()
        if isinstance(item, discord.ui.MediaGallery)
        for media in item.items
    ]


@pytest.mark.parametrize(
    ("message_kw", "confession", "expected"),
    [
        pytest.param(dict(), False, "attachment://SPOILER_guess_crop.jpg", id="v2_photo"),
        pytest.param(
            dict(), True, "attachment://SPOILER_guess_confession.jpg",
            id="v2_confession",
        ),
        pytest.param(
            dict(embed_era=True), False, "attachment://SPOILER_old_crop.png",
            id="embed_era_names_its_own_upload",
        ),
    ],
)
def test_carried_media_references_the_image_the_card_already_has(
    message_kw, confession, expected
):
    from bot_modules.games.guess.cog import _carried_media

    assert _carried_media(_game_message(**message_kw), confession=confession) == expected


def test_carried_media_is_none_for_an_embed_era_round_with_no_upload():
    """A gallery naming a file that isn't on the message fails the whole
    edit, which on a solve would leave the round marked solved and the card
    still open."""
    from bot_modules.games.guess.cog import _carried_media

    msg = _game_message()
    msg.flags.components_v2 = False
    assert _carried_media(msg, confession=False) is None


@pytest.mark.asyncio
async def test_wrong_guess_on_an_embed_era_round_converts_it_in_place():
    game_msg = _game_message(embed_era=True)
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=_make_round()), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_count_guesses_for_round", return_value=1), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    game_msg.edit.assert_called_once()
    edit_kwargs = game_msg.edit.call_args.kwargs
    # The old embed goes, the old upload stays and the new gallery shows it.
    assert edit_kwargs["embed"] is None and edit_kwargs["content"] is None
    assert "attachments" not in edit_kwargs
    assert _gallery_refs(edit_kwargs["view"]) == ["attachment://SPOILER_old_crop.png"]
    assert "Guesses: **1**" in _view_text(edit_kwargs["view"])


@pytest.mark.asyncio
async def test_embed_era_round_is_re_uploaded_when_the_reference_edit_is_refused():
    """The fallback: if Discord won't let the new gallery reference the
    embed-era upload, the crop is downloaded and uploaded again under the
    same name rather than leaving the round unconvertible."""
    from bot_modules.games.guess.cog import GameView, _edit_round_card

    game_msg = _game_message(embed_era=True)
    game_msg.edit = AsyncMock(
        side_effect=[discord.HTTPException(MagicMock(status=400), "bad ref"), None]
    )
    view = GameView(MagicMock(), ROUND_ID, media="attachment://SPOILER_old_crop.png")

    await _edit_round_card(game_msg, view)

    assert game_msg.edit.await_count == 2
    (upload,) = game_msg.edit.await_args.kwargs["attachments"]
    assert upload.filename == "SPOILER_old_crop.png"
    assert upload.fp.read() == b"old-crop-bytes"


@pytest.mark.asyncio
async def test_a_refused_edit_on_a_v2_round_is_not_retried():
    """Nothing to re-upload from: a V2 card's uploads are not readable off
    the message, so the error surfaces to the caller instead of looping."""
    from bot_modules.games.guess.cog import GameView, _edit_round_card

    game_msg = _game_message()
    game_msg.edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "down")
    )
    with pytest.raises(discord.HTTPException):
        await _edit_round_card(game_msg, GameView(MagicMock(), ROUND_ID))
    assert game_msg.edit.await_count == 1


@pytest.mark.asyncio
async def test_solving_an_embed_era_round_without_an_original_keeps_its_crop():
    game_msg = _game_message(embed_era=True)
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=_make_round()), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    edit_kwargs = game_msg.edit.call_args.kwargs
    assert _gallery_refs(edit_kwargs["view"]) == ["attachment://SPOILER_old_crop.png"]
    assert "Solved!" in _view_text(edit_kwargs["view"])


@pytest.mark.asyncio
async def test_a_reveal_that_cannot_keep_its_image_still_lands_in_words():
    """By the time the card is edited the round is marked solved. A refused
    edit must not leave the channel showing an open round nobody can solve."""
    game_msg = _game_message()
    game_msg.edit = AsyncMock(
        side_effect=[discord.HTTPException(MagicMock(status=400), "bad ref"), None]
    )
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=_make_round()), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    assert game_msg.edit.await_count == 2
    final = game_msg.edit.await_args.kwargs["view"]
    assert "Solved!" in _view_text(final)
    assert _gallery_refs(final) == []
    assert "correct" in _said(interaction).lower()


@pytest.mark.asyncio
async def test_a_round_card_deleted_mid_pick_still_finishes_the_solve(tmp_path):
    """Code review, 2026-09-30: a mod deleting the card while a member's
    picker was open left the reveal edit raising NotFound — the photo was
    never deleted, no audit row was written and the solver's card failed.
    Now the solve finishes; only the edit is skipped."""
    orig_file = tmp_path / "77.png"
    orig_file.write_bytes(b"a-members-photo")
    game_msg = _game_message()
    game_msg.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
    )
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    round_row = _make_round()
    round_row.original_path = str(orig_file)

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_mark_solved", return_value=(1, 1, 1)), \
         patch("bot_modules.games.guess.cog._do_set_original_path") as set_path, \
         patch("bot_modules.games.guess.cog._do_audit") as audit, \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    assert game_msg.edit.await_count == 1  # no text-only retry at a card that's gone
    assert not orig_file.exists()
    set_path.assert_called_once()
    assert audit.call_args.kwargs["action"] == "solve"
    assert "Correct!" in _said(interaction)


# ── The picker card's own lifetime ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cooldown", "expected"),
    [(30, 90), (3600, 840)],
    ids=["extends_past_the_cooldown", "clamped_under_fifteen_minutes"],
)
async def test_wrong_guess_keeps_the_picker_open_through_the_cooldown(cooldown, expected):
    """The picker stays live so the member can guess again once the cooldown
    ends — but never past the 15 minutes an ephemeral card can be edited for,
    or its grey-out on timeout would fail silently."""
    view = _make_select_view(cooldown_seconds=cooldown)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    with patch("bot_modules.games.guess.cog._do_get_last_guess", return_value=None), \
         patch("bot_modules.games.guess.cog._do_load_round", return_value=_make_round()), \
         patch("bot_modules.games.guess.cog._do_insert_guess"), \
         patch("bot_modules.games.guess.cog._do_count_guesses_for_round", return_value=1), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    assert view.timeout == expected
    assert view._select.disabled is False


@pytest.mark.asyncio
async def test_a_vanished_round_closes_the_picker_with_a_notice():
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    with patch("bot_modules.games.guess.cog._do_load_round", return_value=None), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    assert "Round Not Found" in _said(interaction)
    assert view.is_finished()


def test_picker_pages_and_filters_its_members():
    members = [FakeMember(id=i, display_name=f"Member{i:02d}") for i in range(1, 41)]
    view = _make_select_view(guess_members=members)
    assert len(view._select.options) == 25
    assert view._select.placeholder.endswith("(1/2)")

    view._page = 1
    view._rebuild()
    assert [o.label for o in view._select.options][0] == "Member26"
    assert len(view._select.options) == 15

    view._display_members = members[:2]
    view._filter_query = "member0"
    view._page = 0
    view._rebuild()
    assert view._select.placeholder == '🔍 "member0" — 2 matches'
    assert "✕ Clear" in [
        getattr(i, "label", None) for i in view.walk_children()
    ]


# ── GameView tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_callback_message_mentions_timer():
    """The ephemeral 'Who do you think this is?' message must surface the
    countdown so users know the prompt is time-limited."""
    from bot_modules.games.guess.cog import GameView
    from bot_modules.games.guess.models import GuessConfig

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    guesser = FakeMember(id=9999)
    interaction = fake_interaction(user=guesser)
    guess_role = MagicMock()
    guess_role.members = [FakeMember(id=2001, display_name="Alice")]
    interaction.guild.get_role = MagicMock(return_value=guess_role)

    round_row = _make_round(submitter_id=1001)
    config = GuessConfig(guild_id=9001, guess_role_id=GUESS_ROLE_ID, guess_cooldown_seconds=30)

    with patch("bot_modules.games.guess.cog._load_config", return_value=config), \
         patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row):
        await view._guess_callback(interaction)

    msg = _view_text(interaction.followup.send.call_args.kwargs["view"])
    assert "60 seconds" in msg, f"expected timer hint in: {msg!r}"
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_game_view_rejects_submitter_guessing_own_round():
    from bot_modules.games.guess.cog import GameView
    from bot_modules.games.guess.models import GuessConfig

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    submitter = FakeMember(id=1001)
    interaction = fake_interaction(user=submitter)

    round_row = _make_round(submitter_id=1001)
    config = GuessConfig(guild_id=9001, guess_role_id=GUESS_ROLE_ID)

    with patch("bot_modules.games.guess.cog._load_config", return_value=config), \
         patch("bot_modules.games.guess.cog._do_load_round", return_value=round_row):
        await view._guess_callback(interaction)

    msg = interaction.followup.send.call_args.args[0]
    assert "can't guess" in msg.lower() or "own round" in msg.lower()


# ── CropEditorView post tests ────────────────────────────────────────────────

GUILD_ID = 9001
GUESS_CHANNEL_ID = 8001


@pytest.mark.asyncio
async def test_post_persists_original_bytes_and_stores_path(tmp_path, monkeypatch):
    """On Post, the submitter's original bytes are written to <orig_dir>/<round_id><ext>
    and the path is recorded so /correct guess can attach it as a SPOILER."""
    import bot_modules.games.guess.cog as guess_cog
    from bot_modules.games.guess.cog import CropEditorView

    monkeypatch.setattr(guess_cog, "guess_orig_dir", lambda _db: tmp_path / "orig")

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock()
    fake_channel.mention = "#guess"
    fake_channel.is_nsfw = lambda: True
    fake_msg = MagicMock()
    fake_msg.id = 9999
    attached = MagicMock()
    attached.url = "https://cdn.example/crop.jpg"
    fake_msg.attachments = [attached]
    fake_channel.send = AsyncMock(return_value=fake_msg)

    interaction = fake_interaction()
    interaction.guild.get_channel = lambda cid: fake_channel
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    view = CropEditorView(
        bot,
        image_bytes=b"",
        img_w=500,
        img_h=500,
        crop_box=BoundingBox(0.0, 0.0, 500.0, 500.0),
        guild_id=GUILD_ID,
        guess_channel_id=GUESS_CHANNEL_ID,
        submitter_id=1001,
        answer_id=2001,
        difficulty="medium",
        candidate_count=1,
        original_bytes=b"the-original-png-bytes",
        original_ext=".png",
    )

    set_path_calls: list[tuple] = []

    def _capture(*args, **kwargs):
        set_path_calls.append((args, kwargs))

    with patch("bot_modules.games.guess.cog._do_insert_round", return_value=77), \
         patch("bot_modules.games.guess.cog._do_update_round_message"), \
         patch("bot_modules.games.guess.cog._do_set_original_path", side_effect=_capture), \
         patch("bot_modules.games.guess.cog._do_set_crop_box"), \
         patch("bot_modules.games.guess.cog._do_audit"), \
         patch("bot_modules.games.guess.cog.render_crop", return_value=b"\xff\xd8fake"), \
         patch("bot_modules.games.guess.cog._repost_prompt", new_callable=AsyncMock):
        with patch("bot_modules.games.guess.cog.isinstance", lambda obj, types: True):
            await view._on_post(interaction)

    persisted = tmp_path / "orig" / "77.png"
    assert persisted.exists()
    assert persisted.read_bytes() == b"the-original-png-bytes"
    assert set_path_calls and set_path_calls[0][0][2] == str(persisted)
