"""Tests for the economy quest views — the /bank quests claim UI and the
sign-off flow the todo board's Sign-Offs button opens.

The pure card content (the sign-off card's fields, the detail popup, the
claimed receipt, the board's one-line renderer) moved to
``tests/economy/test_quest_layouts.py`` alongside the builders themselves
(``quest_layouts.py``); this file stays the view/interaction layer — buttons,
selects, modals, and the resolve/repaint flow — exercised the way the pilot
does (``tests/dm/test_dm_perms_layouts.py`` for the cards, the cog's own tests
for the wiring).
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quest_views import (
    QuestApproveButton,
    QuestClaimView,
    QuestDenyButton,
    QuestDenyModal,
    QuestDetailSelect,
    SignoffPickSelect,
    _signoff_buttons_row,
    can_manage_economy,
    open_my_quests,
    open_signoff_picker,
    open_wallet,
)
from bot_modules.economy.quests import quest_period
from bot_modules.economy.quests_service import (
    claim_quest,
    create_quest,
    resolve_claim,
    set_quest_active,
)
from bot_modules.economy.service import (
    EconSettings,
    get_balance,
    load_econ_settings,
    save_econ_settings,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
BANK_CHANNEL = 424242
CLAIMANT = 500
MANAGER_ROLE = 7007


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db_path=db, open_db=lambda: open_db(db))


def _enable(db, **overrides) -> None:
    # Set bonuses zeroed — one-quest boards would pay the clear-the-board
    # bonus on approval and skew exact-balance assertions.
    values: dict[str, object] = {
        "enabled": True,
        "quest_set_bonus_daily": 0,
        "quest_set_bonus_weekly": 0,
    }
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _settings(db) -> EconSettings:
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID)


def _member(*, admin=False, role_ids=(), member_id=CLAIMANT, premium=None) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = False
    m.display_name = "Member"
    m.mention = f"<@{member_id}>"
    m.premium_since = premium
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _mk_quest(db, *, qtype="weekly", reward=30, signoff=1, title="Help out") -> int:
    with open_db(db) as conn:
        qid = create_quest(
            conn, GUILD_ID, title=title, description="", qtype=qtype, reward=reward,
            signoff=signoff, criteria="Do the thing", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=1,
        )
        set_quest_active(conn, GUILD_ID, qid, True)
    return qid


def _period(qtype="weekly") -> str:
    return quest_period(qtype, local_day_for(time.time(), 0))


def _pending_claim(db, qid, user_id=CLAIMANT, qtype="weekly") -> int:
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        outcome = claim_quest(
            conn, settings, GUILD_ID, qid, user_id, period=_period(qtype), booster=False
        )
    return outcome.claim_id


def _bot(ctx, *, claimant_premium=None) -> MagicMock:
    bot = MagicMock()
    bot.ctx = ctx
    guild = MagicMock()
    claimant = MagicMock()
    claimant.premium_since = claimant_premium
    guild.get_member = MagicMock(return_value=claimant)
    bot.get_guild = MagicMock(return_value=guild)
    # The todo board is where pending sign-offs live, so every path that files
    # or resolves a claim repaints it. Exposed as ``bot.todo_refresh`` so a
    # test can assert the repaint without reaching through get_cog.
    bot.todo_refresh = AsyncMock(return_value=True)
    bot.get_cog = MagicMock(
        return_value=SimpleNamespace(refresh_board=bot.todo_refresh)
    )
    return bot


def _button_interaction(bot, *, user, card=None) -> MagicMock:
    i = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    i.client = bot
    i.user = user
    i.message = card
    return i


def _select_of(view, cls):
    """The one item of ``cls`` inside ``view``'s component tree.

    A card's board content now shares the view with its selects (Components
    V2 has no separate content-vs-view slot), so a select is no longer
    reliably ``view.children[0]`` — it is found by type instead.
    """
    return next(c for c in view.walk_children() if isinstance(c, cls))


def _view_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


# ── permission gate ───────────────────────────────────────────────────────────


def test_can_manage_admin_and_role():
    settings = EconSettings(manager_role_id=MANAGER_ROLE)
    assert can_manage_economy(_member(admin=True), settings)
    assert can_manage_economy(_member(role_ids=(MANAGER_ROLE,)), settings)
    assert not can_manage_economy(_member(role_ids=(1,)), settings)


# ── persistent custom_id round-trip (survives restart) ────────────────────────


@pytest.mark.asyncio
async def test_custom_id_roundtrip():
    approve = QuestApproveButton(12345)
    deny = QuestDenyButton(678)
    assert approve.custom_id == "econ_claim:approve:12345"
    assert deny.custom_id == "econ_claim:deny:678"

    am = approve.template.match("econ_claim:approve:12345")
    assert am is not None
    rebuilt = await QuestApproveButton.from_custom_id(MagicMock(), MagicMock(), am)
    assert rebuilt.claim_id == 12345

    dm = deny.template.match("econ_claim:deny:678")
    assert dm is not None
    rebuilt_d = await QuestDenyButton.from_custom_id(MagicMock(), MagicMock(), dm)
    assert rebuilt_d.claim_id == 678

    row = _signoff_buttons_row(99)
    assert {getattr(c, "custom_id", None) for c in row.children} == {
        "econ_claim:approve:99",
        "econ_claim:deny:99",
    }


# ── quest board "Show my quests" button ───────────────────────────────────────


@pytest.mark.asyncio
async def test_wallet_button_routes_to_the_cog_panel():
    """The button opens the same ephemeral view /bank wallet does."""
    cog = MagicMock()
    cog.send_wallet_panel = AsyncMock()
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    interaction = _button_interaction(bot, user=_member())

    await open_wallet(interaction)

    bot.get_cog.assert_called_once_with("EconomyCog")
    cog.send_wallet_panel.assert_awaited_once_with(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_wallet_button_is_never_a_dead_button():
    """With the cog unloaded mid-restart the click still answers, not hangs."""
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    interaction = _button_interaction(bot, user=_member())

    await open_wallet(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_show_my_quests_routes_to_the_cog_panel():
    """The button opens the same ephemeral panel /bank quests does."""
    cog = MagicMock()
    cog.send_quests_panel = AsyncMock()
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=cog)
    interaction = _button_interaction(bot, user=_member())

    await open_my_quests(interaction)

    bot.get_cog.assert_called_once_with("EconomyCog")
    cog.send_quests_panel.assert_awaited_once_with(interaction)
    interaction.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_show_my_quests_is_never_a_dead_button():
    """With the cog unloaded mid-restart the click still answers, not hangs."""
    bot = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    interaction = _button_interaction(bot, user=_member())

    await open_my_quests(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True


# ── /bank quests details select ───────────────────────────────────────────────


def _detail_quest(**over) -> dict:
    q = {
        "id": 7,
        "title": "Good Vibes",
        "qtype": "daily",
        "reward": 12,
        "reward_xp": 5,
        "description": "Spread some reactions today.",
        "state": "reaction_given",
        "progress_current": 2,
        "progress_target": 10,
        "spotlight": False,
    }
    q.update(over)
    return q


@pytest.mark.asyncio
async def test_detail_select_shows_full_story(ctx, db):
    _enable(db)
    view = QuestClaimView(
        ctx, _settings(db), cast(discord.Guild, FakeGuild(id=GUILD_ID)),
        [], detailable=[_detail_quest()],
    )
    select = _select_of(view, QuestDetailSelect)
    select._values = ["7"]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    body = _view_text(kwargs["view"])
    assert "Good Vibes" in body
    assert "**12**" in body and "5 XP" in body  # reward line
    assert "Spread some reactions today." in body  # full description
    assert "react to people's messages" in body  # how-it-completes explainer
    assert "2/10" in body  # progress bar


@pytest.mark.asyncio
async def test_detail_select_community_and_stale(ctx, db):
    _enable(db)
    goal = _detail_quest(
        id=8, title="Team goal", qtype="community", state="community",
        current=40, target=100, reward_xp=0,
    )
    view = QuestClaimView(
        ctx, _settings(db), cast(discord.Guild, FakeGuild(id=GUILD_ID)),
        [], detailable=[goal],
    )
    select = _select_of(view, QuestDetailSelect)
    select._values = ["8"]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)
    body = _view_text(interaction.response.send_message.await_args.kwargs["view"])
    assert "40/100" in body  # community bar, no claim-state line

    # A value that left the board (post-reroll stale view) fails soft.
    select._values = ["999"]  # type: ignore[attr-defined]
    interaction2 = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction2)
    msg = interaction2.response.send_message.await_args.args[0]
    assert "no longer on your board" in msg


@pytest.mark.asyncio
async def test_detail_select_names_a_scoped_quests_channel(ctx, db):
    """A channel-scoped quest tells the member where to go (todo #187).

    The list row can't carry it — its columns are a padded monospace table —
    so the details card is the surface that does.
    """
    _enable(db)
    view = QuestClaimView(
        ctx, _settings(db), cast(discord.Guild, FakeGuild(id=GUILD_ID)),
        [], detailable=[_detail_quest(trigger_channel_id=555)],
    )
    select = _select_of(view, QuestDetailSelect)
    select._values = ["7"]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)
    body = _view_text(interaction.response.send_message.await_args.kwargs["view"])
    assert "📍 Only counts in <#555>" in body


@pytest.mark.asyncio
async def test_detail_select_says_nothing_for_an_any_channel_quest(ctx, db):
    """No trigger channel means no extra line — never "any channel".

    Production is full of these: the VN quest channel-scope work (5ebec438)
    shipped with the picker unset, so NULL is the common case and it has to
    render as plainly as it did before the scope line existed.
    """
    _enable(db)
    view = QuestClaimView(
        ctx, _settings(db), cast(discord.Guild, FakeGuild(id=GUILD_ID)),
        [], detailable=[_detail_quest()],
    )
    select = _select_of(view, QuestDetailSelect)
    select._values = ["7"]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)
    body = _view_text(interaction.response.send_message.await_args.kwargs["view"])
    assert "Only counts in" not in body
    assert "<#" not in body


@pytest.mark.asyncio
async def test_detail_select_asks_the_shared_rule_about_a_finished_goal(ctx, db):
    """Wiring only: the card routes through `quests.scope_worth_showing`.

    The rule itself — that a community goal is finished by its counter, since
    its state is never `done` — is covered value-by-value in
    tests/economy/test_economy_quests_logic.py. This proves the card asks it at all,
    which is the part that was wrong: it used to test `state != "done"`
    inline, a guard that never fires for a goal.
    """
    _enable(db)
    goal = _detail_quest(
        id=8, title="Picture week", qtype="community", state="community",
        current=70, target=70, reward_xp=0, trigger_channel_id=555,
    )
    view = QuestClaimView(
        ctx, _settings(db), cast(discord.Guild, FakeGuild(id=GUILD_ID)),
        [], detailable=[goal],
    )
    select = _select_of(view, QuestDetailSelect)
    select._values = ["8"]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)
    body = _view_text(interaction.response.send_message.await_args.kwargs["view"])
    assert "<#555>" not in body


@pytest.mark.asyncio
async def test_detail_select_drops_the_channel_once_the_quest_is_done(ctx, db):
    """Deliberate: a done quest keeps its channel to itself.

    Same call the login digest makes — sending someone to a channel to earn
    what they have already earned reads as a bug to them, not a signpost.
    """
    _enable(db)
    view = QuestClaimView(
        ctx, _settings(db), cast(discord.Guild, FakeGuild(id=GUILD_ID)),
        [], detailable=[_detail_quest(trigger_channel_id=555, state="done")],
    )
    select = _select_of(view, QuestDetailSelect)
    select._values = ["7"]  # type: ignore[attr-defined]
    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)
    body = _view_text(interaction.response.send_message.await_args.kwargs["view"])
    assert "<#555>" not in body


# ── /bank quests claim select ─────────────────────────────────────────────────


def _claim_view(ctx, db, claimable) -> QuestClaimView:
    guild = cast(discord.Guild, FakeGuild(id=GUILD_ID))
    return QuestClaimView(ctx, _settings(db), guild, claimable)


@pytest.mark.asyncio
async def test_instant_claim_pays_once_and_relays_collision(ctx, db):
    _enable(db)
    qid = _mk_quest(db, qtype="daily", reward=15, signoff=0, title="Say hi")
    claimable = [{"id": qid, "title": "Say hi", "qtype": "daily", "reward": 15}]

    view = _claim_view(ctx, db, claimable)
    select = _select_of(view, discord.ui.Select)
    select._values = [str(qid)]  # type: ignore[attr-defined]

    interaction = _button_interaction(_bot(ctx), user=_member())
    await select.callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 15
    interaction.followup.send.assert_awaited_once()
    assert "view" in interaction.followup.send.await_args.kwargs
    body = _view_text(interaction.followup.send.await_args.kwargs["view"])
    assert "🎉 Quest Complete!" in body

    # Second claim same period → collision message relayed, no double pay.
    view2 = _claim_view(ctx, db, claimable)
    select2 = _select_of(view2, discord.ui.Select)
    select2._values = [str(qid)]  # type: ignore[attr-defined]
    interaction2 = _button_interaction(_bot(ctx), user=_member())
    await select2.callback(interaction2)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 15  # unchanged
    msg = interaction2.followup.send.await_args.args[0]
    assert "already completed" in msg.lower()


@pytest.mark.asyncio
async def test_signoff_claim_files_pending_and_repaints_the_board(ctx, db):
    """A sign-off claim posts no card anywhere — it lands on the todo board.

    The card that used to go to the bank channel is gone, so nothing is sent to
    a channel and no card ids are recorded; the only Discord effect is the
    board repaint that makes the claim visible to the mods.
    """
    _enable(db, bank_channel_id=BANK_CHANNEL)
    qid = _mk_quest(db, qtype="weekly", reward=50, signoff=1, title="Sign me")
    claimable = [{"id": qid, "title": "Sign me", "qtype": "weekly", "reward": 50}]

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = BANK_CHANNEL
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_channel = MagicMock(return_value=channel)

    bot = _bot(ctx)
    view = QuestClaimView(ctx, _settings(db), guild, claimable)
    select = _select_of(view, discord.ui.Select)
    select._values = [str(qid)]  # type: ignore[attr-defined]
    interaction = _button_interaction(bot, user=_member())
    await select.callback(interaction)

    channel.send.assert_not_awaited()  # no bank-channel card
    bot.todo_refresh.assert_awaited_once_with(GUILD_ID)
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state, card_channel_id, card_message_id FROM econ_quest_claims "
            "WHERE quest_id = ?",
            (qid,),
        ).fetchone()
        # No payout yet — sign-off pending.
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 0
    assert row["state"] == "pending"
    assert row["card_channel_id"] is None
    assert row["card_message_id"] is None
    assert "quest board" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_signoff_claim_survives_a_guild_with_no_todo_board(ctx, db):
    """No board cog wired → the claim still files, silently.

    Same shape as the old missing-bank-channel case: the claim row is the
    thing that matters, and a guild whose board isn't placed still has the
    dashboard's claims list to resolve from.
    """
    _enable(db)
    qid = _mk_quest(db, qtype="weekly", reward=50, signoff=1, title="No board")
    claimable = [{"id": qid, "title": "No board", "qtype": "weekly", "reward": 50}]
    bot = _bot(ctx)
    bot.get_cog = MagicMock(return_value=None)
    view = _claim_view(ctx, db, claimable)
    select = _select_of(view, discord.ui.Select)
    select._values = [str(qid)]  # type: ignore[attr-defined]
    interaction = _button_interaction(bot, user=_member())

    await select.callback(interaction)  # must not raise

    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state FROM econ_quest_claims WHERE quest_id = ?", (qid,)
        ).fetchone()
    assert row["state"] == "pending"  # claim recorded despite no board
    interaction.followup.send.assert_awaited()


# ── approve / deny resolution ─────────────────────────────────────────────────


def _legacy_card() -> MagicMock:
    """A bank-channel sign-off card from before the move to the board.

    Nothing posts these any more, but the ones already out there stay
    clickable, so the resolve path still has to edit them. Still the embed
    message it was created as, hence ``flags.ephemeral`` False.
    """
    card = MagicMock()
    card.flags = MagicMock(ephemeral=False)
    card.edit = AsyncMock()
    return card


def _ephemeral_message() -> MagicMock:
    """The picker's detail view: a real message, but an ephemeral one."""
    msg = MagicMock()
    msg.flags = MagicMock(ephemeral=True)
    msg.edit = AsyncMock()
    return msg


def _picker_interaction(bot, *, user, members=(), message=None) -> MagicMock:
    guild = FakeGuild(id=GUILD_ID, members={m.id: m for m in members})
    i = fake_interaction(guild=guild)
    i.client = bot
    i.user = user
    i.message = message
    return i


@pytest.mark.asyncio
async def test_approve_pays_edits_card_and_announces_nothing(ctx, db):
    """An approval is announced by the ledger drain, not by this path.

    ``resolve_claim`` credits kind "quest", and the register feed posts every
    such row — so posting a notice here as well would double-report the payout.
    """
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)

    card = _legacy_card()
    bot = _bot(ctx, claimant_premium=None)
    interaction = _button_interaction(bot, user=_member(admin=True, member_id=999), card=card)

    with patch(
        "bot_modules.economy.quest_views.announce_signoff_outcome",
        new=AsyncMock(return_value=True),
    ) as announce:
        await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 30
        state = conn.execute(
            "SELECT state, resolver_id FROM econ_quest_claims WHERE id = ?", (claim_id,)
        ).fetchone()
    assert state["state"] == "paid"
    assert state["resolver_id"] == 999
    card.edit.assert_awaited_once()
    kwargs = card.edit.await_args.kwargs
    # The embed → card conversion: the message's embed slot is explicitly
    # nulled, exactly like every other "edit converting embed to card".
    assert kwargs["embed"] is None
    assert kwargs["content"] is None
    assert kwargs["attachments"] == []
    assert "✅ Quest Approved" in _view_text(kwargs["view"])
    assert not any(
        isinstance(c, discord.ui.Button) for c in kwargs["view"].walk_children()
    )  # resolved card carries no buttons
    announce.assert_not_awaited()
    bot.todo_refresh.assert_awaited_once_with(GUILD_ID)  # row drops off the board
    interaction.response.send_message.assert_awaited()  # ephemeral ack


@pytest.mark.asyncio
async def test_approve_uses_claimant_booster_not_managers(ctx, db):
    _enable(db)  # booster_multiplier 1.5
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)

    card = _legacy_card()
    # Claimant IS boosting; the clicking manager is not — reward must boost.
    bot = _bot(ctx, claimant_premium=object())
    manager = _member(admin=True, member_id=999, premium=None)
    interaction = _button_interaction(bot, user=manager, card=card)

    await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 45  # ceil(30 * 1.5)


@pytest.mark.asyncio
async def test_deny_modal_stores_reason_posts_to_register_and_allows_reclaim(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)

    card = _legacy_card()
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(admin=True, member_id=999))

    modal = QuestDenyModal(claim_id, card)
    modal.reason = MagicMock(value="  not enough proof  ")  # type: ignore[assignment]

    with patch(
        "bot_modules.economy.quest_views.announce_signoff_outcome",
        new=AsyncMock(return_value=True),
    ) as announce:
        await modal.on_submit(interaction)

    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state, deny_reason FROM econ_quest_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 0  # no pay on deny
    assert row["state"] == "denied"
    assert row["deny_reason"] == "not enough proof"
    card.edit.assert_awaited_once()
    kwargs = card.edit.await_args.kwargs
    assert kwargs["embed"] is None
    body = _view_text(kwargs["view"])
    assert "❌ Quest Denied" in body
    assert "not enough proof" in body
    # A denial writes no ledger row, so the register only hears about it here —
    # carrying the reason the modal promised the member.
    announce.assert_awaited_once()
    assert announce.await_args is not None
    announce_kwargs = announce.await_args.kwargs
    assert announce_kwargs["state"] == "denied"
    assert announce_kwargs["deny_reason"] == "not enough proof"
    assert announce_kwargs["user_id"] == CLAIMANT
    bot.todo_refresh.assert_awaited_once_with(GUILD_ID)

    # Denied → re-claimable for the same period.
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        outcome = claim_quest(
            conn, settings, GUILD_ID, qid, CLAIMANT, period=_period(), booster=False
        )
    assert outcome.state == "pending"


@pytest.mark.asyncio
async def test_deny_button_refuses_non_manager_without_modal(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(role_ids=(1,)), card=_legacy_card())

    await QuestDenyButton(claim_id).callback(interaction)

    interaction.response.send_modal.assert_not_awaited()
    msg = interaction.response.send_message.await_args.args[0]
    assert "permission" in msg.lower()


@pytest.mark.asyncio
async def test_approve_refuses_non_manager(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    card = _legacy_card()
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(role_ids=(1,)), card=card)

    await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 0
    card.edit.assert_not_awaited()
    msg = interaction.response.send_message.await_args.args[0]
    assert "permission" in msg.lower()


@pytest.mark.asyncio
async def test_already_resolved_race_refreshes_card_no_double_pay(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    # Resolve it out-of-band (as the dashboard would) → paid.
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        resolve_claim(
            conn, settings, claim_id, approve=True, resolver_id=111, booster=False
        )
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 30

    card = _legacy_card()
    bot = _bot(ctx)
    interaction = _button_interaction(bot, user=_member(admin=True, member_id=999), card=card)

    with patch(
        "bot_modules.economy.quest_views.announce_signoff_outcome",
        new=AsyncMock(return_value=True),
    ) as announce:
        await QuestApproveButton(claim_id).callback(interaction)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 30  # still single pay
    card.edit.assert_awaited_once()  # card refreshed to the true (paid) state
    announce.assert_not_awaited()  # no second announcement
    msg = interaction.response.send_message.await_args.args[0]
    assert "already resolved" in msg.lower()


# ── the board's Sign-Offs button ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signoff_picker_lists_pending_claims_oldest_first(ctx, db):
    """The picker offers every waiting claim, longest-waiting at the top."""
    _enable(db)
    old_q = _mk_quest(db, reward=30, title="Waited ages")
    new_q = _mk_quest(db, reward=70, title="Just now")
    old_claim = _pending_claim(db, old_q, user_id=CLAIMANT)
    _pending_claim(db, new_q, user_id=601)
    with open_db(db) as conn:  # age the first claim
        conn.execute(
            "UPDATE econ_quest_claims SET created_at = created_at - 10000 WHERE id = ?",
            (old_claim,),
        )

    claimant = _member(member_id=CLAIMANT)
    claimant.display_name = "Alex"
    interaction = _picker_interaction(
        _bot(ctx), user=_member(admin=True, member_id=999), members=[claimant]
    )
    await open_signoff_picker(interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    options = view.children[0].options
    assert [o.value for o in options][0] == str(old_claim)
    assert options[0].label == "Alex — Waited ages"
    # The reward and the criteria are what a mod judges against, and both ride
    # in the description where they fit before the claim is opened.
    assert "30 Coins" in options[0].description
    assert "Do the thing" in options[0].description
    # A claimant who has left the guild is never rendered as a raw id.
    assert options[1].label.startswith("someone — ")


@pytest.mark.asyncio
async def test_signoff_picker_refuses_a_non_manager(ctx, db):
    """The economy's manager gate, not the board's mod gate, guards sign-offs."""
    _enable(db, manager_role_id=MANAGER_ROLE)
    qid = _mk_quest(db, reward=30)
    _pending_claim(db, qid)
    interaction = _picker_interaction(_bot(ctx), user=_member(role_ids=(1,)))

    await open_signoff_picker(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "permission" in msg.lower()
    assert "view" not in interaction.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_signoff_picker_says_so_when_nothing_is_waiting(ctx, db):
    _enable(db)
    interaction = _picker_interaction(_bot(ctx), user=_member(admin=True, member_id=999))

    await open_signoff_picker(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "no quest sign-offs" in msg.lower()


@pytest.mark.asyncio
async def test_picking_a_claim_shows_its_detail_with_approve_deny(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=30, title="Sign me")
    claim_id = _pending_claim(db, qid)

    select = SignoffPickSelect(
        [{
            "id": claim_id, "user_id": CLAIMANT, "quest_title": "Sign me",
            "reward": 30, "criteria": "Do the thing", "claimant_name": "Alex",
        }],
        _settings(db),
    )
    select._values = [str(claim_id)]  # type: ignore[attr-defined]
    interaction = _picker_interaction(_bot(ctx), user=_member(admin=True, member_id=999))
    await select.callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["content"] is None
    view = kwargs["view"]
    body = _view_text(view)
    assert body.startswith("## 📋 Quest Sign-Off Requested")
    # The criteria is the whole reason a human is being asked.
    assert "Do the thing" in body
    assert {getattr(c, "custom_id", None) for c in view.walk_children()} >= {
        f"econ_claim:approve:{claim_id}", f"econ_claim:deny:{claim_id}",
    }


@pytest.mark.asyncio
async def test_picking_an_already_resolved_claim_offers_no_buttons(ctx, db):
    """Someone else got there first — show the outcome, not a live decision."""
    _enable(db)
    qid = _mk_quest(db, reward=30, title="Sign me")
    claim_id = _pending_claim(db, qid)
    with open_db(db) as conn:
        resolve_claim(
            conn, load_econ_settings(conn, GUILD_ID), claim_id,
            approve=False, resolver_id=111, deny_reason="too blurry", booster=False,
        )

    select = SignoffPickSelect(
        [{
            "id": claim_id, "user_id": CLAIMANT, "quest_title": "Sign me",
            "reward": 30, "criteria": "Do the thing", "claimant_name": "Alex",
        }],
        _settings(db),
    )
    select._values = [str(claim_id)]  # type: ignore[attr-defined]
    interaction = _picker_interaction(_bot(ctx), user=_member(admin=True, member_id=999))
    await select.callback(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    view = kwargs["view"]
    assert not any(
        isinstance(c, discord.ui.Button) for c in view.walk_children()
    )
    assert "❌ Quest Denied" in _view_text(view)


@pytest.mark.asyncio
async def test_resolving_from_the_board_repaints_the_ephemeral_detail(ctx, db):
    """With no card message, the outcome is painted where the click happened.

    The board flow has no bank-channel card to edit, so an approval that only
    sent an ack would leave the mod looking at a "Sign-Off Requested" card for
    a claim they had just paid out.
    """
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    bot = _bot(ctx)
    detail = _ephemeral_message()
    interaction = _picker_interaction(
        bot, user=_member(admin=True, member_id=999), message=detail
    )

    await QuestApproveButton(claim_id).callback(interaction)

    kwargs = interaction.edit_original_response.await_args.kwargs
    assert kwargs["embed"] is None
    assert kwargs["content"] is None
    assert kwargs["attachments"] == []
    body = _view_text(kwargs["view"])
    assert "✅ Quest Approved" in body
    assert not any(
        isinstance(c, discord.ui.Button) for c in kwargs["view"].walk_children()
    )
    # An ephemeral message cannot be edited through the channel-message
    # endpoint, so the handler must never try — it would 404 every time.
    detail.edit.assert_not_awaited()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, CLAIMANT) == 30


@pytest.mark.asyncio
async def test_denying_from_the_board_repaints_the_ephemeral_detail(ctx, db):
    """Same for the deny half: the modal carries the message it was opened
    from, and that message is the ephemeral detail."""
    _enable(db)
    qid = _mk_quest(db, reward=30)
    claim_id = _pending_claim(db, qid)
    bot = _bot(ctx)
    detail = _ephemeral_message()
    interaction = _picker_interaction(
        bot, user=_member(admin=True, member_id=999), message=detail
    )

    modal = QuestDenyModal(claim_id, detail)
    modal.reason = MagicMock(value="too blurry")  # type: ignore[assignment]
    with patch(
        "bot_modules.economy.quest_views.announce_signoff_outcome",
        new=AsyncMock(return_value=True),
    ):
        await modal.on_submit(interaction)

    detail.edit.assert_not_awaited()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "❌ Quest Denied" in _view_text(kwargs["view"])
    assert kwargs["embed"] is None
