"""``bot_modules/economy/bounty_layouts.py`` — the Components V2 cards.

Pure builders, so they're tested the way the embed builders they replaced
were: call one, walk the items, assert on the text. Three things get their
own sections beyond the copy, matching the pilot's own test file
(``test_dm_perms_layouts.py``):

* the **budget** — the hub grows a Section per open bounty, up to
  ``HUB_LIST_LIMIT``;
* the **paradigm shapes** the style guide fixes (paradigm 5 while open,
  7 once resolved; paradigm 6 for the hub's list; no Container anywhere);
* **``name_fn``** — a card names its poster (and, once awarded, its winner)
  through the resolver, never a raw ``<@id>``.

A fourth section covers the hub's own art (Ben, 2026-09-28): the small lobby
strip a hub without a banner of its own now wears, ships dark until a guild
configures ``header_url``.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.economy.bounty_layouts import (
    AWARD_CUSTOM_ID_TEMPLATE,
    CANCEL_CUSTOM_ID_TEMPLATE,
    CHIP_CUSTOM_ID_TEMPLATE,
    HUB_CHIP_CUSTOM_ID_TEMPLATE,
    HUB_POST_CUSTOM_ID,
    HUB_TITLE_CLIP,
    build_bounty_card_layout,
    build_bounty_hub_layout,
)
from bot_modules.economy.bounty_service import BountyBoardEntry
from bot_modules.economy.service import EconSettings

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731

GUILD = 700
POSTER, WINNER = 2001, 2003


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _all_text(items) -> str:
    return "\n".join(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _visible(row_text: str) -> str:
    """A row's on-screen text: bold syntax gone, a masked link reduced to its
    visible label. Mirrors dm_perms's ``_visible_length`` plus the one thing
    this feature's rows have that DM Perms's don't — a jump link."""
    import re

    row_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", row_text)
    return row_text.replace("**", "")


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


def _row(state: str = "open", **over) -> dict:
    base: dict[str, object] = {
        "id": 1,
        "state": state,
        "title": "Draw the mascot",
        "description": "Any medium, any style.",
        "poster_id": POSTER,
        "winner_id": 0,
        "payout": 0,
        "rake_amount": 0,
        "resolved_at": 1_750_000_000,
    }
    base.update(over)
    return base


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


def _card(state: str = "open", **row_over):
    return build_bounty_card_layout(
        _settings(), _row(state, **row_over), pot=1000, contributors=3, name_fn=NAME_FN
    )


def _hub(entries=(), *, open_total=None, **kwargs):
    entries = list(entries)
    settings_keys = {
        "enabled", "bounty_channel_id", "bounty_min_stake", "bounty_max_open",
        "bounty_expire_days", "bounty_rake_pct", "currency_plural",
    }
    settings_over = {k: v for k, v in kwargs.items() if k in settings_keys}
    hub_kwargs = {k: v for k, v in kwargs.items() if k not in settings_keys}
    return build_bounty_hub_layout(
        _settings(**settings_over),
        GUILD,
        entries,
        open_total=open_total if open_total is not None else len(entries),
        **hub_kwargs,
    )


# ── every state builds, fits, stays bare ────────────────────────────────

ALL_CARDS = [
    pytest.param(lambda: _card("open"), id="open"),
    pytest.param(
        lambda: _card("awarded", winner_id=WINNER, payout=900, rake_amount=100),
        id="awarded",
    ),
    pytest.param(lambda: _card("cancelled"), id="cancelled"),
    pytest.param(lambda: _card("expired"), id="expired"),
]


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_state_builds_into_a_layout_view(build):
    view = _view(build())
    assert view.children
    assert _all_text(build()).strip()
    check_budget(view)


def test_hub_builds_into_a_layout_view_empty_and_full():
    for entries in ([], [_entry(), _entry(bounty_id=2)]):
        view = _view(_hub(entries))
        assert view.children
        assert _all_text(_hub(entries)).strip()
        check_budget(view)


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container) for item in _view(build()).walk_children()
    )


def test_hub_wraps_itself_in_no_container():
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(_hub([_entry()])).walk_children()
    )


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_writes_a_raw_mention(build):
    assert "<@" not in _all_text(build())


def test_hub_writes_no_raw_mention():
    assert "<@" not in _all_text(_hub([_entry()]))


def _lead_heading(items) -> str:
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


@pytest.mark.parametrize(
    ("build", "expected_glyph"),
    [
        pytest.param(lambda: _card("open"), "🎯", id="open"),
        pytest.param(
            lambda: _card("awarded", winner_id=WINNER, payout=900), "🏆", id="awarded"
        ),
        pytest.param(lambda: _card("cancelled"), "✖️", id="cancelled"),
        pytest.param(lambda: _card("expired"), "⌛", id="expired"),
    ],
)
def test_every_card_leads_with_its_own_glyph(build, expected_glyph):
    """Guide § Anatomy: a card's own heading is one glyph-led ``## Title``
    line. This feature keeps its own established vocabulary (see the
    module docstring) rather than the repo-wide table — this sweep pins
    that choice per state so a later edit can't drop one silently."""
    heading = _lead_heading(build())
    assert heading.startswith("## ")
    assert expected_glyph in heading


def test_hub_leads_with_its_own_glyph():
    assert _lead_heading(_hub([])).startswith("## 🎯")


# ── the card: poster/winner named, buttons only while open ─────────────


def test_open_card_names_the_poster_and_shows_the_pot():
    text = _all_text(_card("open"))
    assert f"N{POSTER}" in text
    assert "1,000" in text
    assert "3 contributors" in text


def test_open_card_carries_chip_award_cancel_with_the_right_ids():
    items = _card("open")
    rows = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    assert len(rows) == 1
    ids = {getattr(b, "custom_id", None) for b in rows[0].children}
    assert ids == {
        CHIP_CUSTOM_ID_TEMPLATE.format(bounty_id=1),
        AWARD_CUSTOM_ID_TEMPLATE.format(bounty_id=1),
        CANCEL_CUSTOM_ID_TEMPLATE.format(bounty_id=1),
    }


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("awarded", id="awarded"),
        pytest.param("cancelled", id="cancelled"),
        pytest.param("expired", id="expired"),
    ],
)
def test_resolved_card_carries_no_buttons(state):
    items = _card(state, winner_id=WINNER if state == "awarded" else 0)
    assert not any(isinstance(i, discord.ui.ActionRow) for i in items)
    assert not any(
        isinstance(i, discord.ui.Section) and isinstance(i.accessory, discord.ui.Button)
        for i in items
    )


def test_awarded_card_names_the_winner_and_the_payout():
    text = _all_text(_card("awarded", winner_id=WINNER, payout=900, rake_amount=100))
    assert f"N{WINNER}" in text
    assert "900" in text
    assert "House kept 100" in text


def test_awarded_card_omits_the_rake_line_when_there_is_none():
    text = _all_text(_card("awarded", winner_id=WINNER, payout=1000, rake_amount=0))
    assert "House kept" not in text


def test_cancelled_and_expired_cards_promise_the_refund():
    assert "coins back" in _all_text(_card("cancelled"))
    assert "coins back" in _all_text(_card("expired"))


def test_open_cards_rake_line_only_when_a_rake_is_configured():
    off = build_bounty_card_layout(
        _settings(bounty_rake_pct=0), _row("open"), pot=100, contributors=1, name_fn=NAME_FN
    )
    assert "house keeps" not in _all_text(off)

    on = build_bounty_card_layout(
        _settings(bounty_rake_pct=10), _row("open"), pot=100, contributors=1, name_fn=NAME_FN
    )
    assert "10%" in _all_text(on)


def test_card_description_is_shown_but_skipped_when_blank():
    with_desc = _all_text(_card("open", description="Paint it blue."))
    assert "Paint it blue." in with_desc

    no_desc = build_bounty_card_layout(
        _settings(), _row("open", description=""), pot=10, contributors=0, name_fn=NAME_FN
    )
    assert "Paint it blue." not in _all_text(no_desc)


def test_card_description_is_quoted_and_escaped():
    """A poster's description can't forge the card's own generated lines.

    Guards the fix for the finding that a bare, unescaped description let a
    poster write e.g. a fake ``## Bounty Awarded`` heading that reads as the
    card's own voice — same threat class ``dm/perms/logic.py``'s
    ``safe_field_text`` exists for.
    """
    text = _all_text(_card("open", description="## Bounty Awarded — fake\n*win*"))
    assert "> \\## Bounty Awarded — fake" in text
    assert "> \\*win\\*" in text
    assert "*win*" not in text


def test_card_title_is_escaped():
    """A poster's title lands in the card's own ``## `` heading — a Text
    Display, which renders markdown where the pre-conversion embed's title
    field never did. Unescaped, a title like a masked link becomes a live
    deceptive link on the public board card."""
    text = _all_text(_card("open", title="[Click here!](https://evil.example)"))
    assert discord.utils.escape_markdown("[Click here!](https://evil.example)") in text
    assert "## 🎯 Bounty — [Click here!]" not in text


# ── the hub: how-it-works, the list, Post a Bounty ──────────────────────


def test_empty_hub_invites_the_first_post():
    assert "post the first one" in _all_text(_hub([]))


def test_hub_lists_a_bounty_with_its_pot_and_a_jump_link():
    text = _all_text(_hub([_entry()]))
    assert "Draw the mascot" in text
    assert "340" in text
    assert "jump" in text


def test_hub_row_carries_its_own_chip_in_button():
    items = _hub([_entry(bounty_id=42)])
    sections = [i for i in items if isinstance(i, discord.ui.Section)]
    assert len(sections) == 1
    assert getattr(
        sections[0].accessory, "custom_id", None
    ) == HUB_CHIP_CUSTOM_ID_TEMPLATE.format(bounty_id=42)


def test_a_bounty_whose_card_never_posted_is_listed_without_a_link():
    text = _all_text(_hub([_entry(card_channel_id=0, card_message_id=0)]))
    assert "Draw the mascot" in text
    assert "jump" not in text


def test_hub_capped_list_says_how_many_it_is_not_showing():
    text = _all_text(_hub([_entry(), _entry(bounty_id=2)], open_total=9))
    assert "**7** more" in text


def test_hub_uncapped_list_has_no_tail():
    assert "more further up" not in _all_text(_hub([_entry()]))


def test_hub_rake_line_only_when_a_rake_is_configured():
    off = _hub([], bounty_rake_pct=0)
    assert "house keeps" not in _all_text(off)
    on = _hub([], bounty_rake_pct=10)
    assert "10%" in _all_text(on)


def test_hub_refund_promise_names_the_window_and_vanishes_when_expiry_is_off():
    on = _hub([], bounty_expire_days=14)
    blurb = _all_text(on)
    assert "14 days" in blurb
    assert "refunded in full" in blurb

    off = _hub([], bounty_expire_days=0)
    assert "refunded in full" not in _all_text(off)


def test_hub_blurb_uses_the_guild_currency_name():
    assert "doubloons" in _all_text(_hub([], currency_plural="doubloons"))


def test_hub_carries_a_post_a_bounty_button():
    items = _hub([])
    rows = [i for i in items if isinstance(i, discord.ui.ActionRow)]
    assert len(rows) == 1
    assert getattr(rows[0].children[0], "custom_id", None) == HUB_POST_CUSTOM_ID


def test_long_titles_are_clipped_in_the_hub_list_only():
    text = _all_text(_hub([_entry(title="X" * 100)]))
    assert "…" in text
    assert "X" * 100 not in text
    # The card itself still gets the title in full — only the hub row clips.
    full = _all_text(_card("open", title="X" * 100))
    assert "X" * 100 in full


def test_short_titles_are_left_alone_in_the_hub_list():
    assert "Draw the mascot" in _all_text(_hub([_entry(title="Draw the mascot")]))


def test_hub_row_title_is_escaped():
    """Same threat as the card heading (see ``test_card_title_is_escaped``),
    for the hub's per-row Section text: clip first, then escape, so an
    escape backslash inserted right at the clip boundary is never cut in
    half by the ``…`` truncation. The masked link is kept short enough to
    survive ``HUB_TITLE_CLIP`` intact, so the injection is still live going
    into the escape step."""
    title = "[x](http://e.co) — chip in for real"
    text = _all_text(_hub([_entry(title=title)]))
    assert "\\[x](http://e.co)" in text
    assert "**[x](http://e.co)" not in text


# ── budget: the fullest hub this feature can send ───────────────────────


def test_hub_fits_at_its_read_limit():
    """``HUB_LIST_LIMIT`` (economy_bounty_service.py) is 8 — the fullest hub
    a normal read produces. A Section costs 3 components; 8 of them plus the
    blurb, divider, heading and bottom row stays comfortably under 40."""
    entries = [
        _entry(bounty_id=i, title="A very long bounty title indeed"[:24])
        for i in range(8)
    ]
    view = _view(_hub(entries, bounty_rake_pct=10, bounty_expire_days=14))
    check_budget(view)


def test_hub_row_puts_the_pot_on_its_own_line():
    """Ben, 2026-09-28: the title on one line, the pot and jump link on a
    small second line — never the pot trailing the title after a dash."""
    section = next(i for i in _hub([_entry()]) if isinstance(i, discord.ui.Section))
    text = next(
        c.content for c in section.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )
    title_line, detail_line = text.split("\n")
    assert title_line == "**Draw the mascot**"
    assert detail_line.startswith("-# 340 ")
    assert "in the pot" in detail_line and "jump" in detail_line


def test_hub_row_lines_each_fit_a_phone_for_a_realistic_worst_case():
    """The guide's phone rule: ``~45 visible characters`` for a Section row
    that shares its line with a button — per line, now the row has two.
    ``HUB_TITLE_CLIP`` is chosen against the title line; the detail line
    takes a nine-figure pot, a long currency name and the jump label."""
    entry = _entry(title="X" * 100, pot=999_999_999)
    items = _hub([entry], currency_plural="doubloons")
    section = next(i for i in items if isinstance(i, discord.ui.Section))
    text_display = next(
        c for c in section.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )
    for line in text_display.content.split("\n"):
        assert len(_visible(line.removeprefix("-# "))) <= 45


def test_hub_title_clip_constant_is_shorter_than_the_max_title():
    """Sanity: the clip only bites when it needs to."""
    from bot_modules.economy.bounty_service import MAX_TITLE_LEN

    assert HUB_TITLE_CLIP < MAX_TITLE_LEN


# ── the hub's own art: the lobby strip, ships dark (Ben, 2026-09-28) ─────


def test_hub_wears_no_art_by_default():
    """No ``header_url`` — the default, and every guild until one is
    configured — draws nothing and keeps the plain ``## `` heading. Ships
    dark: a merge and even a restart change nothing until an admin uploads
    lobby header art."""
    items = _hub([])
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in items)
    assert _lead_heading(items) == "## 🎯 The Bounty Board"


def test_hub_wears_the_lobby_strip_when_header_art_is_configured():
    """A guild with lobby header art set wears the small strip in place of
    the tall banner (Ben, 2026-09-28: "every channel hub that has no art yet
    wears the small strip"), and the drawn title replaces the ``## `` heading
    exactly like a titled game lobby's does."""
    items = _hub(
        [], header_url="https://cdn.example/header.png",
        footer_url="https://cdn.example/footer.png", title_in_header=True,
    )
    galleries = [i for i in items if isinstance(i, discord.ui.MediaGallery)]
    assert len(galleries) == 2  # header strip + footer
    # The plain heading is gone — the title is drawn into the header instead.
    assert not any(
        isinstance(i, discord.ui.TextDisplay) and i.content.startswith("## 🎯 The Bounty Board")
        for i in items
    )
    # The footer is the very last item, after the Post Bounty row.
    assert isinstance(items[-1], discord.ui.MediaGallery)


def test_hub_header_alone_does_not_draw_a_title_without_the_flag():
    """``title_in_header=False`` (e.g. ``with_lobby_title`` couldn't draw it)
    keeps the plain heading even though art is configured — the same
    fallback ``with_title`` gives a tall-banner hub."""
    items = _hub([], header_url="https://cdn.example/header.png", title_in_header=False)
    assert _lead_heading(items) == "## 🎯 The Bounty Board"


def test_hub_fits_at_its_read_limit_with_art_on():
    """The strip and footer each cost a component too — the fullest hub with
    art configured must still fit."""
    entries = [
        _entry(bounty_id=i, title="A very long bounty title indeed"[:24])
        for i in range(8)
    ]
    view = _view(_hub(
        entries, bounty_rake_pct=10, bounty_expire_days=14,
        header_url="https://cdn.example/header.png",
        footer_url="https://cdn.example/footer.png", title_in_header=True,
    ))
    check_budget(view)
