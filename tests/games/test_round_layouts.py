"""``games/utils/round_layouts.py`` — the Components V2 frame Would You
Rather, Never Have I Ever and Most Likely To share — plus the two pieces of
mechanics that frame leans on: ``core.layout.bind_present`` and the
stateless ``host_panel.GameHostPanel``.

Each game's own card (its tally, its buttons) is tested beside it; what is
pinned here is the shape the three cannot drift apart on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core.layout import V2_EDIT, bind_present, card, check_budget
from bot_modules.games.utils import host_panel, round_layouts
from bot_modules.games.utils.round_layouts import (
    build_host_panel_layout,
    build_recap_layout,
    build_round_layout,
    fit_names,
    pose_row,
    queue_note,
)


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in card(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _ids(items: list[discord.ui.Item[Any]]) -> list[str]:
    return [
        i.custom_id for i in card(items).walk_children() if getattr(i, "custom_id", None)
    ]


def _round(**overrides):
    kwargs: dict[str, Any] = dict(
        icon="🎲", title="A Game", round_num=3, body="the body", help_id="g_htp",
        controls=[pose_row("✍️ Pose", "g_pose")],
    )
    kwargs.update(overrides)
    return build_round_layout(**kwargs)


# ── the round frame ──────────────────────────────────────────────────────────


def test_round_card_leads_with_a_glyph_heading_and_help_beside_it():
    items = _round()
    intro = items[0]
    assert isinstance(intro, discord.ui.Section)
    assert intro.accessory.custom_id == "g_htp"
    text = _text(items)
    assert text.startswith("## 🎲 A Game\nRound **3**")
    assert "the body" in text


def test_round_card_points_the_host_at_their_controls():
    """Nothing host-only is on the card; on a scheduled or Help-started game
    this line is the only way the host finds Next and End Game."""
    assert f"-# {host_panel.HOST_HINT}" in _text(_round())


def test_notes_are_subtext_and_none_entries_are_skipped():
    text = _text(_round(notes=["first", None, "second"]))
    assert "-# first\n-# second\n-# " in text


@pytest.mark.parametrize(
    "overrides, shown",
    [
        pytest.param({"advance_at": 1_700_000_000}, True, id="timed-round"),
        pytest.param({}, False, id="host-paced"),
        pytest.param({"advance_at": 1_700_000_000, "closed": True}, False, id="closed"),
        pytest.param(
            {"advance_at": 1_700_000_000, "waiting": ("✍️ Pose", "question")}, False,
            id="waiting",
        ),
    ],
)
def test_countdown_shows_only_while_a_timed_round_runs(overrides, shown):
    assert ("<t:1700000000:R>" in _text(_round(**overrides))) is shown


def test_waiting_round_shows_the_notice_instead_of_the_body():
    """vote-games-50: the bank had nothing, so the round waits for a posed
    prompt instead of ending."""
    text = _text(_round(waiting=("✍️ Pose", "question")))
    assert "press **✍️ Pose** to submit one" in text
    assert "the body" not in text


def test_closed_round_is_a_record_with_no_controls():
    """The closed round stays on screen as the round's record. It keeps its
    tally and the ``tail`` and loses every control — Help included — so
    nothing on it can be pressed against a finished round."""
    items = _round(closed=True, tail=["the lives"], notes=["a note"])
    assert _ids(items) == []
    text = _text(items)
    assert text.startswith("## 🎲 A Game — Round Over")
    assert "the body" in text and "the lives" in text
    assert "-# " not in text


def test_pose_row_carries_next_only_when_given_an_id():
    """Next is host-only on a hosted game (so it is on the host panel) and
    any voter's on a scheduled one (so it stays on the card)."""
    assert _ids([pose_row("✍️ Pose", "g_pose")]) == ["g_pose"]
    assert _ids([pose_row("✍️ Pose", "g_pose", next_id="g_next")]) == ["g_pose", "g_next"]


@pytest.mark.parametrize(
    "count, expected",
    [(0, None), (1, "1 question queued"), (3, "3 questions queued")],
)
def test_queue_note(count, expected):
    assert queue_note(count, "question") == expected


def test_fit_names_drops_whole_names_and_counts_them():
    names = ["\\*" * 32] * 40
    fitted = fit_names(names, 300)
    assert len(fitted) <= 300
    assert fitted.endswith("more")
    shown = fitted.rsplit(" +", 1)[0].split(", ")
    assert all(name == "\\*" * 32 for name in shown)
    assert fit_names(["Ada", "Bo"], 300) == "Ada, Bo"


def test_every_button_the_frame_builds_is_grey():
    items = [
        *_round(controls=[pose_row("✍️ Pose", "g_pose", next_id="g_next")]),
        *build_host_panel_layout(
            "🎲", next_id="h_next", end_id="h_end", blurb="b", start=("Start", "h_start"),
        ),
    ]
    buttons = [i for i in card(items).walk_children() if isinstance(i, discord.ui.Button)]
    assert {b.style for b in buttons} == {discord.ButtonStyle.secondary}


# ── the host panel, the recap ────────────────────────────────────────────────


def test_host_panel_is_next_and_end_with_an_optional_start():
    plain = build_host_panel_layout("🎲", next_id="h_next", end_id="h_end", blurb="Run it.")
    assert _ids(plain) == ["h_next", "h_end"]
    assert _text(plain).startswith("## 🎲 Host Controls\nRun it.")
    assert host_panel.HOST_COMMAND in _text(plain)
    with_start = build_host_panel_layout(
        "🎲", next_id="h_next", end_id="h_end", blurb="b", start=("Start", "h_start"),
    )
    assert _ids(with_start) == ["h_start", "h_next", "h_end"]


def test_recap_frame_orders_lead_facts_sections_and_payout():
    items = build_recap_layout(
        icon="🎲", title="A Game", lead="That's the last round!",
        facts=["Rounds: **3**", "Votes: **9**"], sections=["### Best\nx", "### Worst\ny"],
        payout_line="🪙 +5 to everyone",
    )
    text = _text(items)
    assert text.startswith("## 🎲 A Game — Game Over\nThat's the last round!")
    assert "Rounds: **3** · Votes: **9**" in text
    assert text.index("### Best") < text.index("### Worst") < text.index("-# 🪙 +5")
    assert _ids(items) == []


def test_recap_frame_with_nothing_but_a_heading_has_no_stray_line():
    items = build_recap_layout(icon="🎲", title="A Game")
    assert len(items) == 1
    assert _text(items) == "## 🎲 A Game — Game Over"


def test_the_frame_stays_in_budget():
    check_budget(card(_round(body="x" * 3000, notes=["n"] * 3)))
    check_budget(card(build_host_panel_layout(
        "🎲", next_id="a", end_id="b", blurb="b", start=("Start", "c"),
    )))


# ── bind_present ─────────────────────────────────────────────────────────────


def test_bind_present_binds_what_the_card_shows_and_ignores_the_rest():
    view = card(_round(controls=[pose_row("✍️ Pose", "g_pose")]))
    pose, nxt = AsyncMock(), AsyncMock()
    bound = bind_present(view, {"g_pose": pose, "g_next": nxt})
    assert bound == {"g_pose"}
    button = next(i for i in view.walk_children() if getattr(i, "custom_id", None) == "g_pose")
    assert button.callback is pose


def test_v2_edit_clears_what_an_embed_era_message_carries():
    assert V2_EDIT == {"content": None, "embed": None, "attachments": []}


# ── GameHostPanel ────────────────────────────────────────────────────────────


class _RoundView:
    def __init__(self):
        self.press_next = AsyncMock()


def _interaction():
    return SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))


def _panel(bot):
    return host_panel.GameHostPanel(
        bot, "gid",
        build_host_panel_layout("🎲", next_id="h_next", end_id="h_end", blurb="b"),
        {"h_next": "press_next"},
        view_type=_RoundView,
    )


def _press(panel, custom_id):
    button = next(i for i in panel.walk_children() if getattr(i, "custom_id", None) == custom_id)
    return button.callback


async def test_panel_acts_on_whichever_view_is_live_when_pressed():
    """A round game swaps in a new view every round; the panel outlives
    them, so it looks the view up at press time instead of capturing one."""
    first, second = _RoundView(), _RoundView()
    bot = SimpleNamespace(active_views={"gid": first})
    panel = _panel(bot)
    bot.active_views["gid"] = second
    interaction = _interaction()

    await _press(panel, "h_next")(interaction)

    second.press_next.assert_awaited_once_with(interaction)
    first.press_next.assert_not_awaited()


@pytest.mark.parametrize(
    "live", [pytest.param(None, id="game-gone"), pytest.param(object(), id="another-phase")],
)
async def test_panel_press_on_a_dead_game_says_so(live):
    bot = SimpleNamespace(active_views={"gid": live} if live else {})
    interaction = _interaction()

    await _press(_panel(bot), "h_next")(interaction)

    assert interaction.response.send_message.await_args.args[0] == host_panel.GAME_ENDED_REPLY
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_panel_times_out_inside_discords_ephemeral_edit_window():
    """Past 15 minutes the grey-out edit fails silently and the panel would
    look live while every press fails."""
    panel = _panel(SimpleNamespace(active_views={}))
    assert panel.timeout == host_panel.HOST_PANEL_TIMEOUT < 15 * 60
    assert round_layouts.NEXT_LABEL == "⏭️ Next"
