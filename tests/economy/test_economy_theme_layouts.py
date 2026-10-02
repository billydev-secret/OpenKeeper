"""``bot_modules/economy/theme_layouts.py`` — the Components V2 cards for
Flash Themes: the review/receipt card and the live announcement.

Follows ``test_quest_layouts.py``'s convention: these builders are pure, so a
build-and-walk-the-items assertion covers them the way the embed builders
they replaced (``render_theme_review_embed``, ``render_theme_live_embed``,
formerly in ``theme_views.py``) were tested in the deleted "card" section of
``test_economy_theme_views.py`` — that file now keeps only the DM receipt,
untouched by this conversion.

The regression this scope exists for carries over unchanged: ``expired`` is
three different endings — a request nobody reviewed in time (refunded), a
theme that ran its whole window, and one a mod ended early (neither
refunded) — so ``refunded`` (read from the row's ``refunded_at``, never
inferred from ``state``) decides the refund line and the "Ended" vs
"Declined" heading.

The live announcement carries the hub banner/footer (Ben, 2026-09-28,
"option B") — its one documented exception to "channel-resident hub panels
only" (``tests/contracts/test_layout_hub_banner.py``'s ``HUB_BANNER_EXCEPTIONS``); the
banner/no-banner states and the worst-case budget with both galleries are
this file's coverage of that.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.economy.theme_layouts import (
    ANNOUNCEMENT_BANNER_TITLE,
    build_theme_live_layout,
    build_theme_review_layout,
)
from bot_modules.economy.service import EconSettings

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731

#: A theme that went live 5 hours into a 24-hour window — fixed so a card's
#: countdown text is a stable, computable string in an assertion.
EXPIRES_AT = 1_800_000_000.0

BANNER_URL = "https://dash.example/static/panel-banners/" + "b" * 32 + ".webp"
FOOTER_URL = "https://dash.example/static/panel-banners/" + "f" * 32 + ".webp"


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


def _galleries(items) -> list[tuple[str, str | None]]:
    return [
        (g.items[0].media.url, g.items[0].description)
        for g in _view(items).walk_children()
        if isinstance(g, discord.ui.MediaGallery)
    ]


def _settings(**over) -> EconSettings:
    kwargs: dict = {
        "currency_emoji": "💎",
        "currency_name": "gem",
        "currency_plural": "gems",
        "theme_hours": 24,
    }
    kwargs.update(over)
    return EconSettings(**kwargs)


def _review_items(state: str, **over):
    kwargs: dict = dict(
        sponsor_id=42,
        name_fn=NAME_FN,
        title="Cursed Cooking",
        blurb="Post the worst thing you have ever eaten.",
        price=300,
        state=state,
    )
    kwargs.update(over)
    return build_theme_review_layout(_settings(), **kwargs)


def _live_items(**over):
    kwargs: dict = dict(
        sponsor_id=42,
        name_fn=NAME_FN,
        title="Cursed Cooking",
        blurb="Post the worst thing you have ever eaten.",
        expires_at=EXPIRES_AT,
    )
    kwargs.update(over)
    return build_theme_live_layout(_settings(), **kwargs)


ALL_CARDS = [
    pytest.param(
        lambda: _review_items("pending", buttons=discord.ui.ActionRow()),
        id="review-pending",
    ),
    pytest.param(
        lambda: _review_items("approved", resolver_id=99),
        id="review-approved",
    ),
    pytest.param(
        lambda: _review_items("live", resolver_id=99),
        id="review-live",
    ),
    pytest.param(
        lambda: _review_items("expired", resolver_id=99, refunded=False),
        id="review-ended-unrefunded",
    ),
    pytest.param(
        lambda: _review_items("expired", refunded=True),
        id="review-expired-nobody-reviewed",
    ),
    pytest.param(
        lambda: _review_items(
            "denied", resolver_id=99,
            deny_reason="too close to last week's", refunded=True,
        ),
        id="review-denied",
    ),
    pytest.param(lambda: _live_items(), id="live-announcement-no-banner"),
    pytest.param(
        lambda: _live_items(sponsor_id=0, blurb=""),
        id="live-announcement-anonymous-no-blurb",
    ),
    pytest.param(
        lambda: _live_items(banner_url=BANNER_URL, footer_url=FOOTER_URL),
        id="live-announcement-with-banner",
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


def test_the_fullest_live_announcement_stays_inside_the_budget():
    """Worst case: both galleries, a long title and a long blurb — the state
    a guild with hub art configured and a chatty sponsor actually produces."""
    view = _view(
        _live_items(
            banner_url=BANNER_URL,
            footer_url=FOOTER_URL,
            title="X" * 200,
            blurb="Y" * 1000,
        )
    )
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
def test_no_card_writes_a_raw_mention(build):
    """A card ``<@id>`` resolves from the *reading* client's cache — a
    moderator who has never seen the sponsor would see a bare number."""
    assert "<@" not in _all_text(build())


# ── review card: state, refund, and heading logic ─────────────────────────


def test_a_theme_that_ran_is_not_shown_as_declined_and_refunded():
    text = _all_text(_review_items("expired", resolver_id=99, refunded=False))
    assert text.startswith("## 🎨 Theme Ended")
    assert "returned" not in text
    assert "It had its day — no refund." in text


def test_a_request_nobody_reviewed_is_shown_as_refunded():
    text = _all_text(_review_items("expired", refunded=True))
    assert text.startswith("## ❌ Theme Declined")
    assert "💎 **300** gems returned" in text


def test_a_declined_theme_still_shows_the_reason_and_the_refund():
    text = _all_text(
        _review_items(
            "denied", resolver_id=99,
            deny_reason="too close to last week's", refunded=True,
        )
    )
    assert text.startswith("## ❌ Theme Declined")
    assert "> too close to last week's" in text
    assert "💎 **300** gems returned" in text
    assert "Declined by **N99**" in text


def test_a_declined_theme_labels_the_idea_so_it_cant_read_as_the_reason():
    """Two unlabelled ``>`` blocks stacked under "Theme Declined" — the
    buyer's pitch, then the mod's reason — read as if the first is *why*
    (guide § Words). The idea quote gets a label; the reason stays the
    card's sole unlabelled quote, since it *is* the answer to "why"."""
    text = _all_text(
        _review_items(
            "denied", resolver_id=99,
            deny_reason="too close to last week's", refunded=True,
        )
    )
    assert "**Their idea**\n> Post the worst thing you have ever eaten." in text
    idea_pos = text.index("Their idea")
    reason_pos = text.index("too close to last week's")
    assert idea_pos < reason_pos


@pytest.mark.parametrize("state", ["approved", "live"])
def test_an_accepted_theme_owes_nothing_back(state):
    text = _all_text(_review_items(state, resolver_id=99, refunded=False))
    assert text.startswith("## 🎨 Theme Approved")
    assert "returned" not in text


def test_a_pending_card_carries_the_theme_and_the_idea():
    text = _all_text(_review_items("pending", buttons=discord.ui.ActionRow()))
    assert text.startswith("## 📋 Theme Requested")
    assert "Cursed Cooking" in text
    assert "> Post the worst thing you have ever eaten." in text


def test_a_live_card_counts_down_to_its_own_expiry_and_names_who_approved_it():
    """The row's own clock, not the configured duration (guide § Words,
    'Count down, don't state') — a mod re-opening this card hours into the
    day must not read "24 hours" as if the day just started."""
    text = _all_text(_review_items("live", resolver_id=99, expires_at=EXPIRES_AT))
    assert f"ends <t:{int(EXPIRES_AT)}:R>" in text
    assert "24 hours" not in text
    assert "Approved by **N99**" in text


def test_a_live_card_with_no_expiry_falls_back_without_a_stale_duration():
    """No ``expires_at`` (a caller that hasn't been handed the row) prints
    nothing claiming a duration rather than falling back to the configured
    window, which is exactly the stale text the rule rules out."""
    text = _all_text(_review_items("live", resolver_id=99))
    assert "24 hours" not in text
    assert "Running — announced and pinned." in text


def test_an_approved_card_says_it_is_queued():
    text = _all_text(_review_items("approved", resolver_id=99))
    assert "Queued" in text
    assert "Approved by **N99**" in text


def test_ended_card_names_who_ended_it():
    text = _all_text(_review_items("expired", resolver_id=99, refunded=False))
    assert "Ended by **N99**" in text


def test_review_card_pending_only_shows_buttons():
    row = discord.ui.ActionRow()
    assert row in _review_items("pending", buttons=row)
    assert row not in _review_items("approved", buttons=row, resolver_id=99)
    assert row not in _review_items(
        "denied", buttons=row, resolver_id=99, refunded=True
    )


def test_review_card_blurb_is_quoted_and_escaped():
    """Member-written blurb is a ``>`` block, escaped so it can't inject its
    own markdown (guide § Words)."""
    text = _all_text(_review_items("pending", blurb="*not* real"))
    assert "> \\*not\\* real" in text


def test_review_card_deny_reason_is_quoted_and_escaped():
    text = _all_text(
        _review_items(
            "denied", resolver_id=99, deny_reason="*bogus*", refunded=True
        )
    )
    assert "> \\*bogus\\*" in text


def test_review_card_title_is_escaped_in_the_heading_and_the_body():
    """A member-written title lands unquoted in a ``##`` heading and an
    inline bold — both render markdown where an embed title never did, so
    the title gets ``_quote``'s treatment (guide § Words), same as the
    sibling ``blurb``/``deny_reason`` fields."""
    text = _all_text(
        _review_items("pending", title="# *evil* @everyone", buttons=discord.ui.ActionRow())
    )
    assert "\\# \\*evil\\* @everyone" in text
    assert "\n# " not in text and not text.startswith("# ")


def test_review_card_names_sponsor_through_name_fn():
    text = _all_text(_review_items("pending", buttons=discord.ui.ActionRow()))
    assert "N42" in text


def test_review_card_anonymous_sponsor_reads_a_member():
    """A sponsor id of 0 means an erasure detached the row — the card must
    not resolve a name for a member who no longer exists."""
    text = _all_text(_review_items("pending", sponsor_id=0, buttons=discord.ui.ActionRow()))
    assert "a member" in text
    assert "N0" not in text


# ── live announcement ──────────────────────────────────────────────────────


def test_live_announcement_names_the_theme_and_the_sponsor():
    """The member-supplied title is text only — never drawn into art (Ben,
    2026-09-28) — so the ``## 🎨`` heading carries it alone; "Today's Theme"
    lives in the banner image, not the heading text."""
    text = _all_text(_live_items())
    assert text.startswith("## 🎨 Cursed Cooking")
    assert "> Post the worst thing you have ever eaten." in text
    assert "Themed by **N42**." in text


def test_live_announcement_heading_escapes_a_member_written_title():
    """The title sits directly in the ``##`` heading — the one spot an
    unescaped member string would actually change how Discord renders the
    card, not just how it reads (guide § Words)."""
    text = _all_text(_live_items(title="# *evil* @everyone"))
    assert text.startswith("## 🎨 \\# \\*evil\\* @everyone")


def test_live_announcement_counts_down_to_its_own_expiry():
    """Posted once but pinned and read for the whole window, so it states
    when it ends, not how long it runs for (guide § Words, 'Count down,
    don't state')."""
    text = _all_text(_live_items())
    assert f"Ends <t:{int(EXPIRES_AT)}:R>." in text
    assert "24 hours" not in text


def test_live_announcement_carries_no_controls():
    """Paradigm 7 — receipt/notice: no buttons, it is taken down whole when
    the window ends rather than edited in place."""
    view = _view(_live_items())
    assert not any(
        isinstance(item, (discord.ui.ActionRow, discord.ui.Button))
        for item in view.walk_children()
    )


def test_live_announcement_without_a_blurb_has_no_empty_quote_block():
    text = _all_text(_live_items(blurb=""))
    assert "\n> \n" not in text
    assert not text.rstrip("\n").endswith(">")


def test_live_announcement_anonymous_sponsor_reads_a_member():
    text = _all_text(_live_items(sponsor_id=0))
    assert "Themed by **a member**." in text


def test_live_announcement_carries_the_shop_pointer_last():
    text = _all_text(_live_items())
    assert "Buy a themed day of your own" in text.rstrip().splitlines()[-1]


# ── the hub-banner exception ────────────────────────────────────────────


def test_no_banner_configured_adds_no_gallery():
    """Ships dark: a guild that has never uploaded hub art gets exactly the
    card it did before this design existed — no banner, no footer."""
    assert _galleries(_live_items()) == []


def test_a_configured_banner_and_footer_bracket_the_announcement():
    items = _live_items(banner_url=BANNER_URL, footer_url=FOOTER_URL)
    galleries = _galleries(items)
    assert galleries[0] == (BANNER_URL, ANNOUNCEMENT_BANNER_TITLE)
    assert galleries[-1][0] == FOOTER_URL
    view = _view(items)
    # Banner opens, footer closes — the card's own text sits between them.
    assert isinstance(view.children[0], discord.ui.MediaGallery)
    assert isinstance(view.children[-1], discord.ui.MediaGallery)


def test_a_banner_with_no_footer_still_opens_the_card():
    items = _live_items(banner_url=BANNER_URL)
    assert _galleries(items) == [(BANNER_URL, ANNOUNCEMENT_BANNER_TITLE)]


def test_a_footer_with_no_banner_adds_only_the_footer():
    """A guild that configured a footer but never a banner — unlikely from
    the dashboard's own upload flow, but the builder doesn't assume it."""
    from bot_modules.core.layout import HUB_FOOTER_ALT

    items = _live_items(footer_url=FOOTER_URL)
    assert _galleries(items) == [(FOOTER_URL, HUB_FOOTER_ALT)]


def test_the_review_card_never_carries_the_banner():
    """Review cards, DMs and ephemeral replies stay plain — no art (Ben,
    2026-09-28). Only the live announcement is the documented exception."""
    for build in (
        lambda: _review_items("pending", buttons=discord.ui.ActionRow()),
        lambda: _review_items("live", resolver_id=99),
        lambda: _review_items("expired", resolver_id=99, refunded=False),
    ):
        assert _galleries(build()) == []
