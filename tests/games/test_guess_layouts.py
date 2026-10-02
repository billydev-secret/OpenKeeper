"""Guess Who's Components V2 cards (``games/guess/layouts.py``).

Three contracts ride here.

**Names.** A ``<@id>`` in a Text Display is resolved by the *reading* client,
from its own cache. So on a Guess card it renders as a bare numeric id for any
viewer who hasn't seen that member — which for the public leaderboard (which
names *past* posters, the people a reader is least likely to have cached) and
the solved reveal is the normal case, not an edge case. Every builder that
names a member takes a ``name_fn`` and emits plain text.

The exception is the no-contact reveal: when the submitter and the answer are a
no-contact pair, a raw ``User <id>`` is the *correct* output, because naming
them together in the bot's own voice is the bot manufacturing the association
the pair exists to prevent (``docs/specs/no_contact_spec.md``). That branch is pinned
below so a future find-and-replace over the ids can't quietly undo it.

**Spoilers.** Member-submitted media is spoilered on every card that shows it
to anyone but its submitter — the round, the reveal, the inspector.

**The guide.** Every card leads with a glyph-led ``##`` title, buttons are
grey unless the colour means something, and the fullest card each builder can
produce fits Discord's 40-component / 4000-character budget.
"""

from __future__ import annotations

import re
from typing import Any

import discord
import pytest

from bot_modules.core.layout import (
    MAX_ACCESSORY_LABEL,
    card,
    check_budget,
    text_columns,
)
from bot_modules.games.guess import layouts
from bot_modules.games.guess.models import GuessRound

_MENTION = re.compile(r"<@!?\d+>")

#: Text beside a button accessory wraps past about this many columns on a
#: phone (style guide § Screen use).
MAX_ROW_COLUMNS = 30


def _named(uid: int) -> str:
    return f"Member{uid}"


def _walk(items: list[Any]) -> list[Any]:
    return list(card(items).walk_children())


def _seen(items: list[Any]) -> str:
    """Every string a reader actually sees on a card."""
    return "\n".join(
        item.content
        for item in _walk(items)
        if isinstance(item, discord.ui.TextDisplay)
    )


def _galleries(items: list[Any]) -> list[discord.MediaGalleryItem]:
    return [
        media
        for item in _walk(items)
        if isinstance(item, discord.ui.MediaGallery)
        for media in item.items
    ]


def _buttons(items: list[Any]) -> list[discord.ui.Button]:
    return [i for i in _walk(items) if isinstance(i, discord.ui.Button)]


def _by_id(items: list[Any], component_id: int) -> Any:
    return card(items).find_item(component_id)


def _round(**kw) -> GuessRound:
    base = dict(
        id=1, guild_id=99, submitter_id=7, answer_id=8, channel_id=5,
        message_id=6, crop_path="", crop_url="", original_path="",
        difficulty="medium", candidate_count=4, reroll_count=0,
        created_at=1_800_000_000.0, solved_at=None, solver_id=None,
        guesses_to_solve=None, unique_guessers_to_solve=None,
        answer_optout=False, deleted_at=None,
    )
    base.update(kw)
    return GuessRound(**base)  # type: ignore[arg-type]


def _solved(n=_named, **kw) -> list[Any]:
    return layouts.build_solved_layout(
        1, *layouts.reveal_names(8, 7, no_contact=False, name_fn=n),
        9, 3, 2, name_fn=n, **kw,
    )


_LONG_NAME = "W" * 32  # the longest display name Discord allows
_ART = dict(
    header_url="https://example.test/header.webp",
    footer_url="https://example.test/footer.webp",
)


def _full_picker() -> list[Any]:
    """The fullest picker: a full page of the longest names, both arrows and
    an active filter."""
    return layouts.build_picker_layout(
        123456,
        [(_LONG_NAME * 4, str(i)) for i in range(layouts.PICKER_PAGE_SIZE)],
        status=layouts.picker_prompt(60),
        placeholder="x" * 200,
        page=1,
        page_count=3,
        filter_query="w" * 50,
    )


#: Every card the feature builds, at its fullest, by name.
_CARDS: list[tuple[str, Any]] = [
    ("prompt", lambda: layouts.build_prompt_layout()),
    ("prompt_art", lambda: layouts.build_prompt_layout(**_ART)),
    ("help_built_in", lambda: layouts.build_help_layout(None)),
    ("round_photo", lambda: layouts.build_round_layout(
        1, 3, media=layouts.attachment_ref(layouts.CROP_FILENAME))),
    ("round_confession", lambda: layouts.build_round_layout(
        1, 0, media=layouts.attachment_ref(layouts.CONFESSION_FILENAME),
        confession=True)),
    ("solved", lambda: _solved(
        media=layouts.attachment_ref(layouts.FULL_FILENAME))),
    ("round_art", lambda: layouts.build_round_layout(
        1, 3, media=layouts.attachment_ref(layouts.CROP_FILENAME),
        **_ART, title_in_header=True)),
    ("solved_art", lambda: _solved(
        media=layouts.attachment_ref(layouts.FULL_FILENAME),
        **_ART, title_in_header=True)),
    ("picker", _full_picker),
    ("crop_editor", lambda: layouts.build_crop_editor_layout(detections=True)),
    ("crop_editor_manual", lambda: layouts.build_crop_editor_layout(detections=False)),
    ("confession_preview", layouts.build_confession_preview_layout),
    ("consent", lambda: layouts.build_consent_layout(7)),
    ("leaderboard", lambda: layouts.build_leaderboard_layout(
        [(i, 999_999, 999_999) for i in range(5)],
        [(i, 999_999) for i in range(5)],
        name_fn=lambda _uid: _LONG_NAME,
    )),
    ("leaderboard_empty", lambda: layouts.build_leaderboard_layout(
        [], [], name_fn=_named)),
    ("leaderboard_art", lambda: layouts.build_leaderboard_layout(
        [(i, 999_999, 999_999) for i in range(5)],
        [(i, 999_999) for i in range(5)],
        name_fn=lambda _uid: _LONG_NAME, **_ART, title_in_header=True,
    )),
    ("inspector", lambda: layouts.build_inspector_layout(
        _round(solved_at=1_800_000_100.0, solver_id=9,
               crop_url="https://cdn.example.test/crop.jpg"),
        3, 2, name_fn=lambda _uid: _LONG_NAME)),
    ("notice", lambda: layouts.build_notice_layout("✅ Round Posted", "It's up.")),
]
_CARD_IDS = [c[0] for c in _CARDS]


# ── the guide: title, colour, budget ─────────────────────────────────────────


@pytest.mark.parametrize(("label", "build"), _CARDS, ids=_CARD_IDS)
def test_every_card_fits_the_components_v2_budget(label, build):
    check_budget(card(build()))


@pytest.mark.parametrize(("label", "build"), _CARDS, ids=_CARD_IDS)
def test_every_card_leads_with_a_glyph_led_title(label, build):
    first = _seen(build()).splitlines()[0]
    assert re.match(r"^## \S+ \S", first), f"{label} opens on {first!r}"
    assert not first[3].isalnum(), f"{label}'s title has no glyph: {first!r}"


#: The only coloured buttons, and what each colour means.
_COLOURED = {
    layouts.EDITOR_POST_ID: discord.ButtonStyle.success,        # the yes
    layouts.CONFESSION_POST_ID: discord.ButtonStyle.success,
    layouts.CONSENT_JOIN_ID: discord.ButtonStyle.success,
}


@pytest.mark.parametrize(
    "build",
    [
        lambda: layouts.build_crop_editor_layout(detections=True),
        layouts.build_confession_preview_layout,
        lambda: layouts.build_consent_layout(7),
    ],
    ids=["crop_editor", "confession_preview", "consent"],
)
def test_every_cancel_is_the_same_grey_labelled_button(build):
    """Design review, 2026-09-30: the confession preview's Cancel was red,
    the consent card's grey and the editor's an unlabelled red ✗. None of
    them destroys anything — they close the card."""
    (cancel,) = [b for b in _buttons(build()) if b.label == "Cancel"]
    assert cancel.style is discord.ButtonStyle.secondary


@pytest.mark.parametrize(("label", "build"), _CARDS, ids=_CARD_IDS)
def test_buttons_are_grey_unless_the_colour_means_something(label, build):
    for button in _buttons(build()):
        expected = _COLOURED.get(button.id, discord.ButtonStyle.secondary)
        assert button.style is expected, (
            f"{label}: {button.label!r} is {button.style}"
        )


# ── names: no card leaves a member as a raw Discord reference ────────────────

_NAME_CASES: list[tuple[str, Any]] = [
    ("solved_reveal", lambda n: _solved(n)),
    ("leaderboard", lambda n: layouts.build_leaderboard_layout(
        [(7, 4, 2)], [(8, 3)], name_fn=n)),
    ("leaderboard_empty", lambda n: layouts.build_leaderboard_layout(
        [], [], name_fn=n)),
    ("inspector_open", lambda n: layouts.build_inspector_layout(
        _round(), 3, 2, name_fn=n)),
    ("inspector_solved", lambda n: layouts.build_inspector_layout(
        _round(solved_at=1_800_000_100.0, solver_id=9), 3, 2, name_fn=n)),
    ("inspector_deleted", lambda n: layouts.build_inspector_layout(
        _round(deleted_at=1_800_000_100.0), 3, 2, name_fn=n)),
]


@pytest.mark.parametrize(
    ("label", "build"), _NAME_CASES, ids=[c[0] for c in _NAME_CASES]
)
def test_no_guess_card_leaves_a_raw_discord_reference(label, build):
    assert not _MENTION.search(_seen(build(_named))), (
        f"{label} left a raw <@id> reference"
    )


@pytest.mark.parametrize(
    ("label", "build"),
    [c for c in _NAME_CASES if c[0] not in ("leaderboard_empty",)],
    ids=[c[0] for c in _NAME_CASES if c[0] not in ("leaderboard_empty",)],
)
def test_every_guess_card_renders_the_resolved_name(label, build):
    text = _seen(build(_named))
    assert "Member7" in text or "Member8" in text or "Member9" in text, (
        f"{label} never rendered a resolved name"
    )


def test_every_guess_render_site_passes_a_resolver():
    """Walks the cog and requires every call to a name-taking builder to hand
    a resolver over, by keyword — so a render site can't be wired to a
    positional something-else and still pass."""
    import ast
    import inspect
    import pathlib

    from bot_modules.games.guess import cog as guess_cog

    needs = {
        name
        for name, fn in inspect.getmembers(layouts, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
    }
    assert {"build_solved_layout", "build_leaderboard_layout"} <= needs
    # Explicit utf-8: this source is full of em-dashes and the CI runner is
    # Windows, where the default encoding is cp1252.
    source = pathlib.Path(inspect.getfile(guess_cog)).read_text(encoding="utf-8")
    missed = [
        f"guess_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in needs
        and not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


# ── the round card ───────────────────────────────────────────────────────────


def test_round_card_shows_the_count_and_redacts_the_submitter():
    items = layouts.build_round_layout(
        42, 7, media=layouts.attachment_ref(layouts.CROP_FILENAME)
    )
    text = _seen(items)
    assert text.splitlines()[0] == "## 🎭 Round #42"
    assert layouts.PHOTO_QUESTION in text
    assert "Guesses: **7**" in text
    assert f"Submitted by {layouts.REDACTED}" in text


def test_round_card_keeps_the_embed_era_guess_button_id():
    """Rounds posted before the conversion carry ``guess_guess:<round>`` on
    their button; the converted view has to answer the same id or every open
    round in the channel goes dead at the restart."""
    (button,) = _buttons(layouts.build_round_layout(99, 0, media=None))
    assert button.custom_id == "guess_guess:99"
    assert button.label == "Guess"


def test_confession_round_asks_who_wrote_it():
    text = _seen(layouts.build_round_layout(
        1, 0, media=layouts.attachment_ref(layouts.CONFESSION_FILENAME),
        confession=True,
    ))
    assert layouts.CONFESSION_QUESTION in text
    assert layouts.PHOTO_QUESTION not in text


def test_round_card_without_media_draws_no_gallery():
    assert _galleries(layouts.build_round_layout(1, 0, media=None)) == []


_MEMBER_MEDIA_CASES: list[tuple[str, Any]] = [
    ("round", lambda: layouts.build_round_layout(
        1, 0, media=layouts.attachment_ref(layouts.CROP_FILENAME))),
    ("confession_round", lambda: layouts.build_round_layout(
        1, 0, media=layouts.attachment_ref(layouts.CONFESSION_FILENAME),
        confession=True)),
    ("reveal", lambda: _solved(media=layouts.attachment_ref(layouts.FULL_FILENAME))),
    ("inspector", lambda: layouts.build_inspector_layout(
        _round(crop_url="https://cdn.example.test/crop.jpg"), 0, 0, name_fn=_named)),
]


@pytest.mark.parametrize(
    ("label", "build"), _MEMBER_MEDIA_CASES, ids=[c[0] for c in _MEMBER_MEDIA_CASES]
)
def test_member_submitted_media_is_always_spoilered(label, build):
    """Guess never asks the channel whether it is age-gated — rounds may be
    NSFW anywhere the admin points the feature — so the spoiler is
    unconditional on every card someone other than the submitter reads."""
    galleries = _galleries(build())
    assert galleries, f"{label} shows no media"
    assert all(media.spoiler for media in galleries), f"{label} is unspoilered"


@pytest.mark.parametrize(
    "build",
    [
        lambda: layouts.build_crop_editor_layout(detections=True),
        layouts.build_confession_preview_layout,
    ],
    ids=["crop_editor", "confession_preview"],
)
def test_the_submitters_own_preview_is_not_spoilered(build):
    """An ephemeral preview of the member's own upload: a blur over the image
    they are trying to frame would defeat the editor."""
    galleries = _galleries(build())
    assert galleries and not any(media.spoiler for media in galleries)


# ── the reveal ───────────────────────────────────────────────────────────────


def test_solved_reveal_names_answer_submitter_and_solver():
    text = _seen(_solved())
    assert text.splitlines()[0] == "## ✅ Round #1 — Solved!"
    assert "**Answer:** Member8" in text
    assert "**Submitted by:** Member7" in text
    assert "Solved by **Member9**" in text


def test_a_self_submitted_reveal_names_its_member_once():
    """Every submit path makes the submitter the answer, so the two-line
    form printed the same name twice on every real round."""
    answer, submitter = layouts.reveal_names(7, 7, no_contact=False, name_fn=_named)
    text = _seen(layouts.build_solved_layout(
        1, answer, submitter, 9, 3, 2, name_fn=_named, self_submitted=True,
    ))
    assert "It was **Member7**." in text
    assert text.count("Member7") == 1
    assert "Submitted by" not in text


def test_the_solver_line_fits_a_phone_with_the_longest_name():
    """The counts ride on their own subtext line: on one line with a
    32-character name they ran to ~68 columns."""
    text = _seen(layouts.build_solved_layout(
        1, "A", "A", 9, 1234, 567, name_fn=lambda _uid: _LONG_NAME,
        self_submitted=True,
    ))
    for line in text.splitlines()[1:]:
        visible = line.replace("**", "").removeprefix("-# ")
        assert text_columns(visible) <= 45, line


def test_solved_reveal_carries_no_controls():
    assert _buttons(_solved()) == []


@pytest.mark.parametrize(
    ("guess_count", "unique_count", "expected"),
    [
        (1, 1, "-# 1 guess • 1 guesser"),
        (3, 2, "-# 3 guesses • 2 guessers"),
    ],
)
def test_solved_reveal_pluralises_its_counts(guess_count, unique_count, expected):
    text = _seen(layouts.build_solved_layout(
        1, "A", "B", 9, guess_count, unique_count, name_fn=_named
    ))
    assert expected in text


# ── the trap: a no-contact pair stays a pair of numbers ──────────────────────


def test_no_contact_pair_degrades_to_plain_ids_not_names():
    """The one place in Guess where a bare number is the *correct* output.

    Resolving these to display names would have the bot state the association
    the no-contact list exists to prevent, in its own voice. This must survive
    every future sweep that replaces raw ids with resolved names.
    """
    answer, submitter = layouts.reveal_names(
        8, 7, no_contact=True, name_fn=_named
    )
    assert (answer, submitter) == ("User 8", "User 7")

    text = _seen(layouts.build_solved_layout(
        1, answer, submitter, 9, 3, 2, name_fn=_named
    ))
    assert "Member8" not in text and "Member7" not in text
    assert "User 8" in text and "User 7" in text
    assert "Member9" in text  # the solver is still named — never a pair partner


def test_no_contact_degrade_does_not_leak_a_mention_either():
    """Degrading must land on plain ids, not back on ``<@id>``: a mention is
    what the reader's client would resolve into a name for anyone who *has*
    cached the pair, reintroducing the association."""
    answer, submitter = layouts.reveal_names(
        8, 7, no_contact=True, name_fn=lambda uid: f"<@{uid}>"
    )
    assert not _MENTION.search(f"{answer} {submitter}")


# ── the picker ───────────────────────────────────────────────────────────────


def _picker(**kw) -> list[Any]:
    base: dict[str, Any] = dict(status="Who?", placeholder="Who is it?")
    base.update(kw)
    return layouts.build_picker_layout(5, [("Alice", "1"), ("Bo", "2")], **base)


def test_picker_lists_this_pages_members_under_the_status():
    items = _picker(status=layouts.picker_prompt(60))
    text = _seen(items)
    assert text.splitlines()[0] == "## 🎭 Round #5"
    assert layouts.PICKER_ASK in text and "60 seconds" in text
    select = _by_id(items, layouts.PICKER_SELECT_ID)
    assert [(o.label, o.value) for o in select.options] == [("Alice", "1"), ("Bo", "2")]
    assert select.disabled is False


def test_picker_with_no_matches_shows_a_disabled_select():
    items = layouts.build_picker_layout(5, [], status="Who?", placeholder="x")
    select = _by_id(items, layouts.PICKER_SELECT_ID)
    assert select.disabled is True
    assert "No members match" in select.placeholder


@pytest.mark.parametrize(
    ("kw", "present", "disabled"),
    [
        pytest.param(dict(), {"🔍 Filter"}, set(), id="one_page"),
        pytest.param(
            dict(page=0, page_count=3), {"◀", "▶", "🔍 Filter"}, {"◀"},
            id="first_page",
        ),
        pytest.param(
            dict(page=1, page_count=3), {"◀", "▶", "🔍 Filter"}, set(),
            id="middle_page",
        ),
        pytest.param(
            dict(page=2, page_count=3), {"◀", "▶", "🔍 Filter"}, {"▶"},
            id="last_page",
        ),
        pytest.param(
            dict(filter_query="al"), {"🔍 Filter", "✕ Clear"}, set(),
            id="filtered",
        ),
    ],
)
def test_picker_controls(kw, present, disabled):
    """Arrows only when there is a second page, disabled at the ends rather
    than hidden; Clear only while a filter is on."""
    buttons = _buttons(_picker(**kw))
    assert {b.label for b in buttons} == present
    assert {b.label for b in buttons if b.disabled} == disabled


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (dict(filter_query="", match_count=40, page=0, page_count=1), "Who?"),
        (dict(filter_query="", match_count=40, page=1, page_count=2), "Who? (2/2)"),
        (dict(filter_query="al", match_count=1, page=0, page_count=1),
         '🔍 "al" — 1 match'),
        (dict(filter_query="al", match_count=30, page=0, page_count=2),
         '🔍 "al" — 30 matches (1/2)'),
    ],
)
def test_picker_placeholder(kw, expected):
    assert layouts.picker_placeholder("Who?", **kw) == expected


def test_picker_placeholder_is_cut_to_discords_limit():
    text = layouts.picker_placeholder(
        "x" * 400, filter_query="", match_count=0, page=0, page_count=1
    )
    assert len(text) == 150


# ── the crop editor ──────────────────────────────────────────────────────────


def test_crop_editor_has_every_control_bound_to_an_id():
    items = layouts.build_crop_editor_layout(detections=True)
    for component_id in (
        layouts.EDITOR_UP_ID, layouts.EDITOR_DOWN_ID, layouts.EDITOR_LEFT_ID,
        layouts.EDITOR_RIGHT_ID, layouts.EDITOR_ZOOM_IN_ID,
        layouts.EDITOR_ZOOM_OUT_ID, layouts.EDITOR_AUTO_ID,
        layouts.EDITOR_POST_ID, layouts.EDITOR_CANCEL_ID,
    ):
        assert _by_id(items, component_id) is not None
    assert layouts.EDITOR_HINT in _seen(items)
    assert _by_id(items, layouts.EDITOR_AUTO_ID).disabled is False


def test_crop_editor_without_detections_disables_auto_and_says_why():
    items = layouts.build_crop_editor_layout(detections=False)
    assert _by_id(items, layouts.EDITOR_AUTO_ID).disabled is True
    assert layouts.EDITOR_HINT_NO_DETECTIONS in _seen(items)


def test_crop_editor_previews_the_uploaded_editor_image():
    (media,) = _galleries(layouts.build_crop_editor_layout(detections=True))
    assert media.media.url == f"attachment://{layouts.EDITOR_PREVIEW_FILENAME}"


# ── the consent card ─────────────────────────────────────────────────────────


def test_consent_card_discloses_what_joining_stores():
    """The disclosure moment: the facts the role grant waits on must all be
    on the card, not behind a link."""
    text = _seen(layouts.build_consent_layout(7))
    assert "cached on the bot" in text
    assert "cleared after 7 days" in text
    assert "visible to admins" in text
    assert "/guess optout" in text
    labels = [b.label for b in _buttons(layouts.build_consent_layout(7))]
    assert labels == ["Join the Pool", "Cancel"]


# ── the channel prompt ───────────────────────────────────────────────────────


def test_prompt_keeps_its_persistent_ids():
    ids = {b.custom_id for b in _buttons(layouts.build_prompt_layout())}
    assert ids == {"guess_prompt_submit", "guess_prompt_help"}


def test_prompt_without_art_is_a_plain_headed_card():
    items = layouts.build_prompt_layout()
    assert _galleries(items) == []
    assert _seen(items).splitlines()[:2] == ["## 🎭 Guess Who", layouts.PROMPT_TAGLINE]
    assert f"-# {layouts.PROMPT_INTRO}" in _seen(items)


def test_prompt_with_a_titled_strip_drops_its_heading():
    """The title is drawn into the header, so a ``##`` line under it would
    say the name twice; the header's alt text carries it instead."""
    items = layouts.build_prompt_layout(**_ART, title_in_header=True)
    assert "##" not in _seen(items)
    assert _seen(items).splitlines()[0] == layouts.PROMPT_TAGLINE
    assert f"-# {layouts.PROMPT_INTRO}" in _seen(items)
    header, footer = _galleries(items)
    assert header.media.url == _ART["header_url"]
    assert header.description == layouts.TITLE
    assert footer.media.url == _ART["footer_url"]
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)


def test_prompt_with_an_untitled_strip_keeps_its_heading():
    """A failed title drawing must never leave a panel with no name."""
    items = layouts.build_prompt_layout(**_ART, title_in_header=False)
    assert "## 🎭 Guess Who" in _seen(items)
    assert len(_galleries(items)) == 2


def test_prompt_footer_alone_draws_nothing():
    """Ships dark: the strip is the header's; a guild with only hub art set
    sees no change."""
    items = layouts.build_prompt_layout(footer_url=_ART["footer_url"])
    assert _galleries(items) == []


@pytest.mark.parametrize("kw", [dict(), dict(**_ART, title_in_header=True)])
def test_prompt_rows_fit_beside_their_buttons(kw):
    """A row's text stays on one line per line beside its button on a phone,
    and the label is what drops a button under its row."""
    for item in layouts.build_prompt_layout(**kw):
        if not isinstance(item, discord.ui.Section):
            continue
        assert text_columns(item.accessory.label) <= MAX_ACCESSORY_LABEL
        for child in item.children:
            for line in child.content.splitlines():
                if line.startswith("## "):
                    continue  # the heading's width is the title's, not a row's
                visible = line.replace("**", "").removeprefix("-# ")
                assert text_columns(visible) <= MAX_ROW_COLUMNS, line


# ── the public cards wear the lobby strip ────────────────────────────────────

_FRAMED: list[tuple[str, Any, str]] = [
    ("round", lambda **kw: layouts.build_round_layout(
        7, 0, media=layouts.attachment_ref(layouts.CROP_FILENAME), **kw),
     "## 🎭 Round #7"),
    ("solved", lambda **kw: _solved(
        media=layouts.attachment_ref(layouts.FULL_FILENAME), **kw),
     "## ✅ Round #1 — Solved!"),
    ("leaderboard", lambda **kw: layouts.build_leaderboard_layout(
        [(7, 4, 2)], [(8, 3)], name_fn=_named, **kw),
     "## 🏆 Guess Who Leaderboard"),
]
_FRAMED_IDS = [c[0] for c in _FRAMED]


@pytest.mark.parametrize(("label", "build", "heading"), _FRAMED, ids=_FRAMED_IDS)
def test_public_cards_open_on_the_titled_strip_and_close_on_the_footer(
    label, build, heading
):
    """Ben, 2026-09-30: "we're missing headers on a bunch of messages". The
    round, its reveal and the leaderboard are framed like a game lobby. The
    round's own heading stays — it names the round, the strip names the
    game — and only the leaderboard, whose heading *is* the game's name,
    shortens it."""
    items = build(**_ART, title_in_header=True)
    assert isinstance(items[0], discord.ui.MediaGallery)
    assert isinstance(items[-1], discord.ui.MediaGallery)
    header, *_rest, footer = _galleries(items)
    assert header.media.url == _ART["header_url"]
    assert header.description == layouts.TITLE
    assert header.spoiler is False  # the server's own art, never spoilered
    assert footer.media.url == _ART["footer_url"]
    first = _seen(items).splitlines()[0]
    if label == "leaderboard":
        assert first == "## 🏆 Leaderboard"
    else:
        assert first == heading


@pytest.mark.parametrize(("label", "build", "heading"), _FRAMED, ids=_FRAMED_IDS)
def test_public_cards_keep_their_heading_when_the_strip_is_untitled(
    label, build, heading
):
    items = build(**_ART, title_in_header=False)
    assert _seen(items).splitlines()[0] == heading
    assert _galleries(items)[0].description != layouts.TITLE


@pytest.mark.parametrize(("label", "build", "heading"), _FRAMED, ids=_FRAMED_IDS)
def test_public_cards_draw_nothing_without_a_header(label, build, heading):
    """Ships dark: the footer alone never reaches a card."""
    plain = build()
    footer_only = build(footer_url=_ART["footer_url"])
    assert [type(i) for i in footer_only] == [type(i) for i in plain]
    assert _seen(footer_only).splitlines()[0] == heading


# ── Help ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("body", [None, "", "   \n"], ids=["none", "empty", "blank"])
def test_help_without_a_doc_shows_the_built_in_how_to(body):
    """Neither guild has a ``guess`` doc; Help must not answer a server that
    has played for months with a "no doc yet" line."""
    text = _seen(layouts.build_help_layout(body))
    assert text.splitlines()[0] == "## 🎭 How Guess Who Works"
    assert "/guess optin" in text and "first correct guess" in text


def test_help_with_a_doc_shows_the_doc_not_the_built_in():
    text = _seen(layouts.build_help_layout("# House Rules\nFaces stay out."))
    assert text.splitlines()[0] == "## 🎭 House Rules"
    assert "Faces stay out." in text
    assert "/guess optin" not in text


# ── the leaderboard and the mod inspector ────────────────────────────────────


def test_leaderboard_ranks_both_lists():
    text = _seen(layouts.build_leaderboard_layout(
        [(7, 4, 2), (8, 1, 0)], [(9, 3)], name_fn=_named
    ))
    assert "### Top Posters" in text and "### Top Guessers" in text
    assert "🥇 **Member7**\n-# 4 posted • 2 solved • 50%" in text
    assert "🥈 **Member8**\n-# 1 posted • 0 solved • 0%" in text
    assert "🥇 **Member9**\n-# 3 solved" in text


def test_leaderboard_rows_fit_a_phone_and_line_up():
    """Two lines a row: on one, a 32-character name with its figures ran to
    ~60 columns. Every rank is an emoji, so the names start in one column."""
    text = _seen(layouts.build_leaderboard_layout(
        [(i, 999_999, 999_999) for i in range(5)], [(i, 999_999) for i in range(5)],
        name_fn=lambda _uid: _LONG_NAME,
    ))
    rows = [ln for ln in text.splitlines() if not ln.startswith("#")]
    for line in rows:
        assert text_columns(line.replace("**", "").removeprefix("-# ")) <= 45, line
    ranks = [ln.split(" ", 1)[0] for ln in rows if _LONG_NAME in ln]
    assert ranks == ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"] * 2


def test_leaderboard_empty_states():
    text = _seen(layouts.build_leaderboard_layout([], [], name_fn=_named))
    assert "No rounds posted yet — press **Submit** on the Guess Who panel." in text
    assert "No rounds solved yet." in text


def test_inspector_shows_name_and_id_for_every_member():
    text = _seen(layouts.build_inspector_layout(
        _round(solved_at=1_800_000_100.0, solver_id=9), 3, 2, name_fn=_named
    ))
    assert "Member7 (`7`)" in text   # submitter
    assert "Member8 (`8`)" in text   # answer
    assert "Member9 (`9`)" in text   # solver


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (dict(), "⏳ Open"),
        (dict(solved_at=1_800_000_100.0, solver_id=9), "✅ Solved by Member9"),
        (dict(deleted_at=1_800_000_100.0), "🗑 Deleted"),
        # Rounds solved before ``solver_id`` was recorded still render.
        (dict(solved_at=1_800_000_100.0, solver_id=None), "✅ Solved by someone"),
    ],
    ids=["open", "solved", "deleted", "solved_no_solver"],
)
def test_inspector_status_line(row, expected):
    text = _seen(layouts.build_inspector_layout(
        _round(**row), 3, 2, name_fn=_named
    ))
    assert expected in text


def test_inspector_without_a_crop_url_draws_no_gallery():
    assert _galleries(layouts.build_inspector_layout(
        _round(), 0, 0, name_fn=_named
    )) == []


# ── reading a posted round back ──────────────────────────────────────────────


class _Media:
    def __init__(self, url):
        self.media = type("M", (), {"url": url})()


class _Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_gallery_urls_finds_media_at_any_depth():
    """``message.attachments`` is empty on a V2 message, so the round's
    stored crop URL is read out of its gallery."""
    components = [
        _Node(content="## Round"),
        _Node(items=[_Media("https://cdn/a.jpg"), _Media("https://cdn/b.jpg")]),
        _Node(children=[_Node(items=[_Media("https://cdn/c.jpg")])]),
        _Node(accessory=_Node(items=[_Media("https://cdn/d.jpg")])),
    ]
    assert layouts.gallery_urls(components) == [
        "https://cdn/a.jpg", "https://cdn/b.jpg", "https://cdn/c.jpg",
        "https://cdn/d.jpg",
    ]


@pytest.mark.parametrize("components", [None, [], object(), [_Node(items=[_Media("")])]])
def test_gallery_urls_is_empty_for_anything_without_media(components):
    assert layouts.gallery_urls(components) == []
