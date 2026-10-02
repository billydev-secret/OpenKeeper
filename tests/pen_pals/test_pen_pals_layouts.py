"""Tests for ``bot_modules/pen_pals/layouts.py`` — the Components V2 cards
that replaced the embeds and plain-text panels built inline in the cog.

The builders are pure: primitives in, ``discord.ui.Item`` lists out, so every
card is asserted on its text with no Discord mocks. Budget is checked at the
worst case the feature can really produce: a blocklist past the select's 25
options with 32-character names, and an opening message at the dashboard's
1,000-character cap.
"""

from __future__ import annotations

import pathlib
from typing import Any

import discord
import pytest

from bot_modules.core.layout import (
    MAX_ACCESSORY_LABEL,
    card,
    check_budget,
    layout_budget,
    text_columns,
)
from bot_modules.pen_pals import layouts
from bot_modules.pen_pals.layouts import (
    BLOCK_ADD_ID,
    BLOCK_DONE_ID,
    BLOCK_REMOVE_ID,
    END_CANCEL_ID,
    END_CONFIRM_ID,
    HELP_ID,
    JOIN_ID,
    LEAVE_ID,
    OUTCOME_PLAIN,
    OUTCOME_REQUEUED,
    OUTCOME_SKIPPED,
    build_block_layout,
    build_closed_layout,
    build_end_closing_layout,
    build_end_confirm_layout,
    build_ended_by_partner_layout,
    build_help_layout,
    build_intro_layout,
    build_panel_layout,
    build_partner_gone_layout,
    build_status_idle_layout,
    build_status_left_layout,
    build_status_matched_layout,
    build_status_pool_layout,
)

GUILD_ID = 9001
LONG_NAME = "W" * 32  # Discord's display-name ceiling

ART = {"banner_url": "https://example.test/b.webp", "footer_url": "https://example.test/f.webp"}


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _buttons(items: list[discord.ui.Item[Any]]) -> list[discord.ui.Button[Any]]:
    return [b for b in card(items).walk_children() if isinstance(b, discord.ui.Button)]


def _button_ids(items: list[discord.ui.Item[Any]]) -> list[str | None]:
    return [b.custom_id for b in _buttons(items)]


#: Every card the feature can build, in every state. The sweeps below walk it.
ALL_CARDS: dict[str, list[discord.ui.Item[Any]]] = {
    "panel-instant": build_panel_layout(3),
    "panel-scheduled": build_panel_layout(0, scheduled=True, schedule_label="8:00 AM server time"),
    "help-missing": build_help_layout(None),
    "intro": build_intro_layout("Ada", "Bo", 1_800_000_000),
    "closed-requeued": build_closed_layout(
        GUILD_ID, partner_name="Bo", outcome=OUTCOME_REQUEUED, eligible_at=1_800_000_000,
    ),
    "closed-skipped": build_closed_layout(GUILD_ID, partner_name="Bo", outcome=OUTCOME_SKIPPED),
    "closed-plain": build_closed_layout(GUILD_ID, partner_name=None),
    "partner-gone-requeued": build_partner_gone_layout(requeued=True),
    "partner-gone-opted-out": build_partner_gone_layout(requeued=False),
    "ended-by-partner": build_ended_by_partner_layout(),
    "status-matched": build_status_matched_layout(
        "Bo", channel_id=5, expiry_at=1_800_000_000, swaps_left=2, opted_out=True,
    ),
    "status-pool": build_status_pool_layout(2),
    "status-left": build_status_left_layout(1000.0),
    "status-idle": build_status_idle_layout(),
    "block-empty": build_block_layout([]),
    "block-populated": build_block_layout([(2, "Bo")]),
    "end-confirm": build_end_confirm_layout(),
    "end-closing": build_end_closing_layout(),
    "question-first": layouts.build_question_layout(layouts.QUESTION_FIRST, "Q?", 1, 2),
    "question-auto": layouts.build_question_layout(layouts.QUESTION_AUTO, "Q?", 1, 2),
    "question-swap": layouts.build_question_layout(
        layouts.QUESTION_SWAP, "Q?", 1, 2, swaps_left=1,
    ),
}

#: The question cards ping the pair on purpose — the one mention in the feature.
PINGING_CARDS = {"question-first", "question-auto", "question-swap"}


# ── sweeps over every card ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(ALL_CARDS))
def test_every_card_leads_with_a_glyph_led_title(name):
    first = next(
        i for i in card(ALL_CARDS[name]).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )
    heading = first.content.split("\n", 1)[0]
    assert heading.startswith("## "), heading
    assert not heading[3].isalnum(), heading  # a glyph, then the words


@pytest.mark.parametrize("name", sorted(ALL_CARDS))
def test_buttons_are_grey_unless_the_colour_means_something(name):
    """Red for the one destructive press; everything else grey. Join was
    green in the embed era, which the guide keeps for a decision card's yes."""
    for button in _buttons(ALL_CARDS[name]):
        expected = (
            discord.ButtonStyle.danger
            if button.custom_id == END_CONFIRM_ID
            else discord.ButtonStyle.secondary
        )
        assert button.style is expected, (name, button.label)


@pytest.mark.parametrize("name", sorted(ALL_CARDS))
def test_no_card_names_a_member_with_a_mention(name):
    if name in PINGING_CARDS:
        pytest.skip("pings the pair by design — see the question-card tests")
    assert "<@" not in _text(ALL_CARDS[name])


@pytest.mark.parametrize("name", sorted(ALL_CARDS))
def test_every_card_stays_in_budget(name):
    check_budget(card(ALL_CARDS[name]))


def test_no_card_is_boxed_in_a_container():
    for name, items in ALL_CARDS.items():
        assert not any(
            isinstance(i, discord.ui.Container) for i in card(items).walk_children()
        ), name


def test_the_cog_builds_no_embed():
    """The conversion is whole: an ``Embed(`` creeping back into the cog
    would put an embed beside the feature's V2 cards."""
    source = (
        pathlib.Path(layouts.__file__).parent / "cog.py"
    ).read_text(encoding="utf-8")
    assert "Embed(" not in source


# ── the signup panel ────────────────────────────────────────────────────────


def test_panel_instant_mode_describes_matching_on_the_spot():
    text = _text(build_panel_layout(3))
    assert "matched on the spot" in text
    assert "server time" not in text


@pytest.mark.parametrize(
    "label",
    [
        pytest.param("8:00 AM server time", id="daily"),
        pytest.param("Thursdays at 5:00 PM server time", id="weekly"),
    ],
)
def test_panel_scheduled_mode_quotes_the_configured_round_time(label):
    text = _text(build_panel_layout(3, scheduled=True, schedule_label=label))
    assert f"Matches go out at {label}" in text
    assert "matched on the spot" not in text


@pytest.mark.parametrize(
    ("pool_size", "line"),
    [
        pytest.param(0, "No one waiting yet", id="empty"),
        pytest.param(1, "**1** member waiting", id="one"),
        pytest.param(4, "**4** members waiting", id="several"),
    ],
)
def test_panel_pool_line(pool_size, line):
    assert line in _text(build_panel_layout(pool_size))


@pytest.mark.parametrize(
    ("visibility", "words"),
    [
        pytest.param("admin", "the two of you and the admins", id="admin"),
        pytest.param("mods", "the two of you, the admins and the mods", id="mods"),
        pytest.param("everyone", "Everyone can read a chat", id="everyone"),
        pytest.param("bogus", "the two of you and the admins", id="unknown-is-admin"),
    ],
)
def test_panel_privacy_line_follows_the_visibility_dial(visibility, words):
    """The embed's footer said "and admins" whatever the dial was."""
    assert words in _text(build_panel_layout(1, visibility=visibility))


def test_panel_quotes_the_guilds_session_length():
    assert "for 3 days." in _text(build_panel_layout(1, session_label="3 days"))


@pytest.mark.parametrize(
    ("seconds", "label"),
    [
        pytest.param(86400, "1 day", id="one-day"),
        pytest.param(172800, "2 days", id="days"),
        pytest.param(3600, "1 hour", id="one-hour"),
        pytest.param(129600, "36 hours", id="hours-not-a-whole-day"),
        pytest.param(5400, "90 minutes", id="minutes"),
        pytest.param(60, "1 minute", id="one-minute"),
        pytest.param(0, "1 minute", id="zero-floors"),
    ],
)
def test_session_length_label(seconds, label):
    assert layouts.session_length_label(seconds) == label


def test_panel_keeps_the_persistent_ids_the_embed_panel_used():
    """A panel posted before the conversion must still be answered."""
    assert _button_ids(build_panel_layout(1)) == [HELP_ID, JOIN_ID, LEAVE_ID]
    assert (JOIN_ID, LEAVE_ID) == ("pen_pals:join", "pen_pals:leave")


def test_help_sits_beside_the_title_and_fits_on_a_phone():
    section = next(
        i for i in build_panel_layout(1) if isinstance(i, discord.ui.Section)
    )
    assert section.accessory.custom_id == HELP_ID
    assert text_columns(section.accessory.label) <= MAX_ACCESSORY_LABEL
    assert "## 🖊️ Pen Pals" in section.children[0].content


def test_panel_draws_no_art_by_default():
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in build_panel_layout(1))


def test_the_hub_banner_and_footer_frame_the_panel():
    items = build_panel_layout(1, **ART)
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert "## 🖊️ Pen Pals" in _text(items)


def test_a_titled_banner_drops_the_heading_and_keeps_help():
    items = build_panel_layout(1, title_in_banner=True, **ART)
    assert "##" not in _text(items)
    assert HELP_ID in _button_ids(items)
    assert items[0].items[0].description == layouts.PEN_PALS_PANEL_TITLE


def test_title_in_banner_without_a_banner_keeps_the_heading():
    assert "## 🖊️ Pen Pals" in _text(build_panel_layout(1, title_in_banner=True))


def test_a_footer_alone_still_closes_the_panel():
    items = build_panel_layout(1, footer_url=ART["footer_url"])
    assert isinstance(items[-1], discord.ui.MediaGallery)
    assert not isinstance(items[0], discord.ui.MediaGallery)


def test_help_card_falls_back_when_the_guild_has_no_doc():
    text = _text(build_help_layout(None))
    assert layouts.HELP_TITLE in text
    assert "no Pen Pals guide" in text


def test_help_card_shows_the_guilds_doc():
    text = _text(build_help_layout("# How Pen Pals Works\nYou get matched."))
    assert "How Pen Pals Works" in text
    assert "You get matched." in text
    assert layouts.HELP_DOC_KEY == "penpals"


# ── the room intro ──────────────────────────────────────────────────────────


def test_intro_names_the_pair_escaped_and_counts_down():
    text = _text(build_intro_layout("Al_ice", "Bob", 1_800_000_000))
    assert "**Al\\_ice** × **Bob**" in text
    assert "<t:1800000000:R>" in text
    assert "<t:1800000000:F>" in text


def test_intro_shows_the_opening_message_only_when_set():
    assert "Be kind!" in _text(build_intro_layout("A", "B", 1, intro_message="Be kind!"))
    plain = build_intro_layout("A", "B", 1)
    first = plain[0]
    assert isinstance(first, discord.ui.TextDisplay)
    assert first.content == "## 🖊️ Pen Pals"


def test_intro_cuts_an_oversized_opening_message():
    text = _text(build_intro_layout("A", "B", 1, intro_message="x" * 5000))
    assert "x" * layouts.INTRO_MESSAGE_MAX in text
    assert "x" * (layouts.INTRO_MESSAGE_MAX + 1) not in text


@pytest.mark.parametrize(
    ("visibility", "line"),
    [
        pytest.param("admin", "Admins can see this channel.", id="admin"),
        pytest.param("mods", "Admins and mods can see this channel.", id="mods"),
        pytest.param("everyone", "visible to everyone", id="everyone"),
        pytest.param("bogus", "Admins can see this channel.", id="unknown-is-admin"),
    ],
)
def test_intro_says_who_can_see_the_room(visibility, line):
    assert line in _text(build_intro_layout("A", "B", 1, visibility=visibility))


def test_intro_lists_the_commands_with_the_swap_cap():
    text = _text(build_intro_layout("A", "B", 1, max_swaps=5))
    assert "`/penpals new-question` — swap the prompt (5 max)" in text
    assert "`/penpals end`" in text


def test_intro_has_no_controls():
    assert _buttons(build_intro_layout("A", "B", 1)) == []


def test_intro_stays_in_budget_at_the_longest_names_and_message():
    check_budget(card(build_intro_layout(
        LONG_NAME, LONG_NAME, 1_800_000_000, intro_message="x" * 5000, max_swaps=99,
        visibility="everyone",
    )))


# ── the closing DM ──────────────────────────────────────────────────────────


def test_closed_requeued_states_the_wait_and_offers_leave():
    items = build_closed_layout(
        GUILD_ID, partner_name="Bo", outcome=OUTCOME_REQUEUED, eligible_at=1_800_000_000,
    )
    text = _text(items)
    assert "Your time with **Bo** is up" in text
    assert "You're back in the Pen Pals pool." in text
    assert "matched again <t:1800000000:R>" in text
    assert "any time" not in text
    assert _button_ids(items) == [f"pen_pals:dm:leave:{GUILD_ID}"]


def test_closed_requeued_promises_any_time_only_without_a_cooldown():
    text = _text(build_closed_layout(GUILD_ID, partner_name="Bo", outcome=OUTCOME_REQUEUED))
    assert "A new match can come along any time." in text


def test_closed_skipped_explains_and_offers_join():
    items = build_closed_layout(GUILD_ID, partner_name="Bo", outcome=OUTCOME_SKIPPED)
    assert "stayed quiet, so we've unqueued you" in _text(items)
    assert _button_ids(items) == [f"pen_pals:dm:join:{GUILD_ID}"]


def test_closed_plain_has_no_button():
    items = build_closed_layout(GUILD_ID, partner_name="Bo", outcome=OUTCOME_PLAIN)
    assert "Your time with **Bo** is up" in _text(items)
    assert _buttons(items) == []


@pytest.mark.parametrize("outcome", [OUTCOME_REQUEUED, OUTCOME_SKIPPED, OUTCOME_PLAIN])
def test_closed_never_names_a_partner_it_was_not_given(outcome):
    """A blocked or no-contact pair, and a partner who left, all read "your
    pen pal" — the cog passes ``None`` and the card must not invent a name."""
    text = _text(build_closed_layout(GUILD_ID, partner_name=None, outcome=outcome))
    assert "your pen pal" in text
    assert "**" not in text.split("\n", 1)[1].split("\n\n")[0]


def test_closed_escapes_the_partner_name():
    text = _text(build_closed_layout(GUILD_ID, partner_name="Al_ice"))
    assert "**Al\\_ice**" in text


# ── the ended-early DMs ─────────────────────────────────────────────────────


def test_partner_gone_requeued_says_so():
    text = _text(build_partner_gone_layout(requeued=True))
    assert "Your partner is no longer available." in text
    assert "put back in the Pen Pals pool" in text


def test_partner_gone_does_not_promise_an_opted_out_member_a_seat():
    text = _text(build_partner_gone_layout(requeued=False))
    assert "Your partner is no longer available." in text
    assert "haven't put you back" in text
    assert "put back in the Pen Pals pool" not in text


def test_ended_by_partner_says_who_ended_it_and_nothing_else():
    items = build_ended_by_partner_layout()
    assert "Your partner ended the chat early." in _text(items)
    assert _buttons(items) == []


# ── status ──────────────────────────────────────────────────────────────────


def test_status_matched_names_the_partner_and_links_the_channel():
    text = _text(build_status_matched_layout(
        "Al_ice", channel_id=42, expiry_at=1_800_000_000, swaps_left=2,
    ))
    assert "matched with **Al\\_ice** in <#42>" in text
    assert "Closes <t:1800000000:R>" in text
    assert "Question swaps left: **2**" in text
    assert "left the pool" not in text


def test_status_matched_reads_an_unnameable_partner_as_user_id():
    """The cog's ``name_fn`` hands over ``User <id>``; the card shows it as
    given, never a bare number and never a mention."""
    text = _text(build_status_matched_layout(
        "User 999", channel_id=42, expiry_at=1, swaps_left=0,
    ))
    assert "**User 999**" in text


def test_status_matched_with_a_missing_channel_and_spent_swaps():
    text = _text(build_status_matched_layout(
        "Bo", channel_id=None, expiry_at=1, swaps_left=-1,
    ))
    assert "(channel missing)" in text
    assert "Question swaps left: **0**" in text


def test_status_matched_reflects_a_mid_chat_leave():
    text = _text(build_status_matched_layout(
        "Bo", channel_id=1, expiry_at=1, swaps_left=1, opted_out=True,
    ))
    assert "won't be matched again when this chat closes" in text


@pytest.mark.parametrize(
    ("kwargs", "words"),
    [
        pytest.param({}, "as soon as someone eligible joins", id="instant"),
        pytest.param(
            {"scheduled": True, "schedule_label": "8:00 AM server time"},
            "in the next round, at 8:00 AM server time", id="scheduled",
        ),
    ],
)
def test_status_pool_gives_the_position_and_when(kwargs, words):
    text = _text(build_status_pool_layout(3, **kwargs))
    assert "position **#3**" in text
    assert words in text


def test_status_left_counts_back_and_points_at_the_way_in():
    text = _text(build_status_left_layout(1000.0))
    assert "left the Pen Pals pool <t:1000:R>" in text
    assert "`/penpals join`" in text
    assert "**Join Pool**" in text


def test_status_idle_points_at_join():
    assert "`/penpals join`" in _text(build_status_idle_layout())


# ── the blocklist ───────────────────────────────────────────────────────────


def test_blocklist_empty_offers_only_the_add_select():
    items = build_block_layout([])
    assert "You have no blocks." in _text(items)
    selects = [
        i.custom_id for i in card(items).walk_children()
        if isinstance(i, (discord.ui.UserSelect, discord.ui.Select))
    ]
    assert selects == [BLOCK_ADD_ID]
    assert _button_ids(items) == [BLOCK_DONE_ID]


def test_blocklist_lists_names_and_offers_unblock():
    items = build_block_layout([(2, "Al_ice"), (999, "User 999")])
    text = _text(items)
    assert "• Al\\_ice" in text
    assert "• User 999" in text  # someone who has left the server
    assert "never told they were blocked" in text
    remove = next(
        i for i in card(items).walk_children()
        if getattr(i, "custom_id", None) == BLOCK_REMOVE_ID
    )
    assert [o.value for o in remove.options] == ["2", "999"]
    assert remove.max_values == 2


def test_blocklist_counts_what_it_cannot_list():
    blocked = [(i, f"Member {i}") for i in range(1, 31)]
    items = build_block_layout(blocked)
    assert "…and 5 more" in _text(items)
    remove = next(
        i for i in card(items).walk_children()
        if getattr(i, "custom_id", None) == BLOCK_REMOVE_ID
    )
    assert len(remove.options) == layouts.BLOCK_LIST_LIMIT


def test_blocklist_stays_in_budget_past_the_select_limit_with_long_names():
    items = build_block_layout([(i, LONG_NAME) for i in range(1, 61)])
    check_budget(card(items))
    components, chars = layout_budget(card(items))
    assert components < 40 and chars < 4000


# ── the end confirm ─────────────────────────────────────────────────────────


def test_end_confirm_is_a_red_yes_and_a_grey_cancel():
    items = build_end_confirm_layout()
    assert _button_ids(items) == [END_CONFIRM_ID, END_CANCEL_ID]
    assert "your pen pal is told you ended it" in _text(items)


def test_end_closing_has_no_controls():
    assert _buttons(build_end_closing_layout()) == []


# ── the question cards ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "kwargs", "heading", "lead"),
    [
        pytest.param(layouts.QUESTION_FIRST, {}, "Your First Question",
                     "Here's your first question:", id="first"),
        pytest.param(layouts.QUESTION_AUTO, {}, "A New Question",
                     "A new question to keep things going:", id="auto"),
        pytest.param(layouts.QUESTION_SWAP, {"swaps_left": 2}, "Question Swapped",
                     "New question (2 swaps remaining):", id="swap-plural"),
        pytest.param(layouts.QUESTION_SWAP, {"swaps_left": 1}, "Question Swapped",
                     "New question (1 swap remaining):", id="swap-one"),
    ],
)
def test_question_card_heading_lead_and_quote(kind, kwargs, heading, lead):
    text = _text(layouts.build_question_layout(kind, "Why?", 1, 2, **kwargs))
    assert f"## 🖊️ {heading}" in text
    assert lead in text
    assert "> Why?" in text


def test_question_card_pings_both_members_exactly_once():
    text = _text(layouts.build_question_layout(layouts.QUESTION_AUTO, "Q?", 11, 22))
    assert text.count("<@11>") == 1 and text.count("<@22>") == 1


def test_question_mentions_allow_only_the_pair():
    allowed = layouts.question_mentions(11, 22)
    assert {u.id for u in allowed.users} == {11, 22}
    assert allowed.everyone is False and allowed.roles is False


def test_question_card_keeps_a_multiline_question_inside_the_quote():
    text = _text(layouts.build_question_layout(layouts.QUESTION_AUTO, "One\nTwo", 1, 2))
    assert "> One\n> Two" in text


def test_question_card_has_no_art_and_no_controls_by_default():
    items = layouts.build_question_layout(layouts.QUESTION_FIRST, "Q?", 1, 2)
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in items)
    assert _buttons(items) == []


def test_question_card_wears_the_lobby_strip_and_drops_the_heading_when_titled():
    strip = {"header_url": "https://example.test/h.webp", "footer_url": ART["footer_url"]}
    plain = layouts.build_question_layout(layouts.QUESTION_FIRST, "Q?", 1, 2, **strip)
    assert isinstance(plain[0], discord.ui.MediaGallery)
    assert isinstance(plain[-1], discord.ui.MediaGallery)
    assert "## 🖊️ Your First Question" in _text(plain)

    titled = layouts.build_question_layout(
        layouts.QUESTION_FIRST, "Q?", 1, 2, title_in_header=True, **strip,
    )
    assert "##" not in _text(titled)
    assert "Here's your first question:" in _text(titled)
    assert titled[0].items[0].description == layouts.TITLE


def test_question_card_stays_in_budget_with_the_longest_question():
    check_budget(card(layouts.build_question_layout(
        layouts.QUESTION_SWAP, "x" * 2000, 1, 2, swaps_left=99,
        header_url="https://example.test/h.webp", footer_url=ART["footer_url"],
    )))
