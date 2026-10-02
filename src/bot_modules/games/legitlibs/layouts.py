"""LegitLibs' Components V2 cards (``docs/components_v2_style_guide.md``).

The V2 replacement for the embed builders that lived in ``rendering.py``
(which keeps the three text renderers the modes fill a template with).
Pure builders: primitives in, layout items out, buttons carrying the
``ml_*`` custom ids the embed-era views used and no callbacks —
``views.LibsCard`` binds those from the mode closures.

The cards, in the order a round shows them:

- **The lobby** (paradigm 8, live board): the template's title, heat and
  mode, how many have joined against the floor, grey **Join** / **Leave**,
  Help beside the title. It wears the small lobby strip. **Start and Cancel
  are not on it** — everything host-only lives on the ephemeral host panel
  (Ben, 2026-09-30), and one ``-#`` line points the host at ``/games host``.
- **The fill cards** (paradigm 9, game turn card) — Quiplash's fill,
  Classic's round-one fill, the rescue claim and the rescue fill — share
  one shape: the title, the phase's own hint, a facts line (heat, a
  counter, the countdown), the redacted story or the volunteer roster, and
  the one button the phase takes (**📝 Submit Fills** or **🙋 Volunteer**).
  Cancel is on the host panel. The closed frame carries no controls.
- **The host panel** (paradigm 2, ephemeral): **Start** and **Cancel
  Round**, one panel for the whole round; a press out of its phase answers.
- **The reveals** (paradigm 7, notice): Quiplash's one card per anonymous
  submission, its **Who Wrote What** cast, the no-submissions notice, and
  Classic's single attributed story. A filled story is fitted to the card
  rather than Discord's 4,096 — Quiplash never truncated at all, and 25
  thousand-character fills overflow either ceiling.
- **Cancelled** (paradigm 7): the one notice the four cancel paths share.
- **🔁 Another One** rides on a round's last card, appended by the cog once
  the round has ended (``another=True`` on the reveal builders).
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.branding import escape_to_fit, trim_escaped
from bot_modules.core.layout import MAX_CHARS, lobby_art
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.shared.embeds import take_to_fit

from .data import HEAT_ICONS, HEAT_LABELS

ICON = GAME_ICONS["legitlibs"]
TITLE = GAME_NAMES["legitlibs"]

JOIN_ID = "ml_join"
LEAVE_ID = "ml_leave"
LOBBY_HELP_ID = "ml_htp_lobby"
SUBMIT_ID = "ml_submit_fills"
FILL_HELP_ID = "ml_htp_fill"
CLASSIC_SUBMIT_ID = "ml_cl_submit"
CLASSIC_HELP_ID = "ml_cl_htp_fill"
VOLUNTEER_ID = "ml_cl_volunteer"
RESCUE_SUBMIT_ID = "ml_cl_rescue_submit"
ANOTHER_ID = "ml_another"
#: The host panel's ids — new ones, not the old ``ml_start`` /
#: ``ml_cancel_*``: the panel is an ephemeral message with its own
#: short-lived view.
HOST_START_ID = "ml_host_start"
HOST_CANCEL_ID = "ml_host_cancel"

SUBMIT_LABEL = "📝 Submit Fills"
VOLUNTEER_LABEL = "🙋 Volunteer"
ANOTHER_LABEL = "🔁 Another One"

MODE_LABELS = {"quiplash": "Quiplash", "classic": "Classic", "hotseat": "Hot Seat"}

#: The most a template's title may spend of a card, escaped — an admin
#: writes it on the dashboard, with no length cap of its own.
TITLE_LIMIT = 200
#: What the volunteer roster and the contributor line may spend. Classic's
#: default ``player_max`` is 99, so either can outgrow a line.
NAMES_BUDGET = 1200
#: Room a reveal keeps clear of the ceiling for its heading and subtext.
_REVEAL_HEADROOM = 300


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _title(template_title: str) -> str:
    return escape_to_fit(template_title, TITLE_LIMIT)


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, mode.title())


def fit_names(names: list[str], budget: int = NAMES_BUDGET) -> str:
    """Comma-join already-escaped names, dropping whole trailing names past
    ``budget`` and saying how many didn't fit — never a character slice,
    which could split an escape pair (the rescue roster's old bug)."""
    shown, remaining = take_to_fit(names, budget - 24)
    if not remaining:
        return ", ".join(shown) if shown else "—"
    tail = f"_…and {remaining} more_"
    return f"{', '.join(shown)}\n{tail}" if shown else tail


def another_row() -> discord.ui.ActionRow[Any]:
    """🔁 Another One — host or mod, under a finished round's last card."""
    return discord.ui.ActionRow(round_layouts.grey(ANOTHER_LABEL, ANOTHER_ID))


# ── the lobby ────────────────────────────────────────────────────────────────


def build_lobby_layout(
    host_name: str,
    template_title: str,
    tier: int,
    mode: str,
    player_count: int,
    player_min: int,
    *,
    closed: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The lobby shown while players join. **Paradigm 8 — live board.**

    ``closed`` is the frame left once the round has started: no Join, no
    Leave. ``header_url``/``footer_url``/``title_in_header`` are the guild's
    lobby art (``games/utils/lobby_art.read_lobby_art``): no header draws
    nothing, and a header with the title drawn in replaces the ``##`` line.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE} — {mode_label(mode)}"]
    lines.append(f"Hosted by **{discord.utils.escape_markdown(host_name)}**")
    joined = f"**{player_count}** joined" + (
        f" (need {player_min})" if player_count < player_min else " ✓"
    )
    items: list[discord.ui.Item[Any]] = [
        *top,
        # A closed lobby's view is stopped, so a Help on it could only fail;
        # the closed frame carries no controls (ship review, 2026-10-01).
        _text("\n".join(lines)) if closed
        else discord.ui.Section(_text("\n".join(lines)), accessory=round_layouts.grey("❓ Help", LOBBY_HELP_ID)),
        _text(
            f'**"{_title(template_title)}"**\n'
            f"Heat: **{HEAT_LABELS[tier]}** · Mode: **{mode_label(mode)}** · {joined}\n"
            f"-# {HOST_HINT}"
        ),
    ]
    if not closed:
        items.append(discord.ui.ActionRow(round_layouts.grey("Join", JOIN_ID), round_layouts.grey("Leave", LEAVE_ID)))
    return [*items, *bottom]


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's Start and Cancel Round. **Paradigm 2 — panel.**

    One card for the whole round: Start while the lobby is open, Cancel at
    any point before the reveal. A press out of its phase answers rather
    than acting.
    """
    return round_layouts.build_host_panel_layout(
        ICON,
        blurb="Start once everyone is in, or cancel the round.",
        rows=[[round_layouts.grey("Start", HOST_START_ID), round_layouts.red("Cancel Round", HOST_CANCEL_ID)]],
    )


# ── the fill cards ───────────────────────────────────────────────────────────


def build_fill_layout(
    phase: str,
    template_title: str,
    tier: int,
    *,
    hint: str,
    counter: tuple[str, int, int] | None = None,
    deadline_ts: int | None = None,
    body: str | None = None,
    button: tuple[str, str] | None = None,
    help_id: str | None = None,
    closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    """One phase's card. **Paradigm 9 — game turn card.**

    ``phase`` is the heading's suffix ("Fill Phase", "Classic — Rescue
    Round"); ``hint`` the one-line instruction under the title;
    ``counter`` a ``(label, done, total)`` triple for the facts line;
    ``body`` the redacted story or the volunteer roster; ``button`` the
    phase's one ``(label, custom_id)``; ``help_id`` puts Help beside the
    title. ``closed`` is the frame left when the phase ends: no controls.
    """
    facts = [f"Heat: **{HEAT_LABELS[tier]}**"]
    if counter:
        label, done, total = counter
        facts.append(f"{label}: **{done} / {total}**")
    if deadline_ts and not closed:
        facts.append(f"⏳ <t:{deadline_ts}:R>")
    opening = _text(f"## {ICON} {TITLE} — {phase}\n{hint}")
    items: list[discord.ui.Item[Any]] = [
        discord.ui.Section(opening, accessory=round_layouts.grey("❓ Help", help_id))
        if help_id and not closed else opening,
        _text(f'**"{_title(template_title)}"**\n' + " · ".join(facts)),
    ]
    if body:
        items.extend([round_layouts.line(), _text(body)])
    if closed:
        return items
    if button:
        items.append(discord.ui.ActionRow(round_layouts.grey(*button)))
    items.append(_text(f"-# {HOST_HINT}"))
    return items


def build_quiplash_fill_layout(
    template_title: str, tier: int, player_count: int, submitted: int,
    deadline_ts: int | None, redacted_body: str | None, *, closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    return build_fill_layout(
        "Fill Phase", template_title, tier,
        hint=f"Press **{SUBMIT_LABEL}** to fill in the blanks.",
        counter=("Submitted", submitted, player_count),
        deadline_ts=deadline_ts, body=redacted_body,
        button=(SUBMIT_LABEL, SUBMIT_ID), help_id=FILL_HELP_ID, closed=closed,
    )


def build_classic_fill_layout(
    template_title: str, tier: int, player_count: int, done: int, deadline_ts: int,
    *, closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    return build_fill_layout(
        "Classic — Fill Phase", template_title, tier,
        hint=f"Each player has been assigned some blanks. Press **{SUBMIT_LABEL}** to fill in yours.",
        counter=("Players done", done, player_count), deadline_ts=deadline_ts,
        button=(SUBMIT_LABEL, CLASSIC_SUBMIT_ID), help_id=CLASSIC_HELP_ID, closed=closed,
    )


def build_rescue_claim_layout(
    template_title: str, tier: int, unfilled: int, volunteer_names: list[str],
    deadline_ts: int, *, closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    """``volunteer_names`` arrive pre-escaped; the roster is fitted by whole
    names (the embed's raw slice could split an escape pair and lose the
    whole card)."""
    s = "" if unfilled == 1 else "s"
    return build_fill_layout(
        "Classic — Rescue Round", template_title, tier,
        hint=(
            f"{unfilled} blank{s} still unfilled. Press **{VOLUNTEER_LABEL}** to help "
            "rescue them — unfilled blanks will be split across all volunteers."
        ),
        counter=("Unfilled", unfilled, unfilled), deadline_ts=deadline_ts,
        body=f"### Volunteers ({len(volunteer_names)})\n{fit_names(volunteer_names)}",
        button=(VOLUNTEER_LABEL, VOLUNTEER_ID), closed=closed,
    )


def build_rescue_fill_layout(
    template_title: str, tier: int, rescuers_done: int, rescuers_total: int,
    deadline_ts: int, *, closed: bool = False,
) -> list[discord.ui.Item[Any]]:
    return build_fill_layout(
        "Classic — Rescue Fill", template_title, tier,
        hint=f"Volunteers: press **{SUBMIT_LABEL}** to fill in the rescued blanks.",
        counter=("Rescuers done", rescuers_done, rescuers_total), deadline_ts=deadline_ts,
        button=(SUBMIT_LABEL, RESCUE_SUBMIT_ID), closed=closed,
    )


# ── the reveals ──────────────────────────────────────────────────────────────


def _fit_story(filled_body: str, reserved: int) -> str:
    """A filled story that would overflow the card is cut at the ceiling
    with an ellipsis — after escaping, since ``render_filled_body`` has
    already escaped every fill — never between a backslash and the
    character it protects."""
    budget = MAX_CHARS - _REVEAL_HEADROOM - reserved
    return filled_body if len(filled_body) <= budget else trim_escaped(filled_body, budget)


def build_reveal_layout(
    template_title: str,
    tier: int,
    filled_body: str,
    submission_num: int,
    total_submissions: int,
    *,
    by: str | None = None,
    another: bool = False,
) -> list[discord.ui.Item[Any]]:
    """One Quiplash submission. **Paradigm 7 — notice.** Anonymous unless
    ``by`` names its author (the single-submission round, which attributes
    at once since there is nobody to guess between)."""
    subtext = f"-# {HEAT_ICONS[tier]} {_title(template_title)}"
    if by:
        subtext += f" • by {discord.utils.escape_markdown(by)}"
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} Submission {submission_num} of {total_submissions}\n"
            f"{_fit_story(filled_body, len(subtext))}"
        ),
        _text(subtext),
    ]
    if another:
        items.append(another_row())
    return items


def build_cast_layout(
    cast_lines: list[str], *, another: bool = False,
) -> list[discord.ui.Item[Any]]:
    """Who Wrote What: ``cast_lines`` are ``**#n** — name``, pre-escaped."""
    shown, remaining = take_to_fit(cast_lines, MAX_CHARS - _REVEAL_HEADROOM, sep="\n")
    if remaining:
        shown.append(f"_…and {remaining} more_")
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {ICON} Who Wrote What\n" + "\n".join(shown)),
    ]
    if another:
        items.append(another_row())
    return items


def build_no_submissions_layout(
    template_title: str, *, another: bool = False,
) -> list[discord.ui.Item[Any]]:
    items: list[discord.ui.Item[Any]] = [
        _text(
            f"## {ICON} No Submissions\n"
            "Nobody filled in the blanks in time.\n"
            f"-# {_title(template_title)}"
        ),
    ]
    if another:
        items.append(another_row())
    return items


def build_classic_reveal_layout(
    template_title: str,
    tier: int,
    filled_body: str,
    contributor_names: list[str],
    *,
    another: bool = False,
) -> list[discord.ui.Item[Any]]:
    """Classic's finished story, each fill attributed in a spoiler by
    ``render_filled_body_attributed``. ``contributor_names`` are escaped
    here and fitted by whole names."""
    contribs = fit_names([discord.utils.escape_markdown(n) for n in contributor_names])
    subtext = f"-# Classic • {HEAT_ICONS[tier]} • Contributors: {contribs}"
    items: list[discord.ui.Item[Any]] = [
        _text(f"## {ICON} {_title(template_title)}\n{_fit_story(filled_body, len(subtext))}"),
        _text(subtext),
    ]
    if another:
        items.append(another_row())
    return items


def build_cancelled_layout() -> list[discord.ui.Item[Any]]:
    """What a lobby or fill card becomes when the host cancels the round.
    **Paradigm 7 — notice.**"""
    return [_text(f"## {ICON} {TITLE} — Cancelled\nThe host cancelled this round.")]
