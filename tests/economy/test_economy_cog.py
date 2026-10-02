"""Cog-level tests for /bank — wallet view, mod grant matrix, and /bank quests."""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import (
    get_tz_offset_hours,
    open_db,
    set_config_value,
)
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quests import quest_period
from bot_modules.economy.quests_service import (
    claim_quest,
    create_quest,
    set_income_source,
    set_quest_active,
)
from bot_modules.economy.cog.common import (
    _NICK_FORBIDDEN,
    _custom_name_confirmation,
    _resolve_guild_emoji,
)
from bot_modules.core.layout import check_budget
from bot_modules.core.utils import ExpiringView
from bot_modules.economy.service import (
    EconSettings,
    apply_credit,
    get_balance,
    get_ledger,
    get_notify_muted,
    get_streak_shields,
    load_econ_settings,
    save_econ_settings,
)
from bot_modules.voice.master.service import add_name_blocklist
from bot_modules.economy.shop_items_service import get_item
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, FakeRole, fake_interaction

GUILD_ID = 9001
MANAGER_ROLE_ID = 7007


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    # is_mod/member_is_mod answer False by default: these tests are about the
    # ordinary member's paid path, and the staff comp has its own coverage in
    # test_economy_rentals_logic / test_economy_mod_comp.
    def _set(key, value, guild_id=GUILD_ID):
        # core.role_provision persists a provisioned role id through this.
        with open_db(db) as conn:
            set_config_value(conn, key, value, guild_id)
            conn.commit()
        return value

    return SimpleNamespace(
        db_path=db,
        open_db=lambda: open_db(db),
        is_mod=lambda _interaction: False,
        member_is_mod=lambda _member: False,
        set_config_value=_set,
    )


@pytest.fixture(autouse=True)
def _patch_accent():
    """resolve_accent_color reads the guild avatar — stub it to a fixed color."""
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


# The cog is split across economy_cog.py and the economy/cog/ mixins, and a
# service function is looked up in whichever of those modules the running
# method lives in. Patching one module's binding would miss the others, so a
# stub for a function several parts call goes into every module that binds it.
_ECONOMY_PARTS = (
    "bot_modules.economy.economy_cog",
    "bot_modules.economy.cog.base",
    "bot_modules.economy.cog.shop",
    "bot_modules.economy.cog.perks",
    "bot_modules.economy.cog.quests",
    "bot_modules.economy.cog.panels",
    "bot_modules.economy.cog.views",
)


@contextlib.contextmanager
def _patch_everywhere(name, new=None):
    """``patch(...)`` for ``name`` in every economy module that imports it."""
    import importlib

    mock = MagicMock() if new is None else new
    homes = [
        m for m in _ECONOMY_PARTS if hasattr(importlib.import_module(m), name)
    ]
    assert homes, f"no economy module binds {name!r}"
    with contextlib.ExitStack() as stack:
        for m in homes:
            stack.enter_context(patch(f"{m}.{name}", new=mock))
        yield mock


def _queued_notes(db) -> list[tuple[int, int, str]]:
    """(recipient, actor, line) for each economy note waiting on dm_batch —
    pay and gift notes are queued, not sent (economy.notice_batch)."""
    with open_db(db) as conn:
        rows = conn.execute(
            "SELECT user_id, actor_id, payload FROM dm_batch_pending"
            " WHERE kind = 'econ_notice' ORDER BY id"
        ).fetchall()
    return [(int(r[0]), int(r[1]), json.loads(r[2])["line"]) for r in rows]


def _make_cog(ctx):
    from bot_modules.economy.economy_cog import EconomyCog

    _bot = MagicMock()
    _bot.ctx = ctx
    return EconomyCog(_bot)


def _enable(db, **overrides) -> None:
    # Set bonuses zeroed — one-quest boards would pay the clear-the-board
    # bonus on every claim and skew exact-balance assertions.
    values: dict[str, object] = {
        "enabled": True,
        "quest_set_bonus_daily": 0,
        "quest_set_bonus_weekly": 0,
    }
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _member(
    *,
    admin: bool = False,
    role_ids: tuple[int, ...] = (),
    member_id: int = 500,
    is_bot: bool = False,
    premium: object | None = None,
    name: str = "Actor",
) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = is_bot
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.premium_since = premium
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _interaction(actor: MagicMock) -> MagicMock:
    inter = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    inter.user = actor
    return inter


def _view_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )


async def _wallet(cog, interaction) -> None:
    await cog.bank_wallet.callback(cog, interaction)


async def _grant(cog, interaction, member, amount, reason) -> None:
    await cog.bank_grant.callback(cog, interaction, member, amount, reason)


# ── /bank wallet ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wallet_shows_balance_branding_and_ledger(ctx, db):
    _enable(db, currency_emoji="💎", currency_plural="Gems", wallet_name="Vault")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 30, "grant", actor_id=1, meta={"reason": "x"})

    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)

    await _wallet(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "embed" not in kwargs  # a bare V2 card, not an embed
    text = _view_text(kwargs["view"])
    assert "## 💎 Vault" in text
    assert "30" in text and "Gems" in text
    assert "💎" in text
    # The feed renders the register's glyph + human label, not the raw kind.
    assert "🎁 Staff grant" in text and "+30" in text
    assert "· grant ·" not in text  # never the bare snake_case token


@pytest.mark.parametrize("command", ["wallet", "grant", "quests", "mute"])
@pytest.mark.asyncio
async def test_disabled_economy_gate_per_command(ctx, db, command):
    # The disabled check is inlined per command, so each case exercises its
    # own gate. The "nothing written" invariants live in the service suites.
    cog = _make_cog(ctx)  # economy left disabled
    actor = _member(admin=True)  # admin, so only the gate can block
    interaction = _interaction(actor)
    if command == "wallet":
        await _wallet(cog, interaction)
    elif command == "grant":
        await _grant(cog, interaction, _member(member_id=900), 10, "x")
    elif command == "quests":
        await _quests(cog, interaction)
    else:
        await _mute(cog, interaction)

    args = interaction.response.send_message.await_args.args
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "enabled" in args[0].lower()
    assert kwargs["ephemeral"] is True
    assert "embed" not in kwargs and "view" not in kwargs


# ── /bank grant — permission matrix ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "actor_kind", "allowed"),
    [
        ("grant", "admin", True),
        ("grant", "manager", True),
        ("grant", "plain", False),
    ],
)
@pytest.mark.asyncio
async def test_mod_command_permission_matrix(ctx, db, command, actor_kind, allowed):
    _enable(db, manager_role_id=MANAGER_ROLE_ID)
    cog = _make_cog(ctx)
    actor = _member(
        admin=actor_kind == "admin",
        role_ids=(MANAGER_ROLE_ID,) if actor_kind == "manager" else (),
    )

    interaction = _interaction(actor)
    await _grant(cog, interaction, _member(member_id=900), 10, "for helping")
    with open_db(db) as conn:
        acted = get_balance(conn, GUILD_ID, 900) == 10

    assert acted is allowed
    call = interaction.response.send_message.await_args
    if allowed and command == "grant":
        assert "view" in call.kwargs
        assert call.kwargs.get("ephemeral") is not True  # public confirmation
    if not allowed:
        assert "permission" in call.args[0].lower()
        assert call.kwargs["ephemeral"] is True


# ── /bank grant — amounts and booster multiplier ─────────────────────────────


@pytest.mark.asyncio
async def test_grant_booster_target_gets_multiplier(ctx, db):
    _enable(db)  # default booster_multiplier == 1.5
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900, premium=object())  # boosting
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 5, "boost love")

    # The ceil-rounding math itself is test_credit_booster_ceil_rounding
    # (service) — this covers the card calling out the boost.
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert "Booster" in _view_text(view)


@pytest.mark.asyncio
async def test_grant_reason_field_is_escaped_and_capped(ctx, db):
    # The reason param carries no Range/length limit, and the sibling field
    # two lines above it (member.display_name in the description) is already
    # escaped — the Reason field must match, and needs its own cap since
    # nothing upstream bounds it.
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900)
    interaction = _interaction(actor)

    await _grant(cog, interaction, target, 5, "**FREE COINS** ||click|| [x](http://evil.example)")

    view = interaction.response.send_message.await_args.kwargs["view"]
    text = _view_text(view)
    assert "\\*\\*FREE COINS\\*\\*" in text
    assert "\\|\\|click\\|\\|" in text


@pytest.mark.asyncio
async def test_grant_reason_field_fits_the_cap_after_escaping(ctx, db):
    from bot_modules.core.branding import FIELD_VALUE_LIMIT

    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(admin=True)
    target = _member(member_id=900)
    interaction = _interaction(actor)

    # All-markdown reason: escaping doubles it, so a naive raw-length cap
    # upstream would still overflow FIELD_VALUE_LIMIT once escaped.
    await _grant(cog, interaction, target, 5, "*" * 1500)

    view = interaction.response.send_message.await_args.kwargs["view"]
    text = _view_text(view)
    reason_line = next(line for line in text.splitlines() if line.startswith("> "))
    assert len(reason_line.removeprefix("> ")) <= FIELD_VALUE_LIMIT
    assert "\\" in reason_line  # escaped, not raw-truncated


# ── /bank quests — listing state matrix ──────────────────────────────────────


def _mk_quest(
    db,
    *,
    qtype="daily",
    reward=15,
    signoff=0,
    community_target=None,
    active=True,
    title="Quest",
    trigger_words="",
    trigger_channel_id=None,
    trigger_kind="",
) -> int:
    with open_db(db) as conn:
        qid = create_quest(
            conn,
            GUILD_ID,
            title=title,
            description="",
            qtype=qtype,
            reward=reward,
            signoff=signoff,
            criteria="Do the thing",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=community_target,
            created_by=1,
            trigger_words=trigger_words,
            trigger_channel_id=trigger_channel_id,
            trigger_kind=trigger_kind,
        )
        if active:
            set_quest_active(conn, GUILD_ID, qid, True)
    return qid


def _period(db, qtype) -> str:
    with open_db(db) as conn:
        offset = get_tz_offset_hours(conn, GUILD_ID)
    return quest_period(qtype, local_day_for(time.time(), offset))


async def _quests(cog, interaction) -> None:
    await cog.bank_quests.callback(cog, interaction)


@pytest.mark.asyncio
async def test_quests_empty_when_none_active(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await _quests(cog, interaction)
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "no active quests" in _all_text(kwargs["view"]).lower()
    # Nothing claimable → the bare board card, no claim/detail selects.
    assert not any(
        isinstance(c, discord.ui.Select) for c in kwargs["view"].walk_children()
    )


@pytest.mark.asyncio
async def test_quests_listing_state_matrix(ctx, db):
    _enable(db)
    user_id = 500
    daily = _mk_quest(db, qtype="daily", title="Say hi")  # claimable
    weekly_done = _mk_quest(db, qtype="weekly", reward=40, title="Weekly grind")
    weekly_pending = _mk_quest(
        db, qtype="weekly", reward=50, signoff=1, title="Sign me off"
    )
    _mk_quest(
        db, qtype="community", reward=10, community_target=100, title="Team goal"
    )
    _mk_quest(
        db, qtype="monthly", reward=60, community_target=500,
        trigger_kind="message_sent", title="Monthly Marathon",
    )

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        # weekly_done → a paid claim this period; weekly_pending → a pending one.
        claim_quest(
            conn, settings, GUILD_ID, weekly_done, user_id,
            period=_period(db, "weekly"), booster=False,
        )
        claim_quest(
            conn, settings, GUILD_ID, weekly_pending, user_id,
            period=_period(db, "weekly"), booster=False,
        )
        conn.execute(
            "INSERT INTO econ_community_progress (quest_id, current) "
            "SELECT id, 40 FROM econ_quests WHERE title = 'Team goal'"
        )
        conn.execute(
            "INSERT INTO econ_community_progress (quest_id, current) "
            "SELECT id, 200 FROM econ_quests WHERE title = 'Monthly Marathon'"
        )

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=user_id))
    await _quests(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    text = _all_text(kwargs["view"])
    # Two top-level sections now — the member's own board and the guild-wide
    # goals — not one field per cadence.
    assert "### 🧍 Your quests" in text
    assert "### 🌐 Community goals" in text
    personal = text.split("### 🧍 Your quests", 1)[1].split(
        "### 🌐 Community goals", 1
    )[0]
    community = text.split("### 🌐 Community goals", 1)[1]
    # Personal section: cadence sub-labels + one line per quest.
    assert "**Daily**" in personal and "**Weekly**" in personal
    assert "`Say hi" in personal and "🔶 claim below" in personal
    assert "`Weekly grind" in personal and "✅ done" in personal
    assert "`Sign me off" in personal and "⏳ sign-off" in personal
    # Community section: the monthly goal folds in beside the weekly community
    # goal, each under its cadence sub-label, with the ▰▱ bar — fill only, no
    # n/target counts (the shared five-figure totals live in the details popup).
    assert "**Monthly**" in community and "`Monthly Marathon" in community
    assert "**Weekly**" in community and "`Team goal" in community
    assert "▰" in community and "▱" in community
    assert "40/100" not in community and "200/500" not in community
    # The descriptions/explainers moved behind the details select — the
    # list never carries them.
    assert "Do the thing" not in text
    # View always attaches when quests exist (details select at minimum).
    assert "view" in kwargs
    assert daily  # referenced


@pytest.mark.asyncio
async def test_quests_event_quests_have_no_display_section(ctx, db):
    # event ("Anytime") quests aren't board-drawn and don't get a section in
    # the personal panel — they stay a surprise payout rather than a
    # proactively-listed menu, even when dozens are active at once. With no
    # daily/weekly/community quests to fill the other sections, the embed
    # ends up with no fields at all — the 40 events are still reachable via
    # the details/claim select, just never proactively listed.
    from bot_modules.economy.quests import TRIGGER_KINDS

    _enable(db)
    for i, kind in enumerate(list(TRIGGER_KINDS)[:40]):
        _mk_quest(
            db, qtype="event", trigger_kind=kind, reward=10,
            title=f"Anytime quest number {i:02d}",
        )
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _quests(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    text = _all_text(kwargs["view"])
    assert "🧍 Your quests" not in text
    assert "Anytime quest number" not in text
    assert "view" in kwargs  # still claimable/detailable via the select


@pytest.mark.asyncio
async def test_cog_load_registers_persistent_buttons(ctx, db):
    from bot_modules.economy.cog.views import ShopRentButton
    from bot_modules.economy.bounty_views import (
        BountyAwardButton,
        BountyCancelButton,
        BountyChipInButton,
        BountyHubChipButton,
        BountyHubPostButton,
    )
    from bot_modules.economy.pin_views import PinApproveButton, PinDenyButton
    from bot_modules.economy.theme_views import ThemeApproveButton, ThemeDenyButton
    from bot_modules.economy.quest_views import QuestApproveButton, QuestDenyButton
    from bot_modules.economy.auction_views import AuctionBidButton

    bot = MagicMock()
    cog = _make_cog(ctx)
    cog.bot = bot
    try:
        await cog.cog_load()
        # Every persistent button must be re-registered here or its custom_id
        # stops routing after a restart, leaving dead buttons on old messages.
        bot.add_dynamic_items.assert_called_once_with(
            QuestApproveButton,
            QuestDenyButton,
            ShopRentButton,
            PinApproveButton,
            PinDenyButton,
            ThemeApproveButton,
            ThemeDenyButton,
            BountyChipInButton,
            BountyAwardButton,
            BountyCancelButton,
            # The hub panel's two buttons — it is the board's only entry
            # point, so a dead hub means no way to post or chip in at all.
            BountyHubPostButton,
            BountyHubChipButton,
            AuctionBidButton,
        )
    finally:
        cog._auction_settle_loop.cancel()  # don't leak the background loop
    # The shop panel's buttons are a static-custom_id view — it must be
    # re-registered too or the posted panel goes dead after a restart.
    from bot_modules.economy.cog.views import (
        EconomyPanelView,
        ShopPanelView,
        _LegacyShopOpenView,
    )

    added_views = [c.args[0] for c in bot.add_view.call_args_list]
    assert any(isinstance(v, ShopPanelView) for v in added_views)
    # …and so must the single Open Shop / Browse an older panel carries, or
    # it goes dead until its sticky repaints it.
    assert any(isinstance(v, _LegacyShopOpenView) for v in added_views)
    # The Components V2 economy panel — registered alongside (not instead of)
    # any pre-conversion message, so the four buttons it shares a custom_id
    # with keep routing after a restart regardless of which view rendered them.
    assert any(isinstance(v, EconomyPanelView) for v in added_views)


# ── /bank mute + notify_member honoring the pref ─────────────────────────────


async def _mute(cog, interaction) -> None:
    await cog.bank_mute.callback(cog, interaction)


@pytest.mark.asyncio
async def test_bank_mute_toggles_pref(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)

    await _mute(cog, interaction)
    with open_db(db) as conn:
        assert get_notify_muted(conn, GUILD_ID, 500) is True
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True

    # Toggling again turns notifications back on.
    interaction2 = _interaction(actor)
    await _mute(cog, interaction2)
    with open_db(db) as conn:
        assert get_notify_muted(conn, GUILD_ID, 500) is False


# ── Stage 3: transfers / shop / role studio / gift / rentals ─────────────────


def _credit(db, user_id, amount) -> None:
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, user_id, amount, "grant", actor_id=1)


def _settings(db):
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID)


def _add_rental(
    db, perk, *, user_id=500, beneficiary_id=None, state="active",
    catalog_icon_id=None,
) -> None:
    beneficiary_id = user_id if beneficiary_id is None else beneficiary_id
    now = time.time()
    with open_db(db) as conn:
        conn.execute(
            """
            INSERT INTO econ_rentals
                (guild_id, user_id, perk, state, price, started_at, next_bill_at,
                 cancel_at_period_end, suspended, beneficiary_id, catalog_icon_id,
                 created_at)
            VALUES (?, ?, ?, ?, 50, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                GUILD_ID, user_id, perk, state, now, now + 604800, beneficiary_id,
                catalog_icon_id, now,
            ),
        )


def _personal_role(db, user_id=500):
    """The member's personal-role row — what every role-studio setter writes."""
    with open_db(db) as conn:
        return conn.execute(
            "SELECT * FROM econ_personal_roles WHERE user_id = ?", (user_id,)
        ).fetchone()


def _live_rentals(db) -> list:
    with open_db(db) as conn:
        return conn.execute(
            "SELECT * FROM econ_rentals WHERE state IN ('active', 'grace') "
            "ORDER BY id"
        ).fetchall()


def _guild_roles(roles=(), emojis=()) -> MagicMock:
    g = MagicMock()
    g.id = GUILD_ID
    g.roles = list(roles)
    g.emojis = list(emojis)
    return g


def _role_interaction(actor, roles=(), emojis=()) -> MagicMock:
    inter = _interaction(actor)
    inter.guild = _guild_roles(roles, emojis)
    return inter


@contextlib.contextmanager
def _patch_projection():
    """Isolate command logic from the real Discord projector / DM path."""
    with (
        _patch_everywhere(
            "apply_role_perks",
            new=AsyncMock(return_value=True),
        ) as apply_mock,
        _patch_everywhere(
            "revoke_role_perks", new=AsyncMock()
        ) as revoke_mock,
        patch(
            "bot_modules.economy.economy_cog.notify_member",
            new=AsyncMock(return_value=True),
        ) as notify_mock,
    ):
        yield apply_mock, revoke_mock, notify_mock


# ── /bank pay ────────────────────────────────────────────────────────────────


async def _pay(cog, interaction, member, amount) -> None:
    await cog.bank_pay.callback(cog, interaction, member, amount)


def _pay_confirm_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    """The confirm card's Confirm/Cancel button, wherever the ActionRow's
    children live in the tree — a top-level ``view.children`` index would hit
    the heading Text Display/Section instead of the buttons."""
    return next(
        c for c in view.walk_children()
        if isinstance(c, discord.ui.Button) and c.label == label
    )


# The confirm gate triggers *over* 100 (spec §5) — 100 itself sends straight
# through. One test for the threshold, one row per side of it.
@pytest.mark.parametrize(
    ("amount", "needs_confirm"),
    [
        pytest.param(50, False, id="under"),
        pytest.param(100, False, id="exactly-the-threshold"),
        pytest.param(200, True, id="over"),
    ],
)
@pytest.mark.asyncio
async def test_pay_confirm_gate_follows_the_threshold(ctx, db, amount, needs_confirm):
    from bot_modules.economy.cog.views import _PayConfirmView

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))

    with _patch_projection() as (_apply, _revoke, notify):
        await _pay(cog, interaction, _member(member_id=900, name="Bob"), amount)

    kwargs = interaction.response.send_message.await_args.kwargs
    with open_db(db) as conn:
        received = get_balance(conn, GUILD_ID, 900)

    notes = _queued_notes(db)
    if needs_confirm:
        assert isinstance(kwargs["view"], _PayConfirmView)
        assert received == 0  # the gate holds; nothing moved yet
        assert notes == []
    else:
        # Straight through: the private receipt card, not the confirm gate.
        assert not isinstance(kwargs["view"], _PayConfirmView)
        assert received == amount
        assert [(to, by) for to, by, _ in notes] == [(900, 500)]
        assert str(amount) in notes[0][2]


@pytest.mark.asyncio
async def test_pay_confirm_button_executes_transfer(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500)
    recipient = _member(member_id=900)
    interaction = _interaction(sender)

    with _patch_projection():
        await _pay(cog, interaction, recipient, 200)
        view = interaction.response.send_message.await_args.kwargs["view"]
        confirm_inter = _interaction(sender)
        await _pay_confirm_button(view, "Confirm").callback(confirm_inter)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 300
        assert get_balance(conn, GUILD_ID, 900) == 200
    confirm_inter.response.edit_message.assert_awaited_once()


# ── /bank pay public: ────────────────────────────────────────────────────────
#
# Rendering is tested in test_economy_transfers.py. What's tested here is the
# wiring the builder can't see: that the public receipt is actually *sent*, on
# both sides of the confirm threshold, and withheld on a no-contact pair.


def _postable(interaction) -> MagicMock:
    """Give a fake interaction a channel the bot may speak in."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 4242
    channel.permissions_for.return_value = MagicMock(
        send_messages=True, embed_links=True
    )
    interaction.channel = channel
    interaction.guild.me = MagicMock()
    return channel


async def _pay_public(cog, interaction, member, amount, memo=None) -> None:
    await cog.bank_pay.callback(cog, interaction, member, amount, memo, True)


@pytest.mark.asyncio
async def test_public_pay_under_the_threshold_posts_and_withholds_balance(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    # The sender's own reply stays ephemeral and carries the balance...
    ack = interaction.response.send_message.await_args
    assert ack.kwargs["ephemeral"] is True
    assert "460" in ack.args[0]

    # ...and the balance is exactly what the channel does not get.
    posted = interaction.followup.send.await_args.kwargs["view"]
    text = _view_text(posted)
    assert "460" not in text
    assert "Alice → Bob" in text and "<@" not in text


@pytest.mark.asyncio
async def test_public_pay_over_the_threshold_posts_after_confirming(ctx, db):
    """The naive implementation does nothing here: an ephemeral confirm
    message cannot be edited into a public one, so the receipt has to be a
    separate send."""
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500, name="Alice")
    interaction = _interaction(sender)
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 200)
        view = interaction.response.send_message.await_args.kwargs["view"]
        assert "posted in this channel" in _view_text(view)

        confirm_inter = _interaction(sender)
        _postable(confirm_inter)
        await _pay_confirm_button(view, "Confirm").callback(confirm_inter)

    # The ephemeral confirm resolves privately, with the balance...
    confirm_inter.response.edit_message.assert_awaited_once()
    assert "300" in confirm_inter.response.edit_message.await_args.kwargs["content"]
    # ...and the receipt reaches the channel on its own.
    posted = confirm_inter.followup.send.await_args.kwargs["view"]
    text = _view_text(posted)
    assert "Alice → Bob" in text and "<@" not in text


@pytest.mark.asyncio
async def test_public_pay_between_a_no_contact_pair_posts_nothing(ctx, db):
    """The payment still goes through, and the sender gets the ordinary
    private receipt — nothing distinguishes it from an unticked box."""
    from bot_modules.no_contact.service import add_pair

    _enable(db)
    _credit(db, 500, 500)
    add_pair(db, GUILD_ID, 500, 900, created_by=900)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    interaction.followup.send.assert_not_awaited()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "Your balance: 460" in _view_text(kwargs["view"])
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 40


@pytest.mark.asyncio
async def test_private_pay_between_a_no_contact_pair_notifies_nobody(ctx, db):
    """The gate CLAUDE.md makes a hard rule: the recipient's DM is contact.

    The public receipt has been gated since 2026-08-27, but an ordinary
    *private* payment still DM'd the recipient the sender's name and memo (and
    fell back to a post in the bank channel), so the block was reachable
    straight through. The money still moves and the sender's receipt is the
    ordinary one, balance and all — there is nothing here he can tell apart
    from paying anyone else.
    """
    from bot_modules.no_contact.service import add_pair

    _enable(db)
    _credit(db, 500, 500)
    add_pair(db, GUILD_ID, 500, 900, created_by=900)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))

    with _patch_projection() as (_apply, _revoke, notify):
        await _pay(cog, interaction, _member(member_id=900, name="Bob"), 40, "hi")

    notify.assert_not_awaited()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "Your balance: 460" in _view_text(kwargs["view"])
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 900) == 40


@pytest.mark.asyncio
async def test_gift_between_a_no_contact_pair_notifies_nobody(ctx, db):
    """Same rule, same shape: the perk lands, the beneficiary isn't told."""
    from bot_modules.no_contact.service import add_pair

    _enable(db)
    _credit(db, 500, 50)
    add_pair(db, GUILD_ID, 500, 900, created_by=900)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))

    with _patch_projection() as (_apply, _revoke, notify):
        await _gift(cog, interaction, _member(member_id=900, name="Bob"))

    notify.assert_not_awaited()
    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900


@pytest.mark.asyncio
async def test_public_pay_falls_back_quietly_where_the_bot_cannot_post(ctx, db):
    """Same silent downgrade as the no-contact case — that's what makes the
    no-contact case unremarkable."""
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    channel = _postable(interaction)
    channel.permissions_for.return_value = MagicMock(
        send_messages=False, embed_links=True
    )

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    interaction.followup.send.assert_not_awaited()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "Your balance: 460" in _view_text(kwargs["view"])


@pytest.mark.asyncio
async def test_public_option_never_publishes_a_refusal(ctx, db):
    """Announcing that someone is broke is a different feature."""
    _enable(db)
    _credit(db, 500, 10)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))
    _postable(interaction)

    with _patch_projection():
        await _pay_public(cog, interaction, _member(member_id=900, name="Bob"), 40)

    interaction.followup.send.assert_not_awaited()
    args, kwargs = interaction.response.send_message.await_args
    assert kwargs["ephemeral"] is True
    assert "don't have enough" in args[0]


@pytest.mark.asyncio
async def test_pay_cancel_button_aborts(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    sender = _member(member_id=500)
    recipient = _member(member_id=900)
    interaction = _interaction(sender)

    with _patch_projection():
        await _pay(cog, interaction, recipient, 200)
        view = interaction.response.send_message.await_args.kwargs["view"]
        cancel_inter = _interaction(sender)
        cancel_inter.delete_original_response = AsyncMock()
        await _pay_confirm_button(view, "Cancel").callback(cancel_inter)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500
    # Cancel deletes the confirm card silently — no "cancelled" notice.
    cancel_inter.delete_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_pay_transfers_disabled(ctx, db):
    _enable(db, transfers_enabled=False)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection():
        await _pay(cog, interaction, _member(member_id=900), 50)

    args = interaction.response.send_message.await_args.args
    assert "off" in args[0].lower()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500


# ── /bank shop ───────────────────────────────────────────────────────────────


async def _open_shop(cog, interaction) -> None:
    """Open the shop with every feature gate open — the default for these tests.

    Gate-closed behaviour has its own tests that patch the gate explicitly.
    """
    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction)


async def _shop(cog, interaction, section: str = "cosmetics") -> None:
    """Open one section of the shop, as its panel Browse does. ``/bank shop``
    itself serves the section chooser (its own tests below)."""
    await cog.open_shop_section(interaction, section)


def _sections_of(view) -> list[discord.ui.Section]:
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Section)]


def _all_text(view) -> str:
    """Every TextDisplay on a card, however deeply nested, joined."""
    return "\n".join(
        c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )


def _row_for(view, needle: str) -> discord.ui.Section:
    """The Section whose text names ``needle`` — a row is found by its
    title/blurb text since a converted row carries no per-perk custom_id."""
    for section in _sections_of(view):
        text = "\n".join(
            c.content for c in section.children if isinstance(c, discord.ui.TextDisplay)
        )
        if needle in text:
            return section
    raise AssertionError(f"no row on this card mentions {needle!r}")


def _ShopView(*args, **kwargs):
    from bot_modules.economy.cog.views import _ShopView as view_cls

    return view_cls(*args, **kwargs)


def _has_row(view, needle: str) -> bool:
    try:
        _row_for(view, needle)
    except AssertionError:
        return False
    return True


def _button(section: discord.ui.Section) -> discord.ui.Button:
    """A row's accessory, narrowed from ``Item`` — every shop row's
    accessory is a Button, never a Thumbnail."""
    assert isinstance(section.accessory, discord.ui.Button)
    return section.accessory


@pytest.mark.asyncio
async def test_bank_shop_serves_the_section_chooser(ctx, db):
    """``/bank shop`` shows the panel's rows privately: a Browse per stocked
    section (no Specials here — nothing is stocked) and Your Rentals."""
    from bot_modules.economy.cog.views import _ShopChooserView

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_shop.callback(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"]
    view = kwargs["view"]
    assert isinstance(view, _ShopChooserView)
    text = _all_text(view)
    assert "🎨 Role Cosmetics" in text and "🧾 Your Rentals" in text
    assert "🎁 Specials" not in text


@pytest.mark.asyncio
async def test_bank_shop_refuses_when_the_economy_is_off(ctx, db):
    cog = _make_cog(ctx)  # economy never enabled
    interaction = _interaction(_member(member_id=500))

    await cog.bank_shop.callback(cog, interaction)

    assert "isn't enabled" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_chooser_row_opens_its_section(ctx, db):
    """The chooser's buttons are bound on the card itself."""
    from bot_modules.economy.cog.views import _ShopView
    from bot_modules.economy.shop_layouts import SHOP_SECTION_CUSTOM_IDS

    # Something has to stock Server Features: a fresh guild sells nothing there
    # since the sponsored question (price 40 by default) was retired.
    _enable(db, shop_voice_style_enabled=True)
    cog = _make_cog(ctx)
    opened = _panel_button_interaction(ctx, cog)
    await cog.bank_shop.callback(cog, opened)
    chooser = opened.response.send_message.await_args.kwargs["view"]
    button = next(
        b for b in chooser.walk_children()
        if getattr(b, "custom_id", None) == SHOP_SECTION_CUSTOM_IDS["server"]
    )

    press = _panel_button_interaction(ctx, cog)
    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await button.callback(press)

    view = press.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, _ShopView)
    assert "## 🏠 Server Features" in _all_text(view)


@pytest.mark.asyncio
async def test_shop_feature_gate_reaches_the_row(ctx, db):
    """The row-shape for every gate/owned/catalog combination is covered
    directly against ``shop_section_rows`` in test_shop_layouts.py — this is
    the one wiring assertion proving the cog actually threads a real
    ``feature_gate_ok`` result through to the rendered row, not a stub."""
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    async def _gate(bot, guild_id, perk):
        return perk != "role_gradient"

    with _patch_everywhere(
        "feature_gate_ok", new=AsyncMock(side_effect=_gate)
    ):
        await _shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    gated_row = _row_for(view, "Gradient Role")
    assert _button(gated_row).disabled is True
    open_row = _row_for(view, "Custom Role Color")
    assert _button(open_row).disabled is False


@pytest.mark.parametrize(
    "perk",
    ["role_color", "role_name", "role_gradient", "role_holographic", "role_icon"],
)
@pytest.mark.asyncio
async def test_shop_rent_success_each_perk(ctx, db, perk):
    _enable(db)
    _credit(db, 500, 500)
    settings = _settings(db)
    price = int(getattr(settings, f"price_{perk}"))
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection() as (apply_mock, _r, _n):
        await cog.do_rent(interaction, settings, _guild_roles(), perk)

    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["perk"] == perk
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500 - price  # upfront week


@pytest.mark.asyncio
async def test_shop_gift_recipient_gets_the_configurable_row(ctx, db):
    """A gifted perk's entitlement is beneficiary-based, so the recipient's
    shop shows the same "cfg" row a self-rental would — proving the cog's
    ``owned`` set (not just ``shop_section_rows``' own branch, covered in
    test_shop_layouts.py) is built from the beneficiary, not the renter."""
    from bot_modules.economy.perks import CUSTOMISE_LABELS

    _enable(db)
    _add_rental(db, "role_color", user_id=800, beneficiary_id=500)  # gift to 500
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _open_shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    row = _row_for(view, "Custom Role Color")
    assert _button(row).label == CUSTOMISE_LABELS["role_color"]


@pytest.mark.asyncio
async def test_shop_customise_button_opens_modal(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _open_shop(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    button = _row_for(view, "Custom Role Color").accessory
    press = _interaction(_member(member_id=500))
    await button.callback(press)

    from bot_modules.economy.cog.views import _RoleColorModal

    modal = press.response.send_modal.await_args.args[0]
    assert isinstance(modal, _RoleColorModal)


@pytest.mark.asyncio
async def test_rent_confirmation_offers_customise_button(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    interaction.original_response = AsyncMock(return_value=SimpleNamespace(id=555))

    with _patch_projection():
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_name")

    view = interaction.response.send_message.await_args.kwargs["view"]
    accessories = [s.accessory for s in _sections_of(view)]
    assert len(accessories) == 1
    from bot_modules.economy.shop_layouts import POST_RENT_CUSTOMISE_ID

    assert accessories[0].id == POST_RENT_CUSTOMISE_ID
    # Regression: the receipt is an ExpiringLayoutView, so its 300s timeout
    # must have a message to edit — without this its Customise button would
    # keep rendering as live forever (ExpiringLayoutView.on_timeout no-ops
    # with no self.message).
    assert view.message is interaction.original_response.return_value


@pytest.mark.asyncio
async def test_name_modal_submit_sets_role_name(ctx, db):
    """The modal path lands on the same setter/validators as everything else."""
    _enable(db)
    _add_rental(db, "role_name")
    cog = _make_cog(ctx)

    from bot_modules.economy.cog.views import _RoleNameModal

    modal = _RoleNameModal(cog)
    modal.text._value = "Stardust"
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await modal.on_submit(interaction)
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    assert row["name"] == "Stardust"


# ── persistent shop panel ────────────────────────────────────────────────────


def _panel_channel(channel_id: int = 777) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    ch.send = AsyncMock(return_value=MagicMock(id=8888))
    ch.fetch_message = AsyncMock()
    return ch


def _shop_panel_stored(db) -> tuple[int, int]:
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    return s.shop_channel_id, s.shop_message_id


@pytest.mark.asyncio
async def test_post_shop_posts_panel_and_saves_ids(ctx, db):
    """Entry point is post_shop_panel(guild, channel) now — /bank post-shop was
    replaced by Economy → Settings → Post to Discord on 2026-07-28."""
    from bot_modules.economy.cog.views import ShopPanelView

    _enable(db)
    cog = _make_cog(ctx)
    channel = _panel_channel()

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.post_shop_panel(FakeGuild(id=GUILD_ID), channel)

    kwargs = channel.send.await_args.kwargs
    view = kwargs["view"]
    assert isinstance(view, ShopPanelView)
    assert "## 🛍️ Perk Shop" in _all_text(view)
    assert view.timeout is None  # persistent, never expires
    # Each stocked shelf's blurb, its Browse beside it — nothing in the
    # store here, so no Specials — then Your Rentals' Manage.
    text = _all_text(view)
    assert "### 🎨 Role Cosmetics" in text and "🎁 Specials" not in text
    ids = [
        str(b.custom_id) for b in view.walk_children() if isinstance(b, discord.ui.Button)
    ]
    assert ids[0] == "econ_shop_help" and ids[-1] == "econ_shop_open:yours"
    assert "econ_shop_open:cosmetics" in ids
    assert "econ_shop_open:specials" not in ids
    assert "econ_shop_open" not in ids
    assert _shop_panel_stored(db) == (777, 8888)


@pytest.mark.asyncio
async def test_post_shop_refreshes_in_place_with_view(ctx, db):
    _enable(db, shop_channel_id=777, shop_message_id=4444)
    cog = _make_cog(ctx)
    channel = _panel_channel()
    old = MagicMock(edit=AsyncMock(), delete=AsyncMock(), id=4444)
    channel.get_partial_message = MagicMock(return_value=old)

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.post_shop_panel(FakeGuild(id=GUILD_ID), channel)

    assert "view" in old.edit.await_args.kwargs  # re-priced labels ride along
    channel.send.assert_not_awaited()
    assert _shop_panel_stored(db) == (777, 4444)


# The "plain member is refused" case moved with the command. post_shop_panel is
# unguarded by design — its only caller is POST /api/panels/{key}/post, which is
# admin-gated by the route (and covered by tests/web/test_panels_routes.py plus
# the authz sweep). Re-checking permissions in the cog would duplicate a gate
# that now has exactly one door.


# ── shop panel sticky (keep it at the channel bottom) ────────────────────────


def _shop_listener_msg(*, author_bot: bool, channel_id: int, message_id: int):
    m = MagicMock(spec=discord.Message)
    m.guild = FakeGuild(id=GUILD_ID)
    m.author = MagicMock(bot=author_bot)
    m.channel = SimpleNamespace(id=channel_id)
    m.id = message_id
    return m


# The shop panel's sticky machinery is core.sticky.StickyPanel, covered
# generically by tests/core/test_core_sticky.py (debounce, bot-message skip,
# post-before-delete, never-create-on-restick). What remains economy-specific
# is which settings fields it reads and the disabled-economy gate.


@pytest.mark.asyncio
async def test_shop_panel_ids_read_the_shop_fields(ctx, db):
    _enable(db, shop_channel_id=111, shop_message_id=4444)
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "shop") == (111, 4444)


@pytest.mark.asyncio
async def test_shop_panel_ids_are_zero_when_the_economy_is_disabled(ctx, db):
    """A disabled economy reads as "no panel", so the shop never re-sticks."""
    _enable(db, shop_channel_id=111, shop_message_id=4444)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": "0"})
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "shop") == (0, 0)


def _panel_button_interaction(ctx, cog=None, *, member_id: int = 500) -> MagicMock:
    """The panel button reaches the cog via interaction.client.get_cog."""
    interaction = _interaction(_member(member_id=member_id))
    interaction.client = SimpleNamespace(ctx=ctx, get_cog=lambda name: cog)
    return interaction


def _panel_button(view, section: str):
    """A shelf's Browse (from a chooser-era panel, or the chooser) or the
    panel's Your Rentals — whichever ``view`` carries."""
    from bot_modules.economy.shop_layouts import SHOP_SECTION_CUSTOM_IDS

    return next(
        b for b in view.walk_children()
        if getattr(b, "custom_id", None) == SHOP_SECTION_CUSTOM_IDS[section]
    )


@pytest.mark.asyncio
async def test_a_panel_browse_serves_only_its_section(ctx, db):
    from bot_modules.economy.cog.views import ShopPanelView, _ShopView

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    button = _panel_button(ShopPanelView(), "cosmetics")

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await button.callback(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"]
    served = kwargs["view"]
    assert isinstance(served, _ShopView)
    assert "## 🎨 Role Cosmetics" in _all_text(served)
    # One shelf, no arrows across into the next one.
    assert _nav_buttons(served) == []


@pytest.mark.asyncio
async def test_a_closed_section_refuses_by_name(ctx, db):
    """A panel posted while Specials was stocked outlives the last item.
    Serving another shelf would read as the button being wrong."""
    from bot_modules.economy.cog.views import ShopPanelView

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    await _panel_button(ShopPanelView(), "specials").callback(interaction)

    assert "closed right now" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_manage_serves_your_rentals_with_the_refund_row(ctx, db):
    """Your Rentals gathers what the member rents, whatever its shelf, and
    carries Cancel & Refund — the one place it lives since the split."""
    from bot_modules.economy.cog.views import ShopPanelView, _ShopView

    _enable(db, shop_voice_style_enabled=True)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _panel_button(ShopPanelView(), "yours").callback(interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, _ShopView)
    text = _all_text(view)
    assert "## 🧾 Your Rentals" in text
    assert "Custom Role Color" in text
    assert "Voice Style" not in text  # on sale, but not rented
    assert _has_row(view, "Cancel & Refund")


@pytest.mark.asyncio
async def test_an_older_panels_single_browse_opens_the_chooser(ctx, db):
    """The pre-split Open Shop and the stock list's one Browse shared an id;
    a panel still carrying it answers with the section chooser."""
    from bot_modules.economy.cog.views import _LegacyShopOpenView, _ShopChooserView
    from bot_modules.economy.shop_layouts import SHOP_PANEL_OPEN_CUSTOM_ID

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)
    button = next(
        b for b in _LegacyShopOpenView().walk_children()
        if getattr(b, "custom_id", None) == SHOP_PANEL_OPEN_CUSTOM_ID
    )

    await button.callback(interaction)

    assert isinstance(_shop_view(interaction), _ShopChooserView)


@pytest.mark.asyncio
async def test_legacy_panel_rent_button_opens_the_personal_menu(ctx, db):
    # Pre-Open-Shop panels carry per-perk rent buttons; a click on any of
    # them now serves the section chooser instead of renting directly.
    from bot_modules.economy.cog.views import ShopRentButton, _ShopChooserView

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await ShopRentButton("role_color").callback(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"]
    assert isinstance(kwargs["view"], _ShopChooserView)
    assert _live_rentals(db) == []  # opening the menu never rents


@pytest.mark.asyncio
async def test_panel_button_respects_disabled_economy(ctx, db):
    from bot_modules.economy.cog.views import ShopRentButton

    cog = _make_cog(ctx)  # economy never enabled
    interaction = _panel_button_interaction(ctx, cog)

    await ShopRentButton("role_color").callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't enabled" in msg
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_panel_served_menu_rechecks_feature_gates(ctx, db):
    # The panel can outlive a feature being switched off — the menu it
    # serves re-reads the gate at click time and disables the gated row.
    from bot_modules.economy.cog.views import ShopPanelView

    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _panel_button_interaction(ctx, cog)

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=False),
    ):
        await _panel_button(ShopPanelView(), "cosmetics").callback(interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    row = _row_for(view, "Gradient Role")
    assert _button(row).disabled is True
    assert "❌ not available" in _all_text(view)
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_panel_button_without_cog_refuses(ctx, db):
    from bot_modules.economy.cog.views import ShopRentButton

    _enable(db)
    interaction = _panel_button_interaction(ctx)  # cog unloaded mid-flight

    await ShopRentButton("role_color").callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't available" in msg
    assert _live_rentals(db) == []


# ── role studio setters (shared by the shop's customise modals) ──────────────


async def _role_name(cog, interaction, text) -> None:
    await cog.set_role_name(interaction, text)


async def _role_color(cog, interaction, hex_) -> None:
    await cog.set_role_color(interaction, hex_)


async def _role_gradient(cog, interaction, h1, h2) -> None:
    await cog.set_role_gradient(interaction, h1, h2)


async def _role_icon_emoji(cog, interaction, raw) -> None:
    await cog.set_role_icon_emoji(interaction, raw)


async def _role_icon_image(cog, interaction, image) -> None:
    await cog.role_icon.callback(cog, interaction, image)


def _fake_emoji(name="party", eid=999, animated=False, data=b"emoji-bytes"):
    e = MagicMock()
    e.name = name
    e.id = eid
    e.animated = animated
    e.read = AsyncMock(return_value=data)
    return e


@pytest.mark.asyncio
async def test_require_perk_falls_back_for_an_unmapped_perk(ctx, db):
    """A setter for a perk with no _PERK_REFUSAL row still refuses politely.

    SELF_PERKS has six members and the refusal table four, so a future setter
    for role_holographic or voice_style would otherwise raise KeyError and
    show the member "This interaction failed."
    """
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))

    assert await cog._require_perk(interaction, GUILD_ID, "role_holographic") is False
    msg = interaction.response.send_message.await_args.args[0]
    assert "Rent that perk first" in msg


def test_disabled_shop_stops_after_the_settings_row(ctx, db):
    """The economy-off gate stays cheap — no catalog/rental reads to say 'off'.

    The returned values would be empty either way; what this pins is that the
    four post-gate queries aren't issued for a page that will only ever say
    the economy is disabled.
    """
    cog = _make_cog(ctx)  # economy left disabled
    with (
        _patch_everywhere("catalog_price_range") as catalog,
        patch("bot_modules.economy.cog.base.list_refundable_rentals") as rentals,
    ):
        shop = cog._shop_context(GUILD_ID, 500)

    assert shop.settings.enabled is False
    catalog.assert_not_called()
    rentals.assert_not_called()
    assert (shop.owned, shop.balance, shop.icon_range) == (set(), 0, None)
    assert (shop.refundable, shop.shields_held, shop.shield_price) == ([], 0, 0)


@pytest.mark.parametrize(
    ("perk", "rented", "refusal"),
    [
        # No rental at all → the entitlement check refuses.
        ("role_name", False, "rent"),
        ("role_color", False, "perk"),
        # Rented, but the server feature gate is closed → refused at the gate
        # (role_name/role_color aren't feature-gated, so the closed gate below
        # never touches them).
        ("role_gradient", True, "support"),
        ("role_icon", True, "support"),
    ],
)
@pytest.mark.asyncio
async def test_role_studio_setter_refusals(ctx, db, perk, rented, refusal):
    _enable(db)
    if rented:
        _add_rental(db, perk)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=False),
        ),
    ):
        if perk == "role_name":
            await _role_name(cog, interaction, "Cool")
        elif perk == "role_color":
            await _role_color(cog, interaction, "#7B2FF7")
        elif perk == "role_gradient":
            await _role_gradient(cog, interaction, "#111111", "#222222")
        else:
            await _role_icon_emoji(cog, interaction, ":party:")
    assert refusal in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


# ── staff perk comp ────────────────────────────────────────────────────
#
# The comp's own decision table lives in test_economy_rentals_logic; these
# two are the wiring — that the cog's staff verdict actually reaches the
# entitlement read, in both directions. Without them the gate could ignore
# ``is_mod`` entirely and every pure test would still pass.


@pytest.mark.asyncio
async def test_role_setter_comped_for_staff_without_any_rental(ctx, db):
    _enable(db, mod_perk_comp=True)
    ctx.is_mod = lambda _interaction: True
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#00FF00")
    apply_mock.assert_awaited_once()
    assert _personal_role(db)["color"] == 0x00FF00
    # A comp is not a purchase: no rental row was created behind it.
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_role_setter_still_refuses_staff_while_comp_is_off(ctx, db):
    """The dashboard switch is enforced, not decorative."""
    _enable(db)  # mod_perk_comp defaults off
    ctx.is_mod = lambda _interaction: True
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#00FF00")
    assert "perk" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("was_staff", "now_staff", "comp_on", "applies", "revokes"),
    [
        # Promoted → project the comped perks onto their personal role.
        (False, True, True, True, False),
        # Demoted → re-project, which reverts the nick and deletes the role
        # when no real rental is left behind the comp.
        (True, False, True, False, True),
        # Staff-ness unchanged (a nick edit, an unrelated role) → no work.
        (True, True, True, False, False),
        (False, False, True, False, False),
        # Comp switched off for this guild → the listener stays out of it.
        (False, True, False, False, False),
    ],
)
@pytest.mark.asyncio
async def test_staff_change_reprojects_perks(
    ctx, db, was_staff, now_staff, comp_on, applies, revokes
):
    _enable(db, mod_perk_comp=comp_on)
    ctx.member_is_mod = lambda member: member.staff
    cog = _make_cog(ctx)
    before = _member(member_id=500)
    after = _member(member_id=500)
    before.staff, after.staff = was_staff, now_staff
    # A role diff is the listener's cheap pre-filter; give it one so the
    # staff-ness comparison is what decides these cases.
    before.roles = [SimpleNamespace(id=1)]
    after.roles = [SimpleNamespace(id=2)]
    for m in (before, after):
        m.guild = SimpleNamespace(id=GUILD_ID)
    with _patch_projection() as (apply_mock, revoke_mock, _n):
        await cog._on_staff_comp_changed(before, after)
    assert apply_mock.await_count == (1 if applies else 0)
    assert revoke_mock.await_count == (1 if revokes else 0)


@pytest.mark.asyncio
async def test_staff_change_ignores_updates_that_touch_no_roles(ctx, db):
    """The listener sees every nick/avatar/timeout edit — exit before any I/O."""
    _enable(db, mod_perk_comp=True)
    ctx.member_is_mod = MagicMock(return_value=True)
    cog = _make_cog(ctx)
    roles = [SimpleNamespace(id=1)]
    before, after = _member(member_id=500), _member(member_id=500)
    before.roles = after.roles = roles
    with _patch_projection() as (apply_mock, revoke_mock, _n):
        await cog._on_staff_comp_changed(before, after)
    ctx.member_is_mod.assert_not_called()
    apply_mock.assert_not_awaited()
    revoke_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_name_blocklist_hit(ctx, db):
    _enable(db)
    _add_rental(db, "role_name")
    with open_db(db) as conn:
        add_name_blocklist(conn, GUILD_ID, "badword", 1)
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_name(cog, interaction, "My BadWord name")
    assert "allowed" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_name_too_long(ctx, db):
    _enable(db)
    _add_rental(db, "role_name")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection():
        await _role_name(cog, interaction, "x" * 33)
    assert "32" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_role_name_success(ctx, db):
    _enable(db)
    _add_rental(db, "role_name")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_name(cog, interaction, "Stardust")
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    assert row["name"] == "Stardust"


@pytest.mark.asyncio
async def test_role_color_bad_hex(ctx, db):
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection():
        await _role_color(cog, interaction, "not-a-color")
    assert "hex" in interaction.response.send_message.await_args.args[0].lower()


def _staff_role(color=0xFF0000, name="Admins"):
    staff = MagicMock()
    staff.id = 77
    staff.name = name
    staff.color = discord.Color(color)
    staff.permissions = discord.Permissions(administrator=True)
    return staff


@pytest.mark.parametrize(
    "picked",
    [
        pytest.param("#FE0101", id="near-identical-red"),
        pytest.param("#FF0000", id="exactly-the-staff-color"),
    ],
)
@pytest.mark.asyncio
async def test_role_color_matching_a_staff_role_is_allowed(ctx, db, picked):
    """The ΔE staff-collision guard is gone (2026-07-29) — this is the new rule.

    It refused any color within ΔE 25 of a moderation-permissioned role, on
    the grounds that a matching hue could let a member pass for staff in the
    member list. Every staff role in this server now carries a role icon,
    which distinguishes staff regardless of hue, so the color no longer has to.
    Any parseable hex is accepted, up to and including an exact match.
    """
    _enable(db)
    _add_rental(db, "role_color")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), roles=[_staff_role()])
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, picked)
    apply_mock.assert_awaited_once()
    assert _personal_role(db)["color"] == int(picked.lstrip("#"), 16)


@pytest.mark.asyncio
async def test_role_gradient_matching_a_staff_role_is_allowed(ctx, db):
    """Both stops were checked, so both had to stop being checked."""
    _enable(db)
    _add_rental(db, "role_gradient")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), roles=[_staff_role()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _role_gradient(cog, interaction, "#FF0000", "#FE0101")
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    assert (row["color"], row["color2"]) == (0xFF0000, 0xFE0101)


@pytest.mark.asyncio
async def test_role_color_gift_entitlement_allows(ctx, db):
    _enable(db)
    _add_rental(db, "role_color", user_id=800, beneficiary_id=500)  # gifted to 500
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _role_color(cog, interaction, "#00FF00")
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    assert row["color"] == 0x00FF00


@pytest.mark.asyncio
async def test_role_gradient_success(ctx, db):
    _enable(db)
    _add_rental(db, "role_gradient")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere("feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_gradient(cog, interaction, "#111111", "#222222")
    apply_mock.assert_awaited_once()
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT color, color2 FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["color"] == 0x111111 and row["color2"] == 0x222222


@pytest.mark.parametrize("raw", [":party:", "party", "<:party:999>"])
@pytest.mark.asyncio
async def test_role_icon_custom_emoji_success(ctx, db, raw):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere("feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_emoji(cog, interaction, raw)
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    # The emoji's image is downloaded into the managed icon store.
    assert Path(row["icon_path"]).read_bytes() == b"emoji-bytes"


@pytest.mark.parametrize("raw", ["✨", "<:evil:123>", ":nosuch:"])
@pytest.mark.asyncio
async def test_role_icon_rejects_non_server_emoji(ctx, db, raw):
    """Unicode emojis and emojis from other servers are refused."""
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere("feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_emoji(cog, interaction, raw)
    msg = interaction.response.send_message.await_args.args[0]
    assert "custom emoji" in msg
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_icon_rejects_animated_emoji(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    emoji = _fake_emoji(animated=True)
    interaction = _role_interaction(_member(member_id=500), emojis=[emoji])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere("feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_emoji(cog, interaction, ":party:")
    assert "animated" in interaction.response.send_message.await_args.args[0].lower()
    apply_mock.assert_not_awaited()


# ── icon catalog picker: the Custom entry ───────────────────────────────────


def test_icon_catalog_select_last_slot_is_custom(db):
    """The picker always ends with the flat-price bring-your-own entry, and a
    big catalog trims to 24 icons so the total stays within Discord's
    25-option cap."""
    from bot_modules.economy.cog.views import _IconCatalogSelect

    _enable(db)
    icons = [{"id": i + 1, "name": f"Icon {i}", "price": 100} for i in range(30)]
    select = _IconCatalogSelect(MagicMock(), _settings(db), _guild_roles(), icons)
    assert len(select.options) == 25
    assert select.options[-1].value == "custom"
    assert "75" in select.options[-1].description  # the flat price_role_icon
    assert [o.value for o in select.options[:24]] == [str(i + 1) for i in range(24)]


@pytest.mark.asyncio
async def test_pick_custom_icon_rents_flat_when_unowned(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_custom_icon(interaction, _settings(db), _guild_roles())
    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["perk"] == "role_icon"
    assert rentals[0]["catalog_icon_id"] is None
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500 - 75  # flat upfront week
    apply_mock.assert_awaited_once()
    # The confirmation points at the image-upload path.
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert "/bank role icon" in _all_text(view)


@pytest.mark.asyncio
async def test_pick_custom_icon_is_free_for_comped_staff(ctx, db):
    """The picker's Custom entry reaches the rent flow directly, so the comp
    has to be enforced there and not just on the shop's hidden buttons."""
    _enable(db, mod_perk_comp=True)
    _credit(db, 500, 500)
    ctx.is_mod = lambda _interaction: True
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_custom_icon(interaction, _settings(db), _guild_roles())
    assert _live_rentals(db) == []
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500  # not a coin
    apply_mock.assert_awaited_once()
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert "Unlocked" in _all_text(view)


@pytest.mark.asyncio
async def test_pick_catalog_icon_is_free_for_comped_staff(ctx, db):
    """A catalog icon IS the role_icon perk at a per-icon price, so the comp
    covers it — otherwise the curated icons would be the one thing a mod
    still had to buy."""
    from bot_modules.economy.icon_catalog_service import add_catalog_icon

    _enable(db, mod_perk_comp=True)
    _credit(db, 500, 500)
    ctx.is_mod = lambda _interaction: True
    from bot_modules.economy.icon_catalog_service import (
        set_catalog_icon_image,
    )

    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
        set_catalog_icon_image(conn, GUILD_ID, icon_id, "/icons/crown.png")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_catalog_icon(interaction, _guild_roles(), icon_id)
    assert _live_rentals(db) == []
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 500
        row = conn.execute(
            "SELECT icon_path FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["icon_path"] == "/icons/crown.png"  # the art still landed
    apply_mock.assert_awaited_once()
    assert "on the house" in interaction.edit_original_response.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_pick_catalog_icon_still_charges_a_non_mod(ctx, db):
    from bot_modules.economy.icon_catalog_service import add_catalog_icon

    _enable(db, mod_perk_comp=True)
    _credit(db, 500, 500)
    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_catalog_icon(interaction, _guild_roles(), icon_id)
    assert len(_live_rentals(db)) == 1
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 400


@pytest.mark.asyncio
async def test_pick_custom_icon_switches_catalog_rental_free_and_clears_art(ctx, db):
    """Catalog → custom re-tags the paid week (no charge) and blanks the
    projected image — the catalog art belongs to the catalog price."""
    from bot_modules.economy.icon_catalog_service import add_catalog_icon
    from bot_modules.economy.rentals_service import (
        rent_perk,
        upsert_personal_role,
    )

    _enable(db)
    _credit(db, 500, 500)
    settings = _settings(db)
    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
        rent_perk(
            conn, settings, GUILD_ID, 500, "role_icon", catalog_icon_id=icon_id
        )
        upsert_personal_role(conn, GUILD_ID, 500, {"icon_path": "/icons/crown.png"})
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    interaction.original_response = AsyncMock(return_value=SimpleNamespace(id=555))
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.pick_custom_icon(interaction, settings, _guild_roles())
    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["catalog_icon_id"] is None
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 400  # only the upfront 100
        row = conn.execute(
            "SELECT icon_path FROM econ_personal_roles WHERE user_id = 500"
        ).fetchone()
    assert row["icon_path"] == ""
    apply_mock.assert_awaited_once()
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert "next renewal" in _all_text(view)
    # Regression: this send site (the switch-to-custom flow) omitted
    # view.message before the fix.
    assert view.message is interaction.original_response.return_value


@pytest.mark.asyncio
async def test_pick_custom_icon_already_custom_just_offers_customise(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    interaction.original_response = AsyncMock(return_value=SimpleNamespace(id=555))
    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.pick_custom_icon(interaction, _settings(db), _guild_roles())
    assert len(_live_rentals(db)) == 1  # no double rent, no charge
    call = interaction.response.send_message.await_args
    assert "already" in _all_text(call.kwargs["view"])
    # Regression: this send site (already-custom notice) omitted
    # view.message before the fix.
    assert call.kwargs["view"].message is interaction.original_response.return_value


@pytest.mark.asyncio
async def test_role_icon_upload_blocked_for_catalog_renters_only(ctx, db):
    """The upload guard is per-rental, not per-guild: a curated-catalog renter
    is locked to their icon, while a flat-price custom renter uploads freely
    even though the guild stocks a catalog."""
    from bot_modules.economy.icon_catalog_service import add_catalog_icon

    _enable(db)
    with open_db(db) as conn:
        icon_id = add_catalog_icon(conn, GUILD_ID, name="Crown", price=100)
    _add_rental(db, "role_icon", user_id=500, catalog_icon_id=icon_id)
    _add_rental(db, "role_icon", user_id=600)
    cog = _make_cog(ctx)
    assert cog._catalog_locked(GUILD_ID, 500) is True
    assert cog._catalog_locked(GUILD_ID, 600) is False
    assert cog._catalog_locked(GUILD_ID, 700) is False  # no rental at all

    locked = _role_interaction(_member(member_id=500), emojis=[_fake_emoji()])
    free = _role_interaction(_member(member_id=600), emojis=[_fake_emoji()])
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _role_icon_emoji(cog, locked, ":party:")
        await _role_icon_emoji(cog, free, ":party:")
    assert "Custom" in locked.response.send_message.await_args.args[0]
    apply_mock.assert_awaited_once()  # only the custom renter's upload landed


@pytest.mark.asyncio
async def test_role_icon_image_upload_success(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    image = MagicMock()
    image.size = 100
    image.read = AsyncMock(return_value=b"png-bytes")
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere("feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_image(cog, interaction, image)
    apply_mock.assert_awaited_once()
    row = _personal_role(db)
    assert Path(row["icon_path"]).read_bytes() == b"png-bytes"


@pytest.mark.asyncio
async def test_role_icon_image_too_big(ctx, db):
    _enable(db)
    _add_rental(db, "role_icon")
    cog = _make_cog(ctx)
    interaction = _role_interaction(_member(member_id=500))
    image = MagicMock()
    image.size = 300 * 1024
    with (
        _patch_projection() as (apply_mock, _r, _n),
        _patch_everywhere("feature_gate_ok", new=AsyncMock(return_value=True)),
    ):
        await _role_icon_image(cog, interaction, image)
    assert "256KB" in interaction.response.send_message.await_args.args[0]
    apply_mock.assert_not_awaited()


# A converted row and its button can no longer drift the way the old embed
# table and the old _ShopView's own button logic could: shop_section_rows()
# is the single source both build_shop_section_layout (the card) and
# _ShopView (the callback binder) read, so there is exactly one decision to
# get right, not two to keep in sync. That branch matrix — voice-style
# on/off, the shield held/unheld, the raffle toggle — is covered directly
# against shop_section_rows() in tests/economy/test_shop_layouts.py, which is where
# a real drift (if the two builders ever diverged again) would now show up.


# ── /bank gift ───────────────────────────────────────────────────────────────


async def _gift(cog, interaction, member, perk="role_color") -> None:
    # A bare string, not an app_commands.Choice: the perk parameter moved to
    # autocomplete so the picker can be per-guild (a switched-off perk must
    # not be offered), and autocomplete parameters arrive as their raw value.
    await cog.bank_gift.callback(cog, interaction, member, perk)


@pytest.mark.asyncio
async def test_gift_success_both_sides(ctx, db):
    _enable(db)
    _credit(db, 500, 50)
    cog = _make_cog(ctx)
    gifter = _member(member_id=500, name="Alice")
    friend = _member(member_id=900, name="Bob")
    interaction = _interaction(gifter)

    with _patch_projection() as (apply_mock, _r, notify):
        await _gift(cog, interaction, friend)

    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "role_color"
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900
    # Beneficiary's role is projected and DM'd; payer gets the confirmation.
    apply_mock.assert_awaited_once_with(cog.bot, ctx.db_path, GUILD_ID, 900)
    # The beneficiary's note is queued for their next batched DM.
    assert [(to, by) for to, by, _ in _queued_notes(db)] == [(900, 500)]


@pytest.mark.asyncio
async def test_gift_any_perk_bills_base_price(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Alice"))

    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="role_name")

    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "role_name"
    assert rentals[0]["price"] == 35  # the base perk price, no gift surcharge
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900


@pytest.mark.asyncio
async def test_gift_feature_gated_perk_refused_when_gate_closed(ctx, db):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with (
        _patch_projection(),
        _patch_everywhere(
            "feature_gate_ok",
            new=AsyncMock(return_value=False),
        ),
    ):
        await _gift(cog, interaction, _member(member_id=900), perk="role_gradient")

    assert "server feature" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_gift_duplicate_entitlement_requires_confirm(ctx, db):
    """Gifting a perk the friend already has stops at a confirm view."""
    from bot_modules.economy.cog.views import _GiftConfirmView

    _enable(db)
    _credit(db, 500, 100)
    _add_rental(db, "role_color", user_id=900)  # friend self-rents it already
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900))

    # No rental opened yet — the reply is the Gift anyway? confirm gate.
    assert len(_live_rentals(db)) == 1  # just the friend's own rental
    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["view"], _GiftConfirmView)
    assert "already has" in interaction.response.send_message.await_args.args[0]


@pytest.mark.parametrize(
    ("command", "target_kind", "refusal"),
    [
        ("grant", "bot", "bot"),
        ("pay", "bot", "bot"),
        ("gift", "bot", "bot"),
        ("gift", "self", "your own"),
    ],
)
@pytest.mark.asyncio
async def test_self_and_bot_targets_rejected(ctx, db, command, target_kind, refusal):
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    actor = _member(member_id=500, admin=True)  # admin so grant reaches its guard
    target = actor if target_kind == "self" else _member(member_id=901, is_bot=True)
    interaction = _interaction(actor)

    with _patch_projection():
        if command == "grant":
            await _grant(cog, interaction, target, 10, "x")
        elif command == "pay":
            await _pay(cog, interaction, target, 50)
        else:
            await _gift(cog, interaction, target)

    call = interaction.response.send_message.await_args
    assert refusal in call.args[0].lower()
    assert call.kwargs["ephemeral"] is True
    assert _live_rentals(db) == []  # no gift rental opened
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 901) == 0  # nothing credited


# ── /bank wallet: rentals field ──────────────────────────────────────────────


def _wallet_section(view: discord.ui.LayoutView, heading: str) -> str:
    """The ``### {heading}`` Text Display's body, for the wallet card."""
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith(
            f"### {heading}\n"
        ):
            return item.content[len(f"### {heading}\n") :]
    raise AssertionError(f"no {heading!r} section on this card")


@pytest.mark.asyncio
async def test_wallet_shows_active_rentals(ctx, db):
    _enable(db)
    _add_rental(db, "role_color", user_id=500)
    _add_rental(db, "role_color", user_id=800, beneficiary_id=500)  # gift received
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _wallet(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    rentals = _wallet_section(view, "Active Rentals")
    assert "Custom Role Color" in rentals
    assert "gift received" in rentals


@pytest.mark.asyncio
async def test_wallet_active_rentals_section_stays_under_budget(ctx, db):
    # A popular member on the receiving end of many gifts can accrue a dozen+
    # live rentals; each renders ~70 chars, so the joined section would blow
    # past its share of the card's budget unbounded — fit_lines trims.
    _enable(db)
    for payer in range(800, 830):  # 30 distinct gifters -> 30 live rentals
        _add_rental(db, "role_color", user_id=payer, beneficiary_id=500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _wallet(cog, interaction)

    view = interaction.response.send_message.await_args.kwargs["view"]
    check_budget(view)
    rentals = _wallet_section(view, "Active Rentals")
    assert len(rentals) <= 1024
    assert "Custom Role Color" in rentals


# ── on_member_remove cleanup ─────────────────────────────────────────────────


async def _member_remove(cog, member) -> None:
    await cog.on_member_remove(member)


def _leaving_member(member_id) -> MagicMock:
    m = MagicMock()
    m.id = member_id
    m.guild = _guild_roles()
    return m


@pytest.mark.asyncio
async def test_member_remove_cancels_and_reprojects_all(ctx, db):
    _enable(db)
    # Leaver 500 rents a color AND gifts a color to friend 900.
    _add_rental(db, "role_color", user_id=500)
    _add_rental(db, "role_color", user_id=500, beneficiary_id=900)
    cog = _make_cog(ctx)

    with _patch_projection() as (_a, revoke, _n):
        await _member_remove(cog, _leaving_member(500))

    # Both live rentals cancelled.
    assert _live_rentals(db) == []
    # Re-projected for the leaver (500) and the still-present friend (900).
    revoked_ids = {call.args[3] for call in revoke.await_args_list}
    assert revoked_ids == {500, 900}


@pytest.mark.asyncio
async def test_member_remove_beneficiary_leaving_cancels_gift(ctx, db):
    _enable(db)
    # Friend 900 leaves; the gift 500→900 must lapse.
    _add_rental(db, "role_color", user_id=500, beneficiary_id=900)
    cog = _make_cog(ctx)

    with _patch_projection() as (_a, revoke, _n):
        await _member_remove(cog, _leaving_member(900))

    assert _live_rentals(db) == []
    revoked_ids = {call.args[3] for call in revoke.await_args_list}
    assert 900 in revoked_ids


@pytest.mark.asyncio
async def test_member_remove_skips_when_economy_disabled(ctx, db):
    _add_rental(db, "role_color", user_id=500)  # economy left disabled
    cog = _make_cog(ctx)
    with _patch_projection() as (_a, revoke, _n):
        await _member_remove(cog, _leaving_member(500))
    revoke.assert_not_awaited()
    assert len(_live_rentals(db)) == 1  # untouched


# ── trigger-word quests (spec §4.4) ─────────────────────────────────────────


def _trigger_message(
    *,
    author,
    content,
    channel_id: int = 111,
    parent_id: int | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.guild = FakeGuild(id=GUILD_ID)
    msg.author = author
    msg.content = content
    msg.channel = SimpleNamespace(id=channel_id, parent_id=parent_id)
    msg.add_reaction = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def _balance(db, user_id) -> int:
    with open_db(db) as conn:
        return get_balance(conn, GUILD_ID, user_id)


@pytest.mark.asyncio
async def test_trigger_message_pays_instant_quest_once_per_period(ctx, db):
    _enable(db)
    _mk_quest(db, reward=10, title="Say GM", trigger_words="gm, good morning")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _trigger_message(author=member, content="GM everyone!")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 10
    msg.add_reaction.assert_awaited_once_with("✅")
    msg.reply.assert_not_awaited()

    # A repeat inside the same period stays silent and pays nothing more.
    repeat = _trigger_message(author=member, content="gm again")
    await cog._on_trigger_message(repeat)
    assert _balance(db, 501) == 10
    repeat.reply.assert_not_awaited()
    repeat.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_quest_completion_is_reaction_only_regardless_of_role(ctx, db):
    """No reply, no DM either way — game_role_id no longer affects this path
    (it still gates the daily digest / raffle-winner notices elsewhere)."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, title="Say GM", trigger_words="gm")
    cog = _make_cog(ctx)

    for member in (_member(member_id=501, role_ids=(777,)), _member(member_id=502)):
        msg = _trigger_message(author=member, content="gm")
        with patch(
            "bot_modules.economy.economy_cog.notify_member",
            new=AsyncMock(return_value=True),
        ) as notify:
            await cog._on_trigger_message(msg)

        assert _balance(db, member.id) == 10
        msg.add_reaction.assert_awaited_once_with("✅")
        msg.reply.assert_not_awaited()
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_signoff_trigger_quest_boards_the_claim_and_reacts_only(ctx, db):
    """A sign-off quest still files the claim and puts it on the mods' board,
    but the member only gets the 📝 reaction — no reply, no DM, regardless of
    game_role_id."""
    _enable(db, game_role_id=777)
    _mk_quest(db, reward=10, signoff=1, trigger_words="did it")
    cog = _make_cog(ctx)

    for member in (_member(member_id=501, role_ids=(777,)), _member(member_id=502)):
        msg = _trigger_message(author=member, content="did it")
        with (
            patch(
                "bot_modules.economy.cog.quests.refresh_signoff_board", new=AsyncMock()
            ) as board,
            patch(
                "bot_modules.economy.economy_cog.notify_member",
                new=AsyncMock(return_value=True),
            ) as notify,
        ):
            await cog._on_trigger_message(msg)

        board.assert_awaited_once()  # the claim shows up on the todo board
        msg.add_reaction.assert_awaited_once_with("📝")
        msg.reply.assert_not_awaited()
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_message_ignores_non_matches_and_bots(ctx, db):
    _enable(db)
    _mk_quest(db, reward=10, trigger_words="gm")
    cog = _make_cog(ctx)

    await cog._on_trigger_message(
        _trigger_message(author=_member(member_id=501), content="hello there")
    )
    await cog._on_trigger_message(
        _trigger_message(author=_member(member_id=502, is_bot=True), content="gm")
    )
    assert _balance(db, 501) == 0
    assert _balance(db, 502) == 0


@pytest.mark.asyncio
async def test_trigger_channel_scope(ctx, db):
    _enable(db)
    _mk_quest(db, reward=10, trigger_words="gm", trigger_channel_id=222)
    cog = _make_cog(ctx)

    wrong = _trigger_message(
        author=_member(member_id=501), content="gm", channel_id=111
    )
    await cog._on_trigger_message(wrong)
    assert _balance(db, 501) == 0
    wrong.reply.assert_not_awaited()

    right = _trigger_message(
        author=_member(member_id=501), content="gm", channel_id=222
    )
    await cog._on_trigger_message(right)
    assert _balance(db, 501) == 10

    # A thread under the scoped channel counts via parent_id.
    thread = _trigger_message(
        author=_member(member_id=502), content="gm",
        channel_id=333, parent_id=222,
    )
    await cog._on_trigger_message(thread)
    assert _balance(db, 502) == 10


@pytest.mark.asyncio
async def test_trigger_signoff_quest_files_pending_claim_and_boards_it(ctx, db):
    _enable(db)
    qid = _mk_quest(db, reward=10, signoff=1, trigger_words="did it")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _trigger_message(author=member, content="I did it!")
    with patch(
        "bot_modules.economy.cog.quests.refresh_signoff_board", new=AsyncMock()
    ) as board:
        await cog._on_trigger_message(msg)

    assert _balance(db, 501) == 0  # sign-off gates the payout
    board.assert_awaited_once()
    msg.add_reaction.assert_awaited_once_with("📝")
    with open_db(db) as conn:
        claim = conn.execute(
            "SELECT state FROM econ_quest_claims WHERE quest_id = ? AND user_id = 501",
            (qid,),
        ).fetchone()
    assert claim is not None and claim["state"] == "pending"


@pytest.mark.asyncio
async def test_trigger_message_noop_when_economy_disabled(ctx, db):
    _mk_quest(db, reward=10, trigger_words="gm")  # economy left disabled
    cog = _make_cog(ctx)
    msg = _trigger_message(author=_member(member_id=501), content="gm")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 0
    msg.reply.assert_not_awaited()


def test_trigger_quest_excluded_from_manual_claims(ctx, db):
    _enable(db)
    _mk_quest(db, title="Say GM", trigger_words="gm")
    cog = _make_cog(ctx)
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert [q["state"] for q in state] == ["trigger"]


# ── per-member board size (configurable) ────────────────────────────────────


def test_board_size_limits_quests_shown(ctx, db):
    # Six active dailies, board sized to 1 → the member sees exactly one.
    _enable(db, quest_board_daily=1)
    for i in range(6):
        _mk_quest(db, title=f"Daily {i}")
    cog = _make_cog(ctx)
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert len(state) == 1


def test_board_size_zero_shows_no_quests(ctx, db):
    # 0 = cadence off. Guards the inverse regression: gating the board filter
    # on "size > 0" would skip filtering entirely and show the whole pool.
    _enable(db, quest_board_daily=0)
    for i in range(6):
        _mk_quest(db, title=f"Daily {i}")
    cog = _make_cog(ctx)
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert state == []


@pytest.mark.asyncio
async def test_board_size_zero_blocks_trigger_word_claim(ctx, db):
    # The third board gate (the trigger-word on_message path). With the
    # cadence off, saying the phrase must pay nothing rather than fall
    # through to an unfiltered claim.
    _enable(db, quest_board_daily=0)
    _mk_quest(db, reward=10, trigger_words="gm")
    cog = _make_cog(ctx)
    msg = _trigger_message(author=_member(member_id=501), content="gm")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 0
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_word_still_pays_with_board_size_one(ctx, db):
    # Control for the test above: the phrase pays when the cadence is on, so
    # the 0 case is proving the gate and not a broken fixture.
    _enable(db, quest_board_daily=1)
    _mk_quest(db, reward=10, trigger_words="gm")
    cog = _make_cog(ctx)
    msg = _trigger_message(author=_member(member_id=501), content="gm")
    await cog._on_trigger_message(msg)
    assert _balance(db, 501) == 10


def test_board_size_zero_round_trips_through_settings(db):
    # The dial is only usable if a stored "0" loads back as 0 rather than
    # falling through to the default board size.
    _enable(db, quest_board_daily=0, quest_board_weekly=3)
    with open_db(db) as conn:
        loaded = load_econ_settings(conn, GUILD_ID)
    assert loaded.quest_board_daily == 0
    assert loaded.quest_board_weekly == 3


# ── photo-post event quest ──────────────────────────────────────────────────

PHOTO_CHANNEL_ID = 111


def _disable_photo_source(db) -> None:
    with open_db(db) as conn:
        set_income_source(conn, GUILD_ID, "photo_post", False)
        conn.commit()


def _set_photo_config(db, *, channel_id=PHOTO_CHANNEL_ID) -> None:
    opts: dict[str, object] = {"channel_id": str(channel_id) if channel_id else ""}
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled, options)"
            " VALUES (?, 'photo', 1, ?)"
            " ON CONFLICT(guild_id, game_type) DO UPDATE SET options = excluded.options",
            (GUILD_ID, json.dumps(opts)),
        )
        conn.commit()


def _set_photo_schedule(db, *, channel_id=PHOTO_CHANNEL_ID, status="active") -> None:
    """Insert a minimal photo schedule row (games_scheduled), no config row.

    Mirrors the live desync where a schedule was created but the Photo
    Challenge Setup panel (which owns the games_game_config channel) was
    never saved, so the award listener must recover the channel here.
    """
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO games_scheduled"
            " (guild_id, channel_id, game_type, created_by, created_at,"
            "  time_of_day, recurrence, status)"
            " VALUES (?, ?, 'photo', 1, 0, 540, 'daily', ?)",
            (GUILD_ID, channel_id, status),
        )
        conn.commit()


def _today_period(db) -> str:
    with open_db(db) as conn:
        offset = get_tz_offset_hours(conn, GUILD_ID)
    return f"photo_post:{local_day_for(time.time(), offset)}"


def _photo_msg(
    *,
    author,
    message_id: int = 9100,
    channel_id: int = PHOTO_CHANNEL_ID,
    parent_id: int | None = None,
    content_type: str | None = "image/png",
    filename: str = "pic.png",
    with_attachment: bool = True,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.guild = FakeGuild(id=GUILD_ID)
    msg.author = author
    msg.channel = SimpleNamespace(id=channel_id, parent_id=parent_id)
    att = SimpleNamespace(content_type=content_type, filename=filename)
    msg.attachments = [att] if with_attachment else []
    msg.add_reaction = AsyncMock()
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_photo_post_participation_pays_without_quest(ctx, db):
    # The flat participation award pays on the post itself — no quest needed.
    _enable(db)  # reward_photo_post defaults to 5
    _set_photo_config(db)
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_msg(author=member)
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 5
    msg.add_reaction.assert_awaited_once_with("✅")

    # A second photo the same day pays nothing more — once per local day.
    await cog._on_photo_post(_photo_msg(author=member, message_id=9200))
    assert _balance(db, 501) == 5


@pytest.mark.parametrize(
    ("setup", "channel_id", "parent_id", "pays"),
    [
        # A THREAD of the Photo Challenge channel earns too: the listener
        # matches on parent_id, not just the exact channel id (mirrors the
        # trigger-quest / games siblings).
        ("config", 999_888, PHOTO_CHANNEL_ID, True),
        # Neither the scoped channel nor a thread of it pays nothing — the
        # parent_id widening must not swallow the whole guild.
        ("config", 999_888, 555_444, False),
        # A finished (status='done') schedule is not a live channel — the
        # fallback only recovers the channel from an *active* schedule.
        ("done_schedule", PHOTO_CHANNEL_ID, None, False),
    ],
)
@pytest.mark.asyncio
async def test_photo_post_channel_scoping(ctx, db, setup, channel_id, parent_id, pays):
    _enable(db)  # reward_photo_post defaults to 5
    if setup == "config":
        _set_photo_config(db)  # scoped channel is PHOTO_CHANNEL_ID
    else:
        _set_photo_schedule(db, status="done")  # no config row at all
    cog = _make_cog(ctx)

    msg = _photo_msg(
        author=_member(member_id=501), channel_id=channel_id, parent_id=parent_id
    )
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == (5 if pays else 0)
    if pays:
        msg.add_reaction.assert_awaited_once_with("✅")
    else:
        msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_post_no_payout_when_source_disabled(ctx, db):
    # The photo_post income-source toggle gates both payouts. The
    # participation-award half of this gate lives only in the cog listener
    # (EconomyCog._on_photo_post, economy/cog/quests.py) with no service-layer equivalent, so this
    # is its sole enforcement test — a dashboard toggle must never ship
    # unenforced (restored after the 2026-07 dedup removed it as a supposed
    # service-test duplicate; only the quest-bonus half was covered there).
    _enable(db)  # participation 5
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    _disable_photo_source(db)
    cog = _make_cog(ctx)
    msg = _photo_msg(author=_member(member_id=501))
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 0
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_post_pays_via_schedule_channel_without_config(ctx, db):
    # Regression: a photo schedule exists but the Setup panel was never saved,
    # so games_game_config has no 'photo' row. The award listener must recover
    # the channel from the active schedule and still pay — no more silent misses.
    _enable(db)  # participation 5, no config row written
    _set_photo_schedule(db)  # active schedule points at PHOTO_CHANNEL_ID
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_msg(author=member)
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 5
    msg.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_photo_post_config_channel_wins_over_schedule(ctx, db):
    # When the config row carries a channel, it is authoritative even if a
    # schedule points elsewhere — the fallback only fires on an empty config.
    _enable(db)  # participation 5
    _set_photo_config(db, channel_id=PHOTO_CHANNEL_ID)
    _set_photo_schedule(db, channel_id=222)  # different channel
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    # A post in the schedule's channel is ignored (config channel is the gate).
    await cog._on_photo_post(_photo_msg(author=member, channel_id=222))
    assert _balance(db, 501) == 0
    # A post in the configured channel pays.
    await cog._on_photo_post(_photo_msg(author=member, message_id=9300))
    assert _balance(db, 501) == 5


@pytest.mark.asyncio
async def test_photo_post_quest_stacks_on_participation(ctx, db):
    # Participation (5) + an active photo_post quest (10) both pay = 15.
    _enable(db)  # reward_photo_post 5
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10, title="Snap it")
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    msg = _photo_msg(author=member)
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 15
    # The quest outcome carries the ✅ (participation doesn't add a second one).
    msg.add_reaction.assert_awaited_once_with("✅")

    # A second photo the same day pays nothing more — both sides cap per day.
    await cog._on_photo_post(_photo_msg(author=member, message_id=9200))
    assert _balance(db, 501) == 15


@pytest.mark.asyncio
async def test_photo_post_gated_by_channel_and_image(ctx, db):
    # Participation off so this isolates the channel/image/quest gating.
    _enable(db, reward_photo_post=0)
    _set_photo_config(db)
    cog = _make_cog(ctx)
    member = _member(member_id=501)

    # No active photo_post quest and no participation → the gate short-circuits.
    await cog._on_photo_post(_photo_msg(author=member))
    assert _balance(db, 501) == 0

    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    cog = _make_cog(ctx)  # fresh channel cache

    # A post in some other channel is ignored (cheap channel gate).
    await cog._on_photo_post(
        _photo_msg(author=member, channel_id=222, message_id=9400)
    )
    assert _balance(db, 501) == 0

    # A non-image post in the channel is ignored.
    await cog._on_photo_post(
        _photo_msg(author=member, with_attachment=False, message_id=9401)
    )
    assert _balance(db, 501) == 0

    # A real image post in the channel pays the quest (participation off).
    await cog._on_photo_post(_photo_msg(author=member, message_id=9402))
    assert _balance(db, 501) == 10


@pytest.mark.asyncio
async def test_photo_post_ignores_bot_author(ctx, db):
    _enable(db)  # participation on
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    cog = _make_cog(ctx)

    # A bot posting an image never earns (the author.bot guard) — neither the
    # participation award nor the quest.
    bot_author = _member(member_id=777, is_bot=True)
    await cog._on_photo_post(_photo_msg(author=bot_author))
    assert _balance(db, 777) == 0


@pytest.mark.asyncio
async def test_photo_post_noop_when_economy_disabled(ctx, db):
    _set_photo_config(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", reward=10)
    cog = _make_cog(ctx)  # economy left disabled
    msg = _photo_msg(author=_member(member_id=501))
    await cog._on_photo_post(msg)
    assert _balance(db, 501) == 0
    msg.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_post_signoff_files_pending_claim(ctx, db):
    # Participation off so the balance isolates the sign-off gating.
    _enable(db, reward_photo_post=0)
    qid = _mk_quest(
        db, qtype="event", trigger_kind="photo_post", reward=10, signoff=1
    )
    _set_photo_config(db)
    cog = _make_cog(ctx)
    msg = _photo_msg(author=_member(member_id=501))
    with patch(
        "bot_modules.economy.cog.quests.refresh_signoff_board", new=AsyncMock()
    ) as board:
        await cog._on_photo_post(msg)

    assert _balance(db, 501) == 0  # sign-off gates the payout
    board.assert_awaited_once()
    msg.add_reaction.assert_awaited_once_with("📝")
    with open_db(db) as conn:
        claim = conn.execute(
            "SELECT state, period FROM econ_quest_claims "
            "WHERE quest_id = ? AND user_id = 501",
            (qid,),
        ).fetchone()
    assert claim is not None and claim["state"] == "pending"
    assert claim["period"] == _today_period(db)


def test_event_quest_shown_as_auto_not_claimable(ctx, db):
    _enable(db)
    _mk_quest(db, qtype="event", trigger_kind="photo_post", title="Snap it")
    cog = _make_cog(ctx)
    _settings, state, _meta = cog._load_quests_state(GUILD_ID, 501)
    assert [q["state"] for q in state] == ["photo_post"]


# ── pay memo ─────────────────────────────────────────────────────────


def test_clean_memo_collapses_whitespace_and_caps_length():
    from bot_modules.economy.cog.common import _MAX_MEMO_LEN, _clean_memo

    assert _clean_memo("  rent   money  ") == "rent money"
    # Newlines would break the one-line wallet/ledger renders.
    assert _clean_memo("rent\nmoney") == "rent money"
    assert _clean_memo(None) is None
    assert _clean_memo("   ") is None
    assert len(_clean_memo("x" * 500)) == _MAX_MEMO_LEN


async def _pay(cog, interaction, member, amount, memo=None) -> None:
    await cog.bank_pay.callback(cog, interaction, member, amount, memo)


@pytest.mark.asyncio
async def test_pay_memo_reaches_embed_and_dm(ctx, db):
    # Ledger persistence of the memo is the service's job
    # (test_transfer_memo_lands_on_both_ledger_sides) — this covers the
    # card/DM wiring on the cog side.
    _enable(db, transfers_enabled=True)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 50, "grant")

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500, name="Payer"))
    recipient = _member(member_id=600, name="Payee")

    await _pay(cog, interaction, recipient, 20, "  rent   money ")

    # Sender's confirmation card carries the normalised memo.
    text = _view_text(interaction.response.send_message.await_args.kwargs["view"])
    assert "Payment Sent" in text
    assert "rent money" in text

    # Recipient's note carries it too, queued for their next batched DM.
    ((to, by, line),) = _queued_notes(db)
    assert (to, by) == (600, 500)
    assert "rent money" in line


@pytest.mark.asyncio
async def test_pay_memo_cannot_ping_via_the_dm_path(ctx, db):
    """The DM/bank-channel fallback sends raw content — @everyone must not ping."""
    _enable(db, transfers_enabled=True)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 50, "grant")

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await _pay(cog, interaction, _member(member_id=600), 20, "@everyone pay up")

    ((_to, _by, line),) = _queued_notes(db)
    assert "@everyone" not in line


@pytest.mark.asyncio
async def test_pay_memo_survives_the_large_amount_confirm_gate(ctx, db):
    _enable(db, transfers_enabled=True)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 5000, "grant")

    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    recipient = _member(member_id=600)
    await _pay(cog, interaction, recipient, 500, "big one")

    # Over the threshold we get a confirm view, not a transfer.
    kwargs = interaction.response.send_message.await_args.kwargs
    view = kwargs["view"]
    text = _view_text(view)
    assert "Confirm Payment" in text
    assert "big one" in text
    assert view.memo == "big one"

    # Confirming carries the memo through to the ledger.
    confirm_button = _pay_confirm_button(view, "Confirm")
    confirm_inter = _interaction(_member(member_id=500))
    with patch("bot_modules.economy.economy_cog.notify_member", new=AsyncMock()):
        await confirm_button.callback(confirm_inter)
    with open_db(db) as conn:
        import json

        assert json.loads(get_ledger(conn, GUILD_ID, 500, limit=1)[0]["meta"])[
            "memo"
        ] == "big one"


@pytest.mark.asyncio
async def test_shop_view_shield_button_disabled_while_held(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    from bot_modules.economy.shop import SECTION_GAMES

    view = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(),
        section=SECTION_GAMES,
    )
    # On offer and takeable.
    row = _row_for(view, "Streak Shield")
    assert _button(row).disabled is False
    held = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(), shields_held=1,
        section=SECTION_GAMES,
    )
    # Still listed — the table above still shows the row — but it says why it
    # can't be bought again rather than silently doing nothing.
    held_row = _row_for(held, "Streak Shield")
    assert _button(held_row).disabled is True
    held_text = "\n".join(
        c.content for c in held_row.children if isinstance(c, discord.ui.TextDisplay)
    )
    assert "🛡️ held" in held_text


@pytest.mark.asyncio
async def test_buy_shield_debits_and_confirms(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_buy_shield(interaction, _settings(db), _guild_roles())

    assert "ready" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        assert get_streak_shields(conn, GUILD_ID, 500) == 1
        assert get_balance(conn, GUILD_ID, 500) == 100 - 30


@pytest.mark.asyncio
async def test_buy_shield_already_holding_message(ctx, db):
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    await cog.do_buy_shield(
        _interaction(_member(member_id=500)), _settings(db), _guild_roles()
    )
    interaction = _interaction(_member(member_id=500))
    await cog.do_buy_shield(interaction, _settings(db), _guild_roles())
    # Single-charge enforcement is test_purchase_shield_refused_while_holding
    # (service) — this covers the user-facing copy.
    assert "already holding" in interaction.response.send_message.await_args.args[0]


# ── /bank shop: cancel & refund ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_shop_view_shows_refund_button_only_when_something_refundable(ctx, db):
    cog = _make_cog(ctx)
    yours = "yours"
    bare = _ShopView(cog, _settings(db), _guild_roles(), 500, set(), set(), section=yours)
    assert not _has_row(bare, "Cancel & Refund")
    with_rental = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(), section=yours,
        refundable=[
            {
                "id": 1, "perk": "role_color", "state": "active",
                "price": 50, "next_bill_at": time.time() + 604800,
            }
        ],
    )
    assert _has_row(with_rental, "Cancel & Refund")
    with_shield = _ShopView(
        cog, _settings(db), _guild_roles(), 500, set(), set(), shield_price=30,
        section=yours,
    )
    assert _has_row(with_shield, "Cancel & Refund")


def _scoped_view(name, cog, settings):
    """Build one member-scoped view, owned by member 500."""
    from bot_modules.economy.cog import views as mod

    owner, friend = _member(member_id=500), _member(member_id=501)
    guild = _guild_roles()
    return {
        "_RefundPickerView": lambda: mod._RefundPickerView(
            cog, settings, guild, 500, [], 30
        ),
        "_PayConfirmView": lambda: mod._PayConfirmView(
            cog, settings, guild, owner, friend, 250
        ),
        "_GiftConfirmView": lambda: mod._GiftConfirmView(
            cog, settings, guild, owner, friend, "role_color"
        ),
        "_EmojiCancelView": lambda: mod._EmojiCancelView(cog, 7, 500),
        "_RefundConfirmView": lambda: mod._RefundConfirmView(
            cog, settings, guild, 500, "role_color", "Custom Role Color", "back."
        ),
    }[name]()


# Every scoped view routes its ownership guard through _MemberScopedView; the
# refusal copy is per-class, so pin each one. Before the shared base these were
# five hand-rolled interaction_checks and only the picker had a test.
@pytest.mark.parametrize(
    ("view_name", "denial"),
    [
        ("_RefundPickerView", "Open your own shop"),
        ("_PayConfirmView", "This confirmation isn't yours."),
        ("_GiftConfirmView", "This confirmation isn't yours."),
        ("_EmojiCancelView", "This isn't your submission."),
        ("_RefundConfirmView", "This confirmation isn't yours."),
    ],
)
@pytest.mark.asyncio
async def test_scoped_views_reject_other_members(ctx, db, view_name, denial):
    cog = _make_cog(ctx)
    view = _scoped_view(view_name, cog, _settings(db))

    stranger = _interaction(_member(member_id=999))
    assert await view.interaction_check(stranger) is False
    assert denial in stranger.response.send_message.await_args.args[0]

    owner = _interaction(_member(member_id=500))
    assert await view.interaction_check(owner) is True


def test_member_scoped_view_is_an_expiring_view():
    """The inheritance wiring itself: every scoped view gets the shared
    disable-and-edit timeout behaviour, not discord.py's silent default."""
    from bot_modules.economy.cog import views as mod

    assert issubclass(mod._MemberScopedView, ExpiringView)


@pytest.mark.asyncio
async def test_pay_confirm_view_on_timeout_disables_and_edits_its_message(ctx, db):
    cog = _make_cog(ctx)
    view = _scoped_view("_PayConfirmView", cog, _settings(db))
    view.message = AsyncMock(spec=discord.Message)

    await view.on_timeout()

    assert view.children
    assert all(
        item.disabled
        for item in view.children
        if isinstance(item, (discord.ui.Button, discord.ui.Select))
    )
    view.message.edit.assert_awaited_once_with(view=view)


@pytest.mark.asyncio
async def test_turning_the_page_stops_the_view_it_replaces(ctx, db):
    """A page turn swaps in a new view; the old one's timer must die with it.

    discord.py keeps a replaced view's timeout running. Left alive, its
    on_timeout edits a greyed-out copy of the old page back over the live one
    minutes later, and the shop goes dead under a member who is still using
    it. Fails without the stop() in _render_shop.
    """
    _enable(db)
    for n in range(1, 21):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)
    opened = _interaction(_member(member_id=500))

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, opened, "specials")
        old = _shop_view(opened)
        nxt = next(
            b for b in old.walk_children()
            if isinstance(b, discord.ui.Button) and b.label == "▶"
        )
        step = _interaction(_member(member_id=500))
        await nxt.callback(step)

    new = step.response.edit_message.await_args.kwargs["view"]
    assert new is not old
    assert old.is_finished()
    assert not new.is_finished()


@pytest.mark.asyncio
async def test_choosing_a_refund_stops_the_picker_it_replaces(ctx, db):
    """The refund picker is replaced by its confirm gate on the same message,
    so the picker's timer must be stopped or it later greys the gate out.
    Fails without the stop() in show_refund_confirm."""
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    settings, guild = _settings(db), _guild_roles()
    await cog.do_buy_shield(_interaction(_member(member_id=500)), settings, guild)

    picker = _scoped_view("_RefundPickerView", cog, settings)
    from bot_modules.economy.shop_layouts import REFUND_ROW_ID_BASE

    button = picker.find_item(REFUND_ROW_ID_BASE)
    chose = _interaction(_member(member_id=500))
    await button.callback(chose)

    gate = chose.response.edit_message.await_args.kwargs["view"]
    assert "Streak Shield" in _all_text(gate)
    assert picker.is_finished()
    assert not gate.is_finished()


@pytest.mark.asyncio
async def test_refund_picker_previews_prorated_amount(ctx, db):
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    shop = cog._shop_context(GUILD_ID, 500)

    await cog.open_refund_picker(
        interaction, _settings(db), _guild_roles(), shop.refundable, shop.shield_price
    )
    view = interaction.response.send_message.await_args.kwargs["view"]
    sections = _sections_of(view)
    assert len(sections) == 1
    assert "back" in _all_text(view)


@pytest.mark.asyncio
async def test_refund_confirm_revokes_perk_and_confirms(ctx, db):
    # The prorate math and the rental ending now are the rentals service's
    # job — this covers the cog wiring: the perk revoke fires and the picker
    # message is edited with the confirmation.
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500)
    with open_db(db) as conn:
        rid = conn.execute(
            "SELECT id FROM econ_rentals WHERE user_id = 500"
        ).fetchone()["id"]
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.economy.cog.shop.revoke_perk_effect", new=AsyncMock()
    ) as revoke_mock:
        await cog.finalize_refund(
            interaction, _settings(db), _guild_roles(), f"rental:{rid}"
        )
    revoke_mock.assert_awaited_once()
    text = _all_text(interaction.response.edit_message.await_args.kwargs["view"])
    assert "credited back" in text


@pytest.mark.asyncio
async def test_refund_confirm_survives_a_failed_perk_revoke(ctx, db):
    # The refund (money + rental state) already committed by the time
    # revoke_perk_effect runs — a Discord-side failure there must not blow
    # up the interaction and strand the member with no confirmation.
    _enable(db)
    _credit(db, 500, 200)
    _add_rental(db, "role_color", user_id=500)
    with open_db(db) as conn:
        rid = conn.execute(
            "SELECT id FROM econ_rentals WHERE user_id = 500"
        ).fetchone()["id"]
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.economy.cog.shop.revoke_perk_effect",
        new=AsyncMock(side_effect=RuntimeError("discord blew up")),
    ):
        await cog.finalize_refund(
            interaction, _settings(db), _guild_roles(), f"rental:{rid}"
        )
    text = _all_text(interaction.response.edit_message.await_args.kwargs["view"])
    assert "credited back" in text  # still shows success, doesn't raise
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT state FROM econ_rentals WHERE id = ?", (rid,)
        ).fetchone()
        assert row["state"] == "cancelled"  # the refund itself is unaffected


# ── voice-style lease in the shop (sinks round 3, stage 3) ───────────────────


@pytest.mark.asyncio
async def test_rent_voice_style_skips_role_projection(ctx, db):
    # Both dials: pricing the lease no longer implies selling it.
    _enable(db, price_voice_style=30, shop_voice_style_enabled=True)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    interaction.original_response = AsyncMock(return_value=SimpleNamespace(id=555))

    with _patch_projection() as (apply_mock, _r, _n):
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "voice_style")

    apply_mock.assert_not_awaited()  # no personal role involved
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert "Voice Style" in _all_text(view)
    rentals = _live_rentals(db)
    assert len(rentals) == 1 and rentals[0]["perk"] == "voice_style"
    assert rentals[0]["price"] == 30
    # Regression: this send site's return skipped view.message entirely
    # before the fix (voice_style has its own early-return branch in
    # _rent_perk_flow), leaving the receipt's timeout unable to disable it.
    assert view.message is interaction.original_response.return_value


@pytest.mark.asyncio
async def test_gift_voice_style_dark_refused_priced_allowed(ctx, db):
    """Gifting follows the Shop & Perks checkbox, not the price.

    The refusal used to hang off ``price_voice_style <= 0``; it is now the
    generic "is this on sale here" guard, so pricing the lease is no longer
    enough on its own — the box has to be checked.
    """
    _enable(db)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    assert "isn't for sale" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []

    # Priced but still unchecked: the shop doesn't sell it, so neither does
    # the gift route.
    _enable(db, price_voice_style=30)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    assert "isn't for sale" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []

    _enable(db, price_voice_style=30, shop_voice_style_enabled=True)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection() as (apply_mock, _r, _n):
        await _gift(cog, interaction, _member(member_id=900), perk="voice_style")
    apply_mock.assert_not_awaited()  # no role projection for a voice lease
    rentals = _live_rentals(db)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "voice_style"
    assert rentals[0]["user_id"] == 500 and rentals[0]["beneficiary_id"] == 900


@pytest.mark.asyncio
async def test_gift_autocomplete_offers_only_what_the_guild_sells(ctx, db):
    """The wiring reason the perk parameter stopped being a static Choice list.

    ``@app_commands.choices`` is baked in at import and identical in every
    guild, so a switched-off perk would keep appearing in the picker and only
    refuse after someone chose it.
    """
    _enable(db, shop_role_gradient_enabled=False, shop_voice_style_enabled=True)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    choices = await cog._giftable_autocomplete(interaction, "")
    values = [c.value for c in choices]
    assert "role_gradient" not in values
    assert "role_color" in values and "voice_style" in values


@pytest.mark.asyncio
async def test_gift_autocomplete_filters_by_what_was_typed(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    choices = await cog._giftable_autocomplete(interaction, "gradient")
    assert [c.value for c in choices] == ["role_gradient"]


@pytest.mark.asyncio
async def test_gift_refuses_a_perk_that_never_appeared_in_the_picker(ctx, db):
    # Autocomplete is a convenience, not a gate: Discord delivers whatever the
    # client sends, including a hand-typed value.
    _enable(db)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="not_a_perk")
    assert "isn't a perk" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_gift_refused_for_a_perk_the_guild_switched_off(ctx, db):
    _enable(db, shop_role_gradient_enabled=False)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await _gift(cog, interaction, _member(member_id=900), perk="role_gradient")
    assert "isn't for sale" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_rent_refused_for_a_perk_the_guild_switched_off(ctx, db):
    _enable(db, shop_role_color_enabled=False)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    with _patch_projection():
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_color")
    assert "stopped selling" in interaction.response.send_message.await_args.args[0]
    assert _live_rentals(db) == []


@pytest.mark.asyncio
async def test_a_comped_mod_cannot_claim_a_switched_off_perk(ctx, db):
    """The bypass this guards: the comp path skips ``rent_perk`` entirely.

    A comped moderator never reaches the service-layer refusal, so a stale
    shop button would have handed them a perk the server had switched off —
    and moderators are exactly the people looking at a shop embed left open
    while they change the settings.
    """
    _enable(db, mod_perk_comp=True, shop_role_color_enabled=False)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    ctx.is_mod = lambda _i: True
    with _patch_projection() as (apply_mock, _r, _n):
        await cog.do_rent(interaction, _settings(db), _guild_roles(), "role_color")
    assert "stopped selling" in interaction.response.send_message.await_args.args[0]
    apply_mock.assert_not_awaited()  # no role projected either


# ── /bank emoji guards (sinks round 3, stage 4) ──────────────────────────────


@pytest.mark.asyncio
async def test_bank_emoji_rejects_oversize_and_bad_type(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)

    big = MagicMock()
    big.content_type = "image/png"
    big.size = 300 * 1024
    interaction = _interaction(_member(member_id=500))
    await cog.bank_emoji.callback(cog, interaction, big, "party_blob")
    assert "256KB" in interaction.response.send_message.await_args.args[0]

    weird = MagicMock()
    weird.content_type = "image/tiff"
    weird.size = 1024
    interaction = _interaction(_member(member_id=500))
    await cog.bank_emoji.callback(cog, interaction, weird, "party_blob")
    assert "PNG" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_bank_emoji_disabled_at_price_zero(ctx, db):
    _enable(db, price_emoji=0)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await cog.bank_emoji.callback(cog, interaction, None, None)
    assert "isn't enabled" in interaction.response.send_message.await_args.args[0]


# ── weekly raffle in the shop (sinks round 3, stage 5) ───────────────────────


@pytest.mark.asyncio
async def test_buy_raffle_tickets_via_modal_flow(ctx, db):
    _enable(db, raffle_enabled=True)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_buy_raffle_tickets(interaction, _settings(db), "3")

    msg = interaction.response.send_message.await_args.args[0]
    assert "3 ticket(s)" in msg
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 70
        row = conn.execute(
            "SELECT count FROM econ_raffle_tickets WHERE user_id = 500"
        ).fetchone()
    assert row["count"] == 3


@pytest.mark.asyncio
async def test_buy_raffle_tickets_rejects_junk_quantity(ctx, db):
    _enable(db, raffle_enabled=True)
    _credit(db, 500, 100)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))
    await cog.do_buy_raffle_tickets(interaction, _settings(db), "lots")
    assert "whole number" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 100


# ── custom-name perk: renames the role AND the server nickname (#56) ───────────


def test_custom_name_confirmation_variants():
    ok = _custom_name_confirmation("Sir Fluffy", nick_ok=True)
    assert "Sir Fluffy" in ok
    assert "nickname" in ok.lower() and "role" in ok.lower()

    forbidden = _custom_name_confirmation(
        "Sir Fluffy", nick_ok=False, nick_reason=_NICK_FORBIDDEN
    )
    assert forbidden.startswith("Your role name is now **Sir Fluffy**.")
    assert "Manage Nicknames" in forbidden

    plain = _custom_name_confirmation("Sir Fluffy", nick_ok=False)
    assert plain == "Your role name is now **Sir Fluffy**."


# ── role icon: resolving a custom emoji by the short name typed (#89) ─────────


class _FakeEmoji:
    """Just the attributes ``_resolve_guild_emoji`` reads off a real emoji."""

    def __init__(self, name: str, id: int, animated: bool = False):
        self.name, self.id, self.animated = name, id, animated


class _FakeGuild:
    def __init__(self, *emojis: _FakeEmoji):
        self.emojis = list(emojis)


def _emoji_guild() -> _FakeGuild:
    # Mixed-case names are the norm on an emote-heavy server — that's what
    # made this bug hit "some" emojis and not others.
    return _FakeGuild(
        _FakeEmoji("KEKW", 1),
        _FakeEmoji("monkaS", 2),
        _FakeEmoji("peepoHappy", 3),
        _FakeEmoji("pepe", 4),
        _FakeEmoji("dance", 5, animated=True),
    )


@pytest.mark.parametrize(
    "raw, expected_id",
    [
        # The casing the member actually types, vs the server's stored casing.
        (":kekw:", 1),
        (":KEKW:", 1),
        ("KEKW", 1),
        (":monkas:", 2),
        (":monkaS:", 2),
        (":peepohappy:", 3),
        (":PEEPOHAPPY:", 3),
        # Already-lowercase names worked before the fix and must keep working.
        (":pepe:", 4),
        # Animated resolves here; the caller is what refuses it.
        (":DANCE:", 5),
        # The pasted form matches by id regardless of the casing shown.
        ("<:kekw:1>", 1),
        ("<a:DANCE:5>", 5),
        # Whitespace around the input is tolerated.
        ("  :monkas:  ", 2),
    ],
)
def test_resolve_guild_emoji_matches_the_short_name_case_insensitively(
    raw, expected_id
):
    resolved = _resolve_guild_emoji(_emoji_guild(), raw)
    assert resolved is not None, f"{raw!r} should resolve"
    assert resolved.id == expected_id


def test_resolve_guild_emoji_prefers_the_exact_casing_when_both_exist():
    """A server holding both spellings still resolves each one precisely."""
    guild = _FakeGuild(_FakeEmoji("pepe", 10), _FakeEmoji("PEPE", 11))
    assert _resolve_guild_emoji(guild, ":pepe:").id == 10
    assert _resolve_guild_emoji(guild, ":PEPE:").id == 11


@pytest.mark.parametrize("reverse", [False, True])
def test_resolve_guild_emoji_ambiguous_casing_picks_the_oldest_stably(reverse):
    """Matching no casing exactly resolves to the lowest id, whatever the order.

    Discord doesn't enforce emoji-name uniqueness and ``guild.emojis`` arrives
    in gateway-payload order, so an unordered pick would hand the member a
    different icon after a restart or a re-upload.
    """
    emojis = [_FakeEmoji("PEPE", 11), _FakeEmoji("Pepe", 10)]
    if reverse:
        emojis.reverse()
    assert _resolve_guild_emoji(_FakeGuild(*emojis), ":PePe:").id == 10


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ":::",
        ":nothere:",
        "🎉",  # unicode emoji — role icons only take this server's customs
        "<:fromelsewhere:999>",  # another server's custom emoji, by id
    ],
)
def test_resolve_guild_emoji_rejects_what_isnt_this_servers_emoji(raw):
    assert _resolve_guild_emoji(_emoji_guild(), raw) is None


@pytest.mark.asyncio
async def test_set_role_name_also_sets_nickname(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    actor.edit = AsyncMock()
    interaction = _interaction(actor)

    with (
        patch.object(
            cog,
            "_load_role_ctx",
            return_value=(EconSettings(enabled=True), {"role_name": True}),
        ),
        patch.object(cog, "_name_blocklist", return_value=[]),
        patch.object(cog, "_upsert_role"),
        _patch_everywhere(
            "apply_role_perks",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.set_role_name(interaction, "Sir Fluffy")

    actor.edit.assert_awaited_once()
    assert actor.edit.await_args.kwargs.get("nick") == "Sir Fluffy"
    # _apply_and_confirm defers before the slow role-apply, then edits the
    # deferred response — the confirmation lands on edit_original_response.
    msg = interaction.edit_original_response.await_args.kwargs["content"]
    assert "Sir Fluffy" in msg and "nickname" in msg.lower()


@pytest.mark.asyncio
async def test_set_role_name_nick_forbidden_still_renames_role(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    resp = MagicMock(status=403, reason="Forbidden")
    actor.edit = AsyncMock(side_effect=discord.Forbidden(resp, "Missing Permissions"))
    interaction = _interaction(actor)
    upsert = MagicMock()

    with (
        patch.object(
            cog,
            "_load_role_ctx",
            return_value=(EconSettings(enabled=True), {"role_name": True}),
        ),
        patch.object(cog, "_name_blocklist", return_value=[]),
        patch.object(cog, "_upsert_role", upsert),
        _patch_everywhere(
            "apply_role_perks",
            new=AsyncMock(return_value=True),
        ),
    ):
        await cog.set_role_name(interaction, "Sir Fluffy")

    upsert.assert_called_once()  # the role rename still persists
    msg = interaction.edit_original_response.await_args.kwargs["content"]
    assert "Sir Fluffy" in msg
    assert "Manage Nicknames" in msg


# ── Community Bounty (hub panel; /bounty was deleted 2026-07-29) ─────────────


@pytest.mark.asyncio
async def test_bounty_post_posts_card_and_receipt(ctx, db):
    # Escrow/state are the bounty service's job (test_create_escrows_opener_
    # stake) — this covers the cog wiring: the board card posts to the bounty
    # channel and the opener gets a receipt.
    _enable(db, bounty_channel_id=5555, bounty_min_stake=10)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 200, "grant", actor_id=1)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with patch(
        "bot_modules.economy.cog.shop.post_bounty_card", new=AsyncMock()
    ) as card:
        await cog.do_bounty_post(interaction, "Draw the mascot", "as a knight", "50")

    card.assert_awaited_once()
    receipt = interaction.followup.send.await_args.args[0]
    assert "Bounty posted" in receipt


@pytest.mark.asyncio
async def test_bounty_post_bad_stake_rejected(ctx, db):
    _enable(db, bounty_channel_id=5555, bounty_min_stake=10)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 200, "grant", actor_id=1)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.do_bounty_post(interaction, "Draw the mascot", "", "not-a-number")

    interaction.followup.send.assert_awaited()
    assert "whole number" in interaction.followup.send.await_args.args[0]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 200  # nothing charged


@pytest.mark.asyncio
async def test_post_bounty_panel_ignores_the_channel_it_is_handed(ctx, db):
    """The hub's ids are looked up *through* bounty_channel_id, so a hub posted
    anywhere else would be adopted by the restick as if it were on the board.

    That used to be enforced by refusing every other channel, which left the
    dashboard drawing a picker whose only valid answer was the setting sitting
    above it (2026-08-29 audit). The panel owns its destination now, so a
    channel arriving from anywhere — a stale page, a hand-rolled request — is
    ignored rather than obeyed or complained about.
    """
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    board = MagicMock(spec=discord.TextChannel)
    board.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: board if cid == 5555 else None

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ) as place:
        assert await cog.post_bounty_panel(guild, SimpleNamespace(id=6666)) == "msg"

    place.assert_awaited_once_with(guild, board)


@pytest.mark.asyncio
async def test_post_bounty_panel_refuses_a_board_channel_that_has_gone(ctx, db):
    """A board channel deleted since it was set resolves to nothing. Say so —
    place_or_refresh would otherwise be handed None."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: None

    with pytest.raises(ValueError, match="isn't a text channel"):
        await cog.post_bounty_panel(guild)


@pytest.mark.asyncio
async def test_post_bounty_panel_refuses_when_no_board_channel_is_set(ctx, db):
    """bounty_channel_id == 0 is what bounty_enabled() gates the feature on."""
    _enable(db, bounty_channel_id=0)
    cog = _make_cog(ctx)
    guild = FakeGuild(id=GUILD_ID)

    with pytest.raises(ValueError, match="No bounty board channel"):
        await cog.post_bounty_panel(guild)


@pytest.mark.asyncio
async def test_post_bounty_panel_places_it_in_the_board_channel(ctx, db):
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: channel if cid == 5555 else None

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ) as place:
        assert await cog.post_bounty_panel(guild) == "msg"

    place.assert_awaited_once_with(guild, channel)


@pytest.mark.asyncio
async def test_post_bounty_panel_refuses_a_channel_the_casino_hub_holds(ctx, db):
    """Two panels that both chase bot posts can't share a channel: one bottom
    slot, taken from each other on every trigger, so whichever lost is buried.

    Before core.sticky learned to ignore another panel's placement this
    configuration re-posted forever with nobody typing (2026-08-06 review, F1) —
    and prod a second guild had bounty_channel_id ==
    casino_panel_channel_id with the hub simply not posted yet, i.e. one
    dashboard button-press away.
    """
    _enable(db, bounty_channel_id=5555)
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD_ID, "casino_panel_channel_id", "5555"),
        )
    cog = _make_cog(ctx)
    board = MagicMock(spec=discord.TextChannel)
    board.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: board if cid == 5555 else None

    with pytest.raises(ValueError, match="casino hub panel"):
        await cog.post_bounty_panel(guild)


@pytest.mark.asyncio
async def test_post_bounty_panel_allows_a_channel_a_human_only_panel_holds(ctx, db):
    """The shop panel only moves under human messages, so the two trade places
    visibly rather than looping. That is a warning's worth of bad, not a
    refusal's — and refusing here would block a working setup."""
    _enable(db, bounty_channel_id=5555, shop_channel_id=5555)
    cog = _make_cog(ctx)
    board = MagicMock(spec=discord.TextChannel)
    board.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: board if cid == 5555 else None

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ):
        assert await cog.post_bounty_panel(guild) == "msg"


# ── the listener's fast path ─────────────────────────────────────────────────


def test_panel_guilds_lists_only_enabled_guilds_with_a_channel(ctx, db):
    """set_known_guilds is what keeps the on_message listener a set lookup. The
    economy cog was the only migrated cog that never published it, so all five
    of its panels paid a cached id read per message in every guild, forever
    (2026-08-06 review, F6)."""
    _enable(db, guide_channel_id=11, bounty_channel_id=44)
    cog = _make_cog(ctx)

    by_kind = cog._panel_guilds()

    assert by_kind["panel"] == {GUILD_ID}
    assert by_kind["bounty"] == {GUILD_ID}
    assert by_kind["shop"] == set()  # no channel set
    # The retired leaderboard pair is not a panel kind any more, so a guild
    # cannot land on the fast path through it.
    assert "leaderboard" not in by_kind


def test_panel_guilds_excludes_a_guild_with_the_economy_off(ctx, db):
    """A disabled economy makes every panel read (0, 0), so the guild has no
    business on the fast path either."""
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD_ID, {"enabled": False, "guide_channel_id": 11}
        )
    cog = _make_cog(ctx)

    assert cog._panel_guilds()["panel"] == set()


@pytest.mark.asyncio
async def test_publishing_panel_guilds_leaves_the_auction_card_unpublished(ctx, db):
    """The auction card is the one panel that comes and goes. It is posted
    directly rather than through place() — so nothing calls _remember to add its
    guild — and auction_views calls forget() right after posting, which would
    *discard* the guild from a published set and leave the card un-sticky. Its
    TTL cache is the gate."""
    _enable(db, guide_channel_id=11)
    cog = _make_cog(ctx)

    await cog._publish_panel_guilds()

    assert cog.economy_panel._known == {GUILD_ID}
    assert cog.auction_panel._known is None


def test_bounty_panel_ids_are_dark_until_the_board_is_configured(ctx, db):
    """(0, 0) reads as "unposted", so the restick is a no-op — the same
    condition bounty_enabled() gates the whole feature on."""
    cog = _make_cog(ctx)
    posted = {"bounty_panel_channel_id": 5555, "bounty_panel_message_id": 77}

    _enable(db, bounty_channel_id=0, **posted)
    assert cog._bounty_panel_ids(GUILD_ID) == (0, 0)

    _enable(db, enabled=False, bounty_channel_id=5555, **posted)
    assert cog._bounty_panel_ids(GUILD_ID) == (0, 0)

    _enable(db, enabled=True, bounty_channel_id=5555, **posted)
    assert cog._bounty_panel_ids(GUILD_ID) == (5555, 77)


def test_saving_bounty_panel_ids_never_moves_the_board(ctx, db):
    """Only the message id is stored — writing the channel back would let a
    mis-posted hub silently redefine which channel the board is."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)

    cog._save_bounty_panel_ids(GUILD_ID, 9999, 4242)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.bounty_channel_id == 5555
    assert settings.bounty_panel_message_id == 4242


@pytest.mark.asyncio
async def test_refresh_bounty_hub_is_a_no_op_before_the_hub_is_posted(ctx, db):
    """Otherwise place_or_refresh would *create* a hub in a guild that never
    set one up, the first time anybody chipped in."""
    _enable(db, bounty_channel_id=5555, bounty_panel_message_id=0)
    cog = _make_cog(ctx)

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock()
    ) as place:
        await cog.refresh_bounty_hub_panel(FakeGuild(id=GUILD_ID))

    place.assert_not_awaited()


def test_no_bounty_slash_command_survives():
    """The hub panel replaced /bounty — CLAUDE.md's one-panel-over-subcommands."""
    from bot_modules.economy.economy_cog import EconomyCog

    names = {c.name for c in EconomyCog.__cog_app_commands__}
    assert "bounty" not in names


def test_the_bounty_hub_follows_its_own_boards_cards(ctx, db):
    """The hub is the board's only entry point, and the board's own channel is
    where the bot posts cards — so without restick_on_bot a burst of new
    bounties strands the hub above them until a human happens to speak
    (2026-07-29).

    The wiring assertion is the whole change: the burst behaviour it buys is
    covered at the logic layer in test_core_sticky.py. The auction card is the
    contrast — nothing bot-authored lands in its channel while it is live, so
    the flag would buy it nothing.
    """
    cog = _make_cog(ctx)
    assert cog.bounty_panel._restick_on_bot is True
    assert cog.auction_panel._restick_on_bot is False


def test_repointing_the_bounty_board_stops_the_old_hub_being_restuck(ctx, db):
    """Changing bounty_channel_id doesn't touch the panel ids (the dashboard
    save is a partial update). Pairing the old message with the new channel
    would edit-404, post a second hub, and fail to delete the first."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    cog._save_bounty_panel_ids(GUILD_ID, 5555, 4242)
    assert cog._bounty_panel_ids(GUILD_ID) == (5555, 4242)

    _enable(db, bounty_channel_id=6666)  # admin repoints the board

    assert cog._bounty_panel_ids(GUILD_ID) == (0, 0)


@pytest.mark.asyncio
async def test_posting_the_bounty_panel_deletes_a_hub_orphaned_by_a_repoint(ctx, db):
    """An orphaned hub's buttons are static custom_ids, so it stays live —
    Post a bounty on it would file a card into the new board channel."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    cog._save_bounty_panel_ids(GUILD_ID, 5555, 4242)
    _enable(db, bounty_channel_id=6666)

    old_message = MagicMock()
    old_message.delete = AsyncMock()
    old_channel = MagicMock(spec=discord.TextChannel)
    old_channel.get_partial_message.return_value = old_message
    new_channel = MagicMock(spec=discord.TextChannel)
    new_channel.id = 6666
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: old_channel if cid == 5555 else new_channel

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ):
        await cog.post_bounty_panel(guild)

    old_channel.get_partial_message.assert_called_once_with(4242)
    old_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_posting_the_bounty_panel_does_not_delete_a_hub_in_the_current_board(ctx, db):
    """Re-posting into the same channel is a refresh, not a move — deleting
    here would destroy the panel the sticky is about to edit in place."""
    _enable(db, bounty_channel_id=5555)
    cog = _make_cog(ctx)
    cog._save_bounty_panel_ids(GUILD_ID, 5555, 4242)

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 5555
    guild = FakeGuild(id=GUILD_ID)
    guild.get_channel = lambda cid: channel

    with patch.object(
        cog.bounty_panel, "place_or_refresh", new=AsyncMock(return_value="msg")
    ):
        await cog.post_bounty_panel(guild)

    channel.get_partial_message.assert_not_called()
    with open_db(db) as conn:
        assert load_econ_settings(conn, GUILD_ID).bounty_panel_message_id == 4242


# ── custom shop items (glue only — the money is tested in the service) ──


def _store_item(db, **over):
    from bot_modules.economy.shop_items_service import create_item

    fields = {"name": "Shoutout", "price": 100}
    fields.update(over)
    with open_db(db) as conn:
        return create_item(conn, GUILD_ID, **fields)


@pytest.mark.asyncio
async def test_buying_a_role_item_grants_the_role(tmp_path):
    """The one thing the cog owns: telling Discord about a completed sale."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, kind="role", role_id=777, billing="once")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)
    guild = interaction.guild
    role = FakeRole(id=777)
    guild.get_role = MagicMock(return_value=role)
    guild.get_member = MagicMock(return_value=actor)
    actor.roles = []
    actor.add_roles = AsyncMock()

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, guild, item)

    actor.add_roles.assert_awaited_once()
    assert "Bought" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_shop_item_never_grants_an_elevated_role(tmp_path):
    """An item's role is config: one pointed at a mod role sells, but the
    role isn't handed over — it reads as staff work, like a failed grant."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, kind="role", role_id=777, billing="once")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)
    guild = interaction.guild
    role = FakeRole(id=777, permissions=discord.Permissions(ban_members=True))
    guild.get_role = MagicMock(return_value=role)
    guild.get_member = MagicMock(return_value=actor)
    actor.roles = []
    actor.add_roles = AsyncMock()

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, guild, item)

    actor.add_roles.assert_not_awaited()
    assert "couldn't hand you the role" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_a_failed_role_grant_still_keeps_the_purchase(tmp_path):
    """The member paid and the order exists — refusing now would contradict
    a debit that has already been taken, so it reads as staff work instead."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, kind="role", role_id=777, billing="once")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    actor = _member(member_id=500)
    interaction = _interaction(actor)
    guild = interaction.guild
    guild.get_role = MagicMock(return_value=None)  # role deleted since setup
    guild.get_member = MagicMock(return_value=actor)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, guild, item)

    text = interaction.response.send_message.await_args.args[0]
    assert "couldn't hand you the role" in text
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 400


@pytest.mark.asyncio
async def test_buying_a_manual_item_promises_no_time(tmp_path):
    """A human has to do it; inventing an ETA is how a member decides the bot
    is broken an hour later."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    text = interaction.response.send_message.await_args.args[0]
    assert "staff" in text.lower()
    assert "back" in text  # the refund promise


@pytest.mark.asyncio
async def test_an_unaffordable_item_is_refused_without_charging(tmp_path):
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, price=100)

    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 0
        rows = conn.execute("SELECT COUNT(*) AS n FROM econ_shop_purchases").fetchone()
    assert rows["n"] == 0


@pytest.mark.asyncio
async def test_buying_a_staff_item_repaints_the_todo_board(tmp_path):
    """The order lands on the mods' board, so the board has to be told.

    Every other path that adds a todo repaints (/todo, the board's own Add
    button, the dashboard). This one didn't, and the 60s loop is no backstop —
    it only repaints guilds where a recurring chore spawned or was written off.
    A bought order therefore stayed invisible in Discord until somebody
    happened to chat in the board's channel.
    """
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db)
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    todo_cog = MagicMock()
    todo_cog.refresh_board = AsyncMock()
    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    cog.bot.get_cog = MagicMock(
        side_effect=lambda name: todo_cog if name == "TodoCog" else None
    )
    interaction = _interaction(_member(member_id=500))

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    todo_cog.refresh_board.assert_awaited_once_with(GUILD_ID)


@pytest.mark.asyncio
async def test_buying_a_role_item_does_not_touch_the_todo_board(tmp_path):
    """Nothing lands on the board, so nothing needs repainting."""
    db = tmp_path / "econ.db"
    migrated_db(db)
    _enable(db)
    item_id = _store_item(db, kind="role", role_id=777, billing="once")
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 500, 500, "grant")

    todo_cog = MagicMock()
    todo_cog.refresh_board = AsyncMock()
    ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    cog = _make_cog(ctx)
    cog.bot.get_cog = MagicMock(
        side_effect=lambda name: todo_cog if name == "TodoCog" else None
    )
    actor = _member(member_id=500)
    interaction = _interaction(actor)
    interaction.guild.get_role = MagicMock(return_value=MagicMock(spec=discord.Role))
    interaction.guild.get_member = MagicMock(return_value=actor)
    actor.roles = []
    actor.add_roles = AsyncMock()

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
        item = get_item(conn, GUILD_ID, item_id)

    await cog.do_buy_item(interaction, settings, interaction.guild, item)

    todo_cog.refresh_board.assert_not_awaited()


# ── the shop as one paged book ─────────────────────────────────────────────


def _shop_view(interaction):
    return interaction.response.send_message.await_args.kwargs["view"]


def _nav_buttons(view) -> list[tuple[str, bool]]:
    """The shop's ◀ / ▶ pair, in order — empty when the shop is one page."""
    return [
        (b.label, b.disabled)
        for b in view.walk_children()
        if isinstance(b, discord.ui.Button) and b.label in ("◀", "▶")
    ]


@pytest.mark.asyncio
async def test_the_specials_browse_opens_the_store(ctx, db):
    from bot_modules.economy.cog.views import _StoreView

    _enable(db)
    _store_item(db, name="Shoutout")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _shop(cog, interaction, "specials")

    view = _shop_view(interaction)
    assert isinstance(view, _StoreView)
    assert "🎁 Specials" in _all_text(view)


@pytest.mark.asyncio
async def test_a_guild_with_no_store_never_offers_specials(ctx, db):
    """TGM today: no custom items, so the chooser has no Specials row — an
    empty section gets no row rather than a card saying it is empty."""
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_shop.callback(cog, interaction)

    assert "🎁 Specials" not in _all_text(_shop_view(interaction))


@pytest.mark.asyncio
async def test_a_one_page_section_shows_no_arrows(ctx, db):
    """Two dead arrows around the only page there is would be furniture.

    Each button opens one section, so a single page is the usual case —
    here a store of fewer than ten items.
    """
    _enable(db)
    _store_item(db, name="Shoutout")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _shop(cog, interaction, "specials")

    assert _nav_buttons(_shop_view(interaction)) == []


@pytest.mark.asyncio
async def test_the_arrows_step_the_sections(ctx, db):
    """◀ / ▶ re-render the same message on another page of the same
    section, each end disabling rather than wrapping — the guide's
    non-wrapping pager shape. Twenty items is two Specials pages and the
    book ends there: the arrows never cross into the next shelf."""
    _enable(db)
    for n in range(1, 21):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await _shop(cog, interaction, "specials")
        assert _nav_buttons(_shop_view(interaction)) == [("◀", True), ("▶", False)]

        # ▶ steps to the next store page: new rows, same section.
        step = _interaction(_member(member_id=500))
        nxt = next(
            b for b in _shop_view(interaction).walk_children()
            if isinstance(b, discord.ui.Button) and b.label == "▶"
        )
        await nxt.callback(step)

    second = step.response.edit_message.await_args.kwargs["view"]
    assert "Item 11" in _all_text(second)
    assert "Item 01" not in _all_text(second)
    assert _nav_buttons(second) == [("◀", False), ("▶", True)]


@pytest.mark.asyncio
async def test_the_last_page_does_not_wrap(ctx, db):
    """The pager disables at the ends rather than stepping round — a greyed
    arrow reads as "you are at the end", a wrap reads as broken."""
    _enable(db)
    for n in range(1, 21):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        last = _interaction(_member(member_id=500))
        await cog.turn_shop_page(last, 1, section="specials")  # items 11-20
        view = last.response.edit_message.await_args.kwargs["view"]
        assert _nav_buttons(view) == [("◀", False), ("▶", True)]


@pytest.mark.asyncio
async def test_a_big_store_keeps_the_same_two_arrows(ctx, db):
    """Forty-five items is five store pages, still just ◀ / ▶ — no tab row,
    no overflow select, whatever the page count.
    """
    _enable(db)
    for n in range(1, 46):
        _store_item(db, name=f"Item {n:02d}", price=10 * n)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await _shop(cog, interaction, "specials")

    view = _shop_view(interaction)
    assert _nav_buttons(view) == [("◀", True), ("▶", False)]
    assert "Page 1 of " in _all_text(view)


def test_the_registration_views_route_every_panel_id():
    """``cog_load`` registers ``ShopPanelView()`` — a posted panel may show
    any subset of shelves, so it carries every id — and
    ``_LegacyShopOpenView()`` for the single button older panels carry."""
    from bot_modules.economy.cog.views import ShopPanelView, _LegacyShopOpenView
    from bot_modules.economy.shop_layouts import (
        SHOP_HELP_CUSTOM_ID,
        SHOP_PANEL_OPEN_CUSTOM_ID,
        SHOP_SECTION_CUSTOM_IDS,
    )

    def ids(view):
        return {
            b.custom_id for b in view.walk_children()
            if isinstance(b, discord.ui.Button)
        }

    assert ids(ShopPanelView()) == {*SHOP_SECTION_CUSTOM_IDS.values(), SHOP_HELP_CUSTOM_ID}
    assert ids(_LegacyShopOpenView()) == {SHOP_PANEL_OPEN_CUSTOM_ID}


@pytest.mark.parametrize(
    "body, expected",
    [
        pytest.param("# Shop FAQ\n\nRent by the week.", "Rent by the week.", id="doc"),
        pytest.param(None, "no shop FAQ on this server yet", id="no-doc"),
    ],
)
@pytest.mark.asyncio
async def test_panel_help_reads_the_shop_doc_at_click_time(ctx, db, body, expected):
    """The Help answers with the guild's ``shop`` doc as saved now, privately,
    and never pings a mention an admin left in it."""
    from bot_modules.economy.cog.views import ShopPanelView
    from bot_modules.docs.db import create_doc
    from bot_modules.economy.shop_layouts import SHOP_HELP_CUSTOM_ID

    if body is not None:
        with open_db(db) as conn:
            create_doc(conn, GUILD_ID, "shop", "Shop FAQ", body, "", 1, 0.0)
    interaction = _panel_button_interaction(ctx)  # no cog needed
    button = next(
        b for b in ShopPanelView().walk_children()
        if getattr(b, "custom_id", None) == SHOP_HELP_CUSTOM_ID
    )

    await button.callback(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"]
    assert kwargs["allowed_mentions"].everyone is False
    assert expected in _all_text(kwargs["view"])


@pytest.mark.asyncio
async def test_bank_store_is_the_shop_by_a_shorter_name(ctx, db):
    """Same view, same page — a door, not a second shop."""
    from bot_modules.economy.cog.views import _StoreView

    _enable(db)
    _store_item(db, name="Shoutout")
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_store.callback(cog, interaction)

    view = _shop_view(interaction)
    assert isinstance(view, _StoreView)
    assert "🎁 Specials" in _all_text(view)


@pytest.mark.asyncio
async def test_bank_store_refuses_by_name_with_nothing_stocked(ctx, db):
    """Serving the perk ladder here would read as the command being wrong."""
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_store.callback(cog, interaction)

    text = interaction.response.send_message.await_args.args[0]
    assert "hasn't put anything in the store yet" in text


@pytest.mark.asyncio
async def test_bank_store_refuses_when_the_economy_is_off(ctx, db):
    """Same gate as every other member-facing economy surface."""
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    await cog.bank_store.callback(cog, interaction)

    assert "view" not in interaction.response.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_a_page_turn_past_the_end_of_a_shrunken_shop_clamps(ctx, db):
    """An admin can withdraw the last item while a member sits on page 2.

    Trusting the number would hand them an empty page; clamping lands them
    on the section's last page — here its only one.
    """
    from bot_modules.economy.cog.views import _ShopView

    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    with _patch_everywhere(
        "feature_gate_ok",
        new=AsyncMock(return_value=True),
    ):
        await cog.turn_shop_page(interaction, 7, section="cosmetics")

    view = interaction.response.edit_message.await_args.kwargs["view"]
    assert isinstance(view, _ShopView)


@pytest.mark.asyncio
async def test_each_section_carries_only_its_own_controls(ctx, db):
    """A page must not hold a button for a row its own card does not show.

    Otherwise a member taps 🛡️ Shield on the cosmetics page and has to work
    out afterwards what they just bought. The row branch matrix itself
    (which perks appear where) lives in test_shop_layouts.py — this is the
    cog wiring: ``_ShopView`` renders only the rows ``shop_section_rows``
    handed it for that section.
    """
    from bot_modules.economy.shop import SECTION_COSMETICS, SECTION_GAMES

    _enable(db, shop_voice_style_enabled=True)
    cog = _make_cog(ctx)

    def _view(section):
        return _ShopView(
            cog, _settings(db), _guild_roles(), 500, set(), set(),
            has_palette=True, shield_price=250, section=section,
        )

    cosmetics = _view(SECTION_COSMETICS)
    games = _view(SECTION_GAMES)
    assert not _has_row(cosmetics, "Streak Shield")
    assert not _has_row(cosmetics, "Voice Style")
    assert _has_row(games, "Streak Shield")
    assert not _has_row(games, "Custom Role Color")


@pytest.mark.asyncio
async def test_cancel_and_refund_lives_on_your_rentals_only(ctx, db):
    """It ends *any* rental, so it sits on the card that gathers every
    rental — Your Rentals — and on no shelf."""
    from bot_modules.economy.shop import (
        SECTION_COSMETICS, SECTION_GAMES, SECTION_SERVER, SECTION_YOURS,
    )

    _enable(db, shop_voice_style_enabled=True)
    cog = _make_cog(ctx)

    for section in (SECTION_COSMETICS, SECTION_SERVER, SECTION_GAMES, SECTION_YOURS):
        view = _ShopView(
            cog, _settings(db), _guild_roles(), 500, set(), set(),
            has_palette=True, shield_price=250, section=section,
        )
        assert _has_row(view, "Cancel & Refund") is (section == SECTION_YOURS), section


@pytest.mark.asyncio
async def test_the_consumables_are_bought_from_the_shop_not_a_command(ctx, db):
    """A product reachable only by knowing a command's name is one most
    members never find, so the themed day and the paid pin moved into 🏠 Server Features — and their commands went with
    them, rather than leaving two surfaces to drift.
    """
    from bot_modules.economy.economy_cog import EconomyCog
    from bot_modules.economy.shop import SECTION_SERVER

    assert not hasattr(EconomyCog, "bank_theme")
    assert not hasattr(EconomyCog, "bank_sponsor")
    assert not hasattr(EconomyCog, "bank_pin")

    _enable(
        db, shop_voice_style_enabled=True, flash_theme_enabled=True,
        theme_channel_id=42, price_flash_theme=300,
        price_pin_of_day=25, pin_channel_id=43,
    )
    view = _ShopView(
        cog=_make_cog(ctx), settings=_settings(db), guild=_guild_roles(),
        user_id=500, gated=set(), owned=set(), section=SECTION_SERVER,
    )
    for needle in ("Voice Style", "Themed Day", "Pinned Message"):
        assert _has_row(view, needle), needle
    # The sponsored question left the shop with /qotd, which drained its queue.
    assert not _has_row(view, "Question of the Day")


@pytest.mark.asyncio
async def test_choosing_a_consumable_opens_its_modal(ctx, db):
    """Each one still needs the member's words, so the row's button opens
    the box straight from the shop card.
    """
    from bot_modules.economy.cog.views import _PinSubmitModal
    from bot_modules.economy.shop import SECTION_SERVER

    _enable(db, price_pin_of_day=25, pin_channel_id=43)
    view = _ShopView(
        cog=_make_cog(ctx), settings=_settings(db), guild=_guild_roles(),
        user_id=500, gated=set(), owned=set(), section=SECTION_SERVER,
    )
    interaction = _interaction(_member(member_id=500))
    await _button(_row_for(view, "Pinned Message")).callback(interaction)

    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, _PinSubmitModal)


@pytest.mark.asyncio
async def test_a_pin_from_the_shop_still_escrows(ctx, db):
    """The modal path lands in the same handler the command used to call."""
    _enable(db, price_pin_of_day=25, pin_channel_id=43)
    _credit(db, 500, 500)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(member_id=500))

    # The request reaches both review surfaces — a card in the staff-only
    # approvals channel and the mods' todo board — and never the bank channel,
    # which is a member-facing explainer in the main guild while the card names
    # the member and quotes what they wrote.
    with (
        patch(
            "bot_modules.economy.cog.shop.refresh_approvals_board", new=AsyncMock()
        ) as board,
        patch(
            "bot_modules.economy.cog.shop.post_approval_card", new=AsyncMock()
        ) as card,
    ):
        await cog.do_pin_submit(interaction, "Game night Friday!")

    board.assert_awaited_once()
    card.assert_awaited_once()
    assert card.await_args.args[3] == "pin"
    assert "review" in interaction.followup.send.await_args.args[0]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD_ID, 500) == 475
