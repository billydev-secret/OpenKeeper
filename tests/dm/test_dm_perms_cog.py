"""Cog/view wiring for DM permissions.

Only the glue lives here — that each card's buttons actually reach a handler,
and that a handler acts on the row it was drawn beside. What those handlers
*decide* is covered by ``tests/dm/test_dm_perms_logic.py`` and
``tests/dm/test_dm_perms_service.py``; what the cards *look* like is covered by
``tests/dm/test_dm_perms_layouts.py``.

Wiring is worth its own file here because the Components V2 builders return
components with no behaviour attached: a view that forgets to bind one is a
button that silently does nothing, which no builder test can see.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.dm.perms.cog import (
    AskConsentView,
    DmDenyReplyModal,
    DmPermsCog,
    DmRequestModal,
    DmRequestPanelView,
    DmSettingsView,
    RemoveConfirmView,
)
from bot_modules.dm.perms.layouts import (
    CONFIRM_REMOVE_ID,
    DM_CONSENT_ACCEPT_CUSTOM_ID,
    DM_CONSENT_DENY_CUSTOM_ID,
    DM_CONSENT_DENY_REPLY_CUSTOM_ID,
    DM_HELP_PANEL_CUSTOM_ID,
    DM_REQUEST_PANEL_CUSTOM_ID,
    DM_SETTINGS_PANEL_CUSTOM_ID,
    DONE_BUTTON_ID,
    MODE_SELECT_ID,
    REMOVE_BUTTON_ID_BASE,
)
from bot_modules.dm.perms.service import HELP_DOC_KEY
from bot_modules.core.branding_service import PanelArt
from tests.fakes import fake_interaction

TARGET_ID = 424242


def _text_of(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _by_custom_id(view: discord.ui.LayoutView, custom_id: str):
    return next(
        item
        for item in view.walk_children()
        if getattr(item, "custom_id", None) == custom_id
    )


# ── the public request panel ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_panel_open_form_button_opens_the_request_modal():
    """The panel is persistent, so this is the only route into the form."""
    view = DmRequestPanelView(MagicMock())
    interaction = fake_interaction()
    await _by_custom_id(view, DM_REQUEST_PANEL_CUSTOM_ID).callback(interaction)
    interaction.response.send_modal.assert_awaited_once()
    assert isinstance(
        interaction.response.send_modal.await_args.args[0], DmRequestModal
    )


@pytest.mark.asyncio
async def test_panel_settings_button_opens_the_members_own_panel():
    cog = MagicMock()
    view = DmRequestPanelView(cog)
    member = MagicMock(spec=discord.Member)
    interaction = fake_interaction(user=member)
    with patch(
        "bot_modules.dm.perms.cog.open_dm_settings", new=AsyncMock()
    ) as opened:
        await _by_custom_id(view, DM_SETTINGS_PANEL_CUSTOM_ID).callback(interaction)
    opened.assert_awaited_once_with(cog, interaction, member)


@pytest.mark.asyncio
async def test_panel_settings_button_refuses_outside_a_guild():
    """The panel message can be read in a DM search result; the member object
    the panel needs only exists in the guild."""
    view = DmRequestPanelView(MagicMock())
    interaction = fake_interaction(user=MagicMock(spec=discord.User))
    with patch(
        "bot_modules.dm.perms.cog.open_dm_settings", new=AsyncMock()
    ) as opened:
        await _by_custom_id(view, DM_SETTINGS_PANEL_CUSTOM_ID).callback(interaction)
    opened.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_panel_help_button_shows_the_guild_faq_privately_and_pings_nobody():
    """Wiring only — the card's text is ``docs.help_card``'s job
    (test_docs_help_card). The ``dmperms`` doc is read for the clicker's guild, the
    answer is ephemeral, and an admin-written mention in it never pings."""
    view = DmRequestPanelView(MagicMock())
    interaction = fake_interaction(guild=SimpleNamespace(id=42))
    with patch(
        "bot_modules.dm.perms.cog.load_doc_body",
        return_value="# Title\nHi <@&1>",
    ) as load:
        await _by_custom_id(view, DM_HELP_PANEL_CUSTOM_ID).callback(interaction)
    assert load.call_args.args[1:] == (42, HELP_DOC_KEY)
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].roles is False
    assert kwargs["allowed_mentions"].everyone is False
    text = "\n".join(
        i.content
        for i in kwargs["view"].walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )
    assert text.startswith("## 📬 Title")


@pytest.mark.asyncio
async def test_posted_panel_is_bare():
    """The posted hub has no box and no accent bar (Ben, 2026-09-29: "drop
    the box") — so the sticky's build resolves no colour at all, whatever
    the guild's Branding accent."""
    with patch(
        "bot_modules.dm.perms.cog.resolve_panel_art_conn",
        return_value=PanelArt(banner=None, footer=None),
    ):
        content = await DmPermsCog._build_panel(
            SimpleNamespace(bot=MagicMock()), MagicMock(spec=discord.Guild)
        )
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in content.layout.walk_children()
    )


@pytest.mark.asyncio
async def test_posted_panel_carries_the_guild_art():
    """The builder is pure, so the cog reads the guild's Branding banner and
    footer when the sticky builds and hands both down — banner first,
    footer last."""
    banner = "https://dash.example/static/panel-banners/" + "e" * 32 + ".webp"
    footer = "https://dash.example/static/panel-banners/" + "f" * 32 + ".webp"
    with patch(
        "bot_modules.dm.perms.cog.resolve_panel_art_conn",
        return_value=PanelArt(banner=banner, footer=footer),
    ):
        content = await DmPermsCog._build_panel(
            SimpleNamespace(bot=MagicMock()), MagicMock(spec=discord.Guild)
        )
    children = list(content.layout.children)
    assert [g.items[0].media.url for g in (children[0], children[-1])] == [banner, footer]


# ── the consent DM ───────────────────────────────────────────────────────


def test_registered_consent_view_carries_the_three_persistent_ids():
    """``bot.add_view`` registers by custom_id, so the card-less instance the
    cog registers at load must still hold all three — otherwise a press on a
    day-old DM reaches no handler at all."""
    view = AskConsentView(MagicMock())
    ids = {
        item.custom_id
        for item in view.walk_children()
        if getattr(item, "custom_id", None)
    }
    assert ids == {
        DM_CONSENT_ACCEPT_CUSTOM_ID,
        DM_CONSENT_DENY_CUSTOM_ID,
        DM_CONSENT_DENY_REPLY_CUSTOM_ID,
    }
    assert view.timeout is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("custom_id", "accepted"),
    [(DM_CONSENT_ACCEPT_CUSTOM_ID, True), (DM_CONSENT_DENY_CUSTOM_ID, False)],
)
async def test_consent_buttons_reach_the_handler(custom_id, accepted):
    cog = MagicMock()
    view = AskConsentView(cog)
    interaction = fake_interaction()
    with patch.object(AskConsentView, "_handle_click", new=AsyncMock()) as handled:
        await _by_custom_id(view, custom_id).callback(interaction)
    handled.assert_awaited_once_with(interaction, accepted=accepted)


@pytest.mark.asyncio
async def test_a_click_on_a_resolved_request_replaces_the_card(sync_db_path):
    """The DM outlives the request, so a stale press gets a card, not a
    'this interaction failed'."""
    cog = MagicMock()
    cog.bot.ctx = SimpleNamespace(db_path=sync_db_path)
    view = AskConsentView(cog)
    interaction = fake_interaction()
    interaction.message = MagicMock()
    interaction.message.id = 999
    with patch(
        "bot_modules.dm.perms.cog.load_request_by_message_id", return_value=None
    ):
        await view._handle_click(interaction, accepted=True)
    edited = interaction.response.edit_message.await_args.kwargs
    assert "No Longer Active" in _text_of(edited["view"])
    # The pre-conversion DMs are embeds; an edit that only set ``view`` would
    # leave the old embed stacked above the new card.
    assert edited["embed"] is None
    # Nothing on file means no guild in hand, so there is no server to name:
    # unbranded beats half-branded.
    assert "From" not in _text_of(edited["view"])


def _guild(name: str = "The Keep") -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 222
    guild.name = name
    guild.icon = SimpleNamespace(url="https://cdn.example/icon.png")
    return guild


def _attribution(interaction) -> str:
    return _text_of(interaction.response.edit_message.await_args.kwargs["view"])


@pytest.mark.asyncio
async def test_declining_brands_the_card_left_in_the_decliners_own_dm():
    """A DM answered in place still says which server it came from.

    The requester's copy of this same event is a fresh ``send_branded_dm``
    and carries the guild icon and name; the decliner's copy is an *edit* of
    the request DM, so without branding here the two halves of one event
    disagree and the attribution the original DM carried vanishes on the
    edit that answers it.
    """
    view = AskConsentView(MagicMock())
    interaction = fake_interaction()
    with (
        patch.object(AskConsentView, "_finalize_deny", new=AsyncMock()),
        patch(
            "bot_modules.dm.perms.cog._names",
            new=AsyncMock(return_value=lambda uid: f"N{uid}"),
        ),
    ):
        await view._handle_deny(
            interaction,
            guild=_guild(),
            requester=None,
            target=None,
            requester_id=11,
            target_id=22,
            req_type="dm",
            reason="we play chess",
        )
    text = _attribution(interaction)
    assert "Request Declined" in text
    assert "-# From The Keep" in text


def _http_error() -> discord.HTTPException:
    response = MagicMock(status=503, reason="Service Unavailable")
    return discord.HTTPException(response, "boom")


@pytest.mark.asyncio
async def test_plain_decline_is_recorded_even_when_its_card_edit_fails():
    """The press is the member's answer. A card that could not be repainted
    is a stale one — the next press on it gets No Longer Active — so the
    decline goes through rather than leaving the request pending with no
    word to anyone. (The Decline + Note modal is the path that bails, below,
    because there the member has typed something they would lose.)"""
    view = AskConsentView(MagicMock())
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock(side_effect=_http_error())
    with (
        patch.object(AskConsentView, "_finalize_deny", new=AsyncMock()) as done,
        patch(
            "bot_modules.dm.perms.cog._names",
            new=AsyncMock(return_value=lambda uid: f"N{uid}"),
        ),
    ):
        await view._handle_deny(
            interaction,
            guild=_guild(),
            requester=None,
            target=None,
            requester_id=11,
            target_id=22,
            req_type="dm",
            reason="we play chess",
        )
    done.assert_awaited_once()


@pytest.mark.asyncio
async def test_decline_with_note_bails_when_its_card_edit_fails():
    """The modal edits the DM before the DB delete: a failed edit leaves the
    request pending and tells the member nothing changed, so the note they
    typed is not lost to a half-resolved request."""
    cog = MagicMock()
    cog.bot.ctx.db_path = "unused.db"
    guild = _guild()
    cog.bot.get_guild = MagicMock(return_value=guild)
    view = AskConsentView(cog)
    message = MagicMock(spec=discord.Message)
    message.id = 9
    message.edit = AsyncMock(side_effect=_http_error())
    modal = DmDenyReplyModal(view, message)
    modal.reply._value = "not right now"
    interaction = fake_interaction()
    record = {
        "guild_id": guild.id,
        "requester_id": 11,
        "target_id": 22,
        "request_type": "dm",
        "reason": "we play chess",
    }
    with (
        patch(
            "bot_modules.dm.perms.cog.load_request_by_message_id",
            return_value=record,
        ),
        patch.object(AskConsentView, "_finalize_deny", new=AsyncMock()) as done,
        patch(
            "bot_modules.dm.perms.cog._names",
            new=AsyncMock(return_value=lambda uid: f"N{uid}"),
        ),
    ):
        await modal.on_submit(interaction)
    done.assert_not_awaited()
    sent = interaction.response.send_message.await_args
    assert sent.kwargs["ephemeral"] is True
    assert "nothing was changed" in sent.args[0]


@pytest.mark.asyncio
async def test_accepting_brands_the_card_left_in_the_targets_own_dm():
    """Same asymmetry on the accept side: both fresh DMs are branded, so the
    in-place copy the target keeps is too."""
    cog = MagicMock()
    cog.consent_pairs = {}
    cog._post_audit = AsyncMock()
    view = AskConsentView(cog)
    interaction = fake_interaction()
    member = MagicMock(spec=discord.Member)
    member.display_name = "Ada"
    with (
        patch("bot_modules.dm.perms.cog.add_consent_pair"),
        patch("bot_modules.dm.perms.cog.remove_request"),
        patch("bot_modules.dm.perms.cog.write_audit_log"),
        patch("bot_modules.dm.perms.cog._dm", new=AsyncMock()) as dm,
        patch(
            "bot_modules.dm.perms.cog._names",
            new=AsyncMock(return_value=lambda uid: f"N{uid}"),
        ),
    ):
        await view._handle_accept(
            interaction,
            guild=_guild(),
            requester=member,
            target=member,
            requester_id=11,
            target_id=22,
            req_type="dm",
            reason="we play chess",
            source_msg_id=9,
            source_channel_id=8,
        )
    text = _attribution(interaction)
    assert "Connected" in text
    assert "-# From The Keep" in text
    # …and the two fresh copies still go out through the branded sender.
    assert dm.await_count == 2


# ── the member's settings panel ──────────────────────────────────────────


def _settings_view(connections=()) -> DmSettingsView:
    cog = MagicMock()
    cog._mode_roles_for.return_value = {}
    member = MagicMock(spec=discord.Member)
    member.id = 111
    member.guild = MagicMock()
    member.guild.id = 222
    return DmSettingsView(
        cog,
        member,
        mode="ask",
        connections=list(connections),
        name_fn=lambda uid: f"N{uid}",
    )


def _connection(user_id: int) -> dict[str, object]:
    return {
        "user_id": user_id,
        "type": "dm",
        "reason": "",
        "created_at": 1_750_000_000,
    }


def test_settings_panel_binds_every_control_it_draws():
    """Each Remove is drawn by index; a missed binding is a dead red button."""
    view = _settings_view([_connection(1), _connection(2)])
    assert view.find_item(MODE_SELECT_ID).callback == view._on_mode
    assert view.find_item(DONE_BUTTON_ID).callback == view._on_done
    for index in range(2):
        assert view.find_item(REMOVE_BUTTON_ID_BASE + index).callback is not None


@pytest.mark.asyncio
async def test_settings_panel_remove_confirms_for_the_row_it_sits_beside():
    """The rows share one handler, so the wrong closure would remove the
    wrong person — silently, and irreversibly for the pair."""
    view = _settings_view([_connection(501), _connection(502)])
    interaction = fake_interaction()
    interaction.original_response = AsyncMock(return_value=MagicMock())
    await view.find_item(REMOVE_BUTTON_ID_BASE + 1).callback(interaction)
    confirm = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(confirm, RemoveConfirmView)
    assert confirm.user_id == 502
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_settings_panel_applies_a_mode_change_and_redraws_in_place():
    view = _settings_view()
    select = view.find_item(MODE_SELECT_ID)
    select._values = ["closed"]
    interaction = fake_interaction(user=MagicMock(spec=discord.Member))
    with (
        patch(
            "bot_modules.dm.perms.cog.set_member_dm_mode", new=AsyncMock()
        ) as applied,
        patch("bot_modules.dm.perms.cog.write_audit_log"),
    ):
        await select.callback(interaction)
    assert applied.await_args.args[1] == "closed"
    assert view._mode == "closed"
    assert interaction.response.edit_message.await_args.kwargs["view"] is view
    text = _text_of(view)
    assert "Changed to 🔴 **Closed**" in text
    assert "-# Changed" not in text


@pytest.mark.asyncio
async def test_settings_panel_done_greys_the_card_out():
    """A stopped view whose controls still look live fails the next press
    with Discord's generic error and no explanation."""
    view = _settings_view([_connection(1)])
    interaction = fake_interaction()
    await view.find_item(DONE_BUTTON_ID).callback(interaction)
    assert all(
        item.disabled for item in view.walk_children() if hasattr(item, "disabled")
    )
    assert view.is_finished()


@pytest.mark.asyncio
async def test_confirming_a_removal_revokes_that_connection_and_reloads():
    view = _settings_view([_connection(501)])
    view.cog.revoke_connection = AsyncMock(return_value=True)
    view.reload = AsyncMock()
    confirm = RemoveConfirmView(view, _connection(501))
    interaction = fake_interaction()
    await confirm.find_item(CONFIRM_REMOVE_ID).callback(interaction)
    # Deferred first: the revoke fetches, edits and DMs before it can answer.
    interaction.response.defer.assert_awaited_once()
    assert view.cog.revoke_connection.await_args.args[2] == 501
    assert "Connection Removed" in _text_of(
        interaction.edit_original_response.await_args.kwargs["view"]
    )
    view.reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelling_a_removal_deletes_the_confirm_card():
    """Cancel removes nothing and *says* nothing: the ephemeral confirm is
    deleted, leaving the settings panel exactly as it was. The first cut
    replaced it with a "Nothing Changed" card — a receipt for a non-event."""
    view = _settings_view([_connection(501)])
    view.cog.revoke_connection = AsyncMock()
    confirm = RemoveConfirmView(view, _connection(501))
    from bot_modules.dm.perms.layouts import CONFIRM_CANCEL_ID

    interaction = fake_interaction()
    interaction.delete_original_response = AsyncMock()
    await confirm.find_item(CONFIRM_CANCEL_ID).callback(interaction)
    view.cog.revoke_connection.assert_not_awaited()
    interaction.response.defer.assert_awaited_once()
    interaction.delete_original_response.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()


# ── the request form ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_request_modal_submits_who_what_and_why_in_one_go():
    """All three answers reach the submit-time pre-checks together — there is
    no intermediate state left to lose between steps any more."""
    cog = MagicMock()
    cog._submit_dm_request = AsyncMock()
    modal = DmRequestModal(cog)
    member = MagicMock(spec=discord.Member)
    member.id = TARGET_ID
    modal.who._values = [member]
    modal.kind._value = "friend"
    modal.reason._value = "  we play chess  "
    interaction = fake_interaction()
    await modal.on_submit(interaction)
    cog._submit_dm_request.assert_awaited_once_with(
        interaction, member, "friend", "we play chess"
    )


@pytest.mark.asyncio
async def test_the_request_modal_refuses_an_empty_pick():
    cog = MagicMock()
    cog._submit_dm_request = AsyncMock()
    modal = DmRequestModal(cog)
    modal.who._values = []
    interaction = fake_interaction()
    await modal.on_submit(interaction)
    cog._submit_dm_request.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


# ── the expiry sweep ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expiry_sweep_never_opens_the_db_on_the_event_loop(sync_db_path):
    """The sweep must not call get_request_limits() from the coroutine.

    ``expire_stale_pending_requests`` runs in a worker thread, but the loop
    that DMs each requester used to reach back for the guild's expiry window
    with a synchronous ``open_db()`` — under a hot WAL lock that stalls the
    whole bot. The window now travels on the expired row, so a
    ``get_request_limits`` that explodes on contact must never be reached.
    """
    import time

    from bot_modules.core.db_utils import open_db
    from bot_modules.dm.perms.service import (
        init_db,
        set_request_limits,
        upsert_request,
    )

    init_db(sync_db_path)
    upsert_request(sync_db_path, 5, 100, 200, "dm", "", 1, None)
    with open_db(sync_db_path) as conn:
        conn.execute("UPDATE dm_requests SET created_at = ?", (time.time() - 6 * 3600,))
        set_request_limits(conn, 5, expiry_hours=2)

    # Real strings, not the auto-mocked attribute: the audit line escapes the
    # names it renders, and escape_markdown takes a str. A MagicMock survived
    # the old f-string by way of __str__ and hid that these are member text.
    requester = MagicMock(spec=discord.Member)
    requester.id = 100
    requester.display_name = "Requester"
    target = MagicMock(spec=discord.Member)
    target.id = 200
    target.display_name = "Target"
    guild = MagicMock()
    guild.id = 5
    guild.name = "Test Guild"
    guild.get_member = lambda uid: requester if uid == 100 else target

    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=sync_db_path)
    bot.get_guild = lambda _gid: guild

    cog = DmPermsCog(bot)
    sent = AsyncMock()
    with (
        patch("bot_modules.dm.perms.cog.write_audit_log"),
        patch.object(DmPermsCog, "_post_audit", new=AsyncMock()),
        patch("bot_modules.dm.perms.cog._dm", new=sent),
        patch(
            "bot_modules.dm.perms.cog._names",
            new=AsyncMock(return_value=lambda uid: f"N{uid}"),
        ),
        patch(
            "bot_modules.dm.perms.cog.get_request_limits",
            side_effect=AssertionError("blocking DB open on the event loop"),
        ),
    ):
        await cog._expire_stale_now()

    # The requester was still told, with their guild's own 2-hour window.
    assert sent.await_count == 1
    assert "2 hours" in _text_of(sent.await_args.kwargs["layout"])


@pytest.mark.asyncio
async def test_revoke_notifies_a_departed_target_via_fetch_user(sync_db_path):
    """The settings panel lists connections straight from the DB, so a member
    can remove one with someone who has since left the guild — and every
    other guild the bot shares with them, so neither ``guild.get_member`` nor
    ``bot.get_user`` finds them. The confirm card promises "both of you get a
    DM about it" and the spec repeats it unconditionally, so the miss must
    fall back to a REST fetch rather than silently dropping their notice.
    """
    from bot_modules.dm.perms.service import add_consent_pair, init_db

    init_db(sync_db_path)
    add_consent_pair(sync_db_path, 5, 100, 200, rel_type="dm", reason="")

    actor = MagicMock(spec=discord.Member)
    actor.id = 100
    actor.display_name = "Actor"

    guild = MagicMock()
    guild.id = 5
    guild.name = "Test Guild"
    guild.get_member.return_value = None  # departed, not a guild member
    guild.get_channel.return_value = None

    fetched_user = MagicMock(spec=discord.User)
    fetched_user.id = 200

    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=sync_db_path)
    bot.get_user.return_value = None  # not cached from any other guild either
    bot.fetch_user = AsyncMock(return_value=fetched_user)

    cog = DmPermsCog(bot)
    sent = AsyncMock()
    with (
        patch("bot_modules.dm.perms.cog.write_audit_log"),
        patch.object(DmPermsCog, "_post_audit", new=AsyncMock()),
        patch("bot_modules.dm.perms.cog._dm", new=sent),
        patch(
            "bot_modules.dm.perms.cog._names",
            new=AsyncMock(return_value=lambda uid: f"N{uid}"),
        ),
    ):
        removed = await cog.revoke_connection(guild, actor, 200)

    assert removed is True
    bot.fetch_user.assert_awaited_once_with(200)
    dm_recipients = [call.args[0] for call in sent.await_args_list]
    assert actor in dm_recipients
    assert fetched_user in dm_recipients


@pytest.mark.asyncio
async def test_revoke_retires_the_accepted_card_in_the_targets_dm(sync_db_path):
    """The request card being retired is the decision DM the target pressed
    Accept on, so its stored channel is a DM channel. ``guild.get_channel``
    can never resolve one, which left every revoked connection's card in the
    target's DMs unchanged; the edit goes by id, and is branded like every
    other in-place DM edit here."""
    from bot_modules.dm.perms.service import add_consent_pair, init_db

    init_db(sync_db_path)
    add_consent_pair(
        sync_db_path, 5, 100, 200, rel_type="dm", reason="",
        source_msg_id=9001, source_channel_id=8001,
    )
    actor = MagicMock(spec=discord.Member)
    actor.id = 100
    actor.display_name = "Actor"
    guild = MagicMock()
    guild.id = 5
    guild.name = "Test Guild"
    guild.icon = None
    guild.get_channel.return_value = None  # a DM channel is never a guild's
    source = MagicMock()
    source.edit = AsyncMock()
    dm_channel = MagicMock()
    dm_channel.get_partial_message.return_value = source

    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=sync_db_path)
    bot.get_partial_messageable.return_value = dm_channel
    cog = DmPermsCog(bot)
    with (
        patch("bot_modules.dm.perms.cog.write_audit_log"),
        patch.object(DmPermsCog, "_post_audit", new=AsyncMock()),
        patch("bot_modules.dm.perms.cog._dm", new=AsyncMock()),
        patch(
            "bot_modules.dm.perms.cog._names",
            new=AsyncMock(return_value=lambda uid: f"N{uid}"),
        ),
    ):
        assert await cog.revoke_connection(guild, actor, 200) is True

    bot.get_partial_messageable.assert_called_once_with(8001)
    dm_channel.get_partial_message.assert_called_once_with(9001)
    source.edit.assert_awaited_once()
    assert "-# From Test Guild" in _text_of(source.edit.await_args.kwargs["view"])


def test_precheck_dm_request_escapes_the_targets_display_name():
    """``classify_dm_request`` drops ``target_display_name`` straight into the
    refusal string it returns (ephemeral message content) — a markdown
    nickname must not render as bot-authored formatting in the requester's
    own "isn't accepting DM requests" reply.
    """
    from types import SimpleNamespace

    from bot_modules.dm.perms.cog import DmPermsCog

    requester = MagicMock(spec=discord.Member)
    requester.id = 1
    target = MagicMock(spec=discord.Member)
    target.id = 2
    target.bot = False
    target.roles = []
    target.display_name = "[Claim your gift](https://evil.example)"
    guild = MagicMock(spec=discord.Guild)
    guild.id = 5

    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=None)
    cog = DmPermsCog(bot)

    # Force the "closed" branch without touching the DB: the no-contact path
    # (a target on the requester's no-contact list reads as DMs-closed) hits
    # the same classify_dm_request line as an ordinary closed member.
    with (
        patch("bot_modules.dm.perms.cog.is_no_contact", return_value=True),
        patch("bot_modules.dm.perms.cog.is_on_break", return_value=False),
    ):
        error = cog._precheck_dm_request(guild, requester, target)

    escaped_name = discord.utils.escape_markdown(target.display_name)
    assert error == f"❌ {escaped_name} isn't accepting DM requests right now."
    # the leading `[` is what neutralises the masked link — confirm the raw,
    # unescaped bracket never reaches the message the requester is shown
    assert f"❌ {target.display_name} isn't" not in error
