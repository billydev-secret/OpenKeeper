"""Cog-level: /whisper send — the send form and ``_send_impl``."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.whisper.models import Whisper, WhisperConfig
from tests.fakes import FakeMember, FakeRole, fake_interaction

pytestmark = pytest.mark.usefixtures("stub_whisper_cog", "stub_no_contact")




def _refusal_text(interaction, draft: str) -> str:
    """The text of a send refusal's card, asserting it quotes the draft back."""
    kwargs = interaction.response.send_message.call_args.kwargs
    text = "\n".join(
        i.content for i in kwargs["view"].walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )
    assert f"> {draft}" in text, "a send refusal must hand the draft back"
    return text


ROLE = 7001
FEED = 8001
LOG = 8002
SENDER_ID = 1001
TARGET_ID = 2001


@pytest.fixture(autouse=True)
def _stub_accent_color(monkeypatch):
    """resolve_accent_color awaits guild.me.display_avatar.read(), which the
    mocked guilds here can't satisfy — stub it at the use-site namespace."""
    monkeypatch.setattr(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=discord.Color.default()),
    )


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=FEED, log_channel_id=LOG)


def _row(wid: int = 42, message: str = "hello world") -> Whisper:
    """The row ``_send_impl`` reloads after the insert to build the DM card."""
    return Whisper(
        id=wid, guild_id=9001, sender_id=SENDER_ID, target_id=TARGET_ID,
        message=message, created_at=0.0, state="pending", solved=False,
        guesses_left=3, channel_msg_id=None, dm_msg_id=None,
    )


def _make_cog():
    from bot_modules.whisper.cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    cog._last_send_at = {}
    cog._target_sends = {}
    return cog


def _make_target_dmable():
    target = FakeMember(id=TARGET_ID, display_name="Target", roles=[FakeRole(id=ROLE)])
    target.send = AsyncMock(return_value=MagicMock(id=99999))  # type: ignore[attr-defined]
    return target


@pytest.mark.asyncio
async def test_send_happy_path():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, display_name="Sender", roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=88888))
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_insert_whisper", return_value=42), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_row()), \
         patch("bot_modules.whisper.cog._do_set_message_ids") as set_ids, \
         patch("bot_modules.economy.game_rewards.fire_member_trigger", new=AsyncMock()) as trigger:
        await cog._send_impl(interaction, target=target, message="hello world")  # type: ignore[arg-type]

    # The DM is a V2 card branded for the guild (send_branded_dm's layout
    # path): the whisper quoted, the guild's name in the attribution line.
    target.send.assert_awaited_once()  # type: ignore[attr-defined]
    dm_view = target.send.call_args.kwargs["view"]  # type: ignore[attr-defined]
    assert isinstance(dm_view, discord.ui.LayoutView)
    dm_text = "\n".join(
        i.content for i in dm_view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )
    assert "> hello world" in dm_text
    assert "From Test" in dm_text
    # The feed post is a card too, and never pings.
    feed_channel.send.assert_awaited_once()
    feed_kwargs = feed_channel.send.call_args.kwargs
    assert isinstance(feed_kwargs["view"], discord.ui.LayoutView)
    assert feed_kwargs["allowed_mentions"].users is False
    set_ids.assert_called_once_with(":memory:", 42, channel_msg_id=88888, dm_msg_id=99999)
    interaction.response.send_message.assert_awaited()
    trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_rejects_when_sender_lacks_role():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[])  # no role
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "role" in _refusal_text(interaction, "hi").lower()


@pytest.mark.asyncio
async def test_send_rejects_self_target():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await cog._send_impl(interaction, target=sender, message="hi")  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.call_args
    assert "yourself" in _refusal_text(interaction, "hi").lower()


@pytest.mark.asyncio
async def test_send_form_opens_for_a_role_holder():
    """/whisper send opens the one-modal send form (member picker + message)."""
    from bot_modules.whisper.cog import WhisperSendModal
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    role = MagicMock()
    role.id = ROLE
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await cog._open_send_form(interaction)

    # A modal must be the interaction's first response — nothing defers.
    interaction.response.defer.assert_not_awaited()
    interaction.response.send_modal.assert_awaited_once()
    assert isinstance(interaction.response.send_modal.call_args.args[0], WhisperSendModal)


@pytest.mark.asyncio
async def test_send_form_rejects_when_role_unset():
    """/whisper send errors when whisper role isn't configured."""
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    cfg_no_role = WhisperConfig(guild_id=9001, role_id=0, channel_id=FEED, log_channel_id=LOG)
    with patch("bot_modules.whisper.cog._load_config", return_value=cfg_no_role):
        await cog._open_send_form(interaction)

    interaction.response.send_modal.assert_not_awaited()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "configured" in args[0].lower() or "set up" in args[0].lower()


@pytest.mark.asyncio
async def test_send_form_rejects_when_role_missing_from_guild():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=None)

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await cog._open_send_form(interaction)

    interaction.response.send_modal.assert_not_awaited()
    args = interaction.response.send_message.call_args.args
    assert "no longer exists" in args[0].lower()


@pytest.mark.asyncio
async def test_send_form_rejects_when_caller_lacks_role():
    """/whisper send tells callers without the whisper role to opt in first."""
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[])  # not opted-in
    role = MagicMock()
    role.id = ROLE
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()):
        await cog._open_send_form(interaction)

    interaction.response.send_modal.assert_not_awaited()
    args = interaction.response.send_message.call_args.args
    assert "optin" in args[0].lower() or "role" in args[0].lower()


@pytest.mark.asyncio
async def test_send_refuses_a_pick_outside_the_pool_with_the_draft_quoted():
    """The form's member picker is Discord's own and can't mark who has opted
    in, so a pick outside the pool is refused at submit — with the draft
    quoted back so it isn't lost, and counted once (no text kept)."""
    from bot_modules.whisper.service import ERROR_TARGET_NEEDS_ROLE

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = FakeMember(id=TARGET_ID, roles=[])  # never opted in
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_count_pool_refusal") as counted, \
         patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="my *draft*")  # type: ignore[arg-type]

    ins.assert_not_called()
    counted.assert_called_once_with(":memory:", 9001)
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    text = "\n".join(
        i.content for i in kwargs["view"].walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )
    # The refusal line itself (the layout may frame it under a heading).
    assert ERROR_TARGET_NEEDS_ROLE.lstrip("❌ ") in text
    assert "> my \\*draft\\*" in text  # quoted, and escaped


@pytest.mark.asyncio
async def test_send_pool_refusal_still_answers_if_the_count_fails():
    """The count is telemetry; a DB fault there must not swallow the refusal."""
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = FakeMember(id=TARGET_ID, roles=[])
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_count_pool_refusal", side_effect=RuntimeError("db")):
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]

    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_slash_command_opens_form():
    """The /whisper send slash command delegates to _open_send_form."""
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    interaction = fake_interaction(user=sender)
    interaction.guild = None  # easy short-circuit — just confirm the call path
    interaction.response.send_message = AsyncMock()

    await cog.whisper_send.callback(cog, interaction)

    args = interaction.response.send_message.call_args.args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_send_dm_forbidden_does_not_persist():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = FakeMember(id=TARGET_ID, roles=[FakeRole(id=ROLE)])
    target.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no dms"))  # type: ignore[attr-defined]

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog._do_insert_whisper", return_value=42), \
         patch("bot_modules.whisper.cog._do_load_whisper", return_value=_row()), \
         patch("bot_modules.whisper.cog._do_delete_whisper") as mocked_delete:
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]
        mocked_delete.assert_called_once()

    feed_channel.send.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert "DM" in _refusal_text(interaction, "hi") or "deliver" in _refusal_text(interaction, "hi").lower()


# ── B3: rate-limit tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_blocked_by_global_cooldown():
    """A second send within SEND_COOLDOWN_SECONDS should be rejected."""
    from unittest.mock import patch as _patch
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.response.send_message = AsyncMock()

    # Simulate that a send just happened 5 seconds ago
    import time as _t
    cog._last_send_at[SENDER_ID] = _t.time() - 5

    with _patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="spam")  # type: ignore[arg-type]

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "wait" in _refusal_text(interaction, "spam").lower() or "slow" in _refusal_text(interaction, "spam").lower()


@pytest.mark.asyncio
async def test_send_blocked_by_per_target_hourly_cap():
    """Exceeding SEND_PER_TARGET_HOURLY_CAP whispers to same target in 1h is rejected."""
    from bot_modules.whisper.cog import SEND_PER_TARGET_HOURLY_CAP
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.response.send_message = AsyncMock()

    # Fill up cap for this (guild, sender, target) triple
    now = _t.time()
    rate_key = (9001, SENDER_ID, TARGET_ID)
    cog._target_sends[rate_key] = [now - i * 60 for i in range(SEND_PER_TARGET_HOURLY_CAP)]
    # And reset global cooldown so it doesn't block first
    cog._last_send_at[SENDER_ID] = 0

    with _patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="spam")  # type: ignore[arg-type]

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "hour" in _refusal_text(interaction, "spam").lower() or str(SEND_PER_TARGET_HOURLY_CAP) in _refusal_text(interaction, "spam")


@pytest.mark.asyncio
async def test_send_allowed_after_cooldown_elapses():
    """After the cooldown window, the send should proceed normally."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=88888))
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(
        side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid)
    )
    interaction.response.send_message = AsyncMock()

    # Cooldown already elapsed (> 30s ago)
    cog._last_send_at[SENDER_ID] = _t.time() - 60

    with _patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.whisper.cog._do_insert_whisper", return_value=42), \
         _patch("bot_modules.whisper.cog._do_load_whisper", return_value=_row()), \
         _patch("bot_modules.whisper.cog._do_set_message_ids"), \
         _patch("bot_modules.economy.game_rewards.fire_member_trigger", new=AsyncMock()):
        await cog._send_impl(interaction, target=target, message="hello again")  # type: ignore[arg-type]

    target.send.assert_awaited_once()  # type: ignore[attr-defined]
    interaction.response.send_message.assert_awaited()


# ── S3: block whispers to timed-out members ──────────────────────────────────


@pytest.mark.asyncio
async def test_send_blocked_when_target_is_timed_out():
    """Whisper to a timed-out member should be rejected before insert."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])

    timed_out_target = FakeMember(id=TARGET_ID, roles=[FakeRole(id=ROLE)])
    timed_out_target.is_timed_out = lambda: True  # type: ignore[attr-defined]
    timed_out_target.send = AsyncMock()  # type: ignore[attr-defined]

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.response.send_message = AsyncMock()

    # Ensure no cooldown
    cog._last_send_at[SENDER_ID] = _t.time() - 60

    with _patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=timed_out_target, message="hi")  # type: ignore[arg-type]

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "timed out" in _refusal_text(interaction, "hi").lower()


# ── S8: hard-fail when feed/log channel missing ───────────────────────────────


@pytest.mark.asyncio
async def test_send_fails_when_feed_channel_missing():
    """When feed channel is None/invalid, send should error before DB insert."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    # Feed channel returns None; log channel returns a valid TextChannel
    log_channel = MagicMock(spec=discord.TextChannel)
    interaction.guild.get_channel = MagicMock(
        side_effect=lambda cid: None if cid == FEED else log_channel
    )
    interaction.response.send_message = AsyncMock()

    cog._last_send_at[SENDER_ID] = _t.time() - 60  # cooldown elapsed

    with _patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]

    ins.assert_not_called()
    target.send.assert_not_called()  # type: ignore[attr-defined]
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "feed" in _refusal_text(interaction, "hi").lower() or "channel" in _refusal_text(interaction, "hi").lower()


# ── configurable rate limits ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_uses_configured_cooldown_not_hardcoded_default():
    """A guild with a shorter configured cooldown isn't blocked by the 30s default."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=88888))
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(side_effect=lambda cid: {FEED: feed_channel}.get(cid))
    interaction.response.send_message = AsyncMock()

    # Last send was 5s ago — would block under the 30s default, but this
    # guild has configured a 3s cooldown.
    cog._last_send_at[SENDER_ID] = _t.time() - 5
    short_cooldown_cfg = WhisperConfig(
        guild_id=9001, role_id=ROLE, channel_id=FEED, log_channel_id=LOG, cooldown_seconds=3,
    )

    with _patch("bot_modules.whisper.cog._load_config", return_value=short_cooldown_cfg), \
         _patch("bot_modules.whisper.cog._do_insert_whisper", return_value=42), \
         _patch("bot_modules.whisper.cog._do_load_whisper", return_value=_row()), \
         _patch("bot_modules.whisper.cog._do_set_message_ids"), \
         _patch("bot_modules.economy.game_rewards.fire_member_trigger", new=AsyncMock()):
        await cog._send_impl(interaction, target=target, message="hello")  # type: ignore[arg-type]

    target.send.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_send_uses_configured_hourly_cap_not_hardcoded_default():
    """A guild with a lower configured hourly cap is blocked before the 5-send default."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.response.send_message = AsyncMock()

    now = _t.time()
    rate_key = (9001, SENDER_ID, TARGET_ID)
    # Only 2 recent sends — under the hardcoded default cap of 5, but this
    # guild has configured a cap of 2.
    cog._target_sends[rate_key] = [now - 60, now - 120]
    cog._last_send_at[SENDER_ID] = 0
    low_cap_cfg = WhisperConfig(
        guild_id=9001, role_id=ROLE, channel_id=FEED, log_channel_id=LOG, hourly_cap_per_target=2,
    )

    with _patch("bot_modules.whisper.cog._load_config", return_value=low_cap_cfg), \
         _patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="spam")  # type: ignore[arg-type]

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "hour" in _refusal_text(interaction, "spam").lower() or "2" in _refusal_text(interaction, "spam")


@pytest.mark.asyncio
async def test_send_to_a_member_on_a_break_is_quiet_success():
    """Take a Break: no row, no DM, no feed post — and the sender sees the
    ordinary "sent", the no-contact path's answer, not a refusal."""
    from bot_modules.whisper.cog import SENT_CONFIRMATION

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, display_name="Sender", roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog.is_on_break", return_value=True), \
         patch("bot_modules.economy.game_rewards.fire_member_trigger", new=AsyncMock()), \
         patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="hello")  # type: ignore[arg-type]

    target.send.assert_not_awaited()  # type: ignore[attr-defined]
    feed_channel.send.assert_not_awaited()
    ins.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        SENT_CONFIRMATION, ephemeral=True
    )



async def test_send_to_a_no_contact_partner_is_quiet_success(monkeypatch):
    """No-contact: the blocked sender sees the ordinary "sent" — never the
    "Whisper Not Sent" card every other refusal now quotes the draft into,
    which would tell them why (docs/specs/no_contact_spec.md). Nothing is stored,
    DMed or posted."""
    from bot_modules.no_contact import service as no_contact_service
    from bot_modules.whisper.cog import SENT_CONFIRMATION

    monkeypatch.setattr(no_contact_service, "check_and_record", lambda *a, **k: True)
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, display_name="Sender", roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()
    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=feed_channel)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.whisper.cog._load_config", return_value=_cfg()), \
         patch("bot_modules.whisper.cog.is_on_break", return_value=False), \
         patch("bot_modules.economy.game_rewards.fire_member_trigger", new=AsyncMock()), \
         patch("bot_modules.whisper.cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="hello")  # type: ignore[arg-type]

    target.send.assert_not_awaited()  # type: ignore[attr-defined]
    feed_channel.send.assert_not_awaited()
    ins.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        SENT_CONFIRMATION, ephemeral=True
    )


def test_pool_refusal_count_stores_no_member_id(tmp_path):
    """The count answers "how often", not "who": the usage row carries
    user_id 0 so the disclosure texts (usage = commands and panels) stay true."""
    from bot_modules.core.db_utils import open_db
    from bot_modules.whisper.cog import _do_count_pool_refusal
    from tests.db_template import migrated_db

    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    _do_count_pool_refusal(db_path, 9001)
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT guild_id, user_id FROM usage_events "
            "WHERE kind='whisper' AND name='send_refused_pool'"
        ).fetchall()
    assert [tuple(r) for r in rows] == [(9001, 0)]

