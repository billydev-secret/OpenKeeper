"""``bot_modules/economy/pin_layouts.py`` — the Components V2 cards for Pin
of the Day: the mod-review card and the public "Pinned by a Member" card.

Follows the pilot's convention (``tests/dm/test_dm_perms_layouts.py``,
``tests/economy/test_quest_layouts.py``): these builders are pure, so a
build-and-walk-the-items assertion covers them the way the embed builders
they replaced were tested. The review card reuses
``approval_layouts.build_review_card_layout`` — its own generic-shape tests
(headings switching on state, the resolved_by/facts " • " join) already live
in ``test_quest_layouts.py`` and are not repeated here; only pin's own
mapping onto that shape is covered.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.economy.pin_layouts import build_pin_live_layout, build_pin_review_layout
from bot_modules.economy.pin_service import MAX_PIN_LEN
from bot_modules.economy.service import EconSettings

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731


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


def _settings(**over) -> EconSettings:
    kwargs: dict = {"currency_emoji": "💎"}
    kwargs.update(over)
    return EconSettings(**kwargs)


def _review_items(state: str, **over):
    kwargs: dict = dict(
        settings=_settings(),
        sponsor_id=1,
        name_fn=NAME_FN,
        message="Movie night is Friday!",
        price=200,
        state=state,
    )
    kwargs.update(over)
    return build_pin_review_layout(**kwargs)


def _live_items(**over):
    kwargs: dict = dict(
        sponsor_id=1, name_fn=NAME_FN, message="Movie night is Friday!",
        expires_at=1_800_000_000,
    )
    kwargs.update(over)
    return build_pin_live_layout(**kwargs)


ALL_CARDS = [
    pytest.param(
        lambda: _review_items("pending", buttons=discord.ui.ActionRow()),
        id="review-pending",
    ),
    pytest.param(
        lambda: _review_items(
            "live", resolver_id=99, expires_at=1_800_000_000
        ),
        id="review-approved",
    ),
    pytest.param(
        lambda: _review_items(
            "denied", resolver_id=99, deny_reason="off-topic", refunded=True
        ),
        id="review-denied",
    ),
    pytest.param(
        lambda: _review_items("expired", refunded=True),
        id="review-expired-unreviewed",
    ),
    pytest.param(
        lambda: _review_items("expired", resolver_id=99, refunded=False),
        id="review-expired-ran-its-day",
    ),
    pytest.param(
        lambda: _review_items("superseded", resolver_id=99, refunded=False),
        id="review-superseded",
    ),
    pytest.param(lambda: _live_items(), id="live"),
    pytest.param(
        lambda: _live_items(sponsor_id=0), id="live-anonymised-sponsor"
    ),
]


# ── Every card renders, fits, and stays bare ────────────────────────────


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_builds_into_a_layout_view(build):
    """Each builder's items are accepted by a LayoutView and carry text.

    discord.py validates placement on ``add_item`` — a Thumbnail loose at
    top level, or a Button outside a row, raises here rather than as a 400.
    """
    view = _view(build())
    assert view.children, "a card with no items renders as an empty message"
    assert _all_text(build()).strip()
    check_budget(view)


def _lead_heading(items) -> str:
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


def _heading_faults(heading: str) -> list[str]:
    if not heading.startswith("## "):
        return [f"a card-level heading is a `## Title` line: {heading!r}"]
    if len(heading) < 4 or heading[3].isascii():
        return [f"lead the title with the feature's glyph: {heading!r}"]
    return []


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    assert not _heading_faults(_lead_heading(build()))


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_ever_writes_a_raw_mention(build):
    """A card <@id> resolves from the *reading* client's cache — a mod who
    has never seen the sponsor, or a public reader who hasn't cached them,
    would see a bare number."""
    assert "<@" not in _all_text(build())


# ── review card: pin's five states collapse onto three ──────────────────


def test_review_card_pending_names_sponsor_shows_price_and_quotes_message():
    items = _review_items("pending", buttons=discord.ui.ActionRow())
    text = _all_text(items)
    assert "N1" in text
    assert "💎 **200**" in text
    assert "> 📝 Movie night is Friday!" in text
    assert text.startswith("## 📋 Pin Requested")


def test_review_card_message_is_quoted_and_escaped():
    """The submitted pin text is member-typed free text — quoted like a
    reason or a note is (guide § Words), and escaped so it can't inject its
    own markdown. It's also glyph-prefixed so it stays visually distinct
    from the moderator's own unmarked deny_reason_quote that can stack
    right underneath it on the denied-with-reason card."""
    text = _all_text(_review_items("pending", message="*not* a real pin"))
    assert "> 📝 \\*not\\* a real pin" in text


def test_review_card_live_state_uses_the_approved_heading_and_expiry():
    text = _all_text(
        _review_items("live", resolver_id=99, expires_at=1_800_000_000)
    )
    assert text.startswith("## 📌 Pin Approved")
    assert "Approved by **N99**" in text
    assert "Expires <t:1800000000:R>" in text


@pytest.mark.parametrize(
    "state, refunded, heading",
    [
        pytest.param("denied", True, "❌ Pin Declined", id="denied"),
        pytest.param(
            "expired", True, "❌ Pin Declined", id="expired-unreviewed"
        ),
        pytest.param(
            "expired", False, "📌 Pin Ended", id="expired-ran-its-day"
        ),
        pytest.param("superseded", False, "📌 Pin Ended", id="superseded"),
    ],
)
def test_review_card_lapsed_states_use_a_decline_heading_only_when_declined(
    state, refunded, heading
):
    """``denied`` and an unreviewed ``expired`` (refunded) are real
    declines and keep "❌ Pin Declined". A pin that went live and lapsed, or
    lost to a newer approval, is the opposite of a decline — it ran, same as
    an approved one — and reads "📌 Pin Ended" instead, so the heading never
    contradicts the "no refund" facts line printed right below it."""
    text = _all_text(_review_items(state, resolver_id=99, refunded=refunded))
    assert text.startswith(f"## {heading}")


def test_review_card_denied_shows_refund_and_reason():
    text = _all_text(
        _review_items(
            "denied", resolver_id=99, deny_reason="off-topic", refunded=True
        )
    )
    assert "Declined by **N99**" in text
    assert "↩️ 💎 **200**" in text
    assert "refunded" in text
    assert "> off-topic" in text
    # The sponsor's pinned message and the moderator's decline reason stack
    # as two back-to-back `>` blocks — only the sponsor's carries the 📝
    # marker, so a reader can't mistake one author's words for the other's.
    assert "> 📝 Movie night is Friday!" in text
    assert "> 📝 off-topic" not in text


def test_review_card_expired_or_superseded_never_shows_a_refund_unless_true():
    """``expired`` covers both a refunded pending timeout AND an unrefunded
    live pin that ran its day or was pulled early — and ``superseded`` is
    never refunded. Only the row's own ``refunded_at`` (the ``refunded``
    kwarg) may say a refund happened; the card must not assume one from
    state alone (bug: previously always showed "refunded")."""
    for state in ("expired", "superseded"):
        text = _all_text(
            _review_items(state, resolver_id=99, refunded=False)
        )
        assert "refunded" not in text
        assert "no refund" in text


def test_review_card_expired_unreviewed_shows_its_real_refund():
    text = _all_text(_review_items("expired", refunded=True))
    assert "↩️ 💎 **200**" in text
    assert "refunded" in text


def test_review_card_only_an_actual_decline_credits_declined_by():
    """``resolver_id`` on an ``expired``/``superseded`` row is whichever mod
    originally *approved* the pin — ``economy_loop.expire_live_pins`` and
    ``go_live``'s supersede update never touch the column — so the card must
    not say that mod "Declined" a pin they never declined (bug fix)."""
    for state in ("expired", "superseded"):
        text = _all_text(_review_items(state, resolver_id=99, refunded=False))
        assert "Declined by" not in text
        assert "N99" not in text
    # A real decline still credits its resolver.
    text = _all_text(_review_items("denied", resolver_id=99, refunded=True))
    assert "Declined by **N99**" in text


def test_review_card_pending_only_shows_buttons():
    row = discord.ui.ActionRow()
    assert row in _review_items("pending", buttons=row)
    assert row not in _review_items("live", buttons=row, resolver_id=99)
    assert row not in _review_items("denied", buttons=row, resolver_id=99)


def test_review_card_anonymised_sponsor_reads_a_member_not_a_bare_id():
    """A sponsor id of 0 is the GDPR-erasure "detached" case
    (``named_or_anonymous``), not an ordinary unknown member."""
    text = _all_text(_review_items("pending", sponsor_id=0))
    assert "a member" in text
    assert "N0" not in text


# ── public pinned card ────────────────────────────────────────────────


def test_live_card_names_sponsor_quotes_message_and_points_at_the_shop():
    text = _all_text(_live_items())
    assert text.startswith("## 📌 Pinned by a Member")
    assert "> Movie night is Friday!" in text
    assert "N1" in text
    assert "Pin a message of your own for a day" in text
    assert "`/bank shop`" in text


def test_live_card_counts_down_rather_than_stating_a_duration():
    """Guide § Words: 'expires <t:…:R>' stays true whenever it's read; the
    embed this replaces said the now-stale 'up for 24 hours'."""
    text = _all_text(_live_items(expires_at=1_800_000_000))
    assert "Expires <t:1800000000:R>" in text
    assert "24 hours" not in text


def test_live_card_anonymised_sponsor_reads_a_member_not_a_bare_id():
    text = _all_text(_live_items(sponsor_id=0))
    assert "a member" in text
    assert "N0" not in text


# ── worst case ────────────────────────────────────────────────────────


def test_review_card_budget_stays_under_ceiling_at_the_input_caps():
    """The pin text has a hard cap upstream (``MAX_PIN_LEN``,
    ``economy_pin_service``) and the deny-reason modal caps at 400 chars
    (``pin_views._DenyReasonModal``), so a denied card filled to both caps,
    plus a long display name, is the worst case this builder can ever
    actually be asked to render. It still has to clear Discord's 4,000-char
    message-wide ceiling."""
    long_name = lambda uid: "Sponsor " + "Name" * 20  # noqa: E731
    view = _view(
        _review_items(
            "denied",
            name_fn=long_name,
            message="M" * MAX_PIN_LEN,
            deny_reason="R" * 400,
            resolver_id=99,
        )
    )
    check_budget(view)
