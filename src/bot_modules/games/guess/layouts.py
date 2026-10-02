"""Guess Who's Components V2 cards (``docs/components_v2_style_guide.md``).

Pure builders, no IO: primitives and a ``name_fn`` in, a list of layout items
out. Persistent buttons carry the same custom ids the embed-era views used
(``guess_prompt_submit``, ``guess_prompt_help``, ``guess_guess:<round>``), so a
press on a message posted before the conversion still lands; the controls on
the ephemeral cards carry a numeric ``id`` instead, and ``games/guess/cog.py``
binds both kinds. No builder takes a colour.

The cards, by paradigm (guide § The paradigms):

- **The channel prompt** (1, hub panel) — :func:`build_prompt_layout`. Title
  with a grey Help beside it, one sentence, one Submit row. It had drawn no
  art before this conversion, so it wears the small lobby strip
  (``core.layout.lobby_art``) rather than a second tall banner.
- **The round card** (9, game turn card) — :func:`build_round_layout`, which
  a first correct guess edits into :func:`build_solved_layout`. The two
  disabled "chip" buttons the embed era used as labels are a ``-#`` line now.
  **The public cards wear the lobby strip** (Ben, 2026-09-30: "we're missing
  headers on a bunch of messages"): the round card, its reveal and the
  leaderboard open on the guild's small header with "Guess Who" drawn in and
  close on the footer, the way a game lobby does; they keep their own ``##``
  heading, since it names the round, not the game. The ephemeral cards stay
  plain.
- **The guess picker** (9), ephemeral — :func:`build_picker_layout`, redrawn
  in place with the outcome of each pick.
- **The crop editor**, ephemeral — :func:`build_crop_editor_layout`: the
  preview over a D-pad grid.
- **The confession preview** and **the opt-in consent** (5, decision cards),
  ephemeral — :func:`build_confession_preview_layout`,
  :func:`build_consent_layout`.
- **The leaderboard**, **the mod inspector**, **Help** and every one-line
  outcome (7, receipt / notice) — :func:`build_leaderboard_layout`,
  :func:`build_inspector_layout`, :func:`build_help_layout`,
  :func:`build_notice_layout`.

Every builder that names a member takes a ``name_fn``
(:mod:`bot_modules.shared.name_resolver`) and emits plain text: a ``<@id>`` in
a Text Display renders from the *reading* client's cache, so on a public card
it degrades to a bare number for anyone who hasn't seen that member. The one
place a number is right is :func:`reveal_names`' no-contact branch.

**Member-submitted media is always spoilered** — the crop, the full-image
reveal, the confession card and the inspector's copy of the crop — wherever
the card lands. Guess never asked the channel (``docs/specs/guess_spec.md``: the
bot enforces nothing about where a round is posted), so the spoiler is
unconditional rather than gated on the channel's NSFW flag. The submitter's
own previews (the crop editor, the confession preview) are theirs alone and
go unspoilered.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.docs.help_card import build_doc_help_layout
from bot_modules.games.guess.models import GuessRound
from bot_modules.shared.name_resolver import NameFn

Item = discord.ui.Item[Any]

ICON = "🎭"
#: The hub's own name, drawn into the lobby strip's header in place of the
#: ``##`` heading when a guild has lobby header art set.
TITLE = "Guess Who"

#: The Docs-panel doc Help reads. Renaming the key disconnects the button —
#: it falls back to :data:`HELP_BUILT_IN`, never to a blank card.
HELP_DOC_KEY = "guess"

# ── Persistent custom ids — unchanged from the embed-era views ───────────────

PROMPT_SUBMIT_ID = "guess_prompt_submit"
PROMPT_HELP_ID = "guess_prompt_help"
GUESS_ID_TEMPLATE = "guess_guess:{round_id}"

# ── Transient component ids (ephemeral cards; bound with ``find_item``) ─────

PICKER_SELECT_ID = 101
PICKER_PREV_ID = 102
PICKER_NEXT_ID = 103
PICKER_FILTER_ID = 104
PICKER_CLEAR_ID = 105

EDITOR_UP_ID = 201
EDITOR_DOWN_ID = 202
EDITOR_LEFT_ID = 203
EDITOR_RIGHT_ID = 204
EDITOR_ZOOM_IN_ID = 205
EDITOR_ZOOM_OUT_ID = 206
EDITOR_AUTO_ID = 207
EDITOR_POST_ID = 208
EDITOR_CANCEL_ID = 209

CONFESSION_POST_ID = 301
CONFESSION_CANCEL_ID = 302

CONSENT_JOIN_ID = 401
CONSENT_CANCEL_ID = 402

# ── Attachment filenames ─────────────────────────────────────────────────────
#
# A V2 message shows an upload only where a component references it, by
# ``attachment://<filename>``. The ``SPOILER_`` prefix is what the embed era
# relied on; the gallery item's ``spoiler=True`` is what does the work now,
# and the prefix stays so the file is blurred on any client that shows it raw.

CROP_FILENAME = "SPOILER_guess_crop.jpg"
CONFESSION_FILENAME = "SPOILER_guess_confession.jpg"
FULL_FILENAME = "SPOILER_guess_full.jpg"
EDITOR_PREVIEW_FILENAME = "preview.jpg"
CONFESSION_PREVIEW_FILENAME = "confession_preview.jpg"

#: ▒ is U+2592 MEDIUM SHADE × 7 — a redaction bar, not a font glitch.
REDACTED = "▒▒▒▒▒▒▒"

PHOTO_QUESTION = "Who is in this photo?"
CONFESSION_QUESTION = "Who wrote this?"

#: Rows are ``(user_id, posted, solved)`` for posters, ``(user_id, solved)``
#: for guessers — straight from the repo's leaderboard queries.
#: Emoji all the way down, so the five rows start in the same column (the
#: embed era's "4." / "5." sat narrower than the medals above them).
_MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

#: Discord's ceiling on options in one select.
PICKER_PAGE_SIZE = 25


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _button(
    label: str,
    component_id: int,
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
) -> discord.ui.Button[Any]:
    """A transient button: grey unless the colour means something."""
    return discord.ui.Button(
        label=label, style=style, disabled=disabled, id=component_id
    )


def attachment_ref(filename: str) -> str:
    """The ``attachment://`` reference a gallery item uses for an upload."""
    return f"attachment://{filename}"


def _gallery(
    media: str, alt: str, *, spoiler: bool
) -> discord.ui.MediaGallery[Any]:
    return discord.ui.MediaGallery(
        discord.MediaGalleryItem(media, description=alt, spoiler=spoiler)
    )


def _plural(count: int, noun: str) -> str:
    suffix = "" if count == 1 else ("es" if noun.endswith(("s", "ch")) else "s")
    return f"{count} {noun}{suffix}"


def build_notice_layout(title: str, body: str = "") -> list[Item]:
    """A one-line outcome on an ephemeral card. **Paradigm 7 — notice.**

    A V2 message can never go back to plain ``content``, so whatever an
    ephemeral Guess card edits into — posted, couldn't post, joined — is one
    of these. ``title`` arrives glyph-led (``✅ Round Posted``).
    """
    return [_text(f"## {title}" + (f"\n{body}" if body else ""))]


# ── 1. The channel prompt ────────────────────────────────────────────────────

#: The line beside Help. Held under the ~30 columns that fit beside a button
#: on a phone (guide § Screen use) — the first cut put the whole 69-character
#: intro there, and on the titled prompt Help dropped under it.
PROMPT_TAGLINE = "Guess whose photo it is."
#: The rest of the intro, a full-width subtext line under the title block.
PROMPT_INTRO = "Photos go up cropped and anonymous — SFW or NSFW."


def build_prompt_layout(
    *,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[Item]:
    """The sticky prompt at the foot of the Guess channel. **Paradigm 1 — hub
    panel.**

    One kind of row, so no group heading and no divider (guide § Controls).
    The how-to is behind **Help**, which reads the guild's ``guess`` doc. The
    embed's "to upload a photo directly, use /guess submit" line is gone with
    its reason: the form Submit opens takes an upload now.

    ``header_url``/``footer_url``/``title_in_header`` are the guild's lobby
    strip (``core.layout.lobby_art``): nothing draws without a header, and a
    header with the title drawn in replaces the ``##`` line. Either way Help
    sits beside the short :data:`PROMPT_TAGLINE`, and :data:`PROMPT_INTRO`
    runs full width under it.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    help_button: discord.ui.Button[Any] = discord.ui.Button(
        label="Help", style=discord.ButtonStyle.secondary, custom_id=PROMPT_HELP_ID
    )
    title_block = PROMPT_TAGLINE if titled else f"## {ICON} {TITLE}\n{PROMPT_TAGLINE}"
    return [
        *top,
        discord.ui.Section(_text(title_block), accessory=help_button),
        _text(f"-# {PROMPT_INTRO}"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
        discord.ui.Section(
            _text("**Start a Round**\n-# Upload a photo or a link."),
            accessory=discord.ui.Button(
                label="Submit",
                style=discord.ButtonStyle.secondary,
                custom_id=PROMPT_SUBMIT_ID,
            ),
        ),
        *bottom,
    ]


#: What Help shows until an admin writes the ``guess`` doc on the dashboard.
HELP_BUILT_IN = (
    "Members of the Guess pool submit anonymized images — SFW or NSFW. "
    "Everyone else guesses who's in the photo.\n"
    "\n"
    "• **Submit:** press **Submit** on the panel, or `/guess submit` with an "
    "image. The bot crops it and you frame the crop, then post it "
    "anonymously.\n"
    "• **Guess:** press **Guess** on a posted round and pick a name. The "
    "line under the image counts total guesses on the round.\n"
    "• **Solve:** the first correct guess wins, reveals the full image as a "
    "spoiler, and credits the submitter.\n"
    "• **Join the pool:** `/guess optin` to add yourself. Only pool members "
    "can submit and be guessed.\n"
    "• **Leave:** `/guess optout` — your open rounds stop being solvable."
)


def build_help_layout(body_md: str | None) -> list[Item]:
    """The prompt's Help card. **Paradigm 7 — notice.**

    The guild's ``guess`` doc when there is one; :data:`HELP_BUILT_IN` when
    there isn't, so Help never answers with a "no doc yet" line on a server
    that has been playing for months. Send with ``AllowedMentions.none()`` —
    an admin-written doc may hold a role mention.
    """
    return build_doc_help_layout(
        body_md,
        glyph=ICON,
        fallback_title="How Guess Who Works",
        missing_text=HELP_BUILT_IN,
    )


# ── 9. The round card and its reveal ─────────────────────────────────────────


def guess_button(round_id: int) -> discord.ui.Button[Any]:
    """The round's one control. Persistent: a round can sit open for weeks."""
    return discord.ui.Button(
        label="Guess",
        style=discord.ButtonStyle.secondary,
        custom_id=GUESS_ID_TEMPLATE.format(round_id=round_id),
    )


def build_round_layout(
    round_id: int,
    guess_count: int,
    *,
    media: str | None,
    confession: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[Item]:
    """An open round. **Paradigm 9 — game turn card.**

    ``header_url``/``footer_url``/``title_in_header`` are the guild's lobby
    strip (``games/utils/lobby_art.read_lobby_art``); nothing draws without
    a header. The ``##`` line stays either way — it names the round, and
    the strip names the game.

    ``media`` is the crop (or the confession card) as the message carries it
    — ``attachment_ref(CROP_FILENAME)`` on a fresh post, the existing
    upload's own name when an embed-era round converts on its next edit.
    ``None`` draws no gallery, which is also what the restart-time view
    registration passes: it only needs the button.

    The submitter line is a redaction bar on purpose — in Guess the submitter
    is the answer.
    """
    question = CONFESSION_QUESTION if confession else PHOTO_QUESTION
    alt = (
        f"Round {round_id}'s anonymous confession card."
        if confession
        else f"Round {round_id}'s cropped photo."
    )
    top, bottom = lobby_art(
        header_url, footer_url, title=TITLE if header_url and title_in_header else None
    )
    items: list[Item] = [*top, _text(f"## {ICON} Round #{round_id}\n{question}")]
    if media:
        items.append(_gallery(media, alt, spoiler=True))
    items.append(
        _text(f"-# Guesses: **{guess_count}** • Submitted by {REDACTED}")
    )
    items.append(discord.ui.ActionRow(guess_button(round_id)))
    return [*items, *bottom]


def reveal_names(
    answer_id: int,
    submitter_id: int,
    *,
    no_contact: bool,
    name_fn: NameFn,
) -> tuple[str, str]:
    """How the solved reveal refers to the answer and the submitter.

    Normally both are resolved to display names. When the two are a
    **no-contact pair**, both degrade to a bare ``User <id>`` instead: the
    card pings nobody, but the reveal still names them together in the bot's
    own voice, which is the bot manufacturing the association the pair exists
    to prevent. A raw id is the *correct* output there, and this is the one
    place in Guess where a number is not the bug — ``docs/specs/no_contact_spec.md``.
    """
    if no_contact:
        return f"User {answer_id}", f"User {submitter_id}"
    return name_fn(answer_id), name_fn(submitter_id)


def build_solved_layout(
    round_id: int,
    answer_name: str,
    submitter_name: str,
    solver_id: int,
    guess_count: int,
    unique_count: int,
    *,
    name_fn: NameFn,
    media: str | None = None,
    confession: bool = False,
    self_submitted: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[Item]:
    """The public reveal the round card edits into. **Paradigm 7 — receipt.**
    Framed like the round card it replaces.

    ``self_submitted`` is the round's normal case — every submit path sets
    the answer to the submitter — and names them once ("It was **X**")
    rather than as the answer over the submitter. The two-line form is kept
    for a round whose two ids differ, which is also the only shape the
    no-contact ``User <id>`` degrade can take.

    The solver gets their own line and the counts drop to subtext: with a
    32-character name the one-line "Solved by … in N guesses (across M
    guessers)" ran to ~68 columns, past what a phone keeps on one line.

    ``answer_name``/``submitter_name`` arrive already rendered, from
    :func:`reveal_names` — that pair's text is a no-contact decision the
    caller makes with the database, not something a builder can re-derive.
    The solver is named here: a member who is a no-contact partner of either
    side can't have guessed the round in the first place.

    ``media`` is the full image on a photo round whose original was still on
    disk, otherwise whatever the round card already showed. No controls — the
    solve removes the Guess button, as it always has.
    """
    if confession:
        alt = f"Round {round_id}'s confession card."
    else:
        alt = f"Round {round_id}'s photo, revealed."
    top, bottom = lobby_art(
        header_url, footer_url, title=TITLE if header_url and title_in_header else None
    )
    who = (
        f"It was **{answer_name}**."
        if self_submitted
        else f"**Answer:** {answer_name}\n**Submitted by:** {submitter_name}"
    )
    items: list[Item] = [
        *top,
        _text(
            f"## ✅ Round #{round_id} — Solved!\n"
            f"{who}\n"
            f"Solved by **{name_fn(solver_id)}**\n"
            f"-# {_plural(guess_count, 'guess')} • "
            f"{_plural(unique_count, 'guesser')}"
        )
    ]
    if media:
        items.append(_gallery(media, alt, spoiler=True))
    return [*items, *bottom]


# ── 9. The guess picker ──────────────────────────────────────────────────────

#: The picker's status line. The *question* is the select's placeholder
#: (:data:`PHOTO_QUESTION` / :data:`CONFESSION_QUESTION`), one line below —
#: asking it here too put two different questions on one card.
PICKER_ASK = "Pick a name from the list."


def picker_prompt(timeout_seconds: int) -> str:
    """The picker's opening status. It says how long it stays open as an
    idle window, not a countdown: a view's timeout restarts on every press,
    so a ``<t:…:R>`` stamped at open would be wrong after the first page
    turn."""
    return (
        f"{PICKER_ASK}\n"
        f"-# This closes after {timeout_seconds} seconds without a pick."
    )


def picker_placeholder(
    base: str, *, filter_query: str, match_count: int, page: int, page_count: int
) -> str:
    """The select's placeholder: the question, the active filter, the page."""
    if filter_query:
        text = f'🔍 "{filter_query}" — {_plural(match_count, "match")}'
    else:
        text = base
    if page_count > 1:
        text += f" ({page + 1}/{page_count})"
    return text[:150]


def build_picker_layout(
    round_id: int,
    options: Sequence[tuple[str, str]],
    *,
    status: str,
    placeholder: str,
    page: int = 0,
    page_count: int = 1,
    filter_query: str = "",
) -> list[Item]:
    """The ephemeral "who is it?" card. **Paradigm 9 — game turn card.**

    ``options`` is this page's ``(display name, user id)`` pairs — at most
    :data:`PICKER_PAGE_SIZE`. ``status`` is the line under the heading: the
    question at first, then the answer to each pick ("Not it", the cooldown,
    the cap), so the card redraws in place rather than stacking replies.

    Arrows appear only when there is a second page and are disabled at the
    ends rather than hidden (guide § Anatomy). Clear appears only while a
    filter is on.
    """
    if options:
        select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=placeholder[:150],
            options=[
                discord.SelectOption(label=label[:100], value=value)
                for label, value in options[:PICKER_PAGE_SIZE]
            ],
            min_values=1,
            max_values=1,
            id=PICKER_SELECT_ID,
        )
    else:
        select = discord.ui.Select(
            placeholder="No members match that search.",
            options=[discord.SelectOption(label="No results", value="__none__")],
            disabled=True,
            id=PICKER_SELECT_ID,
        )
    buttons: list[discord.ui.Button[Any]] = []
    if page_count > 1:
        buttons.append(_button("◀", PICKER_PREV_ID, disabled=page <= 0))
        buttons.append(
            _button("▶", PICKER_NEXT_ID, disabled=page >= page_count - 1)
        )
    buttons.append(_button("🔍 Filter", PICKER_FILTER_ID))
    if filter_query:
        buttons.append(_button("✕ Clear", PICKER_CLEAR_ID))
    return [
        _text(f"## {ICON} Round #{round_id}\n{status}"),
        discord.ui.ActionRow(select),
        discord.ui.ActionRow(*buttons),
    ]


# ── The crop editor ──────────────────────────────────────────────────────────

EDITOR_HINT = (
    "Move or zoom the red box, or press Auto to snap to a detection, "
    "then ✓ Post."
)
EDITOR_HINT_NO_DETECTIONS = (
    "Nothing was detected — frame your crop by hand, then ✓ Post."
)

#: Braille blank — visible non-whitespace that renders empty, for the
#: disabled spacers that hold the D-pad's cross shape.
_SPACER = "⠀"


def build_crop_editor_layout(*, detections: bool) -> list[Item]:
    """The submitter's ephemeral crop editor.

    ::

        [  ·  ]  [  ↑  ]  [  ·  ]
        [  ←  ]  [  ·  ]  [  →  ]
        [  ·  ]  [  ↓  ]  [  ·  ]
        [ 🔍+ ]  [ Auto]  [ 🔍− ]
        [✓Post]  [Cancel]

    ``detections`` is whether the pipeline found anything: without any, Auto
    is disabled and the hint says to frame by hand. The preview is the
    submitter's own upload on a card only they see, so it is not spoilered.
    Post is green — the yes of the card (guide § Anatomy); Cancel is a grey,
    labelled button like every other Guess Cancel, since closing the editor
    destroys nothing that was ever saved.
    """

    def spacer() -> discord.ui.Button[Any]:
        return discord.ui.Button(
            label=_SPACER, style=discord.ButtonStyle.secondary, disabled=True
        )

    return [
        _text(
            f"## {ICON} Crop Editor\n"
            + (EDITOR_HINT if detections else EDITOR_HINT_NO_DETECTIONS)
        ),
        _gallery(
            attachment_ref(EDITOR_PREVIEW_FILENAME),
            "Your photo with the crop outlined in red.",
            spoiler=False,
        ),
        discord.ui.ActionRow(spacer(), _button("  ↑  ", EDITOR_UP_ID), spacer()),
        discord.ui.ActionRow(
            _button("  ←  ", EDITOR_LEFT_ID),
            spacer(),
            _button("  →  ", EDITOR_RIGHT_ID),
        ),
        discord.ui.ActionRow(spacer(), _button("  ↓  ", EDITOR_DOWN_ID), spacer()),
        discord.ui.ActionRow(
            _button(" 🔍+ ", EDITOR_ZOOM_IN_ID),
            _button(" Auto ", EDITOR_AUTO_ID, disabled=not detections),
            _button(" 🔍− ", EDITOR_ZOOM_OUT_ID),
        ),
        discord.ui.ActionRow(
            _button(" ✓ Post ", EDITOR_POST_ID, style=discord.ButtonStyle.success),
            _button("Cancel", EDITOR_CANCEL_ID),
        ),
    ]


# ── 5. The confession preview and the opt-in consent ─────────────────────────


def build_confession_preview_layout() -> list[Item]:
    """The confession card, shown to its author before it posts. **Paradigm 5
    — decision card.**"""
    return [
        _text(
            f"## {ICON} Your Confession\n"
            "Here's your confession card — post it?"
        ),
        _gallery(
            attachment_ref(CONFESSION_PREVIEW_FILENAME),
            "Your confession as it will be posted.",
            spoiler=False,
        ),
        discord.ui.ActionRow(
            _button("Post", CONFESSION_POST_ID, style=discord.ButtonStyle.success),
            _button("Cancel", CONFESSION_CANCEL_ID),
        ),
    ]


def consent_text(original_max_age_days: int) -> str:
    """What joining the pool stores and for how long — the disclosure the
    role grant waits on (2026-08 review, guess U1). Plain facts, no hedging."""
    return (
        "**Joining the Guess pool means:**\n"
        "• You can submit images and post anonymous confessions, and **you become "
        "a pickable answer** in other people's rounds.\n"
        "• Your original submission is cached on the bot's disk until the round "
        f"is solved (unsolved rounds are cleared after {original_max_age_days} "
        "days); the posted crop stays for the round's lifetime.\n"
        "• Confession text is stored with your user id — anonymous to members, "
        "visible to admins in the audit log, like every anonymous feature here.\n"
        "• Rounds and win/solve stats are kept for the leaderboard.\n"
        "• Leave anytime with `/guess optout` — your open rounds become "
        "unsolvable and past rounds keep their stats.\n\n"
        "Join the pool?"
    )


def build_consent_layout(original_max_age_days: int) -> list[Item]:
    """The ``/guess optin`` disclosure. **Paradigm 5 — decision card.**

    Nothing is granted until Join is pressed. Cancel is grey, not red, as on
    every Guess card: it destroys nothing, it just closes the card.
    """
    return [
        _text(f"## {ICON} Join the Guess Pool\n{consent_text(original_max_age_days)}"),
        discord.ui.ActionRow(
            _button("Join the Pool", CONSENT_JOIN_ID, style=discord.ButtonStyle.success),
            _button("Cancel", CONSENT_CANCEL_ID),
        ),
    ]


# ── 7. The leaderboard and the mod inspector ─────────────────────────────────


def build_leaderboard_layout(
    posters: list[tuple[int, int, int]],
    guessers: list[tuple[int, int]],
    *,
    name_fn: NameFn,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[Item]:
    """The public 🏆 leaderboard — two ranked sections. **Paradigm 7.**

    Each row is two lines — the name, then a ``-#`` line of its figures —
    the guide's shape for a list of things the reader already knows. On one
    line a 32-character name with "posted, solved (pct)" ran to ~60 columns
    and wrapped on a phone.

    Framed like the round card. With the game's name drawn into the strip
    the heading drops to "Leaderboard", so the name isn't said twice.
    """

    def _poster_line(i: int, row: tuple[int, int, int]) -> str:
        user_id, posted, solved = row
        pct = f"{solved / posted * 100:.0f}%" if posted else "—"
        return (
            f"{_MEDALS[i]} **{name_fn(user_id)}**\n"
            f"-# {posted:,} posted • {solved:,} solved • {pct}"
        )

    def _guesser_line(i: int, row: tuple[int, int]) -> str:
        user_id, solved = row
        return f"{_MEDALS[i]} **{name_fn(user_id)}**\n-# {solved:,} solved"

    poster_text = (
        "\n".join(_poster_line(i, r) for i, r in enumerate(posters[: len(_MEDALS)]))
        if posters else "No rounds posted yet — press **Submit** on the Guess Who panel."
    )
    guesser_text = (
        "\n".join(_guesser_line(i, r) for i, r in enumerate(guessers[: len(_MEDALS)]))
        if guessers else "No rounds solved yet."
    )
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    return [
        *top,
        _text("## 🏆 Leaderboard" if titled else f"## 🏆 {TITLE} Leaderboard"),
        _text(f"### Top Posters\n{poster_text}"),
        discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
        _text(f"### Top Guessers\n{guesser_text}"),
        *bottom,
    ]


def _named_with_id(user_id: int, name_fn: NameFn) -> str:
    """``Name (id)`` — the mod-facing rendering.

    Mods act on ids (look a member up, run another command against them), so
    the inspector keeps the number *alongside* the name rather than instead of
    it. Same call the whisper mod-log cards make.
    """
    return f"{name_fn(user_id)} (`{user_id}`)"


def build_inspector_layout(
    round_row: GuessRound,
    guess_count: int,
    unique_count: int,
    *,
    name_fn: NameFn,
) -> list[Item]:
    """The mod-only ephemeral round inspector. **Paradigm 7 — receipt.**

    The crop is spoilered here too (Ben, 2026-09-30): an embed image could
    not be, which is the only reason the embed showed it bare.
    """
    if round_row.deleted_at is not None:
        status = "🗑 Deleted"
    elif round_row.solved_at is not None:
        solver = (
            _named_with_id(round_row.solver_id, name_fn)
            if round_row.solver_id else "someone"
        )
        status = f"✅ Solved by {solver}"
    elif round_row.expired_at is not None:
        status = "⌛ Expired unsolved"
    else:
        status = "⏳ Open"

    items: list[Item] = [
        _text(
            f"## 🔍 Round #{round_row.id} — Inspector\n"
            f"**Status:** {status}\n"
            f"**Submitter:** {_named_with_id(round_row.submitter_id, name_fn)}\n"
            f"**Answer:** {_named_with_id(round_row.answer_id, name_fn)}\n"
            f"**Difficulty:** {round_row.difficulty}\n"
            f"**Guesses:** {guess_count} ({unique_count} unique guessers)\n"
            f"**Re-rolls:** {round_row.reroll_count}\n"
            f"**Created:** <t:{int(round_row.created_at)}:R>"
        )
    ]
    if round_row.crop_url:
        items.append(
            _gallery(
                round_row.crop_url,
                f"Round {round_row.id}'s posted crop.",
                spoiler=True,
            )
        )
    return items


# ── Reading a posted round back ──────────────────────────────────────────────


def gallery_urls(components: Any, _depth: int = 0) -> list[str]:
    """Every media URL a message's galleries show, outermost first.

    ``message.attachments`` reads empty on a V2 message — an upload a
    component references is hidden from that array (guide § Media) — so the
    round's stored ``crop_url`` has to come from the gallery itself. Walks
    duck-typed nodes the way ``bump_tracker.detector_logic.component_texts``
    does for text: ``items`` is unique to a media gallery, ``children`` and
    ``accessory`` are the only nesting attributes discord.py exposes.
    """
    found: list[str] = []
    if _depth > 4 or not isinstance(components, (list, tuple)):
        return found
    for node in components:
        items = getattr(node, "items", None)
        if isinstance(items, (list, tuple)):
            for item in items:
                url = getattr(getattr(item, "media", None), "url", None)
                if isinstance(url, str) and url:
                    found.append(url)
        found.extend(gallery_urls(getattr(node, "children", None), _depth + 1))
        accessory = getattr(node, "accessory", None)
        if accessory is not None:
            found.extend(gallery_urls([accessory], _depth + 1))
    return found
