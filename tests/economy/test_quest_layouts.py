"""``bot_modules/economy/quest_layouts.py`` + ``approval_layouts.py`` — the
Components V2 cards for the ``/bank quests`` board, its detail/claim
receipts, and the manager sign-off card.

Follows the pilot's convention (``tests/dm/test_dm_perms_layouts.py``): these
builders are pure, so a build-and-walk-the-items assertion covers them the
way the embed builders they replaced were tested. Three things get their own
sweep across every card, same as the pilot:

* the **budget** — a large board could still blow the 4,000-char/40-component
  ceiling;
* **no Container, glyph-led heading** — the shape the style guide fixes;
* **``name_fn``** — a ``<@id>`` in the sign-off card shows a bare number to a
  manager who has never seen the claimant.

The board's one-line renderer (``_quest_line_status`` etc.) and the sign-off
card's field content moved here verbatim from ``test_economy_quest_views.py``
when the embed builders they exercised moved to this module; the view/
interaction layer (buttons, selects, the resolution flow) stays tested in
``test_economy_quest_views.py``.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.layout import check_budget
from bot_modules.economy.approval_layouts import build_review_card_layout
from bot_modules.economy.quest_layouts import (
    QUEST_STATE_LABEL,
    _quest_line_reward,
    _quest_line_status,
    _quest_section_lines,
    _status_disp_width,
    build_quest_board_layout,
    build_quest_claimed_layout,
    build_quest_detail_layout,
    build_signoff_card_layout,
)
from bot_modules.economy.service import EconSettings

#: Renders a member as ``N<id>``: unmistakable in an assertion, and nothing
#: like a ``<@id>`` mention, so a builder that skipped the resolver shows up.
NAME_FN = lambda uid: "N" + str(uid)  # noqa: E731


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


def _settings(**over) -> EconSettings:
    kwargs: dict = {"currency_emoji": "💎"}
    kwargs.update(over)
    return EconSettings(**kwargs)


def _signoff_items(state: str, **over):
    kwargs: dict = dict(
        settings=_settings(),
        claimant_id=1,
        name_fn=NAME_FN,
        quest_title="Say hi",
        reward=15,
        criteria="Post in general",
        deny_count=0,
        state=state,
    )
    kwargs.update(over)
    return build_signoff_card_layout(**kwargs)


def _detail_quest(**over) -> dict:
    q = {
        "id": 7,
        "title": "Good Vibes",
        "qtype": "daily",
        "reward": 12,
        "reward_xp": 5,
        "description": "Spread some reactions today.",
        "state": "reaction_given",
        "progress_current": 2,
        "progress_target": 10,
        "spotlight": False,
    }
    q.update(over)
    return q


ALL_CARDS = [
    pytest.param(lambda: build_quest_board_layout(_settings(), []), id="board-empty"),
    pytest.param(
        lambda: build_quest_board_layout(
            _settings(),
            [
                {"qtype": "daily", "state": "claimable", "title": "Say hi", "reward": 10},
                {
                    "qtype": "community", "state": "community", "current": 3,
                    "target": 9, "title": "Buzz", "reward": 5,
                },
            ],
        ),
        id="board-two-sections",
    ),
    pytest.param(
        lambda: _signoff_items("pending", buttons=discord.ui.ActionRow()),
        id="signoff-pending",
    ),
    pytest.param(
        lambda: _signoff_items("paid", resolver_id=99, deny_count=2),
        id="signoff-approved",
    ),
    pytest.param(
        lambda: _signoff_items(
            "denied", resolver_id=99, deny_reason="not enough proof"
        ),
        id="signoff-denied",
    ),
    pytest.param(
        lambda: build_quest_detail_layout(_settings(), _detail_quest()),
        id="detail",
    ),
    pytest.param(
        lambda: build_quest_detail_layout(
            _settings(), _detail_quest(), scope_line="📍 Only counts in <#555>"
        ),
        id="detail-scoped",
    ),
    pytest.param(
        lambda: build_quest_claimed_layout(_settings(), title="Say hi", paid=15),
        id="claimed-no-icon",
    ),
    pytest.param(
        lambda: build_quest_claimed_layout(
            _settings(currency_icon_url="https://cdn.example/coin.png"),
            title="Say hi", paid=15,
        ),
        id="claimed-with-icon",
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
    """A card's own heading is one ``## Title`` line, led by a glyph — the
    board's two ``### section`` headings sit *under* its own ``## Quests``."""
    assert not _heading_faults(_lead_heading(build()))


@pytest.mark.parametrize("build", ALL_CARDS)
def test_no_card_wraps_itself_in_a_container(build):
    assert not any(
        isinstance(item, discord.ui.Container)
        for item in _view(build()).walk_children()
    )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: _signoff_items("pending", buttons=discord.ui.ActionRow()),
            id="signoff-pending",
        ),
        pytest.param(
            lambda: _signoff_items("paid", resolver_id=99),
            id="signoff-approved",
        ),
        pytest.param(
            lambda: _signoff_items("denied", resolver_id=99, deny_reason="blurry"),
            id="signoff-denied",
        ),
    ],
)
def test_signoff_card_never_writes_a_raw_mention(build):
    """A card <@id> resolves from the *reading* client's cache — a manager
    who has never seen the claimant would see a bare number."""
    text = _all_text(build())
    assert "<@" not in text
    assert "N1" in text  # the claimant, through name_fn


# ── glyph vocabulary: detail labels agree with the one-line board ────────


def test_quest_state_glyphs_match_the_board():
    """✅ must mean "done" and 🔶 "claimable" on BOTH the detail labels and
    the one-line board — the two surfaces must not disagree on what ✅ means."""
    assert QUEST_STATE_LABEL["done"].startswith("✅")
    assert QUEST_STATE_LABEL["claimable"].startswith("🔶")
    assert QUEST_STATE_LABEL["pending"].startswith("⏳")
    assert _quest_line_status({"state": "done"}).startswith("✅")
    assert _quest_line_status({"state": "claimable"}).startswith("🔶")
    assert _quest_line_status({"state": "pending"}).startswith("⏳")


# ── sign-off card content ─────────────────────────────────────────────────


def test_signoff_card_pending_names_claimant_shows_reward_and_criteria():
    items = _signoff_items("pending", buttons=discord.ui.ActionRow())
    text = _all_text(items)
    assert "N1" in text
    assert "Say hi" in text
    assert "💎 **15**" in text
    assert "📋 Post in general" in text


def test_signoff_card_names_claimant_and_resolver_never_mentions_them():
    for state, phrase in (("paid", "Approved by **N99**"), ("denied", "Denied by **N99**")):
        text = _all_text(
            _signoff_items(
                state, deny_count=2, resolver_id=99, deny_reason="not enough proof"
            )
        )
        assert "N1" in text
        assert phrase in text
        assert "<@" not in text


def test_signoff_card_pending_only_shows_buttons():
    row = discord.ui.ActionRow()
    assert row in _signoff_items("pending", buttons=row)
    assert row not in _signoff_items("paid", buttons=row, resolver_id=99)
    assert row not in _signoff_items(
        "denied", buttons=row, resolver_id=99, deny_reason="x"
    )


def test_signoff_card_deny_reason_is_quoted_and_escaped():
    """Deny reason is mod-typed free text — quoted like member-typed text is
    (guide § Words), and escaped so it can't inject its own markdown."""
    text = _all_text(
        _signoff_items("denied", resolver_id=99, deny_reason="*not* real")
    )
    assert "> \\*not\\* real" in text


def test_signoff_card_criteria_is_not_quoted():
    """Criteria is mod-authored quest config, not member-typed — a plain
    ``📋`` line, not a ``>`` block."""
    text = _all_text(_signoff_items("pending", buttons=discord.ui.ActionRow()))
    assert "> 📋" not in text
    assert "📋 Post in general" in text


def test_signoff_card_prior_denials_shown_as_facts():
    text = _all_text(_signoff_items("paid", deny_count=2, resolver_id=99))
    assert "🚫 2 prior denials" in text
    text_one = _all_text(_signoff_items("paid", deny_count=1, resolver_id=99))
    assert "🚫 1 prior denial" in text_one
    assert "1 prior denials" not in text_one


def test_signoff_card_uses_the_canonical_state_headings():
    assert _all_text(
        _signoff_items("pending", buttons=discord.ui.ActionRow())
    ).startswith("## 📋 Quest Sign-Off Requested")
    assert _all_text(_signoff_items("paid", resolver_id=99)).startswith(
        "## ✅ Quest Approved"
    )
    assert _all_text(_signoff_items("denied", resolver_id=99)).startswith(
        "## ❌ Quest Denied"
    )
    # An expired claim renders with the denied heading — same outcome for the
    # claimant either way.
    assert _all_text(_signoff_items("expired", resolver_id=99)).startswith(
        "## ❌ Quest Denied"
    )


# ── quest detail popup ────────────────────────────────────────────────────


def test_quest_detail_layout_shows_full_story():
    items = build_quest_detail_layout(_settings(), _detail_quest())
    text = _all_text(items)
    assert "Good Vibes" in text
    assert "**12**" in text and "5 XP" in text
    assert "Spread some reactions today." in text
    assert "react to people's messages" in text
    assert "2/10" in text


def test_quest_detail_layout_community_goal():
    goal = _detail_quest(
        id=8, title="Team goal", qtype="community", state="community",
        current=40, target=100, reward_xp=0,
    )
    text = _all_text(build_quest_detail_layout(_settings(), goal))
    assert "40/100" in text


def test_quest_detail_layout_scope_line_is_a_caller_passthrough():
    """The card trusts the caller's scope_line outright — whether it was
    worth computing at all is `quests.scope_worth_showing`'s call, made in
    ``quest_views.QuestDetailSelect.callback`` before this builder runs."""
    text = _all_text(
        build_quest_detail_layout(
            _settings(), _detail_quest(), scope_line="📍 Only counts in <#555>"
        )
    )
    assert "📍 Only counts in <#555>" in text
    assert "<#555>" not in _all_text(build_quest_detail_layout(_settings(), _detail_quest()))


def test_quest_detail_layout_done_quest_shows_no_progress_bar():
    text = _all_text(
        build_quest_detail_layout(_settings(), _detail_quest(state="done"))
    )
    assert "✅ Completed this period" in text


# ── quest-complete receipt ────────────────────────────────────────────────


def test_quest_claimed_layout_uses_a_thumbnail_section_when_icon_set():
    items = build_quest_claimed_layout(
        _settings(currency_icon_url="https://cdn.example/coin.png"),
        title="Say hi", paid=15,
    )
    assert len(items) == 1
    assert isinstance(items[0], discord.ui.Section)
    assert isinstance(items[0].accessory, discord.ui.Thumbnail)


def test_quest_claimed_layout_is_bare_text_without_an_icon():
    items = build_quest_claimed_layout(_settings(), title="Say hi", paid=15)
    assert len(items) == 1
    assert isinstance(items[0], discord.ui.TextDisplay)
    text = items[0].content
    assert "🎉 Quest Complete!" in text
    assert "Say hi" in text
    assert "💎 **15**" in text


# ── generic review-card shape (approval_layouts) ──────────────────────────


def test_review_card_layout_headings_switch_on_state():
    row = discord.ui.ActionRow()
    pending = build_review_card_layout(
        state="pending", pending_heading="📋 Pending", approved_heading="✅ Approved",
        denied_heading="❌ Denied", body="body text", buttons=row,
    )
    assert isinstance(pending[0], discord.ui.TextDisplay)
    assert pending[0].content.startswith("## 📋 Pending")
    assert row in pending

    approved = build_review_card_layout(
        state="approved", pending_heading="📋 Pending", approved_heading="✅ Approved",
        denied_heading="❌ Denied", body="body text", resolved_by="Approved by X",
        buttons=row,
    )
    assert isinstance(approved[0], discord.ui.TextDisplay)
    assert approved[0].content.startswith("## ✅ Approved")
    assert row not in approved
    assert isinstance(approved[-1], discord.ui.TextDisplay)
    assert approved[-1].content == "-# Approved by X"


def test_review_card_layout_joins_resolved_by_and_facts_with_a_dot():
    items = build_review_card_layout(
        state="denied", pending_heading="P", approved_heading="A", denied_heading="D",
        body="body", resolved_by="Denied by X", facts="🚫 2 prior denials",
    )
    assert isinstance(items[-1], discord.ui.TextDisplay)
    assert items[-1].content == "-# Denied by X • 🚫 2 prior denials"


# ── the board's one-line renderer ────────────────────────────────────────
# The list render lives beside the details popup it must stay consistent
# with, so their tests stay in the same file (moved here with
# build_quest_board_embed's V2 replacement).


def test_quest_line_status_draws_bar_for_counted_and_community():
    """Counted daily/weekly and community/monthly goals render a ▰▱ bar;
    one-shot and claim-state quests keep their glyph phrase."""
    counted = _quest_line_status(
        {"state": "message_sent", "progress_current": 3, "progress_target": 6}
    )
    assert "▰" in counted and "▱" in counted and "3/6" in counted
    community = _quest_line_status(
        {"state": "community", "current": 40, "target": 100}
    )
    assert "▰" in community and "▱" in community and "/" not in community
    one_shot = _quest_line_status({"state": "photo_post"})
    assert one_shot == "☐ to do"
    assert _quest_line_status({"state": "done"}) == "✅ done"
    assert _quest_line_status({"state": "pending"}) == "⏳ sign-off"
    assert _quest_line_status({"state": "claimable"}) == "🔶 claim below"


def test_quest_line_reward_xp_suffix_is_glyph_free():
    """The board's XP suffix carries no ⭐ — on phone widths the reward
    column hugs the wrap point and the star pushed it onto its own line.
    The spotlight bolt (rarer, and last on the line) stays."""
    settings = _settings(currency_emoji="🪙")
    line = _quest_line_reward({"reward": 15, "reward_xp": 8}, settings)
    assert line == "🪙 15 +8xp"
    bolt = _quest_line_reward(
        {"reward": 15, "reward_xp": 8, "spotlight": True}, settings
    )
    assert bolt.endswith("⚡")


def test_quest_section_lines_labels_only_when_multi_cadence():
    """A section spanning >1 cadence gets a bold sub-label per group; a
    single-cadence section is unlabelled (its own ``###`` already names it)."""
    settings = _settings(currency_emoji="🪙")
    two_cadence = _quest_section_lines(
        ("daily", "weekly", "event"),
        {
            "daily": [{
                "state": "message_sent", "progress_current": 2,
                "progress_target": 5, "title": "Chatty", "reward": 10,
            }],
            "weekly": [{"state": "done", "title": "Grind", "reward": 40}],
        },
        settings, 12,
    )
    assert "**Daily**" in two_cadence and "**Weekly**" in two_cadence
    assert any("Chatty" in ln for ln in two_cadence)
    one_cadence = _quest_section_lines(
        ("monthly", "community"),
        {"community": [{
            "state": "community", "current": 3, "target": 9,
            "title": "Buzz", "reward": 5,
        }]},
        settings, 12,
    )
    assert not any(ln.startswith("**") for ln in one_cadence)
    assert any("Buzz" in ln for ln in one_cadence)


def test_quest_section_lines_align_the_reward_column():
    """Every row's title+status code cell is padded to one width, so the
    reward payload after the closing backtick starts at the same column —
    a counted (bar) row and a claim-state (glyph) row included."""
    settings = _settings(currency_emoji="🪙")
    lines = _quest_section_lines(
        ("daily", "weekly", "event"),
        {"daily": [
            {"state": "message_sent", "progress_current": 2,
             "progress_target": 5, "title": "Chatty", "reward": 10},
            {"state": "claimable", "title": "Say hi", "reward": 25},
            {"state": "done", "title": "All done", "reward": 40},
        ]},
        settings, 12,
    )
    cells = [ln.split("`")[1] for ln in lines if ln.startswith("`")]
    assert len(cells) == 3
    assert len({_status_disp_width(c) for c in cells}) == 1


def test_quest_board_layout_budget_stays_under_ceiling_with_many_quests():
    """Defensive cap: ``_SECTION_CHAR_BUDGET`` keeps a large board under the
    message-wide 4,000-char ceiling even with more quests than usually run."""
    quests = [
        {"qtype": "daily", "state": "claimable", "title": f"Quest {i}", "reward": 10}
        for i in range(30)
    ]
    view = _view(build_quest_board_layout(_settings(), quests))
    check_budget(view)


def test_quest_board_layout_drops_overflow_into_and_n_more_tail():
    """A board with more quests than fit under ``_SECTION_CHAR_BUDGET`` stops
    early rather than blowing the section's Text Display past Discord's
    character ceiling — ``fit_lines`` truncates and names what it dropped,
    the same overflow shape the embed field renderer this replaced used, so
    a bumper crop of quests reads as "and N more", never a hard error."""
    quests = [
        {"qtype": "daily", "state": "done", "title": f"Quest {i}", "reward": 10}
        for i in range(80)
    ]
    text = _all_text(build_quest_board_layout(_settings(), quests))
    section = next(
        block for block in text.split("### ") if block.startswith("🧍 Your quests")
    )
    shown = section.count("`Quest")
    assert shown < len(quests)
    assert f"_…and {len(quests) - shown} more_" in section
