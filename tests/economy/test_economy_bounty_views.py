"""Tests for the Bounty Board's wiring (economy/bounty_views.py).

Components V2 since 2026-09-28: what a card or the hub *renders* is now pure
and covered in ``test_economy_bounty_layouts.py``. What's left here is the
wiring those pure functions can't exercise on their own — the persistent
gate a poster now passes (bug #221), the no-contact suppressions on the
award/cancel DMs, and the deliberate no-contact name-degrade on an awarded
card (also bug #221) — plus the one structural fact about the views
themselves: Award/Cancel live only on the card, never on the hub.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.economy.bounty_service import BountyBoardEntry
from bot_modules.economy.service import EconSettings

GUILD = 700
POSTER, STRANGER = 2001, 2002


def _custom_ids(view) -> set[str]:
    return {getattr(item, "custom_id", "") or "" for item in view.walk_children()}


def _settings(**over) -> EconSettings:
    base: dict[str, object] = {
        "enabled": True,
        "bounty_channel_id": 555,
        "bounty_min_stake": 10,
        "bounty_max_open": 3,
        "bounty_expire_days": 14,
        "bounty_rake_pct": 0,
        "currency_plural": "coins",
    }
    base.update(over)
    return EconSettings(**base)  # type: ignore[arg-type]


def _entry(**over) -> BountyBoardEntry:
    base: dict[str, object] = {
        "bounty_id": 1,
        "title": "Draw the mascot",
        "pot": 340,
        "contributors": 4,
        "card_channel_id": 555,
        "card_message_id": 888,
    }
    base.update(over)
    return BountyBoardEntry(**base)  # type: ignore[arg-type]


def _bounty(**over) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 1,
        "state": "open",
        "title": "Draw the mascot",
        "description": "",
        "poster_id": POSTER,
        "winner_id": 0,
        "payout": 0,
        "rake_amount": 0,
        "resolved_at": 0,
    }
    base.update(over)
    return base


# ── Award/Cancel live only on the card, never on the hub ────────────────


def test_award_and_cancel_buttons_live_only_on_the_card_view():
    """The award/cancel DMs link ``interaction.message`` — that must be the card.

    Both handlers take the message the button was clicked on and hand its
    ``jump_url`` to the member they DM. That is only correct while those
    buttons live on ``BountyBoardView``, which ``_refresh_card`` attaches to
    the bounty's own card. The hub is a ``StickyPanel``: it is deleted and
    reposted as chat moves, so routing either button through it would start
    DMing members a permalink that is dead within minutes — the exact failure
    ``_frozen_card_link`` exists to avoid on the auction side.
    """
    from bot_modules.economy.bounty_views import BountyBoardView, BountyHubView

    card = BountyBoardView(
        _settings(), _bounty(), pot=0, contributors=0, name_fn=lambda uid: f"Member {uid}"
    )
    card_ids = _custom_ids(card)
    assert any(cid.startswith("econ_bounty:award:") for cid in card_ids)
    assert any(cid.startswith("econ_bounty:cancel:") for cid in card_ids)

    hub = BountyHubView(_settings(), GUILD, [_entry()], open_total=1)
    hub_ids = _custom_ids(hub)
    assert not any(cid.startswith("econ_bounty:award:") for cid in hub_ids)
    assert not any(cid.startswith("econ_bounty:cancel:") for cid in hub_ids)
    # The hub's own per-row Chip In is a distinct custom_id format from the
    # card's (bounty_layouts.HUB_CHIP_CUSTOM_ID_TEMPLATE vs CHIP_..._TEMPLATE):
    # a click on it has no card message to hand a DM, so it must route
    # differently, not merely look different.
    assert any(cid.startswith("econ_bounty:hub_chip:") for cid in hub_ids)
    assert not any(cid.startswith("econ_bounty:chip:") for cid in hub_ids)


async def test_bounty_card_names_degrade_to_mentions_when_the_lookup_fails(monkeypatch):
    """A bounty repaint runs after an award has already paid out, so a
    known_users hiccup must fall back to <@id> (the shared card_name_fn's
    never-raises guarantee), not abandon the card."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot_modules.economy import bounty_views
    from bot_modules.shared import name_resolver

    monkeypatch.setattr(
        name_resolver, "build_name_fn", AsyncMock(side_effect=RuntimeError("db gone"))
    )
    guild = SimpleNamespace(id=1, get_member=lambda uid: None)
    ctx = SimpleNamespace(db_path="unused")

    name_fn = await bounty_views._card_name_fn(
        ctx, guild, {"poster_id": 11, "winner_id": 22}  # type: ignore[arg-type]
    )

    assert name_fn(11) == "<@11>"
    assert name_fn(22) == "<@22>"


# ── the gate the poster now passes (bug #221) ──────────────────────────


def _make_bounty(conn, poster: int = POSTER) -> int:
    from bot_modules.economy.bounty_service import create_bounty

    return create_bounty(
        conn, _settings(), GUILD, poster,
        title="Draw the mascot", description="", stake=50,
    ).bounty_id


def _member(member_id: int, *, admin: bool = False):
    from unittest.mock import MagicMock

    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.roles = []
    m.guild_permissions = discord.Permissions(administrator=admin)
    return m


@pytest.mark.parametrize(
    ("clicker", "allowed"),
    [
        pytest.param(POSTER, True, id="poster"),
        pytest.param(STRANGER, False, id="stranger"),
    ],
)
async def test_the_card_gate_asks_the_service_who_may_resolve(
    tmp_path, monkeypatch, clicker, allowed
):
    """The wiring: ``_gate_resolve`` loads the row, so the poster gets through.

    The permission matrix itself is the service's (test_economy_bounty_service);
    what can only break here is the view forgetting to look the bounty up and
    falling back to the old manager-only check.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot_modules.core.db_utils import open_db
    from bot_modules.economy import bounty_views
    from bot_modules.economy.bounty_service import create_bounty
    from bot_modules.economy.service import apply_credit
    from tests.db_template import migrated_db

    db = migrated_db(tmp_path / "test.db")
    with open_db(db) as conn:
        apply_credit(conn, GUILD, POSTER, 500, "grant", actor_id=POSTER)
        bounty_id = create_bounty(
            conn, _settings(), GUILD, POSTER,
            title="Draw the mascot", description="", stake=50,
        ).bounty_id

    said = AsyncMock()
    monkeypatch.setattr(bounty_views, "_safe_ephemeral", said)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD),
        user=_member(clicker),
        client=SimpleNamespace(ctx=SimpleNamespace(open_db=lambda: open_db(db))),
    )

    settings = await bounty_views._gate_resolve(interaction, bounty_id)  # type: ignore[arg-type]

    assert (settings is not None) is allowed
    assert said.await_count == (0 if allowed else 1)


@pytest.mark.parametrize(
    ("blocked", "dm_sent"),
    [
        pytest.param(False, True, id="ordinary-pair-gets-the-dm"),
        pytest.param(True, False, id="no-contact-pair-does-not"),
    ],
)
async def test_the_award_handler_consults_the_no_contact_list(
    tmp_path, monkeypatch, blocked, dm_sent
):
    """The wiring: ``_handle_award`` drops the winner's DM for a blocked pair.

    ``should_notify_winner`` itself is the service's (test_economy_bounty_service);
    what can only break here is the handler forgetting to ask it — the ``if``
    guarding ``notify_member`` is the whole enforcement, and nothing else would
    fail if it were deleted. The payout is asserted alongside so the suppression
    can't quietly start costing the winner their coins.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot_modules.core.db_utils import open_db
    from bot_modules.economy import bounty_views
    from bot_modules.economy.bounty_service import create_bounty
    from bot_modules.economy.service import apply_credit, get_balance
    from bot_modules.no_contact.service import add_pair
    from tests.db_template import migrated_db

    winner_id = 2003
    db = migrated_db(tmp_path / "test.db")
    with open_db(db) as conn:
        apply_credit(conn, GUILD, POSTER, 500, "grant", actor_id=POSTER)
        bounty_id = create_bounty(
            conn, _settings(), GUILD, POSTER,
            title="Draw the mascot", description="", stake=50,
        ).bounty_id
    if blocked:
        add_pair(db, GUILD, POSTER, winner_id, created_by=winner_id,
                 protected_user_id=winner_id)

    dm = AsyncMock()
    monkeypatch.setattr(bounty_views, "notify_member", dm)
    monkeypatch.setattr(bounty_views, "refresh_bounty_hub", AsyncMock())
    monkeypatch.setattr(bounty_views, "_safe_ephemeral", AsyncMock())

    winner = _member(winner_id)
    winner.mention = f"<@{winner_id}>"
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD),
        user=_member(POSTER),
        response=SimpleNamespace(defer=AsyncMock()),
        client=SimpleNamespace(
            ctx=SimpleNamespace(open_db=lambda: open_db(db), db_path=db)
        ),
    )

    # card=None: the refresh is a no-op, which keeps the test off Discord.
    await bounty_views._handle_award(interaction, bounty_id, winner, None)  # type: ignore[arg-type]

    assert dm.await_count == (1 if dm_sent else 0)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, winner_id) == 50


async def test_the_award_dm_escapes_the_posters_title(tmp_path, monkeypatch):
    """The title is the poster's member text and the DM goes to a different
    member (the winner) — markdown in it must not render raw in a receipt
    the poster didn't write. build_bounty_card_layout escapes this same DB
    value for the card; this is its DM sibling."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot_modules.core.db_utils import open_db
    from bot_modules.economy import bounty_views
    from bot_modules.economy.bounty_service import create_bounty
    from bot_modules.economy.service import apply_credit
    from tests.db_template import migrated_db

    winner_id = 2005
    db = migrated_db(tmp_path / "test.db")
    with open_db(db) as conn:
        apply_credit(conn, GUILD, POSTER, 500, "grant", actor_id=POSTER)
        bounty_id = create_bounty(
            conn, _settings(), GUILD, POSTER,
            title="**FREE** ||coins|| now", description="", stake=50,
        ).bounty_id

    dm = AsyncMock()
    monkeypatch.setattr(bounty_views, "notify_member", dm)
    monkeypatch.setattr(bounty_views, "refresh_bounty_hub", AsyncMock())
    monkeypatch.setattr(bounty_views, "_safe_ephemeral", AsyncMock())

    winner = _member(winner_id)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD),
        user=_member(POSTER),
        response=SimpleNamespace(defer=AsyncMock()),
        client=SimpleNamespace(
            ctx=SimpleNamespace(open_db=lambda: open_db(db), db_path=db)
        ),
    )

    await bounty_views._handle_award(interaction, bounty_id, winner, None)  # type: ignore[arg-type]

    content = dm.call_args.kwargs["content"]
    assert "**FREE** ||coins|| now" not in content
    assert "\\*\\*FREE\\*\\* \\|\\|coins\\|\\| now" in content


@pytest.mark.parametrize(
    ("blocked", "expected"),
    [
        pytest.param(False, "Poster 2001", id="ordinary-pair-is-named"),
        pytest.param(True, "User 2001", id="no-contact-pair-is-a-bare-id"),
    ],
)
async def test_an_awarded_cards_names_degrade_for_a_no_contact_pair(
    tmp_path, monkeypatch, blocked, expected
):
    """**Deliberate anomaly.** A public card naming a no-contact pair together is
    the association the list exists to prevent, so both names become bare ids —
    the call ``games.guess.layouts.reveal_names`` already makes. This test is the only
    thing standing between that and a cleanup pass "fixing" the numbers.
    """
    from types import SimpleNamespace

    from bot_modules.core.db_utils import open_db
    from bot_modules.economy import bounty_views
    from bot_modules.shared import name_resolver
    from bot_modules.economy.bounty_service import award_bounty, get_bounty
    from bot_modules.economy.service import apply_credit
    from bot_modules.no_contact.service import add_pair
    from tests.db_template import migrated_db

    winner_id = 2003
    db = migrated_db(tmp_path / "test.db")
    with open_db(db) as conn:
        apply_credit(conn, GUILD, POSTER, 500, "grant", actor_id=POSTER)
        bounty_id = _make_bounty(conn)
        award_bounty(
            conn, _settings(), GUILD, bounty_id,
            winner_id=winner_id, resolver_id=POSTER,
        )
        row = get_bounty(conn, bounty_id)
    if blocked:
        add_pair(db, GUILD, POSTER, winner_id, created_by=winner_id,
                 protected_user_id=winner_id)

    async def _names(*_args, **_kwargs):
        return lambda uid: f"Poster {uid}" if uid == POSTER else f"Winner {uid}"

    monkeypatch.setattr(name_resolver, "build_name_fn", _names)
    ctx = SimpleNamespace(open_db=lambda: open_db(db), db_path=db)

    name_fn = await bounty_views._card_name_fn(
        ctx, SimpleNamespace(id=GUILD), row  # type: ignore[arg-type]
    )

    assert name_fn(POSTER) == expected
    assert name_fn(winner_id) == ("User 2003" if blocked else "Winner 2003")


async def test_cancelling_skips_the_refund_dm_to_a_no_contact_contributor(
    tmp_path, monkeypatch
):
    """The wiring: the refund still lands, the note about it does not."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot_modules.core.db_utils import open_db
    from bot_modules.economy import bounty_views
    from bot_modules.economy.bounty_service import contribute
    from bot_modules.economy.service import apply_credit, get_balance
    from bot_modules.no_contact.service import add_pair
    from tests.db_template import migrated_db

    backer = 2003
    db = migrated_db(tmp_path / "test.db")
    with open_db(db) as conn:
        apply_credit(conn, GUILD, POSTER, 500, "grant", actor_id=POSTER)
        apply_credit(conn, GUILD, backer, 500, "grant", actor_id=POSTER)
        bounty_id = _make_bounty(conn)
        contribute(conn, _settings(), GUILD, bounty_id, backer, 40)
    add_pair(db, GUILD, POSTER, backer, created_by=backer, protected_user_id=backer)

    dm = AsyncMock()
    said = AsyncMock()
    monkeypatch.setattr(bounty_views, "notify_member", dm)
    monkeypatch.setattr(bounty_views, "refresh_bounty_hub", AsyncMock())
    monkeypatch.setattr(bounty_views, "_safe_ephemeral", said)
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD),
        user=_member(POSTER),
        message=None,
        response=SimpleNamespace(defer=AsyncMock()),
        client=SimpleNamespace(
            ctx=SimpleNamespace(open_db=lambda: open_db(db), db_path=db)
        ),
    )

    await bounty_views._handle_cancel(interaction, bounty_id)  # type: ignore[arg-type]

    # The poster hears; the member who blocked them does not. Refund notes
    # are queued for the contributor's next batched economy DM, keyed on the
    # poster so a rule added before it goes out still stops it.
    with open_db(db) as conn:
        queued = conn.execute(
            "SELECT user_id, actor_id FROM dm_batch_pending WHERE kind = 'econ_notice'"
        ).fetchall()
    assert [tuple(r) for r in queued] == [(POSTER, POSTER)]
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, backer) == 500
    # And the receipt still counts everyone who was actually refunded.
    assert "2 contributor(s)" in said.await_args_list[-1].args[1]
