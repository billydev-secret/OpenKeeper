"""Help sits beside the title on every game card (Ben, 2026-10-01): the AMA
board, the Name Your Price submission card and the Anonymous Truth or Dare
prompt card had it in the bottom button row."""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import card
from bot_modules.games.ama import layouts as ama
from bot_modules.games.ama.logic import AMA_FORMAT_HOT_SEAT
from bot_modules.games.ffa import layouts as ffa
from bot_modules.games.price import layouts as price
from bot_modules.shared.name_resolver import mention

CASES = [
    pytest.param(
        lambda: ama.build_board_layout(host_name="Ada", mode="unfiltered", game_format=AMA_FORMAT_HOT_SEAT, name_fn=mention),
        ama.HELP_ID, id="ama",
    ),
    pytest.param(
        lambda: price.build_scenario_layout("Eat a bug", 2, 5, 1_700_000_000, 3, 4),
        price.HELP_ID, id="price",
    ),
    pytest.param(lambda: ffa.build_prompt_layout("Q", "TRUTH"), ffa.HELP_ID, id="ffa"),
]


@pytest.mark.parametrize(("build", "help_id"), CASES)
def test_help_is_the_title_sections_accessory_not_a_bottom_row_button(build, help_id):
    items = build()
    first = card(items).children[0]
    assert isinstance(first, discord.ui.Section)
    assert first.accessory.custom_id == help_id
    assert first.accessory.label == "❓ Help"
    rows = [i for i in card(items).children if isinstance(i, discord.ui.ActionRow)]
    assert all(b.custom_id != help_id for r in rows for b in r.children)
