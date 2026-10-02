"""Cog-level tests for the /guess submit command."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.games.guess.models import GuessConfig
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction


@pytest.fixture(autouse=True)
def _no_lobby_art(monkeypatch):
    """The public cards read the guild's lobby strip; the mocked bots here
    have no branding table to read it from."""
    from bot_modules.games.utils.lobby_art import NO_ART

    monkeypatch.setattr(
        "bot_modules.games.guess.cog.read_lobby_art", AsyncMock(return_value=NO_ART)
    )


GUESS_ROLE_ID = 7001
GUESS_CHANNEL_ID = 8001
GUILD_ID = 9001


def _cfg(**overrides: Any) -> GuessConfig:
    defaults: dict[str, Any] = dict(
        guild_id=GUILD_ID,
        guess_role_id=GUESS_ROLE_ID,
        guess_channel_id=GUESS_CHANNEL_ID,
        guess_cooldown_seconds=30,
        crop_difficulty="medium",
        min_image_dimension_px=400,
        max_image_size_mb=10,
    )
    defaults.update(overrides)
    return GuessConfig(**defaults)  # type: ignore[arg-type]


def _guess_member(has_role: bool = True) -> FakeMember:
    roles = [FakeRole(id=GUESS_ROLE_ID)] if has_role else []
    return FakeMember(id=1001, roles=roles)


def _guild(member: FakeMember | None = None) -> FakeGuild:
    m = member or _guess_member()
    g = FakeGuild(id=GUILD_ID)
    g.members[m.id] = m
    return g


def _attachment(
    content_type: str = "image/jpeg",
    size: int = 1_000_000,
    read_return: bytes = b"fake-bytes",
    filename: str = "submission.jpg",
) -> MagicMock:
    a = MagicMock()
    a.content_type = content_type
    a.size = size
    a.filename = filename
    a.read = AsyncMock(return_value=read_return)
    return a


def _make_cog(db_path: str = ":memory:"):
    from bot_modules.games.guess.cog import GuessCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    bot.add_view = MagicMock()
    return GuessCog(bot)


async def _submit(cog: Any, interaction: Any, image: Any) -> None:
    """Invoke guess_submit's underlying coroutine, bypassing the app_commands.Command wrapper."""
    await cog.guess_submit.callback(cog, interaction, image)


# ── Validation rejection tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_rejects_no_guess_role():
    member = _guess_member(has_role=False)
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg()):
        await _submit(cog, interaction, _attachment())

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args.args[0]
    assert "Guess role" in msg


@pytest.mark.asyncio
async def test_submit_rejects_unconfigured_channel():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg(guess_channel_id=0)):
        await _submit(cog, interaction, _attachment())

    msg = interaction.followup.send.call_args.args[0]
    assert "not configured" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_non_image_mime():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg()):
        await _submit(cog, interaction, _attachment(content_type="video/mp4"))

    msg = interaction.followup.send.call_args.args[0]
    assert "image" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_oversized_file():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg(max_image_size_mb=5)):
        await _submit(cog, interaction, _attachment(size=6 * 1024 * 1024))

    msg = interaction.followup.send.call_args.args[0]
    assert "too large" in msg.lower() or "maximum" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_small_dimensions():
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg()):
        with patch("bot_modules.games.guess.cog._validate_dimensions", return_value=(False, 200, 200)):
            await _submit(cog, interaction, _attachment())

    msg = interaction.followup.send.call_args.args[0]
    assert "too small" in msg.lower() or "minimum" in msg.lower()


@pytest.mark.asyncio
async def test_submit_shows_editor_when_no_pipeline_candidates():
    """No detections → still show the crop editor (center-crop default, Auto disabled)."""
    import io as _io
    from PIL import Image
    from bot_modules.games.guess.models import PipelineResult
    from bot_modules.games.guess.cog import CropEditorView

    buf = _io.BytesIO()
    Image.new("RGB", (500, 500)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    empty_result = PipelineResult(candidates=[], crops=[])

    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg()):
        with patch("bot_modules.games.guess.cog.run_pipeline", return_value=empty_result):
            with patch("bot_modules.games.guess.cog.render_crop_editor", return_value=b"\xff\xd8fake"):
                await _submit(cog, interaction, _attachment(read_return=img_bytes))

    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True
    assert isinstance(call_kwargs.kwargs.get("view"), CropEditorView)
    sent_view: CropEditorView = call_kwargs.kwargs["view"]
    assert sent_view._candidate_boxes == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_success_sends_ephemeral_preview(sync_db_path: Path):
    """Full happy-path: pipeline returns a crop, ephemeral preview sent to submitter.

    The guess channel is NOT posted to at submit time — that happens only when
    the submitter clicks the Post button in the preview.
    """
    import io as _io
    from PIL import Image
    from bot_modules.games.guess.models import Detection, BoundingBox, PipelineResult

    buf = _io.BytesIO()
    Image.new("RGB", (500, 500)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    det = Detection(label="BREAST", score=0.9, box=BoundingBox(10, 10, 100, 100))
    fake_result = PipelineResult(candidates=[det], crops=[b"fake-crop-jpeg"])

    member = _guess_member()
    guild = _guild(member)

    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.send = AsyncMock(return_value=_fake_game_message())
    guild.channels[GUESS_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)
    interaction.user.id = member.id

    cog = _make_cog(str(sync_db_path))
    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg()):
        with patch("bot_modules.games.guess.cog.run_pipeline", return_value=fake_result):
            with patch("bot_modules.games.guess.cog._do_insert_round", return_value=42):
                with patch("bot_modules.games.guess.cog._do_update_round_message"):
                    with patch("bot_modules.games.guess.cog._do_set_reroll_count"):
                        await _submit(cog, interaction, _attachment(read_return=img_bytes))

    # Channel post deferred until the user clicks Post — not called here.
    fake_channel.send.assert_not_called()

    # Ephemeral preview sent to submitter
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True


def _fake_game_message() -> MagicMock:
    """Minimal discord.Message fake for testing."""
    msg = MagicMock()
    msg.id = 12345
    msg.attachments = [MagicMock(url="https://cdn.discord.com/fake/crop.jpg")]
    msg.edit = AsyncMock()
    msg.guild = MagicMock()
    return msg


@pytest.mark.asyncio
async def test_cog_load_registers_game_views_from_db(sync_db_path: Path):
    """cog_load queries active rounds and calls bot.add_view for each."""
    from bot_modules.games.guess.cog import GameView
    from bot_modules.games.guess.repo import insert_round
    from bot_modules.core.db_utils import open_db

    with open_db(sync_db_path) as conn:
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1001, answer_id=1001)
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1002, answer_id=1002)

    cog = _make_cog(str(sync_db_path))
    add_view_mock: MagicMock = cog.bot.add_view  # type: ignore[assignment]
    await cog.cog_load()

    gameview_calls = [
        c for c in add_view_mock.call_args_list if isinstance(c.args[0], GameView)
    ]
    assert len(gameview_calls) == 2


# ── Safety patch: fail-closed when Guess role is unconfigured ─────────────────

@pytest.mark.asyncio
async def test_submit_rejects_when_guess_role_not_configured():
    """If guess_role_id is 0 (unconfigured), submit must reject — not fall open.

    Previously _has_guess_role returned True for role_id=0 which let any guild
    member submit on a freshly-installed bot.
    """
    member = _guess_member(has_role=False)  # no guess role at all
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("bot_modules.games.guess.cog._load_config", return_value=_cfg(guess_role_id=0)):
        await _submit(cog, interaction, _attachment())

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args.args[0]
    assert "role" in msg.lower() and ("not configured" in msg.lower() or "ask an admin" in msg.lower())


# ── configurable submission flood cap ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_uses_configured_flood_cap_not_hardcoded_default():
    """A guild with a lower configured flood cap is blocked before the
    hardcoded 5/hour default — proving config.submit_max_per_window, not the
    module constant, drives enforcement."""
    member = _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    cfg = _cfg(submit_max_per_window=1, submit_window_seconds=3600)
    with patch("bot_modules.games.guess.cog._load_config", return_value=cfg):
        # First submit fails validation (no pipeline mocking) but still
        # records a submission attempt against the flood cap.
        await _submit(cog, interaction, _attachment(content_type="video/mp4"))
        interaction.followup.send.reset_mock()
        await _submit(cog, interaction, _attachment(content_type="video/mp4"))

    msg = interaction.followup.send.call_args.args[0]
    assert "submission limit" in msg.lower()
    assert "1 per" in msg


@pytest.mark.asyncio
async def test_on_post_reposts_prompt_after_game_message():
    """After posting a game round, _repost_prompt is called to move the
    sticky status bar below the new round."""
    from bot_modules.games.guess.cog import CropEditorView
    from bot_modules.games.guess.models import BoundingBox

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.is_nsfw = MagicMock(return_value=True)
    fake_channel.send = AsyncMock(return_value=_fake_game_message())

    guild = FakeGuild(id=GUILD_ID)
    guild.channels[GUESS_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(guild=guild)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)

    view = CropEditorView(
        bot,
        b"fake-image",
        100,
        100,
        BoundingBox(0.0, 0.0, 1.0, 1.0),
        GUILD_ID,
        GUESS_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=1,
    )

    with patch("bot_modules.games.guess.cog.render_crop", return_value=b"fake-crop"), \
         patch("bot_modules.games.guess.cog._do_insert_round", return_value=42), \
         patch("bot_modules.games.guess.cog._do_update_round_message"), \
         patch("bot_modules.games.guess.cog._do_set_crop_box"), \
         patch("bot_modules.games.guess.cog._do_audit"), \
         patch("bot_modules.games.guess.cog._repost_prompt", new_callable=AsyncMock) as mock_repost:
        await view._on_post(interaction)

    mock_repost.assert_awaited_once_with(bot, fake_channel, GUILD_ID)


# ── The prompt's Submit form: an upload or a link ────────────────────────────


def _modal(bot=None):
    """The form with its two fields' submitted values settable."""
    from bot_modules.games.guess.cog import _GuessSubmitModal

    if bot is None:
        bot = MagicMock()
        bot.ctx.db_path = ":memory:"
    return _GuessSubmitModal(bot)


async def _submit_form(
    modal, *, uploads=(), url="", member=None, cfg=None, rate_limited=False
):
    member = member or _guess_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild.get_member = lambda uid: guild.members.get(uid)
    with (
        # The real limiter keeps module-level history per user id, which
        # would make these tests depend on each other's order.
        patch(
            "bot_modules.games.guess.cog._submit_rate_limited",
            return_value=rate_limited,
        ),
        patch.object(type(modal.upload), "values", new=property(lambda _: list(uploads))),
        patch.object(type(modal.image_url), "value", new=property(lambda _: url)),
        patch("bot_modules.games.guess.cog._load_config", return_value=cfg or _cfg()),
        patch(
            "bot_modules.games.guess.cog._pipeline_and_open_editor", new_callable=AsyncMock
        ) as editor,
        patch(
            "bot_modules.games.guess.cog._fetch_url_bytes", return_value=b"from-the-link"
        ) as fetch,
    ):
        await modal.on_submit(interaction)
    return interaction, editor, fetch


def _refusal(interaction) -> str:
    return interaction.followup.send.call_args.args[0]


def test_submit_form_offers_an_upload_and_a_link_neither_required():
    modal = _modal()
    assert modal.upload.required is False
    assert modal.image_url.required is False
    assert len(modal.children) == 2


@pytest.mark.asyncio
async def test_submit_form_needs_a_photo_or_a_link():
    interaction, editor, _ = await _submit_form(_modal())
    assert "photo or paste an image link" in _refusal(interaction)
    editor.assert_not_called()


@pytest.mark.asyncio
async def test_submit_form_upload_opens_the_editor_with_the_files_bytes():
    upload = _attachment(read_return=b"uploaded-bytes")
    interaction, editor, fetch = await _submit_form(_modal(), uploads=[upload])
    fetch.assert_not_called()
    assert editor.await_args.args[3] == b"uploaded-bytes"


@pytest.mark.asyncio
async def test_submit_form_upload_wins_over_a_link():
    upload = _attachment(read_return=b"uploaded-bytes")
    _, editor, fetch = await _submit_form(
        _modal(), uploads=[upload], url="https://example.test/x.png"
    )
    fetch.assert_not_called()
    assert editor.await_args.args[3] == b"uploaded-bytes"


@pytest.mark.asyncio
async def test_submit_form_link_is_fetched_when_nothing_is_uploaded():
    _, editor, fetch = await _submit_form(_modal(), url="https://example.test/x.png")
    fetch.assert_called_once()
    assert editor.await_args.args[3] == b"from-the-link"
    assert editor.await_args.kwargs["original_ext"] == ".png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        pytest.param(dict(url="ftp://example.test/x.png"), "http or https", id="bad_scheme"),
        pytest.param(
            dict(uploads=["not-an-image"]), "image file", id="upload_not_an_image",
        ),
        pytest.param(dict(uploads=["too-big"]), "too large", id="upload_too_large"),
        pytest.param(dict(uploads=["ok"], no_role=True), "Guess role", id="no_role"),
    ],
)
async def test_submit_form_runs_the_slash_commands_checks(kw, expected):
    """The form is a second door into the same flow, so it has to refuse
    everything ``/guess submit`` refuses."""
    kinds = {
        "not-an-image": _attachment(content_type="application/pdf"),
        "too-big": _attachment(size=50 * 1024 * 1024),
        "ok": _attachment(),
    }
    uploads = [kinds[name] for name in kw.get("uploads", [])]
    member = _guess_member(has_role=False) if kw.get("no_role") else None
    interaction, editor, _ = await _submit_form(
        _modal(), uploads=uploads, url=kw.get("url", ""), member=member
    )
    assert expected in _refusal(interaction)
    editor.assert_not_called()


@pytest.mark.asyncio
async def test_submit_form_is_rate_limited_like_the_slash_command():
    """Until the form took uploads it skipped the submission limit entirely —
    the panel button was a way round the dial."""
    interaction, editor, _ = await _submit_form(
        _modal(), uploads=[_attachment()], rate_limited=True
    )
    assert "submission limit" in _refusal(interaction)
    editor.assert_not_called()


def test_posted_media_url_skips_the_lobby_header_and_footer():
    """A card wearing the lobby strip has three galleries; the round's stored
    crop URL is the upload's, never the header art (which is first)."""
    from bot_modules.games.guess import layouts
    from bot_modules.games.guess.cog import _posted_media_url

    class _Gallery:
        def __init__(self, url: str) -> None:
            self.items = [MagicMock(media=MagicMock(url=url))]

    msg = MagicMock()
    msg.components = [
        _Gallery("https://dk.example/static/panel-banners/header.webp"),
        _Gallery(
            "https://cdn.discordapp.com/attachments/1/2/"
            f"{layouts.CROP_FILENAME}?ex=abc"
        ),
        _Gallery("https://dk.example/static/panel-banners/footer.webp"),
    ]
    assert _posted_media_url(msg, layouts.CROP_FILENAME).endswith(
        f"/{layouts.CROP_FILENAME}?ex=abc"
    )
    # A card with no matching name and several galleries guesses nothing.
    msg.attachments = []
    assert _posted_media_url(msg, layouts.CONFESSION_FILENAME) == ""
