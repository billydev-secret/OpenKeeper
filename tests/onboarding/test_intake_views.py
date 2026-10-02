"""Tests for intake_views: the pure card layout + post_intake_card contract.

The interactive button flow is Discord glue tested via the service layer
(test_intake_logic.py); here we pin the pure layout branches plus the one
contract the join hook depends on — post_intake_card's return value, which
decides whether the legacy arrival ping falls back — and what a Give role
press owns: asking for the role's prerequisite before it grants, and ticking
the rows that wait on the role it gave.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import (
    get_grant_roles,
    open_db,
    set_config_value,
    upsert_grant_role,
)
from bot_modules.core.layout import check_budget, layout_budget
from bot_modules.core.layout import card as layout_card
from bot_modules.economy.service import save_econ_settings
from bot_modules.onboarding.intake import service as svc
from bot_modules.onboarding.intake import views as intake_views
from bot_modules.onboarding.intake.views import (
    build_intake_layout,
    card_user_ids,
    post_intake_card,
    post_stage2_card,
    step_line,
)
from tests.db_template import migrated_db

GUILD = 42
NEWCOMER = 7
CHANNEL = 555
GREETERS = 808


def _step(key, label, auto_kind="", done_at=None, done_by=None, skipped=0, role_id=0):
    return {
        "step_key": key,
        "label": label,
        "auto_kind": auto_kind,
        "auto_role_id": role_id,
        "done_at": done_at,
        "done_by": done_by,
        "skipped": skipped,
    }


STEPS = [
    _step("greeted", "Greeted", "greeted", done_at=1_700_000_000.0, done_by=99),
    _step("verified", "Verified", "verified", done_at=1_700_000_100.0, done_by=0),
    _step("sfw_questions", "SFW questions asked"),
    _step("nsfw_questions", "NSFW questions asked", skipped=1),
]


# A dict lookup, not a default: an id the card shouldn't be naming raises.
NAMES = {7: "Newbie", 55: "Inviter", 99: "Greeter", 601: "Approver"}
name_fn = NAMES.__getitem__


OPENED = 1_700_000_000.0
FACTS = "-# newbie#1 • joined <t:1700000000:R> • account created <t:1600000000:R> • invited by Inviter"
HEADER = "attachment://intake-header.webp"
FOOTER = "https://cdn.example/footer.webp"


def _layout(**overrides):
    kwargs = dict(
        card_id=1,
        member_id=7,
        member_display="newbie#1",
        account_created_ts=1_600_000_000.0,
        inviter_id=55,
        steps=STEPS,
        name_fn=name_fn,
        greeter_role_id=GREETERS,
        opened_at=OPENED,
    )
    kwargs.update(overrides)
    return build_intake_layout(**kwargs)


def _galleries(items):
    return [i for i in items if isinstance(i, discord.ui.MediaGallery)]


def _walk(items):
    for item in items:
        yield item
        yield from getattr(item, "walk_children", tuple)()


def _text(items):
    return "\n".join(i.content for i in _walk(items) if isinstance(i, discord.ui.TextDisplay))


def _buttons(items):
    return [i for i in _walk(items) if isinstance(i, discord.ui.Button)]


def _below_the_divider(items):
    """The checklist part: everything after the header's visible divider."""
    cut = next(
        i for i, item in enumerate(items)
        if isinstance(item, discord.ui.Separator) and item.visible
    )
    return items[cut + 1 :]


def _rows(items):
    """The button rows at the foot, each as its labels."""
    return [
        [b.label for b in item.children]
        for item in items
        if isinstance(item, discord.ui.ActionRow)
    ]


def test_step_line_states():
    lines = [step_line(s, name_fn) for s in STEPS]
    assert lines[0] == "✅ Greeted — Greeter <t:1700000000:R>"
    # done_by 0 renders as the bot's own tick, not a broken mention.
    assert lines[1] == "✅ Verified — automatically <t:1700000100:R>"
    # U+2610, an outline in the text weight — not the ⬜ emoji, a grey block
    # on the dark theme (Ben, 2026-09-30).
    assert lines[2] == "☐ SFW questions asked"
    assert lines[3] == "⏭️ ~~NSFW questions asked~~ — skipped"
    assert "⬜" not in "".join(lines)


@pytest.mark.parametrize(
    "step, line",
    [
        pytest.param(_step("g", "Main chat", "grant", role_id=11), "☐ Main chat (Member)", id="pending-grant-names-role"),
        pytest.param(_step("g", "Main chat", "grant", role_id=12), "☐ Main chat", id="deleted-role-plain"),
        pytest.param(_step("m", "Asked", role_id=11), "☐ Asked", id="manual-never-names-role"),
    ],
)
def test_step_line_grant_role(step, line):
    assert step_line(step, name_fn, {11: "Member"}) == line


def test_card_user_ids_covers_everyone_the_card_names():
    # Gathered up front so one build_name_fn call covers the whole render.
    assert set(card_user_ids(7, 55, STEPS, 99, 601)) == {7, 55, 99, 601}
    assert card_user_ids(7, None, STEPS[1:]) == [7]


def test_layout_header_progress_and_facts():
    items = _layout()
    header = items[0]
    assert isinstance(header, discord.ui.TextDisplay)  # no avatar → no Section
    # Without art the member's name is the title; the event is a ### line
    # under it, the greeter ping on its own line (the send allow-lists it),
    # then the facts in subtext — nothing in the text is bold (2026-09-30).
    assert header.content.split("\n") == [
        "## Newbie",
        "### 🚪 Just joined",
        f"<@&{GREETERS}>",
        FACTS,
    ]
    assert "**" not in header.content
    # No progress bar: the checklist under the header already shows it.
    assert "▰" not in header.content and "2/4" not in header.content


@pytest.mark.parametrize(
    "stage, opener, opened_at, fact",
    [
        pytest.param(svc.STAGE_JOIN, 0, OPENED, "joined <t:1700000000:R>", id="join"),
        # A Level 5 card's age is its approval: the approver moved off the
        # heading and onto the facts line, with the pill.
        pytest.param(svc.STAGE_LEVEL5, 601, OPENED, "approved by Approver <t:1700000000:R>", id="stage2"),
        pytest.param(svc.STAGE_LEVEL5, 0, OPENED, "promoted <t:1700000000:R>", id="stage2-no-opener"),
        pytest.param(svc.STAGE_LEVEL5, 601, None, "approved by Approver", id="stage2-no-time"),
    ],
)
def test_queue_age_fact_follows_the_stage(stage, opener, opened_at, fact):
    facts = _layout(stage=stage, opener_id=opener, opened_at=opened_at)[0].content.split("\n")[-1]
    assert facts.split(" • ")[1] == fact


@pytest.mark.parametrize("stage", [svc.STAGE_JOIN, svc.STAGE_LEVEL5], ids=["join", "stage2"])
def test_no_opened_time_no_age_fact(stage):
    facts = _layout(stage=stage, opened_at=None)[0].content.split("\n")[-1]
    assert facts.split(" • ")[1] == "account created <t:1600000000:R>"


def test_layout_avatar_goes_beside_the_header_and_facts():
    items = _layout(avatar_url="https://cdn.example/a.png")
    assert isinstance(items[0], discord.ui.Section)
    assert isinstance(items[0].accessory, discord.ui.Thumbnail)
    # One block beside the thumbnail, then straight to the divider.
    lines = items[0].children[0].content.split("\n")
    assert lines[-1].startswith("-# newbie#1")
    assert isinstance(items[1], discord.ui.Separator)


def test_layout_member_display_is_escaped_on_the_facts_line():
    # Off the code span the username renders as markdown: its underscores
    # are escaped so `new_bie_x` shows as typed rather than half in italics.
    facts = _layout(member_display="new_bie_x")[0].content.split("\n")[-1]
    assert facts.startswith("-# new\\_bie\\_x • ")


@pytest.mark.parametrize(
    "inviter_id, is_bot, needle, absent",
    [
        pytest.param(55, False, "invited by Inviter", "via", id="person"),
        # A listing bot's link (DISBOARD): "invited by DISBOARD" read oddly.
        pytest.param(55, True, "via Inviter", "invited by", id="bot"),
        pytest.param(None, False, "inviter unknown", "Inviter", id="unknown"),
        # An unknown inviter is never "via", whatever the flag says.
        pytest.param(None, True, "inviter unknown", "via", id="unknown-flag-ignored"),
    ],
)
def test_layout_inviter_wording(inviter_id, is_bot, needle, absent):
    facts = _layout(inviter_id=inviter_id, inviter_is_bot=is_bot)[0].content.split("\n")[-1]
    assert facts.endswith(needle)
    assert absent not in facts


def test_layout_missing_account_ts():
    facts = _layout(account_created_ts=None)[0].content.split("\n")[-1]
    assert "account created" not in facts


def test_layout_without_greeter_role_pings_nobody():
    assert "<@&" not in _layout(greeter_role_id=0)[0].content


def test_open_card_is_one_checklist_block_with_its_buttons_at_the_foot():
    steps = [
        *STEPS[:2],
        _step("found", "Found their way in"),
        _step("main", "Main chat", "grant", role_id=11),
        _step("asked", "Questions asked", done_at=5.0, done_by=99),
    ]
    items = _layout(steps=steps, role_names={11: "Member"})
    below = _below_the_divider(items)
    # Every line in one block — a Section per pressable row (09-28 to 09-30)
    # gave the pressable steps taller, padded rows among the plain lines.
    # Done lines are subtext, pending lines body text, in procedure order.
    assert isinstance(below[0], discord.ui.TextDisplay)
    assert below[0].content.split("\n") == [
        "-# ✅ Greeted — Greeter <t:1700000000:R>",
        "-# ✅ Verified — automatically <t:1700000100:R>",
        "☐ Found their way in",
        "☐ Main chat (Member)",
        "-# ✅ Questions asked — Greeter <t:5:R>",
    ]
    assert not any(isinstance(i, discord.ui.Section) for i in below)
    # Then the buttons, each naming its step, Dismiss beside them in the same row.
    assert _rows(items) == [
        ["Done: Found their way in", "Give role: Main chat", "Undo: Questions asked", "Dismiss"]
    ]
    ids = [b.custom_id for b in _buttons(items)]
    # The registered dynamic items answer these ids; the builder attaches none.
    assert ids == [
        "intake:step:1:found",
        "intake:grant:1:main",
        "intake:step:1:asked",
        "intake:dismiss:1",
    ]
    assert all(b.style is discord.ButtonStyle.secondary for b in _buttons(items))
    # No emoji on any button: the 🚫 on Dismiss was the card's one icon.
    assert all(b.emoji is None for b in _buttons(items))
    assert "Checklist" not in _text(items)


def _checklist_lines(items):
    return _below_the_divider(items)[0].content.split("\n")


def test_a_line_drops_to_subtext_once_it_is_history():
    # What is still owed is the only body text (design pass, 2026-09-30):
    # a fresh card has no small line, a finished one has nothing else, and
    # a skipped step is history too. The order never changes.
    fresh = [_step("a", "Greeted", "greeted"), _step("b", "Asked"), _step("c", "Main chat", "grant", role_id=11)]
    assert _checklist_lines(_layout(steps=fresh)) == ["☐ Greeted", "☐ Asked", "☐ Main chat"]
    done = [
        _step("a", "Greeted", "greeted", done_at=1.0, done_by=99),
        _step("b", "Asked", done_at=2.0, done_by=0),
        _step("c", "Skipped one", skipped=1),
    ]
    assert _checklist_lines(_layout(steps=done)) == [
        "-# ✅ Greeted — Greeter <t:1:R>",
        "-# ✅ Asked — automatically <t:2:R>",
        "-# ⏭️ ~~Skipped one~~ — skipped",
    ]
    # The plain line is unchanged under the prefix: step_line stays the
    # one place the wording lives.
    assert all(line[3:] == step_line(s, name_fn) for line, s in zip(_checklist_lines(_layout(steps=done)), done))


@pytest.mark.parametrize(
    "step",
    [
        pytest.param(_step("g", "NSFW granted", "grant", role_id=902, done_at=1.0, done_by=99), id="ticked-grant"),
        pytest.param(_step("g", "NSFW granted", "grant"), id="roleless-grant"),
        pytest.param(_step("m", "Asked", skipped=1), id="skipped"),
        pytest.param(_step("v", "Verified", "verified"), id="auto"),
    ],
)
def test_steps_without_a_button(step):
    assert [b.label for b in _buttons(_layout(steps=[step]))] == ["Dismiss"]


@pytest.mark.parametrize(
    ("step", "line"),
    [
        pytest.param(_step("sfw", "SFW questions finished"), "☐ SFW questions finished", id="pending-no-done"),
        pytest.param(
            _step("sfw", "SFW questions finished", done_at=5.0, done_by=99),
            "✅ SFW questions finished — Greeter <t:5:R>",
            id="ticked-no-undo",
        ),
    ],
)
def test_coded_step_draws_its_line_but_no_button(step, line):
    # "We don't need a SFW questions finished button, it's automatic" (Ben,
    # 2026-09-30): the greeter's canned message ticks a coded step, so it
    # gets neither Done nor Undo — like an auto step, and unlike the same
    # step without a code.
    items = _layout(steps=[step], coded_keys={"sfw"})
    assert line in _text(items)
    assert [b.label for b in _buttons(items)] == ["Dismiss"]
    assert len(_buttons(_layout(steps=[step]))) == 2


def test_coded_keys_only_take_the_button_off_manual_steps():
    # A grant step keeps Give role whatever the key set says: its press hands
    # out a role, which no message can do. Only the matching manual step loses
    # its button; the other manual step keeps Done.
    steps = [
        _step("main", "Main chat", "grant", role_id=11),
        _step("sfw", "SFW questions finished"),
        _step("found", "Found their way in"),
    ]
    items = _layout(steps=steps, coded_keys={"main", "sfw"})
    assert _rows(items) == [["Give role: Main chat", "Done: Found their way in", "Dismiss"]]


def test_button_labels_fit_discord_cap():
    # A step label is free text from the dashboard; the button caps at 80.
    steps = [_step("m", "x" * 120), _step("g", "y" * 120, "grant", role_id=11)]
    labels = [b.label for b in _buttons(_layout(steps=steps))]
    assert [len(x) for x in labels] == [80, 80, 7]
    assert labels[0].startswith("Done: xxx") and labels[1].startswith("Give role: yyy")


@pytest.mark.parametrize("count", [1, 4, 5, 10, 20], ids=lambda n: f"{n}-steps")
def test_rows_of_five_whatever_the_length(count):
    # The dashboard caps a stage at 20 steps: 21 buttons with Dismiss, in
    # rows of five, and one text block however long the list gets.
    steps = [_step(f"k{i}", f"A fairly long checklist step {i}") for i in range(count)]
    items = _layout(steps=steps, avatar_url="https://cdn.example/a.png")
    view = layout_card(items)
    check_budget(view)
    assert layout_budget(view)[0] <= 40
    below = _below_the_divider(items)
    assert sum(isinstance(i, discord.ui.TextDisplay) for i in below) == 1
    rows = _rows(items)
    full, rest = divmod(count + 1, 5)
    assert [len(r) for r in rows] == [5] * full + ([rest] if rest else [])
    assert rows[0][0] == "Done: A fairly long checklist step 0"
    assert rows[-1][-1] == "Dismiss"


# ── two stages + closed cards ─────────────────────────────────────────


@pytest.mark.parametrize(
    "stage, opener, event",
    [
        pytest.param(svc.STAGE_JOIN, 0, "### 🚪 Just joined", id="join"),
        # The approver is a fact now (test_queue_age_fact_follows_the_stage),
        # so the heading stays short beside the avatar on a phone.
        pytest.param(svc.STAGE_LEVEL5, 601, "### 🌙 Reached Level 5", id="stage2"),
        pytest.param(svc.STAGE_LEVEL5, 0, "### 🌙 Reached Level 5", id="stage2-no-opener"),
    ],
)
def test_layout_titles_by_stage(stage, opener, event):
    lines = _layout(stage=stage, opener_id=opener)[0].content.split("\n")
    assert lines[:3] == ["## Newbie", event, f"<@&{GREETERS}>"]
    # The header says what happened; "Stage 2" is internal vocabulary.
    assert "Stage" not in lines[1]


@pytest.mark.parametrize(
    ("strip", "expected"),
    [
        pytest.param(None, (None, None, 0), id="strip-fails-to-draw"),
        pytest.param(b"webp", (f"attachment://{intake_views.HEADER_FILE}", FOOTER, 1), id="strip-drawn"),
    ],
)
def test_header_art_degrades_when_the_strip_cannot_be_drawn(monkeypatch, strip, expected):
    """The frame is both strips or neither: a header that fails to draw
    leaves the card its text heading and no footer, never a crash on an
    empty file (pyright caught the ``bytes | None`` reaching ``BytesIO``)."""
    ctx = SimpleNamespace(open_db=lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(
        intake_views, "resolve_panel_art_conn",
        lambda conn, gid: SimpleNamespace(header=HEADER, footer=FOOTER),
    )
    monkeypatch.setattr(intake_views, "titled_strip_bytes", lambda url, title: strip)
    header_url, footer_url, files = asyncio.run(
        intake_views._header_art(ctx, SimpleNamespace(id=1), "Newbie")
    )
    assert (header_url, footer_url, len(files)) == expected


def test_header_art_carries_the_name_and_the_footer_closes_the_card():
    items = _layout(header_url=HEADER, footer_url=FOOTER)
    top, bottom = _galleries(items)
    # The frame is the card: the drawn name opens it and the guild's footer
    # strip is the last item, under the buttons (Ben, 2026-09-30: "use the
    # small header and footer").
    assert items[0] is top and items[-1] is bottom
    assert isinstance(items[-2], discord.ui.ActionRow)
    assert top.items[0].media.url == HEADER
    assert bottom.items[0].media.url == FOOTER
    # The name is drawn into the art: the alt text says it, the heading goes.
    assert top.items[0].description == "Newbie"
    assert "## Newbie" not in _text(items)
    assert _text(items).startswith(f"### 🚪 Just joined\n<@&{GREETERS}>\n-# newbie#1")
    # A closed card ends with the checklist block, then the footer.
    closed = _layout(header_url=HEADER, footer_url=FOOTER, resolved=(svc.RESOLUTION_LEFT, 0))
    assert isinstance(closed[-2], discord.ui.TextDisplay)
    assert closed[-1].items[0].media.url == FOOTER


@pytest.mark.parametrize(
    "header, footer, urls",
    [
        # A guild with a footer but no header strip yet: bare, like its lobbies.
        pytest.param(None, FOOTER, [], id="footer-alone-draws-nothing"),
        # A guild that never uploaded a footer: the header alone.
        pytest.param(HEADER, None, [HEADER], id="header-alone"),
        pytest.param(None, None, [], id="no-art"),
    ],
)
def test_the_frame_is_both_strips_or_neither(header, footer, urls):
    items = _layout(header_url=header, footer_url=footer)
    assert [g.items[0].media.url for g in _galleries(items)] == urls
    assert ("## Newbie" in _text(items)) is (not header)


def test_twenty_steps_with_art_stay_under_budget():
    steps = [_step(f"k{i}", f"A fairly long checklist step {i}") for i in range(20)]
    items = _layout(
        steps=steps,
        avatar_url="https://cdn.example/a.png",
        header_url=HEADER,
        footer_url=FOOTER,
    )
    view = layout_card(items)
    check_budget(view)
    # 34 of 40: the spec's number for the fullest card (the footer added
    # one gallery to the 33 the 09-30 restyle measured).
    assert layout_budget(view)[0] == 34


@pytest.mark.parametrize(
    "resolution, resolver_id, needle",
    [
        pytest.param(svc.RESOLUTION_COMPLETED, 99, "welcomed by Greeter", id="completed"),
        # Only auto steps ever ticked: nobody to credit.
        pytest.param(svc.RESOLUTION_COMPLETED, 0, "welcomed by the checklist", id="auto-only"),
        pytest.param(svc.RESOLUTION_DISMISSED, 99, "Dismissed by Greeter", id="dismissed"),
        pytest.param(svc.RESOLUTION_LEFT, 0, "left the server", id="left"),
        pytest.param(svc.RESOLUTION_BANNED, 0, "banned", id="banned"),
        pytest.param(
            svc.RESOLUTION_ADVANCED, 601,
            "➡️ Reached Level 5, approved by Approver; a new card follows", id="advanced",
        ),
    ],
)
def test_closed_card_headline_no_buttons_no_ping(resolution, resolver_id, needle):
    items = _layout(resolved=(resolution, resolver_id), avatar_url="https://cdn.example/a.png")
    text = _text(items)
    lines = text.split("\n")
    # The outcome takes the event's place as the ### line, so a scan of the
    # channel reads outcomes; "Just joined" is gone, the facts say the stage.
    assert lines[0] == "## Newbie"
    assert lines[1].startswith("### ") and needle in lines[1]
    assert "Just joined" not in text
    assert lines[2] == FACTS
    assert _buttons(items) == []
    # Greeter chat reads a closed card long after the fact: no role ping left,
    # and never a member <@id> (digits to a greeter who never saw them).
    assert "<@" not in text


# ── post_intake_card return contract (drives the legacy-ping fallback) ─


class _Ctx:
    def __init__(self, db_path):
        self.db_path = db_path

    def open_db(self):
        return open_db(self.db_path)


@pytest.fixture
def ctx(tmp_path):
    path = tmp_path / "views.db"
    migrated_db(path)
    return _Ctx(path)


def _member(guild):
    return SimpleNamespace(guild=guild, id=NEWCOMER, mention=f"<@{NEWCOMER}>", roles=[])


def _guild(channel=None):
    return SimpleNamespace(id=GUILD, get_channel=lambda cid: channel, get_role=lambda rid: None)


@pytest.fixture(autouse=True)
def _clean_watch():
    svc._reset_watch_for_tests()
    yield
    svc._reset_watch_for_tests()


async def test_post_card_dark_returns_false(ctx):
    # Intake disabled → the caller must send the legacy arrival ping.
    assert await post_intake_card(ctx, _member(_guild())) is False


async def test_post_card_missing_channel_falls_back(ctx):
    # Regression: a broken/deleted intake channel used to return True and
    # silence arrivals entirely. It must return False (legacy ping fires)
    # and roll the card back so no ghost row sits in the queue.
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
    assert await post_intake_card(ctx, _member(_guild(channel=None))) is False
    with ctx.open_db() as conn:
        assert svc.get_open_card(conn, GUILD, NEWCOMER) is None
    assert svc.is_watched(GUILD, NEWCOMER) is False


async def test_post_card_already_carded_returns_true(ctx):
    # A still-open card from a previous join IS the arrival surface —
    # no legacy ping, and the member goes (back) on the watch set.
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
    assert await post_intake_card(ctx, _member(_guild())) is True
    assert svc.is_watched(GUILD, NEWCOMER) is True


def _approver():
    return SimpleNamespace(id=601, display_name="Approver")


async def test_stage2_post_dark_opens_nothing(ctx):
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
    assert await post_stage2_card(ctx, _member(_guild()), _approver()) == svc.STAGE2_DARK
    with ctx.open_db() as conn:
        card = svc.get_open_card(conn, GUILD, NEWCOMER)
        assert svc.card_stage(card) == svc.STAGE_JOIN  # join card untouched


async def test_stage2_post_missing_channel_keeps_the_join_card(ctx):
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        set_config_value(conn, svc.STAGE2_ENABLED_KEY, "1", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
    assert (
        await post_stage2_card(ctx, _member(_guild(channel=None)), _approver())
        == svc.STAGE2_NO_CHANNEL
    )
    with ctx.open_db() as conn:
        card = svc.get_open_card(conn, GUILD, NEWCOMER)
        assert card is not None and svc.card_stage(card) == svc.STAGE_JOIN


async def test_stage2_post_with_nothing_to_grant_opens_nothing(ctx):
    # The press grants nothing itself while Stage 2 is on, so a card with no
    # Give role line would leave the member stuck: refuse before advancing.
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        set_config_value(conn, svc.STAGE2_ENABLED_KEY, "1", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
    channel = MagicMock(spec=discord.TextChannel)
    outcome = await post_stage2_card(ctx, _member(_guild(channel=channel)), _approver(), 0)
    assert outcome == svc.STAGE2_NO_GRANT
    with ctx.open_db() as conn:
        card = svc.get_open_card(conn, GUILD, NEWCOMER)
        assert card is not None and svc.card_stage(card) == svc.STAGE_JOIN


@pytest.mark.parametrize(
    "roles, exists, outcome",
    [
        pytest.param([], True, svc.STAGE2_NOT_OPTED_IN, id="not-opted-in"),
        pytest.param([4242], False, svc.STAGE2_ROLE_MISSING, id="opt-in-role-deleted"),
    ],
)
async def test_stage2_post_needs_the_optin_role(ctx, roles, exists, outcome):
    with ctx.open_db() as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        set_config_value(conn, svc.STAGE2_ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.STAGE2_OPTIN_ROLE_KEY, "4242", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)

    def no_channel_lookup(_cid):
        pytest.fail("the opt-in gate must refuse before the channel is looked up")

    guild = SimpleNamespace(
        id=GUILD,
        get_channel=no_channel_lookup,
        get_role=lambda rid: SimpleNamespace(id=rid) if exists else None,
    )
    member = SimpleNamespace(
        guild=guild, id=NEWCOMER, mention=f"<@{NEWCOMER}>",
        roles=[SimpleNamespace(id=r) for r in roles],
    )
    assert await post_stage2_card(ctx, member, _approver()) == outcome
    with ctx.open_db() as conn:
        card = svc.get_open_card(conn, GUILD, NEWCOMER)
        # The join card stays open and un-advanced; nobody is pinged.
        assert card is not None and svc.card_stage(card) == svc.STAGE_JOIN


# ── Give role honours Role Required First ─────────────────────────────
#
# The card's button is a third route to a role /grant gates, after the command
# and the role menus. It was first built without the check, so a greeter could
# put a newcomer who never verified into main chat. The verdicts are tabled in
# test_role_grant_logic.py; these rows pin that the press asks, and that a
# refusal leaves the member, the card and the audit log as they were.

GREETER = 99
MAIN_CHAT = 100
VERIFIED = 900
GRANT_STEPS = [
    {"key": "main_chat", "label": "Main chat", "auto": "grant", "role_id": MAIN_CHAT},
    {"key": "questions", "label": "Questions finished"},
]


class _PressCtx(_Ctx):
    """The app context as the press reads it, over the real grant config."""

    def __init__(self, db_path, *, admin):
        super().__init__(db_path)
        self.admin = admin

    def member_is_mod(self, member):
        return False

    def is_admin(self, interaction):
        return self.admin

    def guild_config(self, guild_id):
        with self.open_db() as conn:
            return SimpleNamespace(grant_roles=get_grant_roles(conn, guild_id))


async def _press_give_role(
    ctx, monkeypatch, *, steps, required, held, prerequisite_exists=True,
    admin=False, announce=(0, ""),
):
    """A greeter presses Give role on *Main chat*; returns what the rows read."""
    press_ctx = _PressCtx(ctx.db_path, admin=admin)
    with press_ctx.open_db() as conn:
        set_config_value(conn, svc.GREETER_ROLE_KEY, str(GREETERS), GUILD)
        set_config_value(conn, svc.STEPS_KEY, json.dumps(steps), GUILD)
        upsert_grant_role(
            conn, GUILD, "denizen",
            label="Denizen", role_id=MAIN_CHAT, log_channel_id=0,
            announce_channel_id=announce[0], grant_message=announce[1],
            required_role_id=required,
        )
        card_id = svc.create_card(conn, GUILD, NEWCOMER, 100.0)

    roles = {MAIN_CHAT: SimpleNamespace(id=MAIN_CHAT, name="Denizen")}
    if prerequisite_exists:
        roles[VERIFIED] = SimpleNamespace(id=VERIFIED, name="Verified")
    target = SimpleNamespace(
        id=NEWCOMER, roles=[roles[rid] for rid in held], add_roles=AsyncMock()
    )
    announce_channel = SimpleNamespace(id=announce[0], send=AsyncMock())
    guild = SimpleNamespace(
        id=GUILD, me=None, roles=list(roles.values()),
        get_role=roles.get, get_member=lambda _uid: target,
        get_channel=lambda cid: announce_channel if cid == announce[0] else None,
    )
    actor = MagicMock(spec=discord.Member)
    actor.id = GREETER
    actor.roles = [SimpleNamespace(id=GREETERS)]
    actor.premium_since = None
    said = AsyncMock()
    interaction = SimpleNamespace(
        guild=guild, user=actor, client=SimpleNamespace(ctx=press_ctx),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=said),
    )
    # Role safety is its own gate with its own contract test; redrawing the
    # card is Discord glue. Neither is what these rows are about.
    monkeypatch.setattr(intake_views, "role_block_reason", lambda _role, _bot: None)
    monkeypatch.setattr(intake_views, "_rerender_card", AsyncMock())

    await intake_views._grant_step(interaction, card_id, "main_chat")

    with press_ctx.open_db() as conn:
        rows = {str(s["step_key"]): s for s in svc.steps_for(conn, card_id)}
        audited = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?",
            (intake_views.GRANT_AUDIT_ACTION,),
        ).fetchone()[0]
        paid = [
            str(r["step_key"])
            for r in conn.execute(
                "SELECT step_key FROM econ_intake_rewards WHERE card_id = ? "
                "ORDER BY step_key",
                (card_id,),
            )
        ]
    return SimpleNamespace(
        added=target.add_roles.await_count,
        reply=said.await_args.args[0],
        replies=said.await_count,
        announced=announce_channel.send.await_count,
        rows=rows,
        audited=audited,
        paid=paid,
    )


@pytest.mark.parametrize(
    ("required", "held", "prerequisite_exists", "admin", "granted", "reply"),
    [
        # THE BUG: before the check this press put the role on.
        pytest.param(
            VERIFIED, [], True, False, False, "They need **Verified**",
            id="unverified-refused",
        ),
        pytest.param(
            VERIFIED, [], False, False, False, "misconfigured",
            id="prerequisite-deleted-refused",
        ),
        pytest.param(VERIFIED, [VERIFIED], True, False, True, "Granted", id="verified"),
        pytest.param(VERIFIED, [], True, True, True, "Granted", id="admin-overrides"),
        pytest.param(0, [], True, False, True, "Granted", id="grant-without-prerequisite"),
        # They hold the role already, so the press hands out nothing and only
        # ticks the row; "they need Verified" would read as a failure.
        pytest.param(
            VERIFIED, [MAIN_CHAT], True, False, True, "Granted",
            id="already-holds-the-role",
        ),
    ],
)
async def test_give_role_press_honours_the_prerequisite(
    ctx, monkeypatch, required, held, prerequisite_exists, admin, granted, reply
):
    pressed = await _press_give_role(
        ctx, monkeypatch, steps=GRANT_STEPS, required=required, held=held,
        prerequisite_exists=prerequisite_exists, admin=admin,
    )

    assert pressed.added == (1 if granted else 0)
    assert reply in pressed.reply
    # A refused press ticks nothing, credits nobody and leaves no audit row.
    assert pressed.rows["main_chat"]["done_by"] == (GREETER if granted else None)
    assert pressed.audited == (1 if granted else 0)


# ── Give role posts no announcement ───────────────────────────────────
#
# /grant posts a grant's announcement (its message, in its announce channel,
# with its pings): the main guild's Denizen grant is the welcome into main
# chat, @here included. The card's press does not. It adds the role, audits
# and pays, and says nothing in any channel, which is why the main guild
# keeps its Main chat row automatic and greeters run /grant (Ben,
# 2026-09-29). Teaching the press to announce flips this row, and the
# spec's § Grant steps with it.

ANNOUNCE_CHANNEL = 555


async def test_give_role_press_posts_no_announcement(ctx, monkeypatch):
    pressed = await _press_give_role(
        ctx, monkeypatch, steps=GRANT_STEPS, required=VERIFIED, held=[VERIFIED],
        announce=(ANNOUNCE_CHANNEL, "Welcome to the chat, {member}!"),
    )

    assert pressed.added == 1
    assert "Granted" in pressed.reply
    # One ephemeral reply to the presser, nothing in the announce channel.
    assert pressed.replies == 1
    assert pressed.announced == 0


# ── Give role ticks the rows waiting on its role ──────────────────────
#
# A row can wait on the very role a button beside it gives: the join list's
# "Found their way in" rides the main-chat grant (Ben, 2026-09-29). The role
# event ticks such a row when Discord sends one, and it sends none for a member
# who already holds the role. An automatic row has no button, so without the
# press ticking it the card could never finish.

WAITING_STEPS = [
    {
        "key": "found_way_in", "label": "Found their way in",
        "auto": "role_gained", "role_id": MAIN_CHAT,
    },
    *GRANT_STEPS,
]


@pytest.mark.parametrize(
    ("held", "granted"),
    [
        pytest.param([VERIFIED], True, id="role-given-by-the-press"),
        pytest.param([MAIN_CHAT], True, id="role-already-held"),
        pytest.param([], False, id="refused-press"),
    ],
)
async def test_give_role_press_ticks_the_rows_waiting_on_its_role(
    ctx, monkeypatch, held, granted
):
    with ctx.open_db() as conn:
        save_econ_settings(conn, GUILD, {"enabled": True, "reward_intake_step": 5})

    pressed = await _press_give_role(
        ctx, monkeypatch, steps=WAITING_STEPS, required=VERIFIED, held=held
    )

    waiting = pressed.rows["found_way_in"]
    assert pressed.rows["main_chat"]["done_by"] == (GREETER if granted else None)
    # Ticked as automatic: it names nobody, and only the pressed row pays.
    assert (waiting["done_at"] is not None) is granted
    assert waiting["done_by"] == (svc.AUTO_ACTOR if granted else None)
    assert pressed.paid == (["main_chat"] if granted else [])
    # A row that waits on nothing is left for the greeter.
    assert pressed.rows["questions"]["done_at"] is None
