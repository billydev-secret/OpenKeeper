"""Economy guide — the topics, the card, Help and the dropdown, and the 🔔 toggle.

Also the economy panel's **id wiring**, which lives here rather than with the
board it renders because the ids are the guide's: when the two panels merged on
2026-08-18 the surviving message was the guide's, so ``guide_channel_id`` /
``guide_message_id`` are what the one panel reads and writes. The placement
paths (edit in place, move, a refused post, the disabled gate) come along with
them; the panel's *content* and refresh loops are in
tests/economy/test_economy_leaderboard.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.core.layout import check_budget
from bot_modules.economy.guide import (
    GUIDE_CARD_TIMEOUT_SECONDS,
    GUIDE_TOPIC_CUSTOM_ID,
    HOW_IT_WORKS_CUSTOM_ID,
    NOTIFY_CUSTOM_ID,
    NOTIFY_FAILED_MSG,
    NOTIFY_OFF_MSG,
    NOTIFY_ON_MSG,
    NOTIFY_UNCONFIGURED_MSG,
    PICK_FAILED_MSG,
    TOPIC_GONE_MSG,
    TOPIC_PLACEHOLDER,
    GuideCardView,
    GuideTopic,
    build_guide_layout,
    find_topic,
    guide_topics,
    show_guide,
    show_guide_topic,
    toggle_notify,
    topic_select,
)
from bot_modules.economy.logic import resolve_notify_toggle
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
    save_econ_settings,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
MANAGER_ROLE_ID = 7007
CHANNEL_ID = 111
OTHER_CHANNEL_ID = 222


# ── topics (the guide's content, from live settings) ────────────────────────


def _topics(**kw) -> dict[str, GuideTopic]:
    return {t.key: t for t in guide_topics(EconSettings(**kw))}


def test_four_topics_in_order_on_every_guild():
    # Every optional dial off still leaves four topics that read sensibly:
    # streaks and rentals are unconditional.
    bare = EconSettings(
        xp_per_coin=0.0, transfers_enabled=False, shop_streak_shield_enabled=False,
        booster_multiplier=1.0, demurrage_rate_pct=0,
    )
    for settings in (EconSettings(), bare):
        topics = guide_topics(settings)
        assert [t.key for t in topics] == ["notifications", "earning", "spending", "streaks"]
        for t in topics:
            assert t.gist and t.body
            assert len(t.description) <= 100 and len(t.label) <= 100
    streaks = {t.key: t for t in guide_topics(bare)}["streaks"]
    assert streaks.body.count("\n") == 1  # the streak line and the rentals line
    assert streaks.gist == "come back daily for a growing streak bonus."


def test_earning_quotes_the_live_rates():
    earning = _topics()["earning"]
    # what-pays-what rows are aligned: label in a code cell (padded to the
    # widest row), payment outside it — so match the label and pay separately
    # rather than pin the exact padding, which shifts as rows are added.
    assert "First message of the day" in earning.body
    assert "🪙 5" in earning.body  # text login base
    assert "🪙 15" in earning.body  # voice-first login base
    assert "/bank quests" in earning.body
    assert "🪙 99" in _topics(reward_qotd=99)["earning"].body


@pytest.mark.parametrize(
    "kw, in_body, not_in_body, gist_says_xp",
    [
        # The XP→coin faucet ships off (rate 0): the guide must not promise a
        # nightly conversion that no longer happens; quests are still surfaced.
        pytest.param({}, "/bank quests", "converts into", False, id="faucet-off"),
        pytest.param({"xp_per_coin": 15.0}, "converts into", "a day)", True,
                     id="faucet-on-uncapped"),
        # A ceiling is named, so a heavy day that stops paying isn't a mystery.
        pytest.param({"xp_per_coin": 15.0, "conversion_daily_cap": 1200},
                     "up to 1,200 a day", "the surest way", True, id="faucet-capped"),
    ],
)
def test_earning_conversion_line_gated_on_rate(kw, in_body, not_in_body, gist_says_xp):
    earning = _topics(**kw)["earning"]
    assert in_body in earning.body
    assert not_in_body not in earning.body
    assert ("XP" in earning.gist) is gist_says_xp


@pytest.mark.parametrize("transfers", [True, False])
def test_spending_offers_pay_only_with_transfers(transfers):
    spending = _topics(transfers_enabled=transfers)["spending"]
    assert "/bank shop" in spending.body and "/bank gift" in spending.body
    assert "color, name, gradient, holographic, icon" in spending.body  # named, not priced
    assert "prices in the shop" in spending.body
    assert ("/bank pay" in spending.body) is transfers
    assert ("pay Coins" in spending.gist) is transfers
    assert ("payments" in spending.description) is transfers


@pytest.mark.parametrize(
    "kw, line, gist_bit",
    [
        pytest.param({"shop_streak_shield_enabled": True}, "shield", None, id="shield"),
        pytest.param({"booster_multiplier": 2.0}, "Boosters earn ×2", "boosters earn ×2",
                     id="booster"),
        pytest.param({"demurrage_rate_pct": 3, "demurrage_threshold": 20000},
                     "collects 3% of anything above 20,000 Coins", "hoard tax",
                     id="hoard-tax"),
    ],
)
def test_streaks_fine_print_follows_its_dial(kw, line, gist_bit):
    off = {"shop_streak_shield_enabled": False, "booster_multiplier": 1.0,
           "demurrage_rate_pct": 0}
    without = _topics(**off)["streaks"]
    with_ = _topics(**{**off, **kw})["streaks"]
    assert line not in without.body
    assert line in with_.body
    assert "+10" in with_.body and "grace" in with_.body  # the unconditional lines
    if gist_bit is not None:
        assert gist_bit not in without.gist
        assert gist_bit in with_.gist


def test_notifications_offers_dms_not_channel_access():
    notifications = _topics()["notifications"]
    assert "Notifications" in notifications.body  # names the button to click
    assert "DM" in notifications.body and "DM" in notifications.gist
    # The role is a DM preference, so the guide must not promise access — and
    # must no longer point at the onboarding screen that used to gate it.
    assert "<id:customize>" not in notifications.body
    assert "never what you can see or earn" in notifications.body


def test_topics_use_guild_branding():
    topics = _topics(currency_plural="Gems", currency_emoji="💎")
    assert "💎" in topics["earning"].body  # emoji flows into the earn lines
    assert "Gems" in topics["spending"].body  # plural flows into /bank pay
    assert "Gems" in topics["earning"].gist


def test_find_topic():
    assert find_topic(EconSettings(), "earning").label == "Earning"
    assert find_topic(EconSettings(), "nope") is None


# ── the card and the dropdown ───────────────────────────────────────────────


def _card_text(items) -> str:
    return "\n".join(i.content for i in items if isinstance(i, discord.ui.TextDisplay))


def test_card_overview_lists_every_gist_and_no_body():
    settings = EconSettings(currency_plural="Gems", currency_emoji="💎")
    text = _card_text(build_guide_layout(settings))
    assert text.startswith("## 💎 Gems — How It Works")
    for t in guide_topics(settings):
        assert t.headline in text
        assert t.body not in text


@pytest.mark.parametrize("key", ["notifications", "earning", "spending", "streaks"])
def test_card_at_a_topic_shows_only_that_topic(key):
    settings = EconSettings()
    text = _card_text(build_guide_layout(settings, key))
    picked = find_topic(settings, key)
    assert f"### {picked.emoji} {picked.label}\n{picked.body}" in text
    for other in guide_topics(settings):
        if other.key != key:
            assert other.body not in text


def test_card_with_an_unknown_topic_reads_as_the_overview():
    assert _card_text(build_guide_layout(EconSettings(), "gone")) == _card_text(
        build_guide_layout(EconSettings())
    )


@pytest.mark.parametrize("icon", ["https://cdn.example/gem.png", ""])
def test_card_carries_the_currency_icon_when_set(icon):
    head = build_guide_layout(EconSettings(currency_icon_url=icon))[0]
    if icon:
        assert isinstance(head, discord.ui.Section)
        assert head.accessory.media.url == icon
        assert head.children[0].content.startswith("## 🪙 Coins — How It Works")
    else:
        assert isinstance(head, discord.ui.TextDisplay)


def test_card_is_bare():
    items = build_guide_layout(EconSettings())
    assert not any(isinstance(i, (discord.ui.Container, discord.ui.MediaGallery)) for i in items)


def test_topic_select_offers_every_topic_with_nothing_preselected():
    topics = guide_topics(EconSettings())
    select = topic_select(topics, custom_id=GUIDE_TOPIC_CUSTOM_ID)
    assert select.custom_id == GUIDE_TOPIC_CUSTOM_ID
    assert select.placeholder == TOPIC_PLACEHOLDER
    assert [(o.value, o.label, str(o.emoji)) for o in select.options] == [
        (t.key, t.label, t.emoji) for t in topics
    ]
    assert not any(o.default for o in select.options)
    assert topic_select(topics).custom_id != GUIDE_TOPIC_CUSTOM_ID  # the card's is minted


def test_guide_card_fits_the_budget():
    view = GuideCardView(
        SimpleNamespace(), GUILD_ID,
        EconSettings(xp_per_coin=15.0, conversion_daily_cap=1200,
                     shop_streak_shield_enabled=True, booster_multiplier=2.0,
                     demurrage_rate_pct=3, currency_icon_url="https://cdn.example/gem.png"),
        "earning",
    )
    check_budget(view)
    assert view.timeout == GUIDE_CARD_TIMEOUT_SECONDS


# ── sticky re-stick predicate ────────────────────────────────────────────────

PANEL_CH = 4242
PANEL_MSG = 9999







# ── settings round-trip ─────────────────────────────────────────────────────


def test_guide_ids_round_trip(tmp_path):
    db = tmp_path / "test.db"
    migrated_db(db)
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD_ID, {"guide_channel_id": 123, "guide_message_id": 456}
        )
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.guide_channel_id == 123
    assert settings.guide_message_id == 456


# ── /bank post-guide ────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    def _set(key, value, guild_id=GUILD_ID):
        # core.role_provision persists a provisioned role id through this.
        with open_db(db) as conn:
            set_config_value(conn, key, value, guild_id)
            conn.commit()
        return value

    return SimpleNamespace(
        db_path=db, open_db=lambda: open_db(db), set_config_value=_set
    )


@pytest.fixture(autouse=True)
def _patch_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.economy.economy_cog import EconomyCog

    _bot = MagicMock()
    _bot.ctx = ctx
    return EconomyCog(_bot)


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True}
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _member(*, admin: bool = False, role_ids: tuple[int, ...] = ()) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = 500
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _channel(channel_id: int) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    ch.send = AsyncMock(return_value=MagicMock(id=8888))
    ch.fetch_message = AsyncMock()
    # core.sticky edits/deletes through a partial message (one REST call).
    ch.get_partial_message = MagicMock(return_value=MagicMock(
        edit=AsyncMock(), delete=AsyncMock()
    ))
    return ch


def _interaction(actor, channel, guild=None):
    inter = fake_interaction(guild=guild or FakeGuild(id=GUILD_ID))
    inter.user = actor
    inter.channel = channel
    return inter


async def _post_guide(cog, interaction, channel=None):
    """Post the economy panel.

    /bank post-guide was replaced by a dashboard post control on 2026-07-28,
    and the panel it placed absorbed the leaderboard on 2026-08-18, so this
    drives the one cog method the route calls. ``interaction`` is kept in the
    signature to leave the call sites alone — only its guild is used now, since
    permission is the route's job.
    """
    return await cog.post_economy_panel(
        interaction.guild, channel or interaction.channel
    )


def _stored(db) -> tuple[int, int]:
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    return s.guide_channel_id, s.guide_message_id


# ── sticky repost ────────────────────────────────────────────────────────────


# The sticky machinery itself (debounce, cancel-and-rearm, bot-message skip,
# repost-never-creates, id caching) lives in core.sticky and is covered by
# tests/core/test_core_sticky.py. What stays here is the economy-specific wiring:
# which ids the panel reads, and the disabled-economy gate.


@pytest.mark.asyncio
async def test_panel_ids_are_zero_when_the_economy_is_disabled(ctx, db):
    """A disabled economy must read as "no panel", so nothing re-sticks."""
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": "0"})
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "panel") == (0, 0)


@pytest.mark.asyncio
async def test_panel_ids_read_the_guide_fields(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "panel") == (CHANNEL_ID, 4444)


def test_save_panel_ids_writes_only_the_guide_fields(ctx, db):
    _enable(db, shop_channel_id=777, shop_message_id=888)
    cog = _make_cog(ctx)
    cog._save_panel_ids(GUILD_ID, "panel", CHANNEL_ID, 4444)
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    assert (s.guide_channel_id, s.guide_message_id) == (CHANNEL_ID, 4444)
    assert (s.shop_channel_id, s.shop_message_id) == (777, 888)  # untouched


@pytest.mark.asyncio
async def test_post_guide_disabled_gate(ctx, db):
    cog = _make_cog(ctx)
    interaction = _interaction(_member(admin=True), _channel(CHANNEL_ID))

    # Domain rule, kept on the cog: no currency, no currency guide, however you
    # got here. It raises rather than returning None so the route can name the
    # actual fix — a bare None surfaces as "Discord rejected the post", which
    # sends the admin to check bot permissions instead of the economy toggle.
    with pytest.raises(ValueError, match="disabled"):
        await _post_guide(cog, interaction)


# "Plain member refused" and "rejects a non-text channel" moved with the
# command: post_economy_panel is unguarded by design, and its only caller is
# POST /api/panels/{key}/post, which is admin-gated and does the channel-type
# check (tests/web/test_panels_routes.py, plus the authz sweep).


@pytest.mark.asyncio
async def test_post_guide_posts_and_saves_ids(ctx, db):
    _enable(
        db,
        currency_plural="Gems",
        currency_emoji="💎",
        manager_role_id=MANAGER_ROLE_ID,
    )
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(role_ids=(MANAGER_ROLE_ID,)), channel)

    await _post_guide(cog, interaction)

    view = channel.send.await_args.kwargs["view"]
    assert isinstance(view, discord.ui.LayoutView)
    assert any(
        "Gems — The Bank" in getattr(i, "content", "") for i in view.walk_children()
    )
    assert _stored(db) == (CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_explicit_channel_overrides_current(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    here, there = _channel(CHANNEL_ID), _channel(OTHER_CHANNEL_ID)
    interaction = _interaction(_member(admin=True), here)

    await _post_guide(cog, interaction, there)

    there.send.assert_awaited_once()
    here.send.assert_not_awaited()
    assert _stored(db) == (OTHER_CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_refreshes_in_place(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    old = MagicMock(edit=AsyncMock(), delete=AsyncMock(), id=4444)
    channel.get_partial_message = MagicMock(return_value=old)
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    # Same channel → edited in place, so the panel doesn't hop to the bottom.
    old.edit.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert _stored(db) == (CHANNEL_ID, 4444)  # ids unchanged


@pytest.mark.asyncio
async def test_post_guide_reposts_when_old_message_gone(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    gone = MagicMock(delete=AsyncMock())
    gone.edit = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    channel.get_partial_message = MagicMock(return_value=gone)
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    channel.send.assert_awaited_once()
    assert _stored(db) == (CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_move_deletes_old_panel(ctx, db):
    _enable(db, guide_channel_id=OTHER_CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    old_channel = _channel(OTHER_CHANNEL_ID)
    old = MagicMock(edit=AsyncMock(), delete=AsyncMock())
    old_channel.get_partial_message = MagicMock(return_value=old)
    guild = FakeGuild(id=GUILD_ID, channels={OTHER_CHANNEL_ID: old_channel})
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(admin=True), channel, guild=guild)

    await _post_guide(cog, interaction)

    old.delete.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert _stored(db) == (CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_forbidden_target(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    interaction = _interaction(_member(admin=True), channel)

    result = await _post_guide(cog, interaction)

    # The cog reports failure by returning None; turning that into a
    # user-facing "missing permission" message is the route's job now
    # (tests/web/test_panels_routes.py).
    assert result is None
    assert _stored(db) == (0, 0)  # nothing saved


# ── notifications toggle ─────────────────────────────────────────────────────


NOTIFY_ROLE_ID = 6060


def test_resolve_notify_toggle_grants_when_member_lacks_the_role():
    assert resolve_notify_toggle(role_id=NOTIFY_ROLE_ID, member_role_ids=set()) == "grant"
    assert (
        resolve_notify_toggle(role_id=NOTIFY_ROLE_ID, member_role_ids={999})
        == "grant"
    )


def test_resolve_notify_toggle_removes_when_member_holds_the_role():
    assert (
        resolve_notify_toggle(
            role_id=NOTIFY_ROLE_ID, member_role_ids={999, NOTIFY_ROLE_ID}
        )
        == "remove"
    )


def test_resolve_notify_toggle_unconfigured_without_a_role():
    # An unset role must not read as "grant" — there is nothing to grant.
    assert resolve_notify_toggle(role_id=0, member_role_ids={999}) == "unconfigured"


def _notify_interaction(db, *, member, guild):
    inter = fake_interaction(guild=guild)
    inter.user = member
    inter.client = MagicMock()
    def _set(key, value, guild_id=GUILD_ID):
        # core.role_provision persists a provisioned role id through this.
        with open_db(db) as conn:
            set_config_value(conn, key, value, guild_id)
            conn.commit()
        return value

    inter.client.ctx = SimpleNamespace(
        db_path=db, open_db=lambda: open_db(db), set_config_value=_set
    )
    return inter


def _notify_member_mock(*, role_ids: tuple[int, ...]) -> MagicMock:
    m = _member(role_ids=role_ids)
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


def _guild_with_role(role_id: int | None, *, permissions: discord.Permissions | None = None):
    guild = FakeGuild(id=GUILD_ID)
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    # A plain ping role unless a test says otherwise; core.role_safety reads these.
    role.managed = False
    role.is_default.return_value = False
    role.permissions = permissions or discord.Permissions.none()
    guild.get_role = MagicMock(return_value=None if role_id is None else role)
    return guild, role


@pytest.mark.asyncio
async def test_notify_button_refuses_an_elevated_role(db):
    from bot_modules.economy.guide import NOTIFY_BLOCKED_MSG

    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, _ = _guild_with_role(
        NOTIFY_ROLE_ID, permissions=discord.Permissions(mention_everyone=True)
    )
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    member.add_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_BLOCKED_MSG


@pytest.mark.asyncio
async def test_notify_button_still_removes_an_elevated_role(db):
    # Only the grant is gated: whoever holds a role that turned dangerous can shed it.
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, _ = _guild_with_role(
        NOTIFY_ROLE_ID, permissions=discord.Permissions(mention_everyone=True)
    )
    member = _notify_member_mock(role_ids=(NOTIFY_ROLE_ID,))
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    member.remove_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_button_grants_role_and_confirms(db):
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, role = _guild_with_role(NOTIFY_ROLE_ID)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    member.add_roles.assert_awaited_once()
    assert member.add_roles.await_args.args[0] is role
    member.remove_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_ON_MSG


@pytest.mark.asyncio
async def test_notify_button_removes_role_when_already_opted_in(db):
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, role = _guild_with_role(NOTIFY_ROLE_ID)
    member = _notify_member_mock(role_ids=(NOTIFY_ROLE_ID,))
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    member.remove_roles.assert_awaited_once()
    assert member.remove_roles.await_args.args[0] is role
    member.add_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_OFF_MSG


@pytest.mark.asyncio
async def test_notify_button_provisions_the_role_when_never_configured(db):
    """The dead end this stage exists to remove.

    Pressing 🔔 on a guild that never set an opt-in role used to tell the
    member to go find an admin. Now the role gets made and granted — this is
    the one place in the feature where "first use" is a member asking for it.
    """
    _enable(db)  # no row at all — never configured
    guild = FakeGuild(id=GUILD_ID)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    made = list(guild.roles)
    assert [r.name for r in made] == ["Economy Notifications"]
    member.add_roles.assert_awaited_once()
    assert member.add_roles.await_args.args[0] is made[0]
    assert inter.response.send_message.await_args.args[0] != NOTIFY_UNCONFIGURED_MSG


@pytest.mark.asyncio
async def test_notify_button_honours_a_stored_none(db):
    """Reversed 2026-09-03 (Billy). This test asserted the opposite before.

    The 2026-08-22 call was that a stored 0 on this one dial is a save artifact
    rather than a decision, so the button provisioned over it. But Economy
    Settings tells the admin "(none)" leaves notifications off, and a
    preference the code does not enforce is the one thing CLAUDE.md rules out
    flatly. The dial is now real: with "(none)" stored, the member is told
    notifications aren't set up here and **no role is created**.

    In production this affects exactly one guild — the
    other two hold real role ids, and a guild with no row at all is
    "never configured" and still gets a role made on the first press.
    """
    _enable(db, game_role_id=0)
    guild = FakeGuild(id=GUILD_ID)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    assert list(guild.roles) == [], "made a role over an explicit (none)"
    member.add_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_UNCONFIGURED_MSG


@pytest.mark.asyncio
async def test_notify_button_remakes_a_deleted_role(db):
    """Configured, but the role was deleted in Discord since.

    Used to be the same dead end as unconfigured. Now it is remade and granted
    — and because an id *was* stored, this counts as a recreate, so the mod
    channel hears about it (the old role's holders are gone).
    """
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild = FakeGuild(id=GUILD_ID)  # role 6060 is not in it
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    made = list(guild.roles)
    assert [r.name for r in made] == ["Economy Notifications"]
    assert made[0].id != NOTIFY_ROLE_ID
    member.add_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_button_reports_a_failed_role_edit(db):
    # Bot's own role sits below the notification role → Discord refuses.
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, _ = _guild_with_role(NOTIFY_ROLE_ID)
    member = _notify_member_mock(role_ids=())
    member.add_roles.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    inter = _notify_interaction(db, member=member, guild=guild)

    await toggle_notify(inter)

    assert inter.response.send_message.await_args.args[0] == NOTIFY_FAILED_MSG


@pytest.mark.asyncio
async def test_notify_button_rejects_a_dm_click(db):
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    inter = fake_interaction(guild=None)
    inter.user = MagicMock(spec=discord.User)  # not a Member
    inter.client = MagicMock()

    await toggle_notify(inter)

    assert "only works in a server" in inter.response.send_message.await_args.args[0]


# The button row's own contract (order, ids, no timeout) lives with the view
# in tests/economy/test_economy_quest_views.py. What is asserted here is the wiring —
# that the panel the cog sends carries the guide's two buttons at all.


@pytest.mark.asyncio
async def test_posting_the_panel_attaches_the_guide_buttons(ctx, db):
    """The wiring assertion: the panel the cog actually sends carries them."""
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    view = channel.send.await_args.kwargs["view"]
    ids = [getattr(item, "custom_id", None) for item in view.walk_children()]
    assert HOW_IT_WORKS_CUSTOM_ID in ids
    assert NOTIFY_CUSTOM_ID in ids


def _guide_inter(ctx, *, values=None):
    inter = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    inter.client = SimpleNamespace(ctx=ctx)
    inter.data = {"values": values} if values is not None else {}
    inter.original_response = AsyncMock(return_value=MagicMock(id=4321))
    return inter


def _sent_card(inter) -> GuideCardView:
    kwargs = inter.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "embed" not in kwargs
    view = kwargs["view"]
    assert isinstance(view, GuideCardView)
    return view


def _view_text(view) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


@pytest.mark.asyncio
async def test_help_opens_the_card_at_its_overview(ctx, db):
    """Help is the topic picker now: every gist and the dropdown, no body —
    privately, and off live settings."""
    _enable(db, currency_plural="Gems", currency_emoji="💎")
    inter = _guide_inter(ctx)

    await show_guide(inter)

    view = _sent_card(inter)
    text = _view_text(view)
    assert "## 💎 Gems — How It Works" in text
    for topic in guide_topics(EconSettings(currency_plural="Gems", currency_emoji="💎")):
        assert topic.headline in text
    assert any(isinstance(i, discord.ui.Select) for i in view.walk_children())
    assert view.message is inter.original_response.return_value  # so it can expire


@pytest.mark.asyncio
async def test_panel_pick_opens_the_card_at_that_topic(ctx, db):
    _enable(db, transfers_enabled=True)
    inter = _guide_inter(ctx, values=["spending"])

    assert await show_guide_topic(inter) is True

    text = _view_text(_sent_card(inter))
    assert "### 🛍️ Spending" in text and "/bank pay" in text
    assert "First message of the day" not in text  # one topic at a time


@pytest.mark.parametrize("values", [["retired-topic"], []])
@pytest.mark.asyncio
async def test_panel_pick_of_a_missing_topic_says_so(ctx, db, values):
    _enable(db)
    inter = _guide_inter(ctx, values=values)

    assert await show_guide_topic(inter) is False

    assert inter.response.send_message.await_args.args[0] == TOPIC_GONE_MSG


@pytest.mark.asyncio
async def test_every_pick_in_the_card_rereads_the_settings(ctx, db):
    """The second and third pick in one card quote the rates as they are
    *now*, not as they were when Help was pressed."""
    _enable(db, reward_qotd=7)
    await show_guide(first := _guide_inter(ctx))
    view = _sent_card(first)

    _enable(db, reward_qotd=42)  # an admin retunes a dial mid-read
    pick = _guide_inter(ctx, values=["earning"])
    await view._on_pick(pick)
    pick.response.edit_message.assert_awaited_once_with(view=view)
    assert "🪙 42" in _view_text(view) and "🪙 7" not in _view_text(view)

    _enable(db, reward_qotd=43)
    again = _guide_inter(ctx, values=["earning"])  # the same topic again
    await view._on_pick(again)
    assert "🪙 43" in _view_text(view)

    back = _guide_inter(ctx, values=["notifications"])
    await view._on_pick(back)
    assert "### 🔔 Notifications" in _view_text(view)
    assert "🪙 43" not in _view_text(view)


@pytest.mark.parametrize("fail", ["read", "edit"])
@pytest.mark.asyncio
async def test_a_failed_pick_says_so_privately_and_keeps_the_card(ctx, db, fail):
    """A pick whose settings read or redraw fails answers with a private ❌
    line, not Discord's bare "This interaction failed"."""
    _enable(db)
    await show_guide(first := _guide_inter(ctx))
    view = _sent_card(first)

    pick = _guide_inter(ctx, values=["earning"])
    if fail == "edit":
        pick.response.edit_message = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "boom")
        )
        await view._on_pick(pick)
    else:
        with patch(
            "bot_modules.economy.guide.load_econ_settings",
            side_effect=RuntimeError("db gone"),
        ):
            await view._on_pick(pick)

    pick.response.send_message.assert_awaited_once_with(PICK_FAILED_MSG, ephemeral=True)
    assert view.topic is None  # still the overview it was showing


@pytest.mark.asyncio
async def test_a_refused_send_is_reported_not_raised(ctx, db):
    _enable(db)
    inter = _guide_inter(ctx, values=["earning"])
    inter.response.send_message = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom")
    )
    assert await show_guide_topic(inter) is False


@pytest.mark.parametrize("route", [show_guide, show_guide_topic])
@pytest.mark.asyncio
async def test_the_guide_outside_a_guild_says_so(ctx, db, route):
    inter = fake_interaction()
    inter.guild = None  # fake_interaction substitutes a guild for None
    inter.client = SimpleNamespace(ctx=ctx)
    inter.data = {"values": ["earning"]}

    await route(inter)

    assert "only works in a server" in inter.response.send_message.await_args.args[0]


# ── the panel dropdown's reset ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_panel_picks_coalesce_into_one_in_place_reset(ctx, db):
    """A persistent select keeps showing a member's pick, and re-picking it
    sends nothing — so a pick schedules one in-place redraw (never a
    restick), and a burst of picks shares it."""
    cog = _make_cog(ctx)
    cog.PANEL_RESET_SECONDS = 0.0
    cog.economy_panel.refresh = AsyncMock(return_value=True)

    cog.schedule_panel_reset(GUILD_ID)
    cog.schedule_panel_reset(GUILD_ID)
    await asyncio.gather(*cog._panel_resets.values())

    cog.economy_panel.refresh.assert_awaited_once_with(GUILD_ID, repost_if_missing=False)
    assert cog._panel_resets == {}


@pytest.mark.asyncio
async def test_a_failed_reset_is_logged_and_cleared(ctx, db):
    cog = _make_cog(ctx)
    cog.PANEL_RESET_SECONDS = 0.0
    cog.economy_panel.refresh = AsyncMock(side_effect=RuntimeError("boom"))

    cog.schedule_panel_reset(GUILD_ID)
    await asyncio.gather(*cog._panel_resets.values())

    assert cog._panel_resets == {}  # the next pick can schedule again


@pytest.mark.asyncio
async def test_the_panel_dropdown_opens_the_topic_then_resets(ctx, db):
    from bot_modules.economy.cog.views import EconomyPanelView

    _enable(db)
    cog = _make_cog(ctx)
    cog.schedule_panel_reset = MagicMock()
    select = next(
        i for i in EconomyPanelView().walk_children()
        if getattr(i, "custom_id", None) == GUIDE_TOPIC_CUSTOM_ID
    )
    inter = _guide_inter(ctx, values=["streaks"])
    inter.client = SimpleNamespace(ctx=ctx, get_cog=lambda name: cog)
    inter.guild_id = GUILD_ID

    await select.callback(inter)

    assert "### 🔥 Streaks & perks" in _view_text(_sent_card(inter))
    cog.schedule_panel_reset.assert_called_once_with(GUILD_ID)
