"""Tests for the auction card builder
(economy/auction_layouts.build_auction_card_layout) and the sticky card's
lifecycle glue.

The builder is the pure piece of the Discord glue — it turns an auction row
into a Components V2 layout. The Bid flow and settle path are exercised
through the service (test_economy_auction_service). Here we assert each
lifecycle state renders the right heading, currency, and facts off a real
service row, plus the paradigm-8 shape rules: bare (no Container), a Bid
ActionRow only while open, and within budget.

The second half covers what makes the card safe to run on ``core.sticky``: it
is the only sticky panel in the bot that ends, so ``build_auction_panel`` must
refuse a finished auction and ``_freeze_card`` must move the result to the
bottom exactly once.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.core.sticky import StickyPanel
from bot_modules.economy.auction_layouts import build_auction_card_layout
from bot_modules.economy.auction_views import (
    AuctionBidButton,
    _BidModal,
    _announce_settlement,
    _freeze_card,
    _frozen_card_link,
    _handle_bid,
    build_auction_panel,
    _sticky_check,
    start_auction,
)
from bot_modules.economy.auction_service import (
    attach_card,
    cancel_auction,
    card_ids,
    end_auction_now,
    get_auction,
    get_open_auction,
    open_auction,
    place_bid,
)
from bot_modules.economy.service import (
    EconSettings,
    apply_credit,
    save_econ_settings,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD = 900
HOST, A, B = 5001, 5002, 5003
NOW = 1_800_000_000.0

SETTINGS = EconSettings(
    enabled=True, currency_emoji="🪙", currency_name="Coin", currency_plural="Coins",
    auction_min_bid=10, auction_min_increment=5, auction_soft_close_seconds=300,
    auction_max_duration_hours=168,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


def _open(conn, **kw):
    return open_auction(
        conn, SETTINGS, GUILD, created_by=HOST, title="Founder role",
        description="One-of-a-kind holographic role for a fortnight.",
        duration_hours=48.0, channel_id=1, now=NOW, **kw,
    )


def _name(uid: int) -> str:
    return f"Member {uid}"


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _all_text(items) -> str:
    """Every TextDisplay in the card, however deeply nested, joined."""
    return "\n".join(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _action_rows(items):
    return [item for item in items if isinstance(item, discord.ui.ActionRow)]


def _build(row, *, bids, open_state=False):
    bid_button = AuctionBidButton(int(row["id"])) if open_state else None
    return build_auction_card_layout(
        settings=SETTINGS, auction=row, bids=bids, name_fn=_name, bid_button=bid_button
    )


def test_open_auction_card_shows_opening_bid_and_ends(db):
    with open_db(db) as conn:
        aid = _open(conn)
        row = get_auction(conn, aid)
    items = _build(row, bids=0, open_state=True)
    text = _all_text(items)
    assert "Founder role" in text
    assert "holographic" in text
    # Named, never <@id> — a public card shows digits for an unseen host.
    assert f"Member {HOST}" in text
    # No bids yet → shows the opening floor (min_bid), not a "current bid".
    assert "Opening Bid" in text and "10" in text
    assert "Current Bid" not in text
    # An open auction always carries the Bid button, alone in its own row.
    rows = _action_rows(items)
    assert len(rows) == 1
    assert isinstance(rows[0].children[0], AuctionBidButton)
    assert rows[0].children[0].custom_id == f"econ_auction:bid:{aid}"
    assert not any(isinstance(i, discord.ui.Container) for i in items)


def test_open_auction_card_shows_current_high_bid(db):
    with open_db(db) as conn:
        aid = _open(conn)
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        row = get_auction(conn, aid)
    items = _build(row, bids=1, open_state=True)
    text = _all_text(items)
    assert "Current Bid" in text and "40" in text
    assert f"Member {A}" in text


def test_auction_title_and_description_markdown_is_escaped(db):
    """The title/description are host-authored free text (`/bank auction
    start`) landing in a TextDisplay heading and body line — unlike the old
    embed title, a TextDisplay renders markdown, so a masked link in the
    title would otherwise become a real clickable link on the sticky card."""
    with open_db(db) as conn:
        aid = open_auction(
            conn, SETTINGS, GUILD, created_by=HOST,
            title="Loot Box [Claim your prize](https://phish.example)",
            description="__Click here__ for *free* coins",
            duration_hours=48.0, channel_id=1, now=NOW,
        )
        row = get_auction(conn, aid)
    items = _build(row, bids=0, open_state=True)
    text = _all_text(items)
    # escape_markdown backslash-escapes the syntax characters so Discord
    # renders them literally instead of parsing a link/underline.
    assert "\\[Claim your prize]" in text
    assert "\\_\\_Click here\\_\\_" in text


def test_closed_with_winner_card_is_sold(db):
    with open_db(db) as conn:
        aid = _open(conn)
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        end_auction_now(conn, GUILD, aid, now=NOW + 2)
        row = get_auction(conn, aid)
    items = _build(row, bids=1)
    text = _all_text(items)
    assert "Sold" in text
    assert "Winner" in text and f"Member {A}" in text
    assert "Winning Bid" in text and "40" in text
    # Closed means nothing left to press.
    assert _action_rows(items) == []


def test_closed_with_no_bids_card(db):
    with open_db(db) as conn:
        aid = _open(conn)
        end_auction_now(conn, GUILD, aid, now=NOW + 2)
        row = get_auction(conn, aid)
    items = _build(row, bids=0)
    text = _all_text(items)
    assert "closed" in text.lower()
    assert "Nobody bid" in text
    assert _action_rows(items) == []


def test_cancelled_card_says_refunded(db):
    with open_db(db) as conn:
        aid = _open(conn)
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        cancel_auction(conn, GUILD, aid, resolver_id=HOST, now=NOW + 2)
        row = get_auction(conn, aid)
    items = _build(row, bids=1)
    text = _all_text(items)
    assert "cancelled" in text.lower()
    assert "returned in full" in text
    assert _action_rows(items) == []


def test_open_auction_card_stays_within_budget(db):
    """The fullest open card: description, a high bid, and the Bid row."""
    with open_db(db) as conn:
        aid = _open(conn)
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        row = get_auction(conn, aid)
    items = _build(row, bids=1, open_state=True)
    view = _view(items)
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS


def test_bid_modal_explains_the_mechanic_above_the_amount_field():
    """The mechanics sentence moved off the card into the modal (guide §
    Footers isn't a licensed spot for it); it must land first, ahead of the
    amount field, so a phone reader sees it before typing a number."""
    modal = _BidModal(auction_id=123, card=None)
    assert isinstance(modal.children[0], discord.ui.TextDisplay)
    assert modal.children[0].content == (
        "Outbid? You're refunded instantly. A late bid nudges the clock."
    )
    assert isinstance(modal.children[1], discord.ui.Label)
    assert modal.children[1].component is modal.amount


# ── the sticky card's lifecycle glue ────────────────────────────────────────
#
# The auction card is the only StickyPanel site in the bot that ENDS. These
# cover the two pieces of glue that make that safe: the build callback's
# refusal to render a finished auction (the resurrection guard) and the
# one-shot freeze repost that leaves the result at the bottom.


@pytest.fixture
def bot(db):
    b = MagicMock()
    b.ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    return b


def _guild():
    return cast(discord.Guild, FakeGuild(id=GUILD))


def _text_channel(cid=1):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid
    ch.send = AsyncMock(return_value=SimpleNamespace(id=5555))
    ch.get_partial_message = MagicMock(
        return_value=SimpleNamespace(delete=AsyncMock())
    )
    return ch


# ── host titles escape at the DM / channel-announcement siblings ───────────
#
# build_auction_card_layout escapes the auction title for the persistent
# card; these cover the outbid DM and the settlement announcement + winner
# DM, which read the same DB title but sat unescaped two hundred lines away.


async def test_outbid_dm_escapes_the_hosts_title(bot, db):
    with open_db(db) as conn:
        aid = open_auction(
            conn, SETTINGS, GUILD, created_by=HOST, title="**FREE** ||prize||",
            description="d", duration_hours=48.0, channel_id=1, now=NOW,
        )
        apply_credit(conn, GUILD, A, 100, "grant")
        apply_credit(conn, GUILD, B, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW + 1)
        save_econ_settings(conn, GUILD, SETTINGS.__dict__)

    interaction = fake_interaction(guild=_guild())
    interaction.user = MagicMock(spec=discord.Member, id=B)
    interaction.client = bot

    dm = AsyncMock()
    with patch("bot_modules.economy.auction_views.notify_member", dm):
        # A higher bid than A's 30 (+5 min increment) outbids A, who gets DM'd.
        await _handle_bid(interaction, aid, "60", None)

    content = dm.call_args.kwargs["content"]
    assert "**FREE** ||prize||" not in content
    assert "\\*\\*FREE\\*\\* \\|\\|prize\\|\\|" in content


async def test_settlement_announcement_and_dm_escape_the_hosts_title(bot, db):
    """settled.title is host free text, validated raw and stored raw. Both
    the public settlement post and the winner's DM interpolate it into the
    bot's own ``**...**`` wrapper, so both must escape it — fixed together
    since ``_announce_settlement`` now reads the value once, at the top."""
    channel = _text_channel(1)
    bot.get_channel = MagicMock(return_value=channel)

    with open_db(db) as conn:
        aid = open_auction(
            conn, SETTINGS, GUILD, created_by=HOST, title="**Rare** ||Item||",
            description="d", duration_hours=48.0, channel_id=1, now=NOW,
        )
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        settled = end_auction_now(conn, GUILD, aid, now=NOW + 2)
    assert settled is not None

    dm = AsyncMock()
    with patch("bot_modules.economy.auction_views.notify_member", dm):
        await _announce_settlement(bot, _guild(), settled)

    posted = channel.send.call_args.args[0]
    dm_content = dm.call_args.kwargs["content"]
    for text in (posted, dm_content):
        assert "**Rare** ||Item||" not in text
        assert "\\*\\*Rare\\*\\* \\|\\|Item\\|\\|" in text


async def test_build_auction_panel_renders_the_live_auction(bot, db):
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
    content = await build_auction_panel(bot, _guild())
    assert content is not None
    assert content.layout is not None
    layout_items = list(content.layout.walk_children())
    assert "Founder role" in "\n".join(
        item.content for item in layout_items if isinstance(item, discord.ui.TextDisplay)
    )
    # Open auctions carry the Bid button.
    assert any(isinstance(item, AuctionBidButton) for item in layout_items)


@pytest.mark.parametrize(
    "finish",
    [
        pytest.param("end", id="closed"),
        pytest.param("cancel", id="cancelled"),
    ],
)
async def test_build_auction_panel_refuses_a_finished_auction(bot, db, finish):
    """The resurrection guard, and it is load-bearing rather than defensive.

    card_ids going to (0, 0) stops a restick being *armed*, but one armed just
    before settlement can still be in flight, and _place_locked reads a (0, 0)
    stored id as "not at the bottom" — it would post a fresh card for the
    finished auction. build() runs before send(), so refusing here is what
    makes that interleaving post nothing.
    """
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        if finish == "end":
            end_auction_now(conn, GUILD, aid, now=NOW + 60)
        else:
            cancel_auction(conn, GUILD, aid, resolver_id=HOST, now=NOW + 60)
    assert await build_auction_panel(bot, _guild()) is None


async def test_build_auction_panel_is_none_with_no_auction(bot):
    assert await build_auction_panel(bot, _guild()) is None


async def test_freeze_card_reposts_the_result_and_drops_the_old_card(bot, db):
    """The settlement ping lands below the card, so without this one repost
    the final result is left buried under the announcement of it."""
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        settled = end_auction_now(conn, GUILD, aid, now=NOW + 60)
    assert settled is not None

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    channel.send.assert_awaited_once()
    # The closed card is posted fresh, then the buried one is removed.
    channel.get_partial_message.assert_called_once_with(4242)
    # ...and the new id is recorded, so nothing is left pointing at a dead
    # message (the failure shape behind the casino hub storm).
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 5555
        assert card_ids(conn, GUILD) == (0, 0)  # still dormant


async def test_freeze_card_keeps_the_old_card_when_the_repost_fails(bot, db):
    """Post-before-delete: a failed send must not destroy the working card."""
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        end_auction_now(conn, GUILD, aid, now=NOW + 60)

    channel = _text_channel(cid=1)
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    channel.get_partial_message.assert_not_called()
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 4242  # untouched


async def test_freeze_card_does_nothing_when_no_card_was_ever_posted(bot, db):
    """An auction whose card never posted has nothing to move."""
    with open_db(db) as conn:
        aid = _open(conn)  # no attach_card — message_id stays 0
        end_auction_now(conn, GUILD, aid, now=NOW + 60)
    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)
    channel.send.assert_not_awaited()


async def test_freeze_card_uses_the_current_card_id_not_a_stale_snapshot(bot, db):
    """A restick that placed between the settle claim and the freeze has
    already replaced the card, and the caller's snapshot is a dead id.

    Deleting the snapshot would leave the re-sticked card — rendered while the
    auction was still open, Bid button and all — sitting above the frozen
    result forever. _freeze_card re-reads the row instead.
    """
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)          # the id the caller snapshots
        end_auction_now(conn, GUILD, aid, now=NOW + 60)
        attach_card(conn, aid, 1, 7777)          # ...a restick moved it here

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    # The live card is removed, not the stale snapshot.
    channel.get_partial_message.assert_called_once_with(7777)


async def test_freeze_card_cannot_clobber_the_next_auctions_card(bot, db):
    """A mod may start the next auction while this freeze is mid-send.

    The freeze knows its own auction id, so it must write there — the
    state-blind attach_card_to_latest is only correct on the save_ids path,
    where a guild id is all there is. Writing to "the newest row" here would
    hand the new auction the dead card's ids, and its next restick would
    delete the frozen result and orphan its own card.
    """
    with open_db(db) as conn:
        old_id = _open(conn)
        attach_card(conn, old_id, 1, 4242)
        end_auction_now(conn, GUILD, old_id, now=NOW + 60)

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    # The next auction opens and posts its own card while the freeze runs.
    async def _send(*a, **kw):
        with open_db(db) as conn:
            new_id = open_auction(
                conn, SETTINGS, GUILD, created_by=HOST, title="Next up",
                description="", duration_hours=48.0, channel_id=1, now=NOW + 120,
            )
            attach_card(conn, new_id, 1, 9999)
        return SimpleNamespace(id=5555)

    channel.send = AsyncMock(side_effect=_send)
    await _freeze_card(bot, _guild(), old_id, 1)

    with open_db(db) as conn:
        assert int(get_auction(conn, old_id)["message_id"]) == 5555
        # The new auction keeps its own card, and stays stickable.
        assert card_ids(conn, GUILD) == (1, 9999)


@pytest.mark.parametrize(
    ("channel_kind", "expect", "blocking"),
    [
        pytest.param("thread", "threads", False, id="thread-never-sticks"),
        pytest.param(
            "panel", "stuck to the bottom", False, id="human-only-panel-warns"
        ),
        pytest.param("plain", None, False, id="ordinary-channel-no-warning"),
        # The two residents that follow the bot's own posts. An auction card
        # here is pushed up after every render and never comes back, so the
        # mod is sent elsewhere instead of being handed a broken auction.
        pytest.param(
            "bounty", "another channel", True, id="bounty-board-blocks"
        ),
        pytest.param(
            "casino", "another channel", True, id="casino-hub-blocks"
        ),
    ],
)
async def test_sticky_check_splits_warning_from_refusal(
    bot, db, channel_kind, expect, blocking
):
    """manual.html promises the card stays at the bottom. These are the channels
    where it can't — warned about when the loss is intermittent and visible,
    refused when the resident panel chases bot posts and the card is guaranteed
    to disappear."""
    if channel_kind == "thread":
        channel = MagicMock(spec=discord.Thread)
        channel.id = 77
    else:
        channel = _text_channel(cid=77)
    if channel_kind == "panel":
        with open_db(db) as conn:
            save_econ_settings(conn, GUILD, {"shop_channel_id": 77})
    if channel_kind == "bounty":
        with open_db(db) as conn:
            save_econ_settings(conn, GUILD, {"bounty_channel_id": 77})
    if channel_kind == "casino":
        with open_db(db) as conn:
            conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD, "casino_panel_channel_id", "77"),
            )

    check = await _sticky_check(bot, _guild(), channel)
    if expect is None:
        assert check is None
    else:
        assert check is not None
        assert expect in check.message
        assert check.blocking is blocking


async def test_freeze_card_also_runs_for_a_cancelled_auction(bot, db):
    """Cancel ends the auction too, so the refund notice gets the same
    one-last-move treatment as a sold card."""
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        cancel_auction(conn, GUILD, aid, resolver_id=HOST, now=NOW + 60)

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    posted = channel.send.await_args.kwargs["view"]
    posted_text = "\n".join(
        item.content
        for item in posted.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "cancelled" in posted_text.lower()
    channel.get_partial_message.assert_called_once_with(4242)
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 5555


# ── priming the panel at start ──────────────────────────────────────────────


def _start_interaction(bot, channel):
    member = MagicMock(spec=discord.Member)
    member.id = HOST
    member.guild_permissions = discord.Permissions.all()
    member.roles = []
    inter = fake_interaction(guild=cast("FakeGuild", _guild()), user=member)
    inter.client = bot
    inter.channel = channel
    return inter


async def test_start_auction_refuses_the_bounty_boards_channel(bot, db):
    """The block must land before open_auction, not after.

    The old warning fired once the auction was already committed and the card
    posted, which for a guaranteed-invisible card is the worst of both: escrow
    rules now apply, the single-live-auction slot is consumed, and the mod's only
    way out is /bank auction cancel. Refusing up front costs nothing.
    """
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "enabled": True,
            "auction_min_bid": 10,
            "auction_min_increment": 5,
            "auction_max_duration_hours": 168,
            # The board channel *is* where the hub lives, and the hub follows
            # bot posts down since 2026-07-29.
            "bounty_channel_id": 77,
        })
    channel = _text_channel(cid=77)

    await start_auction(
        _start_interaction(bot, channel),
        title="Founder role", prize="A shiny thing", duration_hours=48.0,
    )

    channel.send.assert_not_awaited()  # no card posted
    with open_db(db) as conn:
        assert get_open_auction(conn, GUILD) is None, (
            "a refused auction must not consume the single-live-auction slot"
        )


async def test_start_auction_still_runs_where_the_clash_only_warns(bot, db):
    """The mirror of the test above: a human-only resident panel is a warning,
    so the auction must still open and post its card."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "enabled": True,
            "auction_min_bid": 10,
            "auction_min_increment": 5,
            "auction_max_duration_hours": 168,
            "shop_channel_id": 77,
        })
    channel = _text_channel(cid=77)
    panel = StickyPanel(
        "econ auction", MagicMock(),
        load_ids=lambda gid: (0, 0),
        save_ids=lambda gid, cid, mid: None,
        build=AsyncMock(),
    )
    bot.get_cog = MagicMock(return_value=SimpleNamespace(auction_panel=panel))

    await start_auction(
        _start_interaction(bot, channel),
        title="Founder role", prize="A shiny thing", duration_hours=48.0,
    )

    channel.send.assert_awaited_once()
    with open_db(db) as conn:
        assert get_open_auction(conn, GUILD) is not None


async def test_start_auction_drops_the_panels_stale_no_card_cache(bot, db):
    """The card is posted here, not through StickyPanel.place, so nothing
    calls ``_remember`` — and ``on_message`` caches "no panel" for five
    minutes off any member message in the guild. Without an explicit
    ``forget`` the brand-new auction simply does not stick until that entry
    lapses, which is the whole feature not working for up to 300s.
    """
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "enabled": True,
            "auction_min_bid": 10,
            "auction_min_increment": 5,
            "auction_max_duration_hours": 168,
        })
    channel = _text_channel(cid=77)
    panel = StickyPanel(
        "econ auction", MagicMock(),
        load_ids=lambda gid: (0, 0),
        save_ids=lambda gid, cid, mid: None,
        build=AsyncMock(),
    )
    # Chat before the auction existed poisons the cache with (0, 0).
    panel._ref[GUILD] = (time.monotonic() + 300.0, 0, 0)
    bot.get_cog = MagicMock(return_value=SimpleNamespace(auction_panel=panel))

    await start_auction(
        _start_interaction(bot, channel),
        title="Founder role", prize="A shiny thing", duration_hours=48.0,
    )

    channel.send.assert_awaited_once()
    assert GUILD not in panel._ref, (
        "start_auction must drop the panel's cached ids, or the new card "
        "will not re-stick until the 300s TTL lapses"
    )
    with open_db(db) as conn:
        assert card_ids(conn, GUILD) == (77, 5555)


# ── the post-freeze card link ──────────────────────────────────────────


async def test_frozen_card_link_reads_the_id_the_freeze_wrote(bot, db):
    """The winner's DM must point at the frozen card, not the deleted one.

    ``_freeze_card`` reposts and deletes, so any id the caller snapshotted
    before it ran is dead. The link has to come from a fresh read.
    """
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)  # the card while bidding was open
        end_auction_now(conn, GUILD, aid, now=NOW + 60)

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)
    await _freeze_card(bot, _guild(), aid, 1)

    with open_db(db) as conn:
        frozen_id = int(get_auction(conn, aid)["message_id"])
    assert frozen_id != 4242, "the freeze should have reposted"

    link = await _frozen_card_link(bot, GUILD, aid)
    assert link == f"\nhttps://discord.com/channels/{GUILD}/1/{frozen_id}"


async def test_frozen_card_link_is_empty_without_a_card(bot, db):
    # An auction whose card never posted has nothing to point at; the DM
    # simply goes out without a link rather than carrying a broken one.
    with open_db(db) as conn:
        aid = _open(conn)  # no attach_card
        end_auction_now(conn, GUILD, aid, now=NOW + 60)
    assert await _frozen_card_link(bot, GUILD, aid) == ""

