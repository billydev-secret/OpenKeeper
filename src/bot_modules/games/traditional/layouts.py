"""Truth or Dare's Components V2 cards (``docs/components_v2_style_guide.md``).

Pure builders, no IO: state and a ``name_fn``/``names`` map in, a list of
layout items out, buttons carrying the same persistent ``tod_*`` custom ids
the embed-era view used and no callbacks — ``games/traditional/cog.py``
binds those.

Four cards:

- **The room board** (paradigm 8, live board). One card covers the whole
  room: before anyone has opted in it is the join screen (``lobby=True`` —
  join instructions, zero participants, no Questions Asked field, since on a
  fresh room that can only ever read 0 — 2026-09-03 embed filler review
  finding 8), and after the first preference toggle it is the working board
  (host, participant and question-count facts, then who opted into each
  category). Each category is its own row with a Join beside it, and Help
  sits beside the opening text. Nothing host-only is on it. A category with nobody in it reads
  "Nobody yet" rather than a bare ``—`` (finding 2), and the card never
  repeats its own title in a footer the way the embed's ``_footer_text``
  did on its default branch (finding 1). The join sentence — the
  single-choice wording when ``single_choice`` is set — rides on both
  states: people join mid-game, and it is the only place the card says a
  second press takes you out. Every button is grey.
- **The host panel** (paradigm 2, panel), ephemeral: Ask Question, Write
  My Own and End Game — everything only the host (or a mod) may press.
  The host gets it on launch; ``/games host`` brings it back.
- **The question card** (paradigm 7, receipt/notice), posted fresh to the
  channel on every Ask Question / Write My Own. No controls — the host
  answers through the modal, not this card. It names the target exactly
  once: the raw ping when the player is a cached member (so it fires their
  notification), a plain resolved name otherwise — never both, since a V2
  message has no ``content=`` to carry a ping separately from the card body
  (style guide § Mentions and names). Sends and edits pass
  :func:`question_mentions`, an allow-list of exactly that one id.
- **The recap** (paradigm 7, receipt/notice), sent once to the channel when
  the room closes. Totals, then a line per category that was actually
  asked — a category nobody touched is dropped rather than printed as a
  zero, same as the embed did.

The room board wears the **small lobby strip** with "Truth or Dare" drawn
between its leaves, and the hub footer (``core.layout.lobby_art``), like
every other game lobby (Ben, 2026-09-29: "switch truth or dare to the small
header" — it wore the tall hub banner for a day before that). A Truth or Dare
room runs in an age-gated channel as a rule, and the art goes
there unspoilered — it's the server's own decoration, not member-submitted
media (style guide § Media). The question card and the recap stay plain:
art frames the board, not every message the game posts.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.games.constants import GAME_ICONS
from bot_modules.games.utils import round_layouts
from bot_modules.games.utils.host_panel import HOST_HINT
from bot_modules.games.traditional.logic import (
    CAT_LABELS,
    CATEGORIES,
    question_pool_size,
    summarize_asked_by_category,
)

#: The game's own glyph, read from the one table ``/games`` and Help list
#: it by. It was 🎲 until the V2 pass, which Risky Rolls also leads with in
#: the same age-gated channels; ❓ is the game's own — every turn is a
#: question.
ICON = GAME_ICONS["traditional"]
TITLE = "Truth or Dare"

#: Per-category glyph — also what the question card leads with, so a card
#: reads at a glance the way the embed's per-category accent color did (a
#: ``LayoutView`` carries no color; the guide's replacement for it).
CAT_EMOJI: dict[str, str] = {
    "sfw_truth": "💭",
    "sfw_dare": "🔥",
    "nsfw_truth": "💋",
    "nsfw_dare": "😈",
}
_CAT_EMOJI_FALLBACK = ICON

JOIN_LABEL = "Join"

SFW_TRUTH_ID = "tod_sfw_truth"
SFW_DARE_ID = "tod_sfw_dare"
NSFW_TRUTH_ID = "tod_nsfw_truth"
NSFW_DARE_ID = "tod_nsfw_dare"
HELP_ID = "tod_htp"
#: The host panel's ids — new ones, not the board's old ``tod_ask`` /
#: ``tod_write_own`` / ``tod_end``: the panel is an ephemeral message with
#: its own short-lived view, never the room's persistent one.
ASK_ID = "tod_host_ask"
WRITE_OWN_ID = "tod_host_write_own"
END_ID = "tod_host_end"

#: Each category's toggle id, in the board's row order.
CAT_BUTTON_IDS: dict[str, str] = {
    "sfw_truth": SFW_TRUTH_ID,
    "sfw_dare": SFW_DARE_ID,
    "nsfw_truth": NSFW_TRUTH_ID,
    "nsfw_dare": NSFW_DARE_ID,
}


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line(spacing: discord.SeparatorSpacing = discord.SeparatorSpacing.small) -> discord.ui.Separator[Any]:
    return discord.ui.Separator(spacing=spacing)


def category_button(custom_id: str) -> discord.ui.Button[Any]:
    """One category's toggle, the accessory beside its row. Grey (style
    guide § Buttons), and just "Join": the row it sits beside already names
    the category and wears its glyph. The board is one shared message, so
    the label can't flip to "Leave" for the member who is in — the join
    sentence says a second tap drops you. The embed era's blurple SFW / red
    NSFW pairs went with the grid: red read as destructive and blurple
    ranked one pair over the other."""
    return discord.ui.Button(
        label=JOIN_LABEL, style=discord.ButtonStyle.secondary, custom_id=custom_id,
    )


def help_button() -> discord.ui.Button[Any]:
    """The board's one button that isn't a category: Help, for everyone."""
    return discord.ui.Button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id=HELP_ID)


def host_controls_row() -> discord.ui.ActionRow[Any]:
    """Ask Question / Write My Own / End Game — the host panel's one row."""
    return discord.ui.ActionRow(*_host_buttons())


def _host_buttons() -> list[discord.ui.Button[Any]]:
    return [
        discord.ui.Button(label="Ask Question", style=discord.ButtonStyle.secondary, custom_id=ASK_ID),
        discord.ui.Button(label="Write My Own", style=discord.ButtonStyle.secondary, custom_id=WRITE_OWN_ID),
        discord.ui.Button(label="End Game", style=discord.ButtonStyle.secondary, custom_id=END_ID),
    ]


#: A category's member list folds into "…and N more" past this many
#: characters — about five lines at the ~30 columns a row keeps beside its
#: Join. Truth or Dare carries no player cap (unlike Compliment/MFK) so a
#: full-channel room can genuinely reach it; 300 let one popular category
#: stand ten lines taller than its neighbours, and the count beside the
#: category's name now carries the size the list no longer has to.
CAT_LIST_CHARS = 150

#: An empty category, in subtext so it recedes and the names stand out once
#: they arrive — four full-weight "Nobody yet" lines were the join screen's
#: loudest text (design review, 2026-09-29).
NOBODY_YET = "-# Nobody yet"


def _capped_names(names: list[str], limit: int = CAT_LIST_CHARS) -> str:
    if not names:
        return NOBODY_YET
    joined = ", ".join(names)
    if len(joined) <= limit:
        return joined
    shown: list[str] = []
    used = 0
    for index, name in enumerate(names):
        piece = name if not shown else f", {name}"
        if used + len(piece) > limit:
            shown.append(f", …and {len(names) - index} more")
            break
        shown.append(piece)
        used += len(piece)
    return "".join(shown)


def _category_rows(
    payload: dict[str, Any], names: dict[str, str] | None,
) -> list[discord.ui.Item[Any]]:
    """One ``Section`` per category — its glyph, name and head count over
    who's in it, with its Join beside it (style guide § Controls: a row with
    its own action).

    One line only, a large one between the SFW pair and the NSFW pair: that
    is the card's real split (the NSFW pair works only in an age-restricted
    channel), and a line between every row drew SFW Dare as far from SFW
    Truth as from the 18+ pair (design review, 2026-09-29)."""
    prefs: dict[str, list[str]] = payload.get("prefs", {})
    rows: list[discord.ui.Item[Any]] = []
    previous_nsfw = False
    for cat in CATEGORIES:
        members_in_cat = [
            # An uncached member reads ``User <id>`` — the name resolver's
            # own fallback — never a bare number (style guide § Mentions
            # and names); the fallback carries no markdown to escape.
            (
                discord.utils.escape_markdown(str(names[uid]))
                if names and uid in names
                else f"User {uid}"
            )
            for uid, cats in prefs.items()
            if cat in cats
        ]
        emoji = CAT_EMOJI.get(cat, _CAT_EMOJI_FALLBACK)
        if rows and cat.startswith("nsfw_") and not previous_nsfw:
            rows.append(_line(spacing=discord.SeparatorSpacing.large))
        previous_nsfw = cat.startswith("nsfw_")
        count = f" · {len(members_in_cat)}" if members_in_cat else ""
        rows.append(discord.ui.Section(
            _text(f"{emoji} **{CAT_LABELS[cat]}**{count}\n{_capped_names(members_in_cat)}"),
            accessory=category_button(CAT_BUTTON_IDS[cat]),
        ))
    return rows


def _facts_lines(host_name: str, payload: dict[str, Any], *, lobby: bool) -> list[str]:
    """The facts beside Help, one short line each: a row with a button
    beside it keeps about 30 columns on a phone, and the old one-line
    ``Host · playing · asked`` string ran to 40 and wrapped ragged
    (design review, 2026-09-29)."""
    participants: list = payload.get("participants", [])
    facts = [
        f"Hosted by **{discord.utils.escape_markdown(host_name)}**",
        f"**{len(participants)}** playing",
    ]
    if not lobby:
        # Dropped on the join screen entirely (2026-09-03 embed filler
        # review, finding 8): a fresh room's count can only ever be 0, and
        # this field earns its place once there is something to count.
        asked: dict[str, str] = payload.get("asked", {})
        prefs: dict[str, list[str]] = payload.get("prefs", {})
        total_pool = question_pool_size(prefs, asked)
        done = len(asked)
        # "1 of 4", not "1/4": the 4 is player-and-category pairs, which a
        # bare fraction never explained.
        asked_value = f"{done} of {total_pool}" if total_pool else str(done)
        facts.append(f"**{asked_value}** questions asked")
    return facts


JOIN_HINT = "Tap Join on what you're up for. Tap again to leave."
JOIN_HINT_SINGLE = "Tap Join on the one category you're up for."


def build_room_layout(
    host_name: str,
    payload: dict[str, Any],
    *,
    lobby: bool = False,
    names: dict[str, str] | None = None,
    single_choice: bool = False,
    header_url: str | None = None,
    footer_url: str | None = None,
    title_in_header: bool = False,
) -> list[discord.ui.Item[Any]]:
    """The room board, joining or in play. **Paradigm 8 — live board.**

    ``lobby`` distinguishes the join screen (before any preference has been
    toggled, ever — ``games_traditional_cog`` tracks this the same way the
    embed-era ``_lobby_card`` flag did) from the working board; both carry
    the same buttons — Help and a Join per category; the host's controls
    are on :func:`build_host_panel_layout`. ``header_url``/``footer_url``/
    ``title_in_header`` are the guild's lobby art
    (``games/utils/lobby_art.read_lobby_art``), framing it the way every
    lobby's is: no header draws nothing, and a header with the title drawn
    in replaces the ``##`` line.
    """
    titled = bool(header_url and title_in_header)
    top, bottom = lobby_art(header_url, footer_url, title=TITLE if titled else None)
    lines = [] if titled else [f"## {ICON} {TITLE}"]
    lines.extend(_facts_lines(host_name, payload, lobby=lobby))
    items: list[discord.ui.Item[Any]] = [
        *top,
        # Help sits beside the card's opening text, where a hub's sits
        # beside its title (style guide § Controls). Everything host-only
        # lives on the host panel (:func:`build_host_panel_layout`; Ben,
        # 2026-09-29), so the board carries only what any player can press.
        discord.ui.Section(_text("\n".join(lines)), accessory=help_button()),
        # The hint is an instruction, not a fact, so it leaves Help's row
        # for the full width, in subtext. On both states, not just the join
        # screen: people join mid-game, and this is the only place the card
        # says a second press takes you out.
        _text(
            f"-# {JOIN_HINT_SINGLE if single_choice else JOIN_HINT}\n"
            f"-# {HOST_HINT}"
        ),
        _line(spacing=discord.SeparatorSpacing.large),
        *_category_rows(payload, names),
    ]
    return [*items, *bottom]


def build_host_panel_layout() -> list[discord.ui.Item[Any]]:
    """The host's controls, on an ephemeral card. **Paradigm 2 — panel.**

    Ben, 2026-09-29: "move everything that's host only to an ephemeral
    message brought up by slash command". The host gets it on launch and
    from :data:`HOST_COMMAND` any time after; a mod gets it from the same
    command. It carries no game state — the board shows the count, and a
    copy here would go stale the moment a question went out.
    """
    return round_layouts.build_host_panel_layout(
        ICON, rows=[_host_buttons()],
    )


def build_question_layout(
    category: str,
    question: str,
    *,
    target_id: int | None = None,
    target_name: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The question card posted when a host asks. **Paradigm 7 — notice.**

    Names the target exactly once (style guide § Mentions and names): the
    raw ``<@id>`` ping when ``target_id`` is a cached member (send/edit with
    :func:`question_mentions` so it actually notifies), a plain resolved
    ``target_name`` otherwise. The embed era carried both — a ping in
    ``content=`` and a resolved name in the embed's author line — which a V2
    message, with no ``content=`` of its own, can no longer do; picking one
    is the fix, not a loss.

    Unknown category keys (stale payloads) fall back to a neutral glyph and
    a title-cased label rather than crashing. The question's own markdown is
    left intact and blockquoted so a multi-line question stays inside the
    quote instead of spilling out after the first line.
    """
    emoji = CAT_EMOJI.get(category, _CAT_EMOJI_FALLBACK)
    label = CAT_LABELS.get(category) or category.replace("_", " ").title()
    who = f"<@{target_id}>" if target_id else f"**{discord.utils.escape_markdown(target_name or 'them')}**"
    quoted = "> " + question.replace("\n", "\n> ")
    return [_text(f"## {emoji} {label}\nFor {who}\n{quoted}")]


def question_mentions(target_id: int | None) -> discord.AllowedMentions:
    """The question card's allow-list: exactly the target while the card
    pings them, else nobody."""
    if target_id is None:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False, roles=False, replied_user=False,
        users=[discord.Object(id=target_id)],
    )


def build_recap_layout(
    payload: dict[str, Any],
    *,
    note: str | None = None,
    payout_line: str | None = None,
) -> list[discord.ui.Item[Any]]:
    """The game-over recap. **Paradigm 7 — notice.**

    Totals, then one line per category that was actually asked —
    a category nobody touched is dropped rather than printed as a zero, to
    keep the card compact the way the embed's skip-if-zero loop did.
    ``note`` says why the room ended when it wasn't the host's own End Game
    (the idle close uses it). ``payout_line``
    (``economy.game_rewards.payout_footer_line``) is the ``-#`` line the
    embed's ``append_payout_footer`` used to stamp on afterward.
    """
    participants: list = payload.get("participants", [])
    asked: dict[str, str] = payload.get("asked", {})
    total_q = len(asked)
    by_cat = summarize_asked_by_category(asked)

    heading = f"## {ICON} {TITLE} — Game Over"
    if note:
        heading = f"{heading}\n{note}"
    facts = [f"**{total_q}** asked", f"**{len(participants)}** played"]
    # Counts questions that went out as the bank wrote them — the host sent
    # the pre-filled box unedited.
    bank_asked = payload.get("bank_asked", 0)
    if bank_asked:
        facts.append(f"**{bank_asked}** from the bank")
    # One inline line mirroring the board's 2x2 grid — the glyphs are the
    # ones players watched on the buttons all game, so they need no label.
    by_glyph = " · ".join(
        f"{CAT_EMOJI.get(cat, _CAT_EMOJI_FALLBACK)} **{count}**"
        for cat, count in by_cat.items()
        if count
    )
    body = " · ".join(facts) + (f"\n{by_glyph}" if by_glyph else "")
    items: list[discord.ui.Item[Any]] = [_text(heading), _line(), _text(body)]
    if payout_line:
        items.append(_text(f"-# {payout_line}"))
    return items
