"""Server Features — card builders and the hub's wiring.

Budget and shape checks on the fullest card each builder can produce, plus the
hub's persistent-id contract: the ids are frozen once a panel is posted.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import (
    MAX_ACCESSORY_LABEL,
    check_budget,
    text_columns,
)
from bot_modules.server_features import layouts as sf
from bot_modules.server_features.logic import (
    MAX_MENU_OPTIONS,
    MAX_MENUS,
    MAX_MENUS_WITH_REPLY_DMS,
    HOLD_TEXT,
    MenuOption,
    PingRow,
    RoleCard,
    RoleMenu,
)
from bot_modules.no_contact.logic import OwnEntry


def _names(uid: int) -> str:
    return f"Member{uid}"


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _fullest_roles_card(*, reply_dms: bool | None = None) -> RoleCard:
    pings = tuple(
        PingRow(key=f"k{i}", role_id=i, label="Game Lobbies", emoji="🎮",
                blurb="Get pinged when a party-game lobby opens.", held=bool(i % 2))
        for i in range(5)
    )
    menus = tuple(
        RoleMenu(
            prompt_id=m, title="What are you into?", single_select=False, required=False,
            options=tuple(
                MenuOption(label=f"Option {o}", description="x" * 100, emoji="",
                           role_ids=(m * 100 + o,), selected=o == 0)
                for o in range(MAX_MENU_OPTIONS)
            ),
        )
        for m in range(MAX_MENUS if reply_dms is None else MAX_MENUS_WITH_REPLY_DMS)
    )
    return RoleCard(pings=pings, menus=menus, menus_truncated=True, reply_dms=reply_dms)


def _entries(n: int, *, removable: bool = True) -> list[OwnEntry]:
    return [OwnEntry(other_id=10 + i, removable=removable, set_by_moderator=not removable)
            for i in range(n)]


def _texts(items) -> str:
    view = _view(items)
    return "\n".join(
        c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)
    )


ALL_CARDS = [
    pytest.param(lambda: sf.build_roles_layout(_fullest_roles_card(), note="Saved."), id="roles-full"),
    pytest.param(
        lambda: sf.build_roles_layout(_fullest_roles_card(reply_dms=True), note="Saved."),
        id="roles-full-with-reply-dms",
    ),
    pytest.param(lambda: sf.build_roles_layout(RoleCard((), ())), id="roles-empty"),
    pytest.param(lambda: sf.build_roles_layout(RoleCard((), ()), held=True), id="roles-held"),
    pytest.param(lambda: sf.build_no_contact_layout(_entries(25), name_fn=_names, page=1), id="nc-paged"),
    pytest.param(lambda: sf.build_no_contact_layout([], name_fn=_names), id="nc-empty"),
    pytest.param(lambda: sf.build_remove_confirm_layout(user_id=1, name_fn=_names), id="nc-confirm"),
    pytest.param(lambda: sf.build_no_contact_answer_layout("✅ Done."), id="nc-answer"),
]


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_card_fits_the_budget(build):
    check_budget(_view(build()))


@pytest.mark.parametrize("build", ALL_CARDS)
def test_every_ephemeral_card_is_bare_and_titled(build):
    items = build()
    assert not any(isinstance(i, discord.ui.Container) for i in items)
    first = items[0]
    assert isinstance(first, discord.ui.TextDisplay)
    heading = first.content.splitlines()[0]
    assert heading.startswith("## ") and not heading[3].isalnum()


def test_hub_fits_and_is_bare_between_its_art():
    """No box since 2026-09-29: the banner opens the panel and the footer
    closes it, both directly on the message (style guide § No container)."""
    items = sf.build_hub_layout(banner_url="https://x/b.png", footer_url="https://x/f.png")
    check_budget(_view(items))
    assert not any(
        isinstance(i, discord.ui.Container) for i in _view(items).walk_children()
    )
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)


def test_hub_carries_every_frozen_id_on_one_size_of_button():
    view = _view(sf.build_hub_layout())
    buttons = [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert {b.custom_id for b in buttons} == {
        "server_features:ticket", "server_features:ask", "server_features:roles",
        "server_features:ask_to_dm", "server_features:dms", "server_features:break",
        "server_features:no_contact", "server_features:my_data", "server_features:info",
    }
    assert {b.label for b in buttons} == {sf.OPEN_LABEL}
    assert text_columns(sf.OPEN_LABEL) <= MAX_ACCESSORY_LABEL


def test_title_in_the_banner_drops_the_heading():
    """The DM Requests hub's pattern: with the title drawn into the banner
    the ``##`` line would say it twice, so it goes and the alt text names
    the panel for a screen reader."""
    items = sf.build_hub_layout(banner_url="https://x/b.png", title_in_banner=True)
    gallery = items[0]
    assert isinstance(gallery, discord.ui.MediaGallery)
    assert [i.description for i in gallery.items] == [sf.SERVER_FEATURES_PANEL_TITLE]
    assert not any(line.startswith("## ") for line in _texts(items).splitlines())


@pytest.mark.parametrize(
    "banner_url, title_in_banner",
    [
        pytest.param(None, True, id="no-banner-keeps-heading"),
        pytest.param("https://x/b.png", False, id="switch-off-keeps-heading"),
    ],
)
def test_the_heading_stays_unless_the_banner_carries_it(banner_url, title_in_banner):
    items = sf.build_hub_layout(banner_url=banner_url, title_in_banner=title_in_banner)
    assert f"## 🧭 {sf.SERVER_FEATURES_PANEL_TITLE}" in _texts(items)


def test_about_you_group_sits_above_safety():
    """Billy, 2026-09-26: "do it above safety"."""
    text = _texts(sf.build_hub_layout())
    assert text.index("### Your Settings") < text.index("### About You")
    assert text.index("### About You") < text.index("ℹ️ Your Info")
    assert text.index("ℹ️ Your Info") < text.index("### Safety & Privacy")


def test_ask_to_dm_sits_above_dm_settings_under_your_settings():
    """Ben, 2026-09-30: the DM Requests panel's Open Form button "to the left
    of the dm preferences" — reaching out, then your own preference, the
    order that panel keeps."""
    text = _texts(sf.build_hub_layout())
    assert text.index("### Your Settings") < text.index("📨 Ask to DM")
    assert text.index("📨 Ask to DM") < text.index("📬 DM Settings")
    assert text.index("📬 DM Settings") < text.index("### About You")


def test_hub_names_the_guilds_assistant():
    assert "Ask Bramble" in _texts(sf.build_hub_layout(assistant_name="Bramble"))


# ── Roles & Pings ────────────────────────────────────────────────────────


def test_roles_card_says_each_ping_state_in_words():
    text = _texts(sf.build_roles_layout(_fullest_roles_card()))
    assert "· On" in text and "· Off" in text


def test_roles_card_preselects_and_bounds_each_menu():
    card = _fullest_roles_card()
    view = _view(sf.build_roles_layout(card))
    select = view.find_item(sf.MENU_SELECT_ID_BASE)
    assert isinstance(select, discord.ui.Select)
    assert [o.default for o in select.options][:2] == [True, False]
    assert (select.min_values, select.max_values) == (0, MAX_MENU_OPTIONS)
    assert select.options[0].value == card.menus[0].options[0].value


def test_a_truncated_card_points_at_customize():
    assert "Channels & Roles" in _texts(sf.build_roles_layout(_fullest_roles_card()))


def test_a_held_member_is_offered_nothing():
    items = sf.build_roles_layout(_fullest_roles_card(reply_dms=False), held=True)
    view = _view(items)
    assert HOLD_TEXT in _texts(items)
    assert view.find_item(sf.PING_BUTTON_ID_BASE) is None
    assert view.find_item(sf.MENU_SELECT_ID_BASE) is None
    assert view.find_item(sf.REPLY_DMS_BUTTON_ID) is None


@pytest.mark.parametrize(
    ("reply_dms", "state", "label"),
    [
        pytest.param(False, "· Off", "Turn On", id="off"),
        pytest.param(True, "· On", "Turn Off", id="on"),
    ],
)
def test_the_reply_dms_row_says_its_state_and_what_a_press_does(reply_dms, state, label):
    card = RoleCard((), (), reply_dms=reply_dms)
    items = sf.build_roles_layout(card)
    button = _view(items).find_item(sf.REPLY_DMS_BUTTON_ID)
    assert isinstance(button, discord.ui.Button) and button.label == label
    text = _texts(items)
    assert "### Bot DMs" in text and f"Replies to Your Threads** {state}" in text


def test_no_reply_dms_row_when_the_guild_offers_none():
    view = _view(sf.build_roles_layout(_fullest_roles_card()))
    assert view.find_item(sf.REPLY_DMS_BUTTON_ID) is None
    assert "Bot DMs" not in _texts(sf.build_roles_layout(_fullest_roles_card()))


# ── No Contact ───────────────────────────────────────────────────────────


def test_only_a_removable_entry_gets_a_button():
    entries = [*_entries(1), *_entries(1, removable=False)]
    entries[1] = OwnEntry(other_id=99, removable=False, set_by_moderator=True)
    items = sf.build_no_contact_layout(entries, name_fn=_names)
    view = _view(items)
    assert view.find_item(sf.REMOVE_BUTTON_ID_BASE) is not None
    assert view.find_item(sf.REMOVE_BUTTON_ID_BASE + 1) is None
    assert "**Member99** · set by a moderator" in _texts(items)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda fn: sf.build_no_contact_layout(_entries(1), name_fn=fn), id="list"),
        pytest.param(
            lambda fn: sf.build_no_contact_layout(_entries(1, removable=False), name_fn=fn),
            id="moderator-row",
        ),
        pytest.param(lambda fn: sf.build_remove_confirm_layout(user_id=10, name_fn=fn), id="confirm"),
    ],
)
def test_a_name_cannot_break_out_of_its_bold(build):
    """Every name here sits inside the card's own ``**``: a display name
    carrying markdown would close it early and restyle the rest."""
    text = _texts(build(lambda _uid: "**Root**"))
    assert "\\*\\*Root\\*\\*" in text


@pytest.mark.parametrize(
    ("count", "page", "expected"),
    [
        pytest.param(0, 0, (0, 1, 0), id="empty-is-one-page"),
        pytest.param(10, 0, (0, 1, 10), id="exactly-ten"),
        pytest.param(11, 1, (1, 2, 1), id="second-page"),
        pytest.param(11, 5, (1, 2, 1), id="past-the-end-clamps"),
    ],
)
def test_list_page(count, page, expected):
    got_page, pages, rows = sf.list_page(_entries(count), page)
    assert (got_page, pages, len(rows)) == expected


def test_arrows_only_with_a_second_page():
    one = _view(sf.build_no_contact_layout(_entries(3), name_fn=_names))
    two = _view(sf.build_no_contact_layout(_entries(11), name_fn=_names))
    assert one.find_item(sf.PREV_PAGE_ID) is None
    assert two.find_item(sf.PREV_PAGE_ID).disabled is True
    assert two.find_item(sf.NEXT_PAGE_ID).disabled is False


def test_no_contact_row_reassures_in_one_sentence():
    """Billy, 2026-09-27: "They aren't told." read as a warning. The row says
    what the button does and keeps the anonymity promise in one sentence."""
    text = _texts(sf.build_hub_layout())
    assert sf.NO_CONTACT_BLURB in text
    assert "They aren't told." not in text
    assert sf.NO_CONTACT_BLURB.count(".") == 1
    assert "anonymous" in sf.NO_CONTACT_BLURB


def test_hub_is_named_help_and_settings():
    """Bug-list row 262 (Billy, 2026-09-27). Only the copy is renamed; the ids
    stay frozen (``test_hub_carries_every_frozen_id_on_one_size_of_button``)."""
    assert sf.SERVER_FEATURES_PANEL_TITLE == "Help & Settings"
    assert "## 🧭 Help & Settings" in _texts(sf.build_hub_layout())


@pytest.mark.parametrize(
    ("build", "sentence", "retired"),
    [
        pytest.param(sf.build_hub_layout, "Everything here is yours to use or change.",
                     "Everything that's yours", id="hub-intro"),
        pytest.param(sf.build_hub_layout, "Take time away without losing your roles.",
                     "Step away;", id="break-row"),
        pytest.param(sf.build_hub_layout, "See your level, rank, activity, roles and opt-ins.",
                     "Your level and rank", id="info-row"),
        pytest.param(sf.build_hub_layout, "See what's stored about you and delete what's yours.",
                     "stored; delete", id="my-data-row"),
        pytest.param(lambda: sf.build_no_contact_layout([], name_fn=_names),
                     sf.NO_CONTACT_CARD_INTRO, "Anyone on your list can't", id="no-contact-card"),
    ],
)
def test_copy_reads_as_whole_sentences(build, sentence, retired):
    """The 2026-09-28 copy pass: no semicolon splices or clipped fragments."""
    text = _texts(build())
    assert sentence in text
    assert retired not in text


def test_no_contact_card_names_dm_requests_and_games():
    """The old intro listed six surfaces and missed DM requests, payments,
    bounties and games, which the list also blocks."""
    assert "DM request" in sf.NO_CONTACT_CARD_INTRO
    assert "game" in sf.NO_CONTACT_CARD_INTRO
    assert "They are **not** told." not in sf.NO_CONTACT_CARD_INTRO
