"""Tests for ``bot_modules/whisper/layouts.py`` — the Components V2 cards
that replaced ``whisper/embeds.py`` and the cog's inline embeds.

The builders are pure: a ``Whisper`` row, primitives and a ``name_fn`` in,
``discord.ui.Item`` lists out, so every card is asserted on its text with no
Discord mocks. Budget is checked at the fullest card the feature can really
produce: a full inbox page (ten rows, both arrows, a filter on) and a DM
carrying a 1,000-character whisper with every button.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
from bot_modules.whisper import layouts
from bot_modules.whisper.layouts import (
    ACTION_LABELS,
    DELETE_CANCEL_ID,
    DELETE_CONFIRM_ID,
    GUESS_CLEAR_ID,
    GUESS_NEXT_ID,
    GUESS_PICK_ID,
    GUESS_PREV_ID,
    HELP_ID,
    INBOX_ACTION_IDS,
    INBOX_BACK_ID,
    INBOX_CLEAR_ID,
    INBOX_FILTER_ID,
    INBOX_ID,
    INBOX_NEXT_ID,
    INBOX_OPEN_ID_TEMPLATE,
    INBOX_PAGE_SIZE,
    INBOX_PREV_ID,
    OPTIN_CANCEL_ID,
    OPTIN_CONFIRM_ID,
    ROW_PREVIEW_CHARS,
    SEND_ID,
    SENT_ID,
    build_delete_confirm_layout,
    build_deleted_layout,
    build_guess_digest_layout,
    build_guess_list_picker_layout,
    build_guess_native_picker_layout,
    build_guess_result_layout,
    build_help_layout,
    build_inbox_detail_layout,
    build_inbox_list_layout,
    build_launcher_layout,
    build_line_layout,
    build_optin_done_layout,
    build_optin_layout,
    build_optout_done_layout,
    build_pool_refusal_layout,
    build_send_refusal_layout,
    build_reply_audit_layout,
    build_reply_dm_layout,
    build_reply_report_audit_layout,
    build_report_audit_layout,
    build_send_feed_layout,
    build_share_feed_layout,
    build_solved_feed_layout,
    build_whisper_dm_layout,
)
from bot_modules.whisper.models import STATE_PENDING, STATE_SHARED, Whisper, WhisperReply, WhisperState
from bot_modules.whisper.service import ERROR_TARGET_NEEDS_ROLE, MAX_MESSAGE_LENGTH, GuessOutcome

NOW = 1_000_000.0


def _whisper(
    *,
    id: int = 12,
    sender_id: int = 100,
    target_id: int = 200,
    message: str = "you have a lovely laugh",
    created_at: float = NOW - 3 * 3600,
    state: WhisperState = STATE_PENDING,
    solved: bool = False,
    guesses_left: int = 3,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=id, guild_id=1, sender_id=sender_id, target_id=target_id,
        message=message, created_at=created_at, state=state, solved=solved,
        guesses_left=guesses_left, channel_msg_id=5, dm_msg_id=6,
        deleted_at=deleted_at,
    )


def _names(mapping: dict[int, str]):
    """A stand-in resolver; the real chain is tested in test_name_resolver_logic.
    Its unknown-id fallback is ``User <id>``, never a bare number."""
    return lambda uid: mapping.get(uid, f"User {uid}")


NAME_FN = _names({100: "Sender Sam", 200: "Target Tina"})


def _view(items: list[discord.ui.Item[Any]]) -> discord.ui.LayoutView:
    return card(items)


def _text(items: list[discord.ui.Item[Any]]) -> str:
    return "\n".join(
        i.content for i in _view(items).walk_children()
        if isinstance(i, discord.ui.TextDisplay)
    )


def _buttons(items: list[discord.ui.Item[Any]]) -> list[discord.ui.Button[Any]]:
    return [b for b in _view(items).walk_children() if isinstance(b, discord.ui.Button)]


def _button_ids(items: list[discord.ui.Item[Any]]) -> list[str | None]:
    return [b.custom_id for b in _buttons(items)]


def _button(items: list[discord.ui.Item[Any]], custom_id: str) -> discord.ui.Button[Any]:
    return next(b for b in _buttons(items) if b.custom_id == custom_id)


def _sections(items: list[discord.ui.Item[Any]]) -> list[discord.ui.Section[Any]]:
    return [s for s in _view(items).walk_children() if isinstance(s, discord.ui.Section)]


# ── Every card ──────────────────────────────────────────────────────────


ALL_CARDS: list[tuple[str, list[discord.ui.Item[Any]]]] = [
    ("launcher", build_launcher_layout()),
    ("send feed", build_send_feed_layout(200, NAME_FN)),
    ("share feed", build_share_feed_layout(_whisper(), NAME_FN)),
    ("solved feed", build_solved_feed_layout(200, NAME_FN)),
    ("dm", build_whisper_dm_layout(_whisper(), now=NOW)),
    ("reply dm", build_reply_dm_layout(whisper_id=12, whisper_message="a", reply_content="b", reply_id=3)),
    ("digest", build_guess_digest_layout(whisper_id=12, guesses=[("Ann", GuessOutcome(False, 2, False))])),
    ("inbox list", build_inbox_list_layout(
        [_whisper()], mode="received", total=1, shown=1, page=0, page_count=1,
        filter_query="", name_fn=NAME_FN, now=NOW,
    )),
    ("inbox detail", build_inbox_detail_layout(_whisper(), mode="received", name_fn=NAME_FN, now=NOW)),
    ("delete confirm", build_delete_confirm_layout(12)),
    ("deleted", build_deleted_layout(12)),
    ("native picker", build_guess_native_picker_layout(_whisper())),
    ("list picker", build_guess_list_picker_layout(
        _whisper(), [discord.SelectOption(label="Ann", value="1")],
        placeholder="Pick the sender…", page=0, page_count=1, filter_query="",
    )),
    ("guess result", build_guess_result_layout(GuessOutcome(True, 2, False))),
    ("opt-in", build_optin_layout()),
    ("opted in", build_optin_done_layout()),
    ("opted out", build_optout_done_layout()),
    ("pool refusal", build_pool_refusal_layout(ERROR_TARGET_NEEDS_ROLE, "draft")),
    ("send refusal", build_send_refusal_layout("❌ Slow down.", "draft")),
    ("reply audit", build_reply_audit_layout(
        whisper_id=12, from_user_id=100, to_user_id=200, content="c", name_fn=NAME_FN,
    )),
    ("report audit", build_report_audit_layout(whisper=_whisper(), reason="r", name_fn=NAME_FN)),
    ("reply report audit", build_reply_report_audit_layout(
        reply=WhisperReply(3, 12, 100, 200, "c", NOW), reporter_id=200, reason="r", name_fn=NAME_FN,
    )),
    ("help", build_help_layout(None)),
]


@pytest.mark.parametrize(("name", "items"), ALL_CARDS, ids=[n for n, _ in ALL_CARDS])
def test_every_card_leads_with_a_glyph_led_title(name, items):
    """Guide § Anatomy: no lighter class of card drops the ``##`` heading.
    The one-line refusal (``build_line_layout``) is the bare Text Display
    the guide allows for a one-line answer, and is not on this list."""
    first = next(i for i in _view(items).walk_children() if isinstance(i, discord.ui.TextDisplay))
    line = first.content.splitlines()[0]
    assert line.startswith("## "), f"{name}: {line!r}"
    assert not line[3:4].isalpha(), f"{name}: heading has no glyph: {line!r}"


@pytest.mark.parametrize(("name", "items"), ALL_CARDS, ids=[n for n, _ in ALL_CARDS])
def test_every_card_is_under_budget(name, items):
    check_budget(_view(items))


@pytest.mark.parametrize(("name", "items"), ALL_CARDS, ids=[n for n, _ in ALL_CARDS])
def test_no_card_contains_a_container(name, items):
    assert not any(isinstance(i, discord.ui.Container) for i in _view(items).walk_children())


@pytest.mark.parametrize(("name", "items"), ALL_CARDS, ids=[n for n, _ in ALL_CARDS])
def test_no_card_names_a_member_with_a_mention(name, items):
    assert "<@" not in _text(items), name


@pytest.mark.parametrize(("name", "items"), ALL_CARDS, ids=[n for n, _ in ALL_CARDS])
def test_buttons_are_grey_unless_the_colour_means_something(name, items):
    """Guide § Anatomy: red is a destructive press, green the yes on a
    decision card. Delete (red, confirm behind it) and Opt In (green yes)
    are the only two; Report went grey with this conversion."""
    for b in _buttons(items):
        if b.style is discord.ButtonStyle.danger:
            assert b.label in {"Delete", "Yes, Delete"}, (name, b.label)
        elif b.style is discord.ButtonStyle.success:
            assert b.label == "Opt In", (name, b.label)
        else:
            assert b.style is discord.ButtonStyle.secondary, (name, b.label)


@pytest.mark.parametrize(("name", "items"), ALL_CARDS, ids=[n for n, _ in ALL_CARDS])
def test_accessory_labels_fit_beside_their_row(name, items):
    for section in _sections(items):
        acc = section.accessory
        if isinstance(acc, discord.ui.Button):
            assert text_columns(acc.label or "") <= MAX_ACCESSORY_LABEL, (name, acc.label)


# ── The launcher (paradigm 1) ───────────────────────────────────────────


def test_launcher_has_help_beside_the_title_and_one_grey_button_per_row():
    items = build_launcher_layout()
    assert _text(items).startswith("## 📬 Whisper\n")
    assert _button_ids(items) == [HELP_ID, SEND_ID, INBOX_ID, SENT_ID]
    # The three entry-point ids are the embed era's, so a launcher posted
    # before the conversion keeps answering after it.
    assert (SEND_ID, INBOX_ID, SENT_ID) == ("whisper:send", "whisper:check", "whisper:check_sent")


def test_launcher_rows_stay_beside_their_buttons():
    """Guide § Screen use: ~30 columns beside a button."""
    for sentence, label, _ in layouts.LAUNCHER_ROWS:
        assert text_columns(sentence) <= 30, sentence
        assert text_columns(label) <= MAX_ACCESSORY_LABEL


def test_launcher_draws_nothing_without_a_header():
    items = build_launcher_layout(footer_url="https://x/footer.webp")
    assert not any(isinstance(i, discord.ui.MediaGallery) for i in _view(items).walk_children())


def test_launcher_with_the_title_drawn_drops_its_heading():
    items = build_launcher_layout(
        header_url="https://x/h.webp", footer_url="https://x/f.webp", title_in_header=True,
    )
    galleries = [i for i in _view(items).walk_children() if isinstance(i, discord.ui.MediaGallery)]
    assert len(galleries) == 2
    assert galleries[0].items[0].description == "Whisper"
    assert "## " not in _text(items)
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)


def test_launcher_keeps_its_heading_when_the_header_carries_no_title():
    items = build_launcher_layout(header_url="https://x/h.webp", title_in_header=False)
    assert "## 📬 Whisper" in _text(items)


# ── Feed notices (paradigm 7) ───────────────────────────────────────────


def test_send_feed_names_the_recipient_and_carries_no_content():
    text = _text(build_send_feed_layout(200, NAME_FN))
    assert "Someone sent **Target Tina** an anonymous message." in text
    assert "lovely laugh" not in text


def test_share_feed_quotes_the_whisper_escaped():
    w = _whisper(message="## fake heading\n*wink*")
    text = _text(build_share_feed_layout(w, NAME_FN))
    assert "**Target Tina** shared" in text
    assert "> \\## fake heading\n> \\*wink\\*" in text


def test_solved_feed_names_the_target_resolved():
    text = _text(build_solved_feed_layout(200, NAME_FN))
    assert "**Target Tina** worked out who sent their whisper." in text


def test_an_unknown_id_reads_user_id_never_a_bare_number():
    text = _text(build_send_feed_layout(999, NAME_FN))
    assert "User 999" in text


# ── The whisper DM (paradigm 5) ─────────────────────────────────────────


def test_dm_quotes_the_whisper_and_carries_every_button_when_fresh():
    items = build_whisper_dm_layout(_whisper(), now=NOW)
    text = _text(items)
    assert "## 📬 You Got a Whisper" in text
    assert "> you have a lovely laugh" in text
    assert "-# Whisper #12 · 3 guesses left." in text
    assert layouts.GUESS_HINT in text
    assert _button_ids(items) == [
        "whisper:guess:12", "whisper:share:12", "whisper:reply:12", "whisper:delete:12",
    ]


@pytest.mark.parametrize(
    ("whisper", "labels"),
    [
        pytest.param(_whisper(solved=True), ["Share", "Reply", "Delete"], id="solved"),
        pytest.param(_whisper(guesses_left=0), ["Share", "Reply", "Delete"], id="exhausted"),
        pytest.param(_whisper(state=STATE_SHARED), ["Guess", "Reply"], id="shared"),
        pytest.param(_whisper(deleted_at=NOW), [], id="deleted"),
    ],
)
def test_dm_buttons_follow_the_state(whisper, labels):
    items = build_whisper_dm_layout(whisper, now=NOW)
    assert [b.label for b in _buttons(items)] == labels


def test_dm_drops_the_guess_hint_once_guessing_is_over():
    text = _text(build_whisper_dm_layout(_whisper(guesses_left=0), now=NOW))
    assert layouts.GUESS_HINT not in text
    assert "Out of guesses — the sender stays anonymous." in text


def test_dm_delete_is_red_and_the_rest_grey():
    styles = {b.label: b.style for b in _buttons(build_whisper_dm_layout(_whisper(), now=NOW))}
    assert styles["Delete"] is discord.ButtonStyle.danger
    assert styles["Guess"] is styles["Share"] is styles["Reply"] is discord.ButtonStyle.secondary


def test_dm_at_the_message_cap_leaves_room_for_the_branding():
    """``brand_dm_layout`` adds up to three components and a name line."""
    items = build_whisper_dm_layout(_whisper(message="x" * MAX_MESSAGE_LENGTH), now=NOW)
    components, chars = layout_budget(_view(items))
    assert components <= 37
    assert chars <= 3_800


# ── Reply DM and the digest (paradigm 7) ────────────────────────────────


def test_reply_dm_labels_both_quotes_and_carries_only_report():
    items = build_reply_dm_layout(
        whisper_id=12, whisper_message="original", reply_content="the reply", reply_id=7,
    )
    text = _text(items)
    assert "**Whisper #12**\n> original\n**Their reply**\n> the reply" in text
    assert _button_ids(items) == ["whisper:report_reply:7"]
    assert _buttons(items)[0].style is discord.ButtonStyle.secondary


def test_reply_dm_clips_a_long_original():
    items = build_reply_dm_layout(
        whisper_id=12, whisper_message="y" * 300, reply_content="r", reply_id=7,
    )
    assert "y" * 199 + "…" in _text(items)
    assert "y" * 200 not in _text(items)


def test_digest_heads_with_the_whisper_and_lists_every_guess():
    text = _text(build_guess_digest_layout(
        whisper_id=12,
        guesses=[
            ("Ann", GuessOutcome(False, 2, False)),
            ("someone", GuessOutcome(False, 0, True)),
        ],
    ))
    assert text == (
        "## 📬 Guesses on Whisper #12\n"
        "They guessed Ann. Wrong, 2 left.\n"
        "They guessed someone. Wrong — they're out of guesses. You're safe."
    )


# ── The inbox (paradigm 6) ──────────────────────────────────────────────


def _list(whispers, **kw):
    args = dict(
        mode="received", total=len(whispers), shown=len(whispers), page=0,
        page_count=1, filter_query="", name_fn=NAME_FN, now=NOW,
    )
    args.update(kw)
    return build_inbox_list_layout(whispers, **args)


def test_inbox_list_has_an_open_beside_each_row():
    items = _list([_whisper(id=1), _whisper(id=2)])
    assert "## 📬 Your Inbox · 2" in _text(items)
    assert _button_ids(items) == [
        INBOX_OPEN_ID_TEMPLATE.format(whisper_id=1),
        INBOX_OPEN_ID_TEMPLATE.format(whisper_id=2),
        INBOX_FILTER_ID,
    ]
    assert "**#1** · New · 3h ago\n-# you have a lovely laugh" in _text(items)


def test_inbox_rows_are_held_to_the_phone_width():
    long = _whisper(message="a" * 200)
    items = _list([long], mode="sent")
    row = _sections(items)[0].children[0].content
    head, sub = row.splitlines()
    assert text_columns(head) <= 30
    assert sub.startswith("-# To Target Tina · ")
    assert text_columns(sub[3:]) <= ROW_PREVIEW_CHARS


@pytest.mark.parametrize(
    ("mode", "message", "escaped"),
    [
        pytest.param("received", "[click](http://x.test)", "\\[click](http://x.test)", id="link"),
        pytest.param("received", "*hi* *you*", "\\*hi\\* \\*you\\*", id="emphasis"),
        pytest.param("sent", "*hi* *you*", "\\*hi\\* \\*you\\*", id="sent"),
    ],
)
def test_inbox_row_preview_is_escaped(mode, message, escaped):
    """The preview sits in the bot's own ``-#`` line: a sender's masked link
    or emphasis must not render there."""
    items = _list([_whisper(message=message)], mode=mode)
    row = _sections(items)[0].children[0].content
    assert escaped in row


def test_inbox_sent_row_names_the_recipient_and_a_long_name_leaves_no_preview():
    items = _list([_whisper(target_id=300)], mode="sent", name_fn=_names({300: "A" * 32}))
    row = _sections(items)[0].children[0].content
    assert row.splitlines()[1] == "-# To " + "A" * 32


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param("received", "No whispers yet — when someone sends you one, it lands here.", id="received"),
        pytest.param("sent", "You haven't sent any active whispers in this server.", id="sent"),
    ],
)
def test_inbox_empty_is_a_sentence_with_no_controls(mode, expected):
    items = _list([], mode=mode)
    assert expected in _text(items)
    assert _buttons(items) == []


def test_inbox_pages_show_arrows_disabled_at_the_ends_and_the_page_line():
    items = _list([_whisper()], total=25, shown=25, page=0, page_count=3)
    assert "-# Page 1 of 3" in _text(items)
    assert _button(items, INBOX_PREV_ID).disabled is True
    assert _button(items, INBOX_NEXT_ID).disabled is False
    last = _list([_whisper()], total=25, shown=25, page=2, page_count=3)
    assert _button(last, INBOX_NEXT_ID).disabled is True


def test_inbox_filter_shows_the_match_count_and_a_clear():
    items = _list([_whisper()], total=9, shown=1, filter_query="laugh")
    assert '-# 1 match for "laugh"' in _text(items)
    assert INBOX_CLEAR_ID in _button_ids(items)


def test_inbox_filter_with_no_matches_keeps_clear_reachable():
    items = _list([], total=9, shown=0, filter_query="zzz")
    assert "Nothing matches that search." in _text(items)
    assert _button_ids(items) == [INBOX_FILTER_ID, INBOX_CLEAR_ID]


def test_inbox_filter_query_is_escaped():
    items = _list([_whisper()], total=1, shown=1, filter_query="*bold*")
    assert '"\\*bold\\*"' in _text(items)


def test_inbox_fullest_page_is_under_budget():
    """Ten rows, both arrows, Filter and Clear: the worst the list gets."""
    items = _list(
        [_whisper(id=i, message="m" * 200) for i in range(INBOX_PAGE_SIZE)],
        mode="sent", total=50, shown=30, page=1, page_count=3, filter_query="m",
    )
    check_budget(_view(items))
    assert layout_budget(_view(items))[0] == 36


def test_inbox_detail_shows_the_whisper_its_actions_and_back():
    items = build_inbox_detail_layout(_whisper(), mode="received", name_fn=NAME_FN, now=NOW)
    text = _text(items)
    assert text.startswith("## 📬 Whisper #12\nNew · 3h ago")
    assert "> you have a lovely laugh" in text
    assert "-# 3 guesses left." in text
    assert _button_ids(items) == [
        INBOX_ACTION_IDS["guess"], INBOX_ACTION_IDS["share"], INBOX_ACTION_IDS["reply"],
        INBOX_ACTION_IDS["report"], INBOX_ACTION_IDS["delete"], INBOX_BACK_ID,
    ]
    assert [b.label for b in _buttons(items)][:5] == [ACTION_LABELS[k] for k in ("guess", "share", "reply", "report", "delete")]


def test_inbox_detail_sent_mode_names_the_recipient_and_offers_reply_only():
    items = build_inbox_detail_layout(_whisper(), mode="sent", name_fn=NAME_FN, now=NOW)
    assert "## 📬 Whisper #12 → Target Tina" in _text(items)
    assert "-# 3 guesses remain for the target." in _text(items)
    assert [b.label for b in _buttons(items)] == ["Reply", "◀ Back"]


def test_inbox_detail_received_mode_never_resolves_a_name():
    def _boom(uid: int) -> str:
        raise AssertionError("received mode must not resolve a name")

    build_inbox_detail_layout(_whisper(), mode="received", name_fn=_boom, now=NOW)


def test_inbox_detail_shared_whisper_drops_share_and_delete_stays():
    items = build_inbox_detail_layout(_whisper(state=STATE_SHARED), mode="received", name_fn=NAME_FN, now=NOW)
    assert [b.label for b in _buttons(items)] == ["Guess", "Reply", "Report", "Delete", "◀ Back"]


def test_inbox_detail_locked_whisper_has_no_guess():
    old = _whisper(created_at=NOW - 31 * 86400)
    items = build_inbox_detail_layout(old, mode="received", name_fn=NAME_FN, now=NOW)
    assert "Guess" not in [b.label for b in _buttons(items)]
    assert "Locked — too old to guess on now." in _text(items)


# ── Delete confirm (paradigm 10) ────────────────────────────────────────


def test_delete_confirm_names_the_whisper_with_a_red_yes_and_a_grey_cancel():
    items = build_delete_confirm_layout(12)
    assert "## ⚠️ Delete Whisper #12?" in _text(items)
    assert "The sender keeps their copy." in _text(items)
    yes, cancel = _buttons(items)
    assert (yes.label, yes.style, yes.custom_id) == ("Yes, Delete", discord.ButtonStyle.danger, DELETE_CONFIRM_ID)
    assert (cancel.label, cancel.style, cancel.custom_id) == ("Cancel", discord.ButtonStyle.secondary, DELETE_CANCEL_ID)


def test_deleted_receipt():
    assert _text(build_deleted_layout(12)) == "## 🚫 Whisper #12 Deleted\nIt's gone from your inbox."


# ── Guess pickers and results (paradigm 9 → 7) ──────────────────────────


def test_native_picker_is_a_user_select_under_the_guess_count():
    items = build_guess_native_picker_layout(_whisper(guesses_left=1))
    assert "Whisper #12 · **1 guess** left" in _text(items)
    selects = [i for i in _view(items).walk_children() if isinstance(i, discord.ui.UserSelect)]
    assert len(selects) == 1 and selects[0].custom_id == GUESS_PICK_ID


def test_list_picker_pages_and_filters_like_the_inbox():
    items = build_guess_list_picker_layout(
        _whisper(), [discord.SelectOption(label="Ann", value="1")],
        placeholder="Pick the sender… (2/3)", page=1, page_count=3, filter_query="a",
    )
    select = next(i for i in _view(items).walk_children() if isinstance(i, discord.ui.Select))
    assert select.placeholder == "Pick the sender… (2/3)" and select.custom_id == GUESS_PICK_ID
    assert _button_ids(items) == [GUESS_PREV_ID, GUESS_NEXT_ID, layouts.GUESS_FILTER_ID, GUESS_CLEAR_ID]


def test_list_picker_with_no_matches_shows_a_disabled_placeholder():
    items = build_guess_list_picker_layout(
        _whisper(), [], placeholder="p", page=0, page_count=1, filter_query="zz",
    )
    select = next(i for i in _view(items).walk_children() if isinstance(i, discord.ui.Select))
    assert select.disabled is True
    assert select.placeholder == "No members match that search."


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(GuessOutcome(True, 2, False), "## ✅ You Solved It\nYou guessed right.", id="correct"),
        pytest.param(GuessOutcome(False, 2, False), "## ❌ Not Them\n2 guesses left.", id="wrong"),
        pytest.param(GuessOutcome(False, 1, False), "## ❌ Not Them\n1 guess left.", id="wrong-singular"),
        pytest.param(
            GuessOutcome(False, 0, True),
            "## ❌ Out of Guesses\nWrong — and that was the last one. The sender stays anonymous.",
            id="exhausted",
        ),
    ],
)
def test_guess_result_cards(outcome, expected):
    assert _text(build_guess_result_layout(outcome)) == expected


def test_line_layout_is_one_bare_text_display():
    items = build_line_layout("❌ Nope.")
    assert len(items) == 1 and _text(items) == "❌ Nope."


# ── Opt-in (paradigm 5) ─────────────────────────────────────────────────


def test_optin_card_has_a_green_yes_and_a_grey_cancel():
    items = build_optin_layout()
    assert "## 📬 Join Whisper" in _text(items)
    yes, cancel = _buttons(items)
    assert (yes.label, yes.style, yes.custom_id) == ("Opt In", discord.ButtonStyle.success, OPTIN_CONFIRM_ID)
    assert (cancel.style, cancel.custom_id) == (discord.ButtonStyle.secondary, OPTIN_CANCEL_ID)


def test_opt_receipts():
    assert _text(build_optin_done_layout()) == "## ✅ You're In\nYou can send and receive whispers now."
    assert "The ones you already have stay." in _text(build_optout_done_layout())


# ── Pool refusal with the draft ─────────────────────────────────────────


def test_pool_refusal_quotes_the_draft_escaped_and_fits_a_full_one():
    items = build_pool_refusal_layout(ERROR_TARGET_NEEDS_ROLE, "*" * MAX_MESSAGE_LENGTH)
    text = _text(items)
    assert text.startswith("## ❌ They Haven't Opted In\n" + ERROR_TARGET_NEEDS_ROLE[2:])
    assert "> " + "\\*" * MAX_MESSAGE_LENGTH in text
    check_budget(_view(items))


# ── Mod-log notices (paradigm 7) ────────────────────────────────────────


def test_reply_audit_names_both_parties_keeps_ids_and_stamps_the_time():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    text = _text(build_reply_audit_layout(
        whisper_id=7, from_user_id=100, to_user_id=200, content="hi *there*",
        name_fn=NAME_FN, now=now,
    ))
    assert "## 💬 Whisper Reply" in text
    assert "**From** Sender Sam (`100`)\n**To** Target Tina (`200`)" in text
    assert "> hi \\*there\\*" in text
    assert f"-# Whisper #7 · <t:{int(now.timestamp())}:f>" in text


def test_report_audit_quotes_body_and_reason_escaped():
    text = _text(build_report_audit_layout(
        whisper=_whisper(message="## forged"), reason="spam _ish_", name_fn=NAME_FN,
    ))
    assert "## 🚨 Whisper Reported" in text
    assert "**Sender** Sender Sam (`100`)" in text
    assert "**Reporter** Target Tina (`200`) · the recipient" in text
    assert "> \\## forged" in text
    assert "**Reason**\n> spam \\_ish\\_" in text
    assert "-# Whisper #12 · <t:" in text


def test_reply_report_audit_carries_both_ids():
    text = _text(build_reply_report_audit_layout(
        reply=WhisperReply(3, 12, 100, 200, "c", NOW), reporter_id=200, reason="r", name_fn=NAME_FN,
    ))
    assert "## 🚨 Reply Reported" in text
    assert "**Sender** Sender Sam (`100`) · anonymous to the reporter" in text
    assert "-# Reply #3 · Whisper #12 · <t:" in text


def test_audit_still_shows_the_id_when_the_name_is_unknown():
    """A departed user with no row still gives the mod something to click."""
    text = _text(build_reply_audit_layout(
        whisper_id=7, from_user_id=100, to_user_id=200, content="hi", name_fn=_names({}),
    ))
    assert "**From** User 100 (`100`)" in text


# ── Help ─────────────────────────────────────────────────────────────────


def test_help_without_a_doc_says_so_under_the_feature_title():
    text = _text(build_help_layout(None))
    assert text.startswith("## 📬 Whisper")
    assert layouts.HELP_MISSING in text


def test_help_reads_the_doc_it_is_given():
    text = _text(build_help_layout("# Whisper FAQ\nHow it works."))
    assert "How it works." in text
    assert layouts.HELP_DOC_KEY == "whisper"


def test_send_refusal_quotes_the_draft_escaped_under_a_generic_heading():
    items = build_send_refusal_layout("❌ Slow down.", "my *draft*")
    text = _text(items)
    assert "## ❌ Whisper Not Sent" in text
    assert "Slow down." in text
    assert "> my \\*draft\\*" in text


@pytest.mark.parametrize("draft", ["", "   ", "\n"])
def test_a_blank_draft_is_not_quoted_back(draft):
    """The empty-message refusal has nothing to hand back; a blank quote
    under "Here's what you wrote" read as a bug (ship review, 2026-10-01)."""
    from bot_modules.whisper.layouts import build_send_refusal_layout

    text = "\n".join(i.content for i in build_send_refusal_layout("❌ Write something first.", draft))
    assert "Write something first." in text
    assert "Here's what you wrote" not in text and "\n> " not in text
