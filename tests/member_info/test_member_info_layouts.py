"""The ``/info`` card's Components V2 builder.

Budget and shape on the fullest card the builder can produce, plus every
regression the embed it replaced carried: no moderation fields, the roles
line capped by length, an activity section that doesn't contradict its own
heading, and sections that vanish rather than render empty.
"""

from __future__ import annotations

from typing import Any

import discord
import pytest

from bot_modules.core.layout import (
    MAX_ACCESSORY_LABEL,
    check_budget,
    text_columns,
)
from bot_modules.member_info import layouts as mi
from bot_modules.member_info.logic import (
    AccountFacts,
    FeatureState,
    build_optin_rows,
)
from bot_modules.member_info.views import MemberInfoView

ALL_KEYS = (
    "pen_pals", "whispers", "guess", "dm_mode", "wellness", "birthday",
    "no_contact", "my_data",
)


def _facts(**kwargs) -> AccountFacts:
    base: dict[str, Any] = dict(
        account_age_days=400,
        created_ts=1_700_000_000,
        joined_ts=1_710_000_000,
        role_names=["Denizen"],
        level=7,
        total_xp=12_345,
        xp_by_source={"text": 10_000, "voice": 2_345},
        msgs_30d=312,
        top_channels=[(123, 90)],
        last_seen_ts=1_720_000_000,
        level_floor_xp=10_800,
        next_level_xp=14_700,
        xp_rank=(12, 340),
    )
    base.update(kwargs)
    return AccountFacts(**base)


def _build(**kwargs):
    base: dict[str, Any] = dict(display_name="Ada", avatar_url=None, facts=_facts(), optin_rows=[])
    base.update(kwargs)
    return mi.build_member_info_layout(**base)


def _view(items) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


def _texts(items) -> list[str]:
    return [
        c.content for c in _view(items).walk_children()
        if isinstance(c, discord.ui.TextDisplay)
    ]


def _section(items, heading: str) -> str:
    return next(t for t in _texts(items) if t.startswith(f"### {heading}"))


def _fullest(**kwargs):
    rows = build_optin_rows({key: FeatureState(configured=True) for key in ALL_KEYS})
    return _build(
        avatar_url="https://cdn.example/a.png",
        facts=_facts(
            role_names=["R" * 100 for _ in range(40)],
            xp_by_source={f"source_{i}": 1234 for i in range(30)},
            top_channels=[(10**18 + i, 999) for i in range(25)],
        ),
        optin_rows=rows,
        wallet_line="🪙 **999,999** coins · 🔥 400-day streak (best: 500)",
        wallet_extra=[f"**Perk {i}** — 🪙 500/wk" for i in range(30)],
        help_lines=["`/ask` — ask Poppy.", "The Help guide lists everything."],
        chart_filename="info_activity.png",
        **kwargs,
    ), rows


# ── Shape ────────────────────────────────────────────────────────────────


def test_the_fullest_card_fits_the_budget():
    """Every opt-in configured, worst-case text everywhere, chart attached."""
    items, _ = _fullest()
    check_budget(_view(items))


def test_card_is_bare_and_leads_with_its_titled_heading():
    items, _ = _fullest()
    assert not any(isinstance(i, discord.ui.Container) for i in items)
    head = items[0]
    assert isinstance(head, discord.ui.Section)
    assert isinstance(head.accessory, discord.ui.Thumbnail)
    assert _texts([head])[0].startswith(f"## {mi.INFO_TITLE}\n")


def test_without_an_avatar_the_heading_is_plain_text():
    items = _build()
    assert isinstance(items[0], discord.ui.TextDisplay)
    assert items[0].content.startswith(f"## {mi.INFO_TITLE}\n")


def test_display_name_markdown_is_escaped():
    assert "**Ada\\_x**" in _texts(_build(display_name="Ada_x"))[0]


def test_every_optin_button_is_grey_and_fits_beside_its_row():
    items, rows = _fullest()
    buttons = [c for c in _view(items).walk_children() if isinstance(c, discord.ui.Button)]
    assert len(buttons) == len(rows)
    for button in buttons:
        assert button.style is discord.ButtonStyle.secondary
        assert text_columns(button.label or "") <= MAX_ACCESSORY_LABEL


def test_the_chart_is_a_described_gallery_only_when_attached():
    items, _ = _fullest()
    (gallery,) = [i for i in items if isinstance(i, discord.ui.MediaGallery)]
    assert gallery.items[0].media.url == "attachment://info_activity.png"
    assert gallery.items[0].description
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in _build())


# ── Content ──────────────────────────────────────────────────────────────


def test_card_has_no_moderation_fields():
    """The member-facing card must not grow /modinfo's mod-only sections."""
    items, _ = _fullest()
    blob = " ".join(_texts(items)).lower()
    for forbidden in ("watch", "warning", "jail", "ticket"):
        assert forbidden not in blob


def test_joined_line_shows_time_since_joining_not_a_date():
    """Mods asked how long someone has been here, not on what day they came."""
    blob = " ".join(_texts(_build()))
    assert "Joined: <t:1710000000:R>" in blob
    assert "<t:1710000000:D>" not in blob


def test_level_shows_bar_rank_and_what_is_left():
    level = _section(_build(), "⭐ Level")
    assert level.startswith("### ⭐ Level 7\n")
    # 12,345 is 1,545 into a 3,900-XP level: 39.6%, truncated to 39.
    assert "`▰▰▰▰▱▱▱▱▱▱ 39%` · 2,355 XP to Level 8" in level
    assert "12,345 XP · Rank #12 of 340" in level


def test_level_bar_never_claims_100_before_the_level():
    level = _section(
        _build(facts=_facts(total_xp=14_699, next_level_xp=14_700)), "⭐ Level"
    )
    assert "99%" in level and "1 XP to Level 8" in level


def test_level_without_rank_or_bar_still_reads():
    level = _section(
        _build(facts=_facts(xp_rank=None, level_floor_xp=None)), "⭐ Level"
    )
    assert "Rank" not in level and "▰" not in level
    assert "2,355 XP to Level 8" in level


def test_a_brand_new_member_gets_a_card_with_nothing_empty():
    """No XP, no messages, no roles, no opt-ins — still a valid card."""
    items = _build(
        facts=_facts(
            level=None, total_xp=0.0, xp_by_source={}, msgs_30d=0, top_channels=[],
            last_seen_ts=None, role_names=[], joined_ts=None,
            level_floor_xp=None, next_level_xp=None, xp_rank=None,
        )
    )
    assert all(t.strip() for t in _texts(items))
    assert "No XP yet" in _section(items, "⭐ Level")
    assert "None yet." in _section(items, "🎭 Roles")


@pytest.mark.parametrize(
    ("kwargs", "heading", "present"),
    [
        pytest.param({}, "💰 Wallet", False, id="no-economy-no-wallet"),
        pytest.param({"wallet_line": "🪙 **500** coins"}, "💰 Wallet", True, id="wallet"),
        pytest.param({}, "🔔 Your Opt-Ins", False, id="no-optins-no-heading"),
    ],
)
def test_optional_sections_vanish_rather_than_render_empty(kwargs, heading, present):
    texts = _texts(_build(**kwargs))
    assert any(t.startswith(f"### {heading}") for t in texts) is present


def test_every_optin_row_is_listed_by_name():
    items, rows = _fullest()
    blob = "\n".join(_texts(items))
    for row in rows:
        assert f"**{row.emoji} {row.label}**" in blob


def test_an_unreachable_flow_shows_its_status_without_a_button():
    rows = build_optin_rows(
        {key: FeatureState(configured=True, actionable=False) for key in ALL_KEYS}
    )
    items = _build(optin_rows=rows)
    assert not any(isinstance(i, discord.ui.Section) for i in items)
    assert "**✉️ Pen Pals**" in "\n".join(_texts(items))


def test_help_lines_are_the_footer_and_only_when_given():
    items = _build(help_lines=["`/ask` — ask Meadow-bot how anything here works."])
    assert _texts(items)[-1].startswith("-# `/ask` — ask Meadow-bot")
    assert "Meadow-bot" not in "\n".join(_texts(_build()))


# ── Regression: the roles line must never overrun its share ──────────────
# Discord allows 100-character role names, so the count cap alone let twelve
# of them build a 1233-character embed field, and Discord rejected the whole
# card — past the command's defer(), a card that never arrives.


def test_roles_line_is_capped_and_reports_everything_it_dropped():
    roles = _section(
        _build(facts=_facts(role_names=["R" * 100 for _ in range(40)])), "🎭 Roles"
    )
    body = roles.split("\n", 1)[1]
    assert len(body) <= mi.SECTION_LIMIT
    shown = body.count("R" * 100)
    assert f"+{40 - shown} more" in body


# ── Regression: activity must not contradict its own heading ────────────
# Discord evicts a thread from the guild cache when it archives, and
# processed_messages holds only the thread's own id — so a member whose month
# was spent in now-archived threads has a real count and nothing nameable.


@pytest.mark.parametrize(
    ("msgs_30d", "says_nothing_recorded"),
    [
        pytest.param(150, False, id="messages-in-unnameable-places"),
        pytest.param(0, True, id="genuinely-nothing"),
    ],
)
def test_activity_empty_case(msgs_30d, says_nothing_recorded):
    activity = _section(
        _build(facts=_facts(msgs_30d=msgs_30d, top_channels=[])), "💬 Activity"
    )
    assert f"— {msgs_30d:,} msgs" in activity
    assert ("No messages recorded" in activity) is says_nothing_recorded


# ── The view binds exactly the builder's buttons ─────────────────────────


def test_view_binds_a_callback_to_every_actionable_row():
    items, rows = _fullest()
    view = MemberInfoView(object(), items, rows)
    for index, row in enumerate(rows):
        button = view.find_item(mi.OPTIN_BUTTON_ID_BASE + index)
        assert button is not None, row.key
        assert button.callback.__qualname__.startswith("_optin_callback")
