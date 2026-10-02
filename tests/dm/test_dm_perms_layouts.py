"""``bot_modules/dm/perms/layouts.py`` — the Components V2 cards.

These builders are pure, so they are testable exactly the way the embed
builders they replaced were: call one, walk the items, assert on the text.
Three things are worth more than the strings, and get their own sections:

* the **budget** — the settings panel grows a Section per connection, and a
  card that overflows 40 components fails at send time on whichever member
  happens to have the most;
* the **paradigm shapes** the style guide fixes (one dial, a row per
  connection with its own danger button, a thumbnail on the decision card,
  no Container anywhere) — the shape *is* the design decision;
* **``name_fn``** — a ``<@id>`` inside a card renders from the reading
  client's cache, and these cards are read in DMs where that cache is
  usually empty.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import MAX_COMPONENTS, check_budget, layout_budget
from bot_modules.dm.perms.layouts import (
    DM_PANEL_TITLE,
    CONFIRM_CANCEL_ID,
    CONFIRM_REMOVE_ID,
    DM_CONSENT_ACCEPT_CUSTOM_ID,
    DM_CONSENT_DENY_CUSTOM_ID,
    DM_CONSENT_DENY_REPLY_CUSTOM_ID,
    DM_HELP_PANEL_CUSTOM_ID,
    DM_REQUEST_PANEL_CUSTOM_ID,
    DM_SETTINGS_PANEL_CUSTOM_ID,
    DONE_BUTTON_ID,
    HELP_TITLE,
    MAX_CONNECTION_ROWS,
    MAX_ROW_CHARS,
    MODE_ORDER,
    MODE_SELECT_ID,
    NEXT_PAGE_ID,
    PREV_PAGE_ID,
    REMOVE_BUTTON_ID_BASE,
    build_acceptance_layout,
    build_audit_notice_layout,
    build_consent_action_row,
    build_denial_layout_for_requester,
    build_denial_layout_for_view,
    build_expired_layout,
    build_guild_unavailable_layout,
    build_help_layout,
    build_panel_layout,
    build_remove_confirm_layout,
    build_remove_done_layout,
    build_remove_missing_layout,
    build_request_dm_layout,
    build_request_sent_layout,
    build_revoked_layout,
    build_settings_layout,
    build_stale_request_layout,
    connection_page,
    mode_sentence,
    request_type_options,
)

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731

#: A fixed Unix time for the ``<t:…:R>`` expiry the request cards carry.
EXPIRES_AT = 1_750_086_400


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


def _connections(count: int) -> list[dict[str, object]]:
    return [
        {
            "user_id": 1000 + i,
            "type": "dm" if i % 2 == 0 else "friend",
            "reason": "we play chess",
            "created_at": 1_750_000_000 + i,
        }
        for i in range(count)
    ]


def _visible_length(row_text: str) -> int:
    """What a Section row costs on screen: markdown syntax is not drawn.

    Shared by the hub panel's two rows and the settings panel's connection
    rows — every Section in this feature is held to ``MAX_ROW_CHARS``, since
    a Section's accessory sits *beside* its text on a phone and a wrapped
    row pushes its own button down the card.
    """
    return len(row_text.replace("**", ""))


def _request_dm_items(**overrides):
    kwargs = dict(
        requester_id=11,
        requester_avatar_url="https://cdn.example/av.png",
        guild_name="The Keep",
        request_type="dm",
        reason="we play chess",
        expires_at=EXPIRES_AT,
        name_fn=NAME_FN,
    )
    kwargs.update(overrides)
    return build_request_dm_layout(**kwargs)


BANNER = "https://dash.example/static/panel-banners/" + "b" * 32 + ".png"
FOOTER = "https://dash.example/static/panel-banners/" + "f" * 32 + ".png"

#: Every builder, with arguments that exercise its optional branches, so the
#: render/name_fn/no-Container sweeps below cover the whole module from one
#: table rather than one test per card.
ALL_CARDS = [
    pytest.param(lambda: build_panel_layout(), id="panel"),
    pytest.param(
        lambda: build_panel_layout(banner_url=BANNER), id="panel-banner"
    ),
    pytest.param(
        lambda: build_panel_layout(banner_url=BANNER, footer_url=FOOTER),
        id="panel-art",
    ),
    pytest.param(lambda: build_help_layout(FAQ), id="help"),
    pytest.param(lambda: build_help_layout(None), id="help-no-doc"),
    pytest.param(
        lambda: build_settings_layout(mode="ask", connections=[], name_fn=NAME_FN),
        id="settings-empty",
    ),
    pytest.param(
        lambda: build_settings_layout(
            mode="open", connections=_connections(3), name_fn=NAME_FN, changed=True
        ),
        id="settings-just-changed",
    ),
    pytest.param(
        lambda: build_settings_layout(
            mode="open",
            connections=_connections(3),
            name_fn=NAME_FN,
            note="Removed your connection with N1000.",
        ),
        id="settings-after-removal",
    ),
    pytest.param(
        lambda: build_settings_layout(
            mode="ask", connections=_connections(14), name_fn=NAME_FN, page=1
        ),
        id="settings-page-2",
    ),
    pytest.param(
        lambda: build_remove_confirm_layout(
            user_id=77, request_type="dm", name_fn=NAME_FN
        ),
        id="remove-confirm",
    ),
    pytest.param(
        lambda: build_remove_done_layout(user_id=1000, name_fn=NAME_FN),
        id="remove-done",
    ),
    pytest.param(
        lambda: build_remove_missing_layout(user_id=1000, name_fn=NAME_FN),
        id="remove-missing",
    ),
    pytest.param(lambda: _request_dm_items(), id="request-dm"),
    pytest.param(lambda: _request_dm_items(reason=""), id="request-dm-no-reason"),
    pytest.param(
        lambda: _request_dm_items(request_type="friend"), id="request-dm-friend"
    ),
    pytest.param(
        lambda: build_acceptance_layout(
            viewer_id=22,
            requester_id=11,
            target_id=22,
            request_type="dm",
            name_fn=NAME_FN,
        ),
        id="accepted",
    ),
    pytest.param(
        lambda: build_denial_layout_for_view(
            requester_id=11, request_type="dm", name_fn=NAME_FN
        ),
        id="declined-view",
    ),
    pytest.param(
        lambda: build_denial_layout_for_view(
            requester_id=11, request_type="dm", reply="not right now", name_fn=NAME_FN
        ),
        id="declined-view-with-note",
    ),
    pytest.param(
        lambda: build_denial_layout_for_requester(
            target_id=22,
            guild_name="The Keep",
            request_type="dm",
            name_fn=NAME_FN,
        ),
        id="declined-requester",
    ),
    pytest.param(
        lambda: build_denial_layout_for_requester(
            target_id=22,
            guild_name="The Keep",
            request_type="friend",
            reply="not right now",
            name_fn=NAME_FN,
        ),
        id="declined-requester-with-note",
    ),
    pytest.param(
        lambda: build_request_sent_layout(
            target_id=22,
            guild_name="The Keep",
            request_type="dm",
            reason="we play chess",
            expires_at=EXPIRES_AT,
            name_fn=NAME_FN,
        ),
        id="request-sent",
    ),
    pytest.param(
        lambda: build_request_sent_layout(
            target_id=22,
            guild_name="The Keep",
            request_type="friend",
            reason="",
            expires_at=EXPIRES_AT,
            name_fn=NAME_FN,
        ),
        id="request-sent-no-reason",
    ),
    pytest.param(
        lambda: build_expired_layout(
            target_id=22,
            guild_name="The Keep",
            request_type="dm",
            expiry_label="24 hours",
            name_fn=NAME_FN,
        ),
        id="expired",
    ),
    pytest.param(
        lambda: build_revoked_layout(
            viewer_id=11, actor_id=11, other_id=22, name_fn=NAME_FN
        ),
        id="revoked-dm",
    ),
    pytest.param(
        lambda: build_revoked_layout(
            viewer_id=None, actor_id=11, other_id=22, name_fn=NAME_FN
        ),
        id="revoked-channel",
    ),
    pytest.param(lambda: build_stale_request_layout(), id="stale"),
    pytest.param(lambda: build_guild_unavailable_layout(), id="guild-gone"),
    pytest.param(
        lambda: build_audit_notice_layout(
            message="Ada asked Bo", timestamp=1_750_000_000
        ),
        id="audit-notice",
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
    """The card's first line — its heading, wherever the text display sits."""
    return next(
        item.content
        for item in _view(items).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ).splitlines()[0]


def _heading_faults(heading: str) -> list[str]:
    """What is wrong with a card's lead line, per guide § Anatomy."""
    if not heading.startswith("## "):
        return [f"a card-level heading is a `## Title` line: {heading!r}"]
    if len(heading) < 4 or heading[3].isascii():
        return [f"lead the title with the feature's glyph: {heading!r}"]
    return []


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_leads_with_a_glyph_led_title(build):
    """A card's own heading is one ``## Title`` line, led by a glyph.

    Guide § Anatomy: ``###`` is a section heading *within* a multi-part card
    (this feature has exactly one — ``### Your Connections`` under the
    settings panel's own ``## 📬 DM Settings``), so a card whose sole
    heading is ``###`` is half-styled beside its siblings. The remove flow's
    three answers, its confirm and the mod-feed notice shipped that way and
    read as unreviewed rather than deliberate, which is what this sweep is
    for: it fails the next card that copies whichever heading level happened
    to render first.
    """
    assert not _heading_faults(_lead_heading(build()))


@pytest.mark.parametrize(
    ("heading", "faulty"),
    [
        pytest.param("## 🚫 Connection Removed", False, id="glyph-led-title"),
        pytest.param("### Connection Removed", True, id="section-heading-as-title"),
        pytest.param("## Connection Removed", True, id="no-glyph"),
        pytest.param("Connection Removed", True, id="no-heading"),
    ],
)
def test_the_heading_sweep_can_actually_see_a_violation(heading, faulty):
    """The sweep above is born green; this is what proves it can go red."""
    assert bool(_heading_faults(heading)) is faulty


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    """A card is bare (guide § Paradigms): weight comes from headings and
    subtext, not from a coloured box around everything. The hub panel was
    the one trial exception until 2026-09-29; inside the box its text and
    buttons overhung the banner's gold frame, so the box went."""
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


#: The only colours a button may carry, and what each one means (guide
#: § Controls). Blurple ``primary`` is absent on purpose: it marks no
#: meaning, only "this one matters more", and two hub buttons in two
#: colours read as a ranking (Billy, 2026-09-24).
BUTTON_STYLES = {
    discord.ButtonStyle.secondary,  # the default
    discord.ButtonStyle.success,  # the yes on a decision card
    discord.ButtonStyle.danger,  # destructive, or the no on a decision card
}


@pytest.mark.parametrize("build", ALL_CARDS)
def test_buttons_are_grey_unless_the_colour_means_something(build):
    styles = {
        i.style
        for i in _view(build()).walk_children()
        if isinstance(i, discord.ui.Button)
    }
    assert styles <= BUTTON_STYLES, styles - BUTTON_STYLES


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_writes_a_raw_mention(build):
    """Members are named through ``name_fn``.

    An embed/layout mention resolves from the *reading* client's cache, so a
    ``<@id>`` in a DM card renders as a bare number for anyone who has not
    seen that member — which in a DM is most people.
    """
    assert "<@" not in _all_text(build())


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: _request_dm_items(), id="request-dm"),
        pytest.param(
            lambda: build_acceptance_layout(
                viewer_id=11,
                requester_id=11,
                target_id=22,
                request_type="dm",
                name_fn=NAME_FN,
            ),
            id="accepted",
        ),
        pytest.param(
            lambda: build_revoked_layout(
                viewer_id=11, actor_id=11, other_id=22, name_fn=NAME_FN
            ),
            id="revoked",
        ),
        pytest.param(
            lambda: build_settings_layout(
                mode="ask", connections=_connections(1), name_fn=NAME_FN
            ),
            id="settings-row",
        ),
        pytest.param(
            lambda: build_remove_confirm_layout(
                user_id=1000, request_type="dm", name_fn=NAME_FN
            ),
            id="remove-confirm",
        ),
        pytest.param(
            lambda: build_remove_done_layout(user_id=1000, name_fn=NAME_FN),
            id="remove-done",
        ),
        pytest.param(
            lambda: build_remove_missing_layout(user_id=1000, name_fn=NAME_FN),
            id="remove-missing",
        ),
    ],
)
def test_cards_naming_a_member_use_the_resolver(build):
    text = _all_text(build())
    assert "N11" in text or "N22" in text or "N1000" in text


# ── Budget ──────────────────────────────────────────────────────────────


def test_settings_panel_fits_with_a_full_page_of_connections():
    """The fullest card this feature can send, at the page size it caps to."""
    view = _view(
        build_settings_layout(
            mode="open",
            connections=_connections(MAX_CONNECTION_ROWS),
            name_fn=NAME_FN,
            note="Changed to Open.",
        )
    )
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components <= MAX_COMPONENTS


def _sections(items) -> list[discord.ui.Section]:
    return [
        i for i in _view(items).walk_children() if isinstance(i, discord.ui.Section)
    ]


def _arrows(items) -> tuple[discord.ui.Button | None, discord.ui.Button | None]:
    view = _view(items)
    return view.find_item(PREV_PAGE_ID), view.find_item(NEXT_PAGE_ID)


def test_settings_panel_pages_at_ten_rows():
    """An eleventh connection goes to a second page, not off the card, and
    the subtext says which page this is."""
    items = build_settings_layout(
        mode="open", connections=_connections(14), name_fn=NAME_FN
    )
    check_budget(_view(items))
    assert len(_sections(items)) == MAX_CONNECTION_ROWS
    assert "Page 1 of 2 • 14 connections" in _all_text(items)


def test_the_second_page_holds_the_rows_a_plain_cut_hid():
    """Rows come newest first, so a cut at ten hid the *oldest* connections —
    and the settings panel is the one surface that can remove one. Prod had
    members with eleven and twelve connections the day the pilot shipped."""
    items = build_settings_layout(
        mode="open", connections=_connections(14), name_fn=NAME_FN, page=1
    )
    text = _all_text(items)
    assert "Page 2 of 2 • 14 connections" in text
    assert len(_sections(items)) == 4
    assert "N1010" in text and "N1013" in text
    assert "N1009" not in text


@pytest.mark.parametrize(
    ("page", "prev_disabled", "next_disabled"),
    [
        pytest.param(0, True, False, id="first-page"),
        pytest.param(1, False, True, id="last-page"),
    ],
)
def test_arrows_are_disabled_at_the_ends_not_hidden(page, prev_disabled, next_disabled):
    """The bottom row keeps its shape from page to page."""
    prev, nxt = _arrows(
        build_settings_layout(
            mode="open", connections=_connections(14), name_fn=NAME_FN, page=page
        )
    )
    assert prev is not None and nxt is not None
    assert prev.disabled is prev_disabled
    assert nxt.disabled is next_disabled
    assert (prev.label, nxt.label) == ("◀", "▶")


@pytest.mark.parametrize("count", [0, 1, MAX_CONNECTION_ROWS])
def test_a_single_page_shows_no_arrows(count):
    prev, nxt = _arrows(
        build_settings_layout(
            mode="open", connections=_connections(count), name_fn=NAME_FN
        )
    )
    assert prev is None and nxt is None


def test_settings_panel_fits_with_arrows_on_a_full_page():
    """The fullest card this feature can send: ten rows and both arrows is the
    40-component ceiling exactly, so the next item added here overflows."""
    view = _view(
        build_settings_layout(
            mode="open",
            connections=_connections(14),
            name_fn=NAME_FN,
            note="Changed to Open.",
        )
    )
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components == MAX_COMPONENTS


@pytest.mark.parametrize(
    ("count", "page", "expected"),
    [
        pytest.param(0, 0, (0, 1, 0), id="empty-is-one-page"),
        pytest.param(10, 0, (0, 1, 10), id="exactly-one-page"),
        pytest.param(11, 0, (0, 2, 10), id="first-of-two"),
        pytest.param(11, 1, (1, 2, 1), id="last-of-two"),
        pytest.param(11, 7, (1, 2, 1), id="past-the-end-clamps"),
        pytest.param(11, -3, (0, 2, 10), id="negative-clamps"),
    ],
)
def test_connection_page_clamps_and_slices(count, page, expected):
    got_page, got_count, rows = connection_page(_connections(count), page)
    assert (got_page, got_count, len(rows)) == expected


# ── Paradigm 1: the hub panel ───────────────────────────────────────────
#
# The hub's rows do two different kinds of thing — Open Form reaches out to
# another member, Settings configures yourself — so each is its own ``###``
# group with a visible Separator between them (guide § Controls, Billy
# 2026-09-20: "can you put some sort of separation between requesting and
# setting your preference"). Back to back the two Sections read as one list,
# which is what these assertions exist to stop coming back.
#
# From 2026-09-24 to 2026-09-29 the whole card was one tile, on trial
# (Billy: "put it back as a little embed tile … let's use components v2 in
# an embed and see if it's better"). The trial's second line under the intro
# and its Help button beside the title stayed; the box did not (Ben,
# 2026-09-29: "move the banners out for full width and drop the box").


def _panel_parts() -> list:
    return build_panel_layout()


def _is_text(item, prefix: str) -> bool:
    return isinstance(item, discord.ui.TextDisplay) and item.content.startswith(prefix)


def _is_section(item, custom_id: str) -> bool:
    return (
        isinstance(item, discord.ui.Section)
        and getattr(item.accessory, "custom_id", None) == custom_id
    )


def _is_divider(item) -> bool:
    return isinstance(item, discord.ui.Separator)


#: The card's parts in the order they must appear, as
#: ``(predicate, description)``. A sequence assertion rather than a set of
#: "contains" checks, because the whole point of the grouping is *where* the
#: heading and the dividers sit relative to the rows.
PANEL_SHAPE = [
    (lambda i: _is_section(i, DM_HELP_PANEL_CUSTOM_ID), "the title, Help beside it"),
    (lambda i: _is_text(i, "Want to reach someone"), "the one-sentence intro"),
    (_is_divider, "the line above Ask Someone"),
    (lambda i: _is_text(i, "### Ask Someone"), "the first group's heading"),
    (lambda i: _is_section(i, DM_REQUEST_PANEL_CUSTOM_ID), "the Open Form row"),
    (lambda i: _is_text(i, "-# If they accept"), "the first group's caveat"),
    (_is_divider, "the divider between groups"),
    (lambda i: _is_text(i, "### Your Preference"), "the second group's heading"),
    (lambda i: _is_section(i, DM_SETTINGS_PANEL_CUSTOM_ID), "the DM Settings row"),
    (lambda i: _is_text(i, "-# Open, Ask or Closed"), "the second group's blurb"),
]


def test_panel_lays_its_parts_out_in_order():
    """Title + Help, intro, line, then each group: heading, row, ``-#``.

    The ``-#`` line describes what an accepted *request* leads to, so it
    sits under **Ask Someone** — the group it is about — not at the foot of
    the whole card where it read as a footnote to both. The three-status
    legend that used to close the card is gone (Billy, 2026-09-20: "let's
    drop the 'your status shows on your profile' line").
    """
    parts = _panel_parts()
    assert len(parts) == len(PANEL_SHAPE), [type(i).__name__ for i in parts]
    for item, (matches, description) in zip(parts, PANEL_SHAPE, strict=True):
        assert matches(item), f"expected {description}, got {item!r}"


def test_title_in_the_banner_drops_the_heading_and_moves_help_to_the_intro():
    """With the title drawn into the banner (Billy, 2026-09-26) the ``##``
    line would say it twice: it goes, the banner's alt text carries the name
    for a screen reader, and Help keeps its place beside the first line."""
    parts = build_panel_layout(banner_url=BANNER, title_in_banner=True)
    assert isinstance(parts[0], discord.ui.MediaGallery)
    assert [i.description for i in parts[0].items] == [DM_PANEL_TITLE]
    assert _is_section(parts[1], DM_HELP_PANEL_CUSTOM_ID)
    assert parts[1].children[0].content.startswith("Want to reach someone")
    assert not any(line.startswith("## ") for line in _all_text(parts).splitlines())
    # Banner + (intro, Help) stand where (title, Help) + intro stood.
    assert len(parts) == len(PANEL_SHAPE)


def test_title_in_the_banner_needs_a_banner():
    assert build_panel_layout(title_in_banner=True)[0].children[
        0
    ].content == f"## 📬 {DM_PANEL_TITLE}"


def test_panel_draws_two_large_lines():
    """Two visible, large dividers: under the intro (Billy, 2026-09-24:
    "put a horizontal line above ask someone") and between the groups
    (2026-09-20: small spacing read as crowded on the posted gallery). A
    third would split a group in two."""
    separators = [i for i in _panel_parts() if _is_divider(i)]
    assert len(separators) == 2
    for sep in separators:
        assert sep.visible, "an invisible separator does not read as a break"
        assert sep.spacing is discord.SeparatorSpacing.large


def test_panel_headings_are_section_headings_under_the_card_title():
    """``##`` is the card, ``###`` is a group inside it (guide § Anatomy)."""
    headings = [
        i.content.splitlines()[0]
        for i in _view(build_panel_layout()).walk_children()
        if isinstance(i, discord.ui.TextDisplay) and i.content.startswith("#")
    ]
    assert headings == ["## 📬 DM Requests", "### Ask Someone", "### Your Preference"]


def test_panel_puts_each_button_beside_its_own_line():
    """Three Sections with a button accessory each — not a bare row of
    buttons under a wall of text (guide § Paradigm 1)."""
    sections = [i for i in _panel_parts() if isinstance(i, discord.ui.Section)]
    ids = [s.accessory.custom_id for s in sections]
    assert ids == [
        DM_HELP_PANEL_CUSTOM_ID,
        DM_REQUEST_PANEL_CUSTOM_ID,
        DM_SETTINGS_PANEL_CUSTOM_ID,
    ]


@pytest.mark.parametrize(
    ("custom_id", "label", "style"),
    [
        pytest.param(
            DM_HELP_PANEL_CUSTOM_ID, "Help", discord.ButtonStyle.secondary, id="help"
        ),
        pytest.param(
            DM_REQUEST_PANEL_CUSTOM_ID,
            "Open Form",
            discord.ButtonStyle.secondary,
            id="ask-someone",
        ),
        pytest.param(
            DM_SETTINGS_PANEL_CUSTOM_ID,
            "DM Settings",
            discord.ButtonStyle.secondary,
            id="your-preference",
        ),
    ],
)
def test_panel_buttons_keep_their_ids_labels_and_styles(custom_id, label, style):
    """The panel is a sticky message the persistent views key off by
    ``custom_id``: the ids are on-disk state and never change. The labels
    are not — the boot-time ``place_or_refresh`` repaints the panel — so
    this pins what the buttons *say* as the design decision it is: a button
    carries the name of the panel it opens (guide § Words)."""
    button = next(
        i
        for i in _view(build_panel_layout()).walk_children()
        if isinstance(i, discord.ui.Button) and i.custom_id == custom_id
    )
    assert button.label == label
    assert button.style is style


@pytest.mark.parametrize("index", [0, 1, 2])
def test_panel_rows_stay_on_one_line(index):
    """A hub row shares its line with its button, exactly as a connection row
    does — and unlike a connection row it carries no member name, so it has
    no excuse for running long (guide § Screen use on a phone). The title
    row is held to it too: the intro sentence sits *below* it, not beside
    Help, so the button doesn't squeeze it into a wrap."""
    sections = [i for i in _panel_parts() if isinstance(i, discord.ui.Section)]
    text = sections[index].children[0].content
    assert "\n" not in text, "a row is one line, never two"
    assert _visible_length(text) <= MAX_ROW_CHARS


def test_panel_fits_its_budget():
    """The hub is nowhere near the ceiling: three Sections (three
    components each), the intro, two headings, two blurbs and two lines."""
    view = _view(build_panel_layout())
    check_budget(view)
    components, _chars = layout_budget(view)
    assert components == 16


def test_panel_drops_the_numbered_how_to():
    """The five-step walkthrough described a flow that no longer exists: the
    request is one self-explanatory modal, and the long explanation is the
    FAQ behind Help."""
    text = _all_text(build_panel_layout())
    assert "1." not in text and "Step" not in text


def test_panel_groups_are_even_each_row_then_its_blurb():
    """Each group closes on one ``-#`` line straight after its row (guide
    § Controls: a caveat goes under its group) — Billy, 2026-09-24: a blurb
    under the preferences "so they're even"."""
    parts = _panel_parts()
    for custom_id in (DM_REQUEST_PANEL_CUSTOM_ID, DM_SETTINGS_PANEL_CUSTOM_ID):
        row = next(i for i in parts if _is_section(i, custom_id))
        assert _is_text(parts[parts.index(row) + 1], "-# ")
    assert len([i for i in parts if _is_text(i, "-#")]) == 2


def test_panel_has_no_status_legend():
    """Billy, 2026-09-20: "let's drop the 'your status shows on your profile'
    line". The settings panel spells the three modes out when it opens,
    which is the moment a member is choosing one."""
    text = _all_text(build_panel_layout())
    assert "status" not in text.lower()
    assert "DMs:" not in text


# ── Help: the FAQ doc behind the hub's Help button ─────────────────────

#: The prod ``dmperms`` doc's shape, abridged: its own ``#`` heading, then
#: paragraphs and a list.
FAQ = (
    "# What do the DM permissions mean?\n"
    "Direct messages and friend requests are opt-in here.\n\n"
    "- DMs Closed: no unsolicited contact, period\n"
    "- DMs Open: you can message freely"
)


def _help_text(body) -> str:
    return "\n".join(
        i.content
        for i in _view(build_help_layout(body)).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


@pytest.mark.parametrize(
    ("body", "starts"),
    [
        pytest.param(
            FAQ,
            "## 📬 What do the DM permissions mean?\nDirect messages",
            id="doc-heading-becomes-the-title",
        ),
        pytest.param(
            "No heading, just prose.",
            f"## 📬 {HELP_TITLE}\nNo heading, just prose.",
            id="headingless-doc-gets-the-feature-title",
        ),
        pytest.param(
            None, f"## 📬 {HELP_TITLE}\nThere's no DM requests FAQ", id="no-doc"
        ),
    ],
)
def test_help_card_reads_the_faq_under_the_dm_glyph(body, starts):
    """How a doc becomes a card is ``docs.help_card``'s job
    (test_docs_help_card); this pins what DM Perms hands it."""
    assert _help_text(body).startswith(starts)


def test_help_card_leads_with_one_title_and_no_second_heading():
    """The doc's ``#`` would print a second, bigger title under the card's
    ``##`` (guide § Anatomy) — it is lifted into the title instead."""
    lines = _help_text(FAQ).splitlines()
    assert lines[0] == "## 📬 What do the DM permissions mean?"
    assert not any(line.startswith("# ") for line in lines)


# ── Paradigm 2: the settings panel ──────────────────────────────────────


def _select(items) -> discord.ui.Select:
    selects = [
        i for i in _view(items).walk_children() if isinstance(i, discord.ui.Select)
    ]
    assert len(selects) == 1, "the dial is one select, not several controls"
    return selects[0]


def test_settings_has_exactly_one_dial_with_the_three_modes():
    """Three states that cannot combine are a dropdown, not three toggles."""
    select = _select(build_settings_layout(mode="ask", connections=[], name_fn=NAME_FN))
    assert [o.value for o in select.options] == list(MODE_ORDER)
    assert all(o.description for o in select.options)
    assert select.id == MODE_SELECT_ID


@pytest.mark.parametrize("mode", MODE_ORDER)
def test_settings_preselects_the_current_mode(mode):
    select = _select(build_settings_layout(mode=mode, connections=[], name_fn=NAME_FN))
    assert [o.value for o in select.options if o.default] == [mode]


@pytest.mark.parametrize("mode", MODE_ORDER)
def test_settings_says_the_current_mode_in_words(mode):
    """A dropdown hides its descriptions until opened, so the card states the
    current setting at rest."""
    text = _all_text(build_settings_layout(mode=mode, connections=[], name_fn=NAME_FN))
    assert mode_sentence(mode) in text


def test_settings_falls_back_to_ask_for_an_unknown_mode():
    """A mode read from a role that was since renamed still renders a card."""
    assert "Ask" in mode_sentence("who-knows")


def test_settings_renders_a_row_per_connection_with_its_own_remove():
    items = build_settings_layout(
        mode="open", connections=_connections(3), name_fn=NAME_FN
    )
    sections = [i for i in _view(items).children if isinstance(i, discord.ui.Section)]
    assert len(sections) == 3
    for index, section in enumerate(sections):
        accessory = section.accessory
        assert accessory.style is discord.ButtonStyle.danger
        assert accessory.id == REMOVE_BUTTON_ID_BASE + index


def test_settings_row_names_the_member_and_the_kind_of_connection():
    items = build_settings_layout(
        mode="open", connections=_connections(2), name_fn=NAME_FN
    )
    rows = [i for i in _view(items).children if isinstance(i, discord.ui.Section)]
    assert "N1000" in rows[0].children[0].content
    assert rows[0].children[0].content.endswith("· DM")
    assert rows[1].children[0].content.endswith("· Friend")


@pytest.mark.parametrize("kind", ["dm", "friend"])
def test_a_connection_row_stays_on_one_line_for_the_longest_possible_name(kind):
    """A Section's button sits *beside* its text, so a wrapped row pushes its
    own Remove down the card (guide § Screen use on a phone).

    The worst case is a member using all 32 characters Discord allows in a
    display name. The first draft carried the join date as well, and
    ``since <t:…:D>`` renders as "September 20, 2026" — 49 characters for a
    five-letter name, before anyone with a long one opened the panel.
    """
    longest_name = "W" * 32
    items = build_settings_layout(
        mode="open",
        connections=[{"user_id": 1000, "type": kind, "created_at": 1_750_000_000}],
        name_fn=lambda _uid: longest_name,
    )
    row = next(i for i in _view(items).children if isinstance(i, discord.ui.Section))
    text = row.children[0].content
    assert "\n" not in text, "a row is one line, never two"
    assert "<t:" not in text, "a rendered date is far longer than its markup"
    assert _visible_length(text) <= MAX_ROW_CHARS


def test_settings_with_no_connections_says_so_and_still_offers_the_dial():
    items = build_settings_layout(mode="closed", connections=[], name_fn=NAME_FN)
    assert not [i for i in _view(items).children if isinstance(i, discord.ui.Section)]
    assert "Nobody yet" in _all_text(items)
    assert _select(items) is not None


def test_settings_carries_a_done_button():
    items = build_settings_layout(mode="ask", connections=[], name_fn=NAME_FN)
    buttons = [
        i
        for i in _view(items).walk_children()
        if isinstance(i, discord.ui.Button) and i.id == DONE_BUTTON_ID
    ]
    assert len(buttons) == 1


def test_settings_note_renders_as_subtext():
    """A removal's answer lands on the panel behind the confirm card."""
    items = build_settings_layout(
        mode="open",
        connections=[],
        name_fn=NAME_FN,
        note="Removed your connection with N1000.",
    )
    assert items[-1].content == "-# Removed your connection with N1000."


def test_settings_drops_the_paging_line_while_a_note_is_showing():
    """A footer does one job (embed_style_guide.md § Footers). A removal's
    note and a paging count are two jobs, so the redraw that carries the
    note drops the count rather than stacking two subtext lines."""
    with_note = _all_text(
        build_settings_layout(
            mode="open",
            connections=_connections(14),
            name_fn=NAME_FN,
            note="Removed your connection with N1000.",
        )
    )
    assert "-# Removed your connection" in with_note
    assert "Page 1 of 2" not in with_note


def test_settings_changed_is_the_status_line_not_a_second_line():
    """The redraw after a mode pick answers in the status sentence itself
    ("Changed to …"); the first draft added a ``-#`` line under the card
    that said the same thing a second time."""
    at_rest = build_settings_layout(mode="open", connections=[], name_fn=NAME_FN)
    changed = build_settings_layout(
        mode="open", connections=[], name_fn=NAME_FN, changed=True
    )
    assert "You're on 🟢 **Open**" in at_rest[0].content
    assert "Changed to 🟢 **Open**" in changed[0].content
    assert "Changed" not in _all_text(at_rest)
    assert not [i for i in changed if _is_text(i, "-#")]


def test_settings_has_no_subtext_at_rest():
    """With no note and one page there is nothing for a ``-#`` line to say.
    The audit-transparency line the panel used to state on every redraw
    moved to the receipts that add a log row (below)."""
    items = build_settings_layout(
        mode="open", connections=_connections(3), name_fn=NAME_FN
    )
    assert not [i for i in items if _is_text(i, "-#")]
    assert "logged for audit" not in _all_text(items)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: build_request_sent_layout(
                target_id=22,
                guild_name="The Keep",
                request_type="dm",
                reason="",
                expires_at=EXPIRES_AT,
                name_fn=NAME_FN,
            ),
            id="sent",
        ),
        pytest.param(
            lambda: build_acceptance_layout(
                viewer_id=11,
                requester_id=11,
                target_id=22,
                request_type="dm",
                name_fn=NAME_FN,
            ),
            id="connected",
        ),
        pytest.param(
            lambda: build_revoked_layout(
                viewer_id=11, actor_id=11, other_id=22, name_fn=NAME_FN
            ),
            id="removed-dm",
        ),
        pytest.param(
            lambda: build_remove_done_layout(user_id=22, name_fn=NAME_FN),
            id="removed-confirm",
        ),
    ],
)
def test_receipts_that_add_a_log_row_say_so(build):
    """Consent grants and removals are logged, and the card that records the
    moment says so — it was the embed's footer before, and the settings
    panel's on every redraw until the 2026-09-20 design pass."""
    assert "logged for audit" in _all_text(build())


# ── Paradigm 10: the remove confirm ─────────────────────────────────────


def test_remove_confirm_offers_a_danger_yes_and_a_plain_cancel():
    items = build_remove_confirm_layout(
        user_id=1000, request_type="dm", name_fn=NAME_FN
    )
    buttons = {
        i.id: i
        for i in _view(items).walk_children()
        if isinstance(i, discord.ui.Button)
    }
    assert buttons[CONFIRM_REMOVE_ID].style is discord.ButtonStyle.danger
    assert buttons[CONFIRM_CANCEL_ID].style is discord.ButtonStyle.secondary


def test_remove_confirm_echoes_the_row_that_was_pressed():
    """The confirm names the member and the kind exactly as the row did, so
    a mis-tap on a phone is visible before it is confirmed — in the short
    form the row uses, not the request form's long one."""
    text = _all_text(
        build_remove_confirm_layout(
            user_id=1000, request_type="friend", name_fn=NAME_FN
        )
    )
    assert "**N1000** · Friend" in text
    assert "Friend Request" not in text


def test_remove_outcomes_are_builders_so_the_gallery_can_render_them():
    """Both ways a confirmed Remove can end are cards like any other.

    Built inline in the view, they had no paradigm, no budget row and no
    entry in ``scripts/layout_samples.py`` — so the one review that looks at
    a rendering rather than a diff would never have seen them.
    """
    done = _all_text(build_remove_done_layout(user_id=1000, name_fn=NAME_FN))
    assert "Connection Removed" in done and "N1000" in done

    missing = _all_text(build_remove_missing_layout(user_id=1000, name_fn=NAME_FN))
    assert missing.startswith("## ❌ Nothing to Remove")
    assert "❌" in missing, "a press that did nothing is a denial, not a receipt"
    assert "N1000" in missing


# ── Paradigm 5: the consent decision card ───────────────────────────────


def test_request_dm_puts_the_requesters_avatar_beside_the_ask():
    """The embed's author icon becomes a Section thumbnail — the one picture
    worth carrying on a "someone wants to connect" card."""
    view = _view(_request_dm_items())
    section = next(i for i in view.children if isinstance(i, discord.ui.Section))
    assert isinstance(section.accessory, discord.ui.Thumbnail)
    assert "av.png" in section.accessory.media.url


def test_request_dm_carries_the_three_persistent_decision_buttons():
    view = _view(_request_dm_items())
    ids = [
        i.custom_id for i in view.walk_children() if isinstance(i, discord.ui.Button)
    ]
    assert ids == [
        DM_CONSENT_ACCEPT_CUSTOM_ID,
        DM_CONSENT_DENY_CUSTOM_ID,
        DM_CONSENT_DENY_REPLY_CUSTOM_ID,
    ]


def test_request_dm_accept_is_the_green_one():
    row = build_consent_action_row()
    styles = [c.style for c in row.children]
    assert styles[0] is discord.ButtonStyle.success
    assert styles[1] is discord.ButtonStyle.danger


def _quote_block(items) -> str:
    return next(
        item.content
        for item in items
        if isinstance(item, discord.ui.TextDisplay) and item.content.startswith("> ")
    )


def test_request_dm_quotes_the_reason_and_counts_the_window_down():
    """The reason is a ``>`` quote straight under the ask, and the expiry a
    ``<t:…:R>`` so it reads "in 23 hours" on the reader's clock rather than
    a "24 hours" that was true only at send time."""
    text = _all_text(_request_dm_items())
    assert "> we play chess" in text
    assert f"<t:{EXPIRES_AT}:R>" in text


def test_request_dm_omits_the_quote_when_no_reason_was_given():
    """An optional field left blank leaves no empty quote behind."""
    with pytest.raises(StopIteration):
        _quote_block(_request_dm_items(reason=""))


def test_request_dm_escapes_markdown_in_the_reason():
    """The reason is attacker-controlled text arriving from a modal."""
    reason = "[click me](https://evil.example) **bold**"
    body = _quote_block(_request_dm_items(reason=reason))[2:]
    assert body == discord.utils.escape_markdown(reason)
    assert "\\[click me]" in body and "\\*\\*bold\\*\\*" in body


def test_a_multi_line_reason_quotes_every_line():
    """A ``>`` on the first line only would quote one line and drop the
    rest back to body text."""
    quote = _quote_block(_request_dm_items(reason="line one\nline two"))
    assert quote == "> line one\n> line two"


@pytest.mark.parametrize(
    ("request_type", "heading", "asks"),
    [
        pytest.param("dm", "## 📨 DM Request", "wants to DM you", id="dm"),
        pytest.param(
            "friend",
            "## 📨 Friend Request",
            "wants to add you as a friend",
            id="friend",
        ),
    ],
)
def test_request_dm_says_who_where_and_what_in_its_first_line(
    request_type, heading, asks
):
    """Who, from where, and for what — in the sentence the reader sees
    first. The server is named there because a display name is per-server
    and the branding line lands at the foot of the card."""
    first = _request_dm_items(request_type=request_type)[0].children[0].content
    assert first.startswith(heading)
    assert f"**N11** from The Keep {asks}" in first


# ── Paradigm 7: receipts and notices ────────────────────────────────────


@pytest.mark.parametrize(
    ("viewer_id", "named", "not_named"),
    [
        pytest.param(11, "N22", "N11", id="requester"),
        pytest.param(22, "N11", "N22", id="target"),
    ],
)
def test_acceptance_names_the_other_party(viewer_id, named, not_named):
    """A card addressed to one member names the *other* one: "You and
    **Bo**", never "**Ada** ↔ **Bo**" read by Ada (guide § Words)."""
    text = _all_text(
        build_acceptance_layout(
            viewer_id=viewer_id,
            requester_id=11,
            target_id=22,
            request_type="dm",
            name_fn=NAME_FN,
        )
    )
    assert f"You and **{named}**" in text
    assert not_named not in text


def test_denial_for_the_decliner_echoes_their_own_note():
    text = _all_text(
        build_denial_layout_for_view(
            requester_id=11, request_type="dm", reply="not right now", name_fn=NAME_FN
        )
    )
    assert "You declined **N11**'s DM request." in text
    assert "**Your note**\n> not right now" in text


def test_denial_for_the_requester_attributes_the_note():
    """Unlabelled under "declined your request", the decliner's words would
    read as the reason for declining rather than a message to the reader."""
    text = _all_text(
        build_denial_layout_for_requester(
            target_id=22,
            guild_name="The Keep",
            request_type="dm",
            reply="not right now",
            name_fn=NAME_FN,
        )
    )
    assert "**N22** declined your DM request in The Keep." in text
    assert "**Note from N22**\n> not right now" in text


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: build_denial_layout_for_view(
                requester_id=11, request_type="dm", name_fn=NAME_FN
            ),
            id="decliner",
        ),
        pytest.param(
            lambda: build_denial_layout_for_requester(
                target_id=22, guild_name="The Keep", request_type="dm", name_fn=NAME_FN
            ),
            id="requester",
        ),
    ],
)
def test_denial_without_a_note_shows_no_note_block(build):
    assert "note" not in _all_text(build()).lower()


@pytest.mark.parametrize(
    ("viewer_id", "sentence"),
    [
        pytest.param(11, "You and **N22** are no longer connected.", id="actor"),
        pytest.param(22, "You and **N11** are no longer connected.", id="other"),
        pytest.param(
            None, "**N11** and **N22** are no longer connected.", id="channel"
        ),
    ],
)
def test_revoked_names_the_other_party_and_not_who_pressed_remove(viewer_id, sentence):
    """Symmetric on purpose: a card that said who removed it reads as an
    accusation, and the other party can work it out. A channel card (no
    viewer) is read by both, so it names both."""
    assert sentence in _all_text(
        build_revoked_layout(
            viewer_id=viewer_id, actor_id=11, other_id=22, name_fn=NAME_FN
        )
    )


def test_audit_notice_timestamps_itself_in_subtext():
    """``embed.timestamp`` has no layout equivalent, so the record card
    carries the time as a Discord timestamp in its ``-#`` line."""
    text = _all_text(
        build_audit_notice_layout(message="Ada asked Bo", timestamp=1_750_000_000)
    )
    assert "Ada asked Bo" in text
    assert "-# <t:1750000000:f>" in text


def test_audit_notice_is_one_text_display():
    """A mod feed is a log, not a card per line."""
    items = build_audit_notice_layout(message="Ada asked Bo", timestamp=1)
    assert len(items) == 1


# ── Paradigm 4: the request form's radio ────────────────────────────────


def test_request_type_options_describe_both_choices_and_default_to_dm():
    options = request_type_options()
    assert [o.value for o in options] == ["dm", "friend"]
    assert all(o.description for o in options)
    assert [o.value for o in options if o.default] == ["dm"]


# ── One name per thing, one glyph per outcome ───────────────────────────
#
# The design pass of 2026-09-20 found the same panel called "Settings", "My
# DM Settings" and "the server's DM panel" across the twenty cards, and a
# connection's kind spelled "Direct Message" on one card and "DM" on the
# next. Guide § Words fixes both; these sweeps fail the next card that
# drifts.

#: Names the cards used to use for things they now have one name for.
_OLD_NAMES = ("Direct Message", "My DM Settings", "DM panel", "Decline With")

#: The glyph each outcome leads with (guide § Words). A card headed by any
#: other glyph is inventing a new kind of outcome.
_HEADING_GLYPHS = {"✅", "❌", "🚫", "⌛", "⚠️", "📨", "📬", "📜"}


@pytest.mark.parametrize("build", ALL_CARDS)
def test_cards_use_one_name_per_thing(build):
    text = _all_text(build())
    assert not [old for old in _OLD_NAMES if old in text], text


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_heading_glyph_is_in_the_outcome_vocabulary(build):
    heading = _lead_heading(build())
    glyph = heading[3:].split(" ", 1)[0]
    assert glyph in _HEADING_GLYPHS, heading


# ── the hub banner (channel panel only) ─────────────────────────────────
# Billy, 2026-09-26: the banner goes on "just the main panels that live in
# the channel", never an ephemeral card. The DM hub draws it as the
# message's first item, above the title — on the message, not in a box, so
# the art's gold frame (drawn at the picture's edge) and the text and
# buttons below share one edge (Ben, 2026-09-29). The cards the hub opens
# never take one (the builders have no parameter for it, and
# test_layout_hub_banner pins the callers).


def test_panel_banner_opens_the_message_above_the_title():
    first, second = build_panel_layout(banner_url=BANNER)[:2]
    assert isinstance(first, discord.ui.MediaGallery)
    assert [i.media.url for i in first.items] == [BANNER]
    assert isinstance(second, discord.ui.Section)  # the title row follows


def test_panel_footer_closes_the_message():
    """The footer strip is the last item, under the last blurb."""
    children = build_panel_layout(banner_url=BANNER, footer_url=FOOTER)
    assert isinstance(children[-1], discord.ui.MediaGallery)
    assert [i.media.url for i in children[-1].items] == [FOOTER]
    assert _is_text(children[-2], "-# Open, Ask or Closed")
    assert len(children) == len(_panel_parts()) + 2


def test_panel_without_a_banner_is_unchanged():
    parts = build_panel_layout(banner_url=None, footer_url=None)
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in parts)
    assert len(parts) == len(_panel_parts())
