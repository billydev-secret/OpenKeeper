"""The nine host panels are one shared card (``round_layouts.build_host_panel_layout``).

Moving the hand-written copies onto it must not change a character a host
sees: the heading, the blurb, the ``/games host`` pointer and every button's
label, style and id are pinned here."""

from __future__ import annotations

from typing import Any

import discord
import pytest

from bot_modules.games.ama import layouts as ama
from bot_modules.games.ama.logic import AMA_FORMAT_HOT_SEAT, AMA_FORMAT_PANEL
from bot_modules.games.fantasies import layouts as fantasies
from bot_modules.games.ffa import layouts as ffa
from bot_modules.games.hottakes import layouts as hottakes
from bot_modules.games.legitlibs import layouts as legitlibs
from bot_modules.games.price import layouts as price
from bot_modules.games.rushmore import layouts as rushmore
from bot_modules.games.traditional import layouts as traditional
from bot_modules.games.ttl import layouts as ttl
from bot_modules.games.utils import round_layouts

GREY, RED = discord.ButtonStyle.secondary, discord.ButtonStyle.danger
POINTER = "-# Only you can see this. /games host brings it back."


def _shape(items: list[discord.ui.Item[Any]]) -> tuple[str, list[list[tuple[str, Any, str]]]]:
    text, rows = "", []
    for item in items:
        if isinstance(item, discord.ui.TextDisplay):
            text = item.content
        else:
            rows.append([(b.label, b.style, b.custom_id) for b in item.children])  # type: ignore[union-attr]
    return text, rows


CASES = [
    pytest.param(
        lambda: ama.build_host_panel_layout(AMA_FORMAT_HOT_SEAT),
        f"## {ama.ICON} Host Controls\nMove the hot seat along, or end the AMA with its recap.\n{POINTER}",
        [[("⏭️ Skip", GREY, ama.HOST_SKIP_ID), ("🔄 New Hot Seat", GREY, ama.HOST_NEW_SEAT_ID),
          ("🏁 End AMA", GREY, ama.HOST_END_ID)]],
        id="ama-hot-seat",
    ),
    pytest.param(
        lambda: ama.build_host_panel_layout(AMA_FORMAT_PANEL),
        f"## {ama.ICON} Host Controls\nEnd the AMA with its recap.\n{POINTER}",
        [[("🏁 End AMA", GREY, ama.HOST_END_ID)]],
        id="ama-panel",
    ),
    pytest.param(
        fantasies.build_host_panel_layout,
        f"## {fantasies.ICON} Host Controls\nOpen a round, close its submissions, move the vote along, "
        f"or end the game with its recap.\n{POINTER}",
        [[(fantasies.START_LABEL, GREY, fantasies.HOST_START_ID), (fantasies.CLOSE_LABEL, GREY, fantasies.HOST_CLOSE_ID)],
         [(round_layouts.NEXT_LABEL, GREY, fantasies.HOST_NEXT_ID), (fantasies.END_LABEL, GREY, fantasies.HOST_END_ID)]],
        id="fantasies",
    ),
    pytest.param(
        ffa.build_host_panel_layout,
        f"## {ffa.ICON} Host Controls\nPull another prompt, or close the game with its recap.\n{POINTER}",
        [[(round_layouts.NEXT_LABEL, GREY, ffa.HOST_NEXT_ID), (ffa.CLOSE_LABEL, GREY, ffa.HOST_CLOSE_ID)]],
        id="ffa",
    ),
    pytest.param(
        hottakes.build_host_panel_layout,
        f"## {hottakes.ICON} Host Controls\nOpen the vote once {hottakes.MIN_TAKES} takes are in or scrap the "
        f"lobby; during the vote, skip to the next take.\n{POINTER}",
        [[(hottakes.START_LABEL, GREY, hottakes.HOST_START_ID), ("Cancel Game", RED, hottakes.HOST_CANCEL_ID),
          (hottakes.NEXT_LABEL, GREY, hottakes.HOST_NEXT_ID)]],
        id="hottakes",
    ),
    pytest.param(
        legitlibs.build_host_panel_layout,
        f"## {legitlibs.ICON} Host Controls\nStart once everyone is in, or cancel the round.\n{POINTER}",
        [[("Start", GREY, legitlibs.HOST_START_ID), ("Cancel Round", RED, legitlibs.HOST_CANCEL_ID)]],
        id="legitlibs",
    ),
    pytest.param(
        price.build_host_panel_layout,
        f"## {price.ICON} Host Controls\nStart the lobby, write or draw a round's scenario, move a round "
        f"along, or end the game.\n{POINTER}",
        [[("Start", GREY, price.HOST_START_ID), ("Cancel Lobby", RED, price.HOST_CANCEL_ID)],
         [(price.WRITE_LABEL, GREY, price.HOST_WRITE_ID), ("🎲 Draw From the Bank", GREY, price.HOST_DRAW_ID)],
         [("⏭️ Skip", GREY, price.HOST_SKIP_ID), ("➕ Add Rounds", GREY, price.HOST_ADD_ROUNDS_ID),
          ("End Game", GREY, price.HOST_END_ID)]],
        id="price",
    ),
    pytest.param(
        rushmore.build_host_panel_layout,
        f"## {rushmore.ICON} Host Controls\nStart the draft once everyone is in.\n{POINTER}",
        [[("Start Draft", GREY, rushmore.START_ID)]],
        id="rushmore",
    ),
    pytest.param(
        ttl.build_host_panel_layout,
        f"## {ttl.ICON} Host Controls\nStart the guessing once three have submitted; during a round, "
        f"move on to the next subject.\n{POINTER}",
        [[(ttl.START_LABEL, GREY, ttl.HOST_START_ID), (round_layouts.NEXT_LABEL, GREY, ttl.HOST_NEXT_ID)]],
        id="ttl",
    ),
    pytest.param(
        traditional.build_host_panel_layout,
        f"## {traditional.ICON} Host Controls\n{POINTER}",
        [[("Ask Question", GREY, traditional.ASK_ID), ("Write My Own", GREY, traditional.WRITE_OWN_ID),
          ("End Game", GREY, traditional.END_ID)]],
        id="traditional",
    ),
]


@pytest.mark.parametrize(("build", "text", "rows"), CASES)
def test_every_host_panel_renders_exactly_what_it_did_before_the_shared_card(build, text, rows):
    assert _shape(build()) == (text, rows)


def test_the_shared_card_with_no_blurb_drops_the_line_and_the_shorthand_still_works():
    assert _shape(round_layouts.build_host_panel_layout("X")) == ("## X Host Controls\n" + POINTER, [[]])
    text, rows = _shape(round_layouts.build_host_panel_layout(
        "X", blurb="b", next_id="n", end_id="e", start=("Go", "s"),
    ))
    assert text == f"## X Host Controls\nb\n{POINTER}"
    assert rows == [[("Go", GREY, "s"), (round_layouts.NEXT_LABEL, GREY, "n"), (round_layouts.END_LABEL, GREY, "e")]]


def test_fit_lines_drops_whole_trailing_lines_into_a_more_count():
    lines = ["x" * 40 for _ in range(10)]
    out = round_layouts.fit_lines(lines, 200)
    assert out.endswith("more_") and len(out) <= 200
    assert round_layouts.fit_lines(["a", "b"], 200) == "a\nb"
