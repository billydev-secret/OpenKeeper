"""The economy channel panel as a V2 hub — ids, order, frame, budget.

The section text is ``leaderboard.panel_sections``' and is tested there
(``test_economy_leaderboard``); this file covers the frame the layout puts
around it and the view that binds its buttons.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.economy.cog.views import EconomyPanelView
from bot_modules.core.layout import check_budget
from bot_modules.economy.bank_panel_layout import (
    INTRO,
    build_economy_panel_layout,
    economy_panel_title,
)
from bot_modules.economy.guide import (
    GUIDE_TOPIC_CUSTOM_ID,
    HOW_IT_WORKS_CUSTOM_ID,
    HOW_IT_WORKS_HEADING,
    NOTIFY_CUSTOM_ID,
    TOPIC_PLACEHOLDER,
    guide_topics,
)
from bot_modules.economy.leaderboard import (
    BOARD_HEADING,
    FEED_HEADING,
    TOP_EARNERS_HEADING,
    CommunityGoal,
    FeedLine,
    LeaderboardData,
    QuestLine,
)
from bot_modules.economy.quest_views import QUEST_BOARD_CUSTOM_ID, WALLET_CUSTOM_ID
from bot_modules.economy.service import EconSettings

NOW = 1_700_000_000.0
BANNER = "https://dash.example/static/panel-banners/" + "c" * 32 + ".webp"
FOOTER = "https://dash.example/static/panel-banners/" + "e" * 32 + ".webp"


def _names(uid: int) -> str:
    return f"Member {uid}"


def _data() -> LeaderboardData:
    return LeaderboardData(
        top_earners=[(1, 120), (2, 80)],
        community=[CommunityGoal("Goal", 1, 10, completed=False, settled=False)],
        quests=[QuestLine("daily", "Chatter", 5, 0)],
        feed=(FeedLine("Say hi", 3, NOW - 60),),
    )


def _items(**kw) -> list[discord.ui.Item]:
    items = build_economy_panel_layout(
        EconSettings(), kw.pop("data", None) or _data(), _names, now_ts=NOW, **kw
    )
    assert not any(isinstance(i, discord.ui.Container) for i in items), (
        "the hub is bare — its parts sit on the message, not in a box"
    )
    return items


def _view(**kw) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView()
    for item in build_economy_panel_layout(
        kw.pop("settings", None) or EconSettings(),
        kw.pop("data", None) or _data(), _names, now_ts=NOW, **kw
    ):
        view.add_item(item)
    return view


def _button_ids(view: discord.ui.LayoutView) -> list[str]:
    return [
        i.custom_id
        for i in view.walk_children()
        if isinstance(i, discord.ui.Button) and i.custom_id
    ]


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


def test_ids_are_frozen():
    # Panels already posted in a channel carry these; renaming one strands
    # every live button until the sticky repaints.
    assert HOW_IT_WORKS_CUSTOM_ID == "econ_guide_howto"
    assert GUIDE_TOPIC_CUSTOM_ID == "econ_guide_topic"
    assert NOTIFY_CUSTOM_ID == "econ_guide_notify"
    assert QUEST_BOARD_CUSTOM_ID == "econ:show_my_quests"
    assert WALLET_CUSTOM_ID == "econ:show_my_wallet"


def test_help_beside_the_title_then_the_row_at_the_foot():
    assert _button_ids(_view()) == [
        HOW_IT_WORKS_CUSTOM_ID,
        QUEST_BOARD_CUSTOM_ID,
        WALLET_CUSTOM_ID,
        NOTIFY_CUSTOM_ID,
    ]
    items = _items()
    head = items[0]
    assert isinstance(head, discord.ui.Section)
    assert head.accessory.label == "Help"
    assert head.accessory.custom_id == HOW_IT_WORKS_CUSTOM_ID
    (heading,) = head.children
    assert heading.content == "## 🪙 Coins — The Bank"
    assert items[1].content == INTRO
    # the button row sits under the board, above the live stamp
    row_at = next(
        n for n, i in enumerate(items)
        if isinstance(i, discord.ui.ActionRow)
        and all(isinstance(c, discord.ui.Button) for c in i.children)
    )
    assert items[row_at + 1].content.startswith("-# ⚡ Live")
    assert row_at == len(items) - 2


def test_how_it_works_sits_between_the_board_and_the_buttons():
    """Billy, 2026-09-28: the guide's headlines on the panel, one topic a
    pick away — after the board, which is what the channel is for."""
    items = _items()
    block_at = next(
        n for n, i in enumerate(items)
        if isinstance(i, discord.ui.TextDisplay)
        and i.content.startswith(f"### {HOW_IT_WORKS_HEADING}")
    )
    block, row = items[block_at], items[block_at + 1]
    assert isinstance(items[block_at - 1], discord.ui.Separator)
    assert isinstance(items[block_at + 2], discord.ui.Separator)
    text = _text(_view())
    assert text.index(f"### {FEED_HEADING}") < text.index(f"### {HOW_IT_WORKS_HEADING}")
    for topic in guide_topics(EconSettings()):
        assert topic.headline in block.content
        assert topic.body not in block.content  # headlines only on the panel
    (select,) = row.children
    assert isinstance(select, discord.ui.Select)
    assert select.custom_id == GUIDE_TOPIC_CUSTOM_ID
    assert select.placeholder == TOPIC_PLACEHOLDER
    assert [o.value for o in select.options] == [
        "notifications", "earning", "spending", "streaks"
    ]


def test_how_it_works_follows_the_guild_dials():
    def block(settings):
        items = build_economy_panel_layout(settings, _data(), _names, now_ts=NOW)
        return next(
            i.content for i in items
            if isinstance(i, discord.ui.TextDisplay)
            and i.content.startswith(f"### {HOW_IT_WORKS_HEADING}")
        )

    assert "pay Coins to a member" in block(EconSettings(transfers_enabled=True))
    assert "pay Coins to a member" not in block(EconSettings(transfers_enabled=False))


def test_sections_in_order_without_the_pulse():
    text = _text(_view())
    order = [text.index(f"### {h}") for h in (TOP_EARNERS_HEADING, BOARD_HEADING, FEED_HEADING)]
    assert order == sorted(order)
    assert "Pulse" not in text


def test_registration_view_is_persistent_and_routes_every_id():
    view = EconomyPanelView()
    assert view.timeout is None
    assert view.is_persistent()
    assert sorted(_button_ids(view)) == sorted(
        [HOW_IT_WORKS_CUSTOM_ID, QUEST_BOARD_CUSTOM_ID, WALLET_CUSTOM_ID, NOTIFY_CUSTOM_ID]
    )
    selects = [i for i in view.walk_children() if isinstance(i, discord.ui.Select)]
    assert [s.custom_id for s in selects] == [GUIDE_TOPIC_CUSTOM_ID]
    for item in view.walk_children():
        if isinstance(item, (discord.ui.Button, discord.ui.Select)):
            # core.layout.bind sets the callback on the instance
            assert "callback" in vars(item), item.custom_id


def test_no_box_banner_first_footer_last():
    items = _items(banner_url=BANNER, footer_url=FOOTER)
    first, last = items[0], items[-1]
    assert isinstance(first, discord.ui.MediaGallery)
    assert [m.media.url for m in first.items] == [BANNER]
    assert isinstance(last, discord.ui.MediaGallery)
    assert [m.media.url for m in last.items] == [FOOTER]
    plain = _items()
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in plain)


def test_title_in_the_banner_moves_help_beside_the_intro():
    items = _items(banner_url=BANNER, title_in_banner=True)
    banner, head = items[0], items[1]
    assert banner.items[0].description == "Coins — The Bank"
    assert isinstance(head, discord.ui.Section)
    (intro,) = head.children
    assert intro.content == INTRO
    assert head.accessory.custom_id == HOW_IT_WORKS_CUSTOM_ID
    lines = _text(_view(banner_url=BANNER, title_in_banner=True)).splitlines()
    assert not any(line.startswith("## ") for line in lines)


def test_title_in_the_banner_needs_a_banner():
    assert "## 🪙 Coins — The Bank" in _text(_view(title_in_banner=True))


@pytest.mark.parametrize(
    "settings, title",
    [
        (EconSettings(), "Coins — The Bank"),
        (EconSettings(currency_plural="Gems", currency_emoji="💎"), "Gems — The Bank"),
    ],
)
def test_title_carries_no_emoji(settings, title):
    # it is the string a banner font draws
    assert economy_panel_title(settings) == title


def test_fullest_panel_fits_the_budget():
    data = LeaderboardData(
        top_earners=[(uid, 10_000 - uid) for uid in range(1, 6)],  # TOP_N
        community=[
            CommunityGoal(
                f"Goal number {i} with a long enough title to eat budget",
                i, 100, completed=False, settled=False, auto=True,
                kind_flavor="a flavor line that also takes up meaningful room",
                contributors=12, today_delta=340,
            )
            for i in range(30)
        ],
        quests=[QuestLine("daily", f"Quest {i}", 5, 0) for i in range(10)],
        today_by_user={uid: 500 for uid in range(1, 6)},
        feed=tuple(FeedLine(f"Quest title {i}", 9, NOW - i) for i in range(10)),
        set_bonuses_today=3,
        day_roll_ts=NOW + 3600,
        week_roll_ts=NOW + 7200,
        casino_biggest=(1, 3000),
        casino_luckiest=(2, 1250),
    )
    # Every dial that lengthens a How it works gist, on.
    settings = EconSettings(
        xp_per_coin=15.0, conversion_daily_cap=1200, transfers_enabled=True,
        booster_multiplier=2.0, demurrage_rate_pct=3,
        currency_plural="Sparkling Gemstones",
    )
    check_budget(_view(settings=settings, data=data, banner_url=BANNER, footer_url=FOOTER))
