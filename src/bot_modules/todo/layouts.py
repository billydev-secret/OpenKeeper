"""The todo list's and the bug list's Components V2 cards
(``docs/components_v2_style_guide.md``).

Pure builders, no IO: the text ``board_logic`` and ``change_logic`` already
decide goes in, a list of layout items comes out, controls carrying custom ids
and no callbacks — ``todo/cog.py`` and ``todo/cog_bug.py`` bind those. What a
row says, who it names and what it must never name are still those two
modules' decisions; nothing here reads a row for itself except the Complete
picker's option labels.

The cards:

- **The todo board** and **the bug board** (paradigm 8, live board), the
  sticky panels. A ``##`` title, one Text Display per section the board has
  anything in, a ``-#`` counts line, and every button in one row at the foot.
  The rows are text, not a button apiece: fifteen ``Section`` rows would cost
  45 of the 40 components a message may carry, and the one Complete button
  over a multi-pick is how a mod ticks several off at once anyway.
  **Both boards are bare — no banner, no lobby strip, no footer art** (Ben,
  2026-09-30). The guide's general rule gives a newly converted channel hub
  the small lobby strip; these two are staff worklists, the todo board is
  already the tallest card the bot posts, and the strip would cost a mod a
  screen of scroll on the card they read most.
  ``tests/todo/test_todo_layouts.py`` fails if art is added.
- **The Complete picker** (paradigm 2, ephemeral panel): a heading, one
  sentence and a multi-pick dropdown, redrawn in place into its outcome.
- **The change cards** (paradigm 7, notice): the arrival card, the noon chore
  digest and the new-bug card. No controls. A bug card's screenshots ride in
  a ``MediaGallery`` under its lines — a V2 message shows no attachment a
  component does not reference.

Every button is grey: nothing here is a decision or a destructive press.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import discord

from bot_modules.todo.board_logic import EMPTY_BOARD, complete_option_label

TODO_TITLE = "📋 Server Todo"
BUG_TITLE = "🐛 Bug List"

#: The boards' persistent ids — the embed-era views' own, unchanged, so the
#: boot repaint converts a posted board in place and no press is orphaned.
TODO_ADD_ID = "todo_board_add"
TODO_COMPLETE_ID = "todo_board_complete"
TODO_SIGNOFFS_ID = "todo_board_signoffs"
TODO_APPROVALS_ID = "todo_board_approvals"
TODO_CONFESSIONS_ID = "todo_board_confessions"
BUG_ADD_ID = "bug_board_add"
BUG_COMPLETE_ID = "bug_board_complete"

#: The pickers' dropdowns. Transient: each lives on one ephemeral message.
TODO_PICK_ID = "todo_complete_pick"
BUG_PICK_ID = "bug_complete_pick"

#: Discord's cap on a select's options.
MAX_PICK_OPTIONS = 25

#: What the todo board's text may run to — Discord's 4000 across every Text
#: Display, less a margin so the ceiling is never met exactly.
BOARD_TEXT_BUDGET = 3900

#: Closes a section that lost rows to ``BOARD_TEXT_BUDGET``.
TRIMMED_LINE = "…cut short to fit the board."


def _text(content: str) -> discord.ui.TextDisplay[Any]:
    return discord.ui.TextDisplay(content)


def _line() -> discord.ui.Separator[Any]:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.large)


def _button(label: str, emoji: str, custom_id: str) -> discord.ui.Button[Any]:
    return discord.ui.Button(
        label=label,
        emoji=emoji,
        style=discord.ButtonStyle.secondary,
        custom_id=custom_id,
    )


# ── the boards ──────────────────────────────────────────────────────────────


def _fit_sections(
    sections: Sequence[tuple[str, str]], room: int
) -> list[tuple[str, str]]:
    """Trim rows off the longest section until the bodies fit in ``room``.

    The rows are clipped raw and then escaped (``board_logic.render_rows``
    says why), so a board whose every row is solid markdown doubles on the
    wire: all five sections at their caps measure about 4,140 characters,
    over the 4,000 a card may carry, and Discord answers the whole repaint
    with a 400. Nothing real looks like that — the same full board in plain
    words is about 3,300 and passes through untouched — but a board that
    cannot paint is the one failure this surface may not have, so the
    pathological one loses rows instead, longest section first, and says so.
    """
    bodies = [body.split("\n") for _, body in sections]

    def size(lines: list[str]) -> int:
        return len("\n".join(lines))

    while sum(size(lines) for lines in bodies) > room:
        longest = max(bodies, key=size)
        if longest[-1] == TRIMMED_LINE:
            longest.pop()
        if len(longest) <= 1:
            break
        longest.pop()
        # An overflow line sits under a blank one; don't leave the blank.
        while len(longest) > 1 and not longest[-1]:
            longest.pop()
        longest.append(TRIMMED_LINE)
    fitted = [(heading, "\n".join(lines)) for (heading, _), lines in zip(sections, bodies)]
    # Row-trimming stops at one line per section, so a section whose single
    # row is itself enormous could still overflow and 400 the whole board
    # (ship review, 2026-10-01). Clip the longest bodies by characters as
    # the last resort, so the board always paints.
    while sum(len(body) for _, body in fitted) > room:
        i = max(range(len(fitted)), key=lambda k: len(fitted[k][1]))
        heading, body = fitted[i]
        over = sum(len(b) for _, b in fitted) - room
        keep = max(0, len(body) - over - len(TRIMMED_LINE) - 1)
        if keep == 0 and body == TRIMMED_LINE:
            break
        fitted[i] = (heading, (body[:keep].rstrip() + "\n" + TRIMMED_LINE).lstrip())
    return fitted


def build_todo_board_layout(
    sections: Sequence[tuple[str, str]], footer: str
) -> list[discord.ui.Item[Any]]:
    """The todo board: ``board_logic.board_sections`` under the title, then
    ``render_board_footer`` as subtext and the five buttons.

    A board with nothing on it says so once, in the title's own block.
    """
    items: list[discord.ui.Item[Any]] = []
    if sections:
        items.append(_text(f"## {TODO_TITLE}"))
        overhead = (
            len(f"## {TODO_TITLE}") + len(f"-# {footer}")
            + sum(len(f"### {heading}\n") for heading, _ in sections)
        )
        for heading, body in _fit_sections(sections, BOARD_TEXT_BUDGET - overhead):
            items.append(_line())
            items.append(_text(f"### {heading}\n{body}"))
    else:
        items.append(_text(f"## {TODO_TITLE}\n{EMPTY_BOARD}"))
    items.append(_text(f"-# {footer}"))
    # Five is a full action row: a sixth button would need a second row.
    items.append(
        discord.ui.ActionRow(
            _button("Add Task", "➕", TODO_ADD_ID),
            _button("Complete", "✅", TODO_COMPLETE_ID),
            _button("Sign-Offs", "✍️", TODO_SIGNOFFS_ID),
            _button("Approvals", "🧾", TODO_APPROVALS_ID),
            _button("Confessions", "🕵️", TODO_CONFESSIONS_ID),
        )
    )
    return items


def build_bug_board_layout(body: str, footer: str) -> list[discord.ui.Item[Any]]:
    """The bug board: ``render_rows`` under the title, the count as subtext,
    File a Bug and Complete. Never a filer's name — ``render_rows`` prints
    none for a bug, and nothing here is handed one."""
    return [
        _text(f"## {BUG_TITLE}"),
        _text(body),
        _text(f"-# {footer}"),
        discord.ui.ActionRow(
            _button("File a Bug", "🐛", BUG_ADD_ID),
            _button("Complete", "✅", BUG_COMPLETE_ID),
        ),
    ]


# ── the Complete picker ─────────────────────────────────────────────────────


def build_complete_picker_layout(
    rows: Sequence[Mapping[str, Any]], *, bugs: bool = False
) -> list[discord.ui.Item[Any]]:
    """The ephemeral card behind a board's Complete button.

    ``rows`` are what may be ticked, in the board's own order; the first 25
    are offered, which is Discord's cap on a dropdown.
    """
    options = []
    for row in rows[:MAX_PICK_OPTIONS]:
        label, desc = complete_option_label(row)
        options.append(
            discord.SelectOption(
                label=label, value=str(row["id"]), description=desc or None
            )
        )
    if bugs:
        heading = "## ✅ Complete Bugs\nPick every bug that's fixed."
        placeholder = "Pick the bugs that are fixed…"
    else:
        heading = "## ✅ Complete Tasks\nPick everything you finished."
        placeholder = "Pick the task you finished…"
    select: discord.ui.Select[Any] = discord.ui.Select(
        custom_id=BUG_PICK_ID if bugs else TODO_PICK_ID,
        placeholder=placeholder,
        min_values=1,
        max_values=len(options),
        options=options,
    )
    return [_text(heading), discord.ui.ActionRow(select)]


def build_completed_layout(done: int, *, bugs: bool = False) -> list[discord.ui.Item[Any]]:
    """What the picker redraws into: how many this pick closed, or that
    somebody else got there first."""
    if bugs:
        if not done:
            return [_text(
                "## ❌ Already Closed\nThose were already closed by someone"
                " else, so nothing changed."
            )]
        noun = "bug" if done == 1 else "bugs"
        return [_text(f"## ✅ Bugs Fixed\nMarked **{done}** {noun} fixed.")]
    if not done:
        return [_text(
            "## ❌ Already Completed\nThose were already completed by someone"
            " else, so nothing changed."
        )]
    noun = "task" if done == 1 else "tasks"
    return [_text(f"## ✅ Tasks Completed\nMarked **{done}** {noun} complete.")]


# ── change cards ────────────────────────────────────────────────────────────


def screenshot_alt(bug_id: int) -> str:
    """A screenshot's alt text. Names the bug and never who filed it."""
    return f"Screenshot for bug #{int(bug_id)}"


def build_change_card_layout(
    title: str,
    body: str,
    *,
    images: Sequence[tuple[str, int]] = (),
    spoiler: bool = False,
) -> list[discord.ui.Item[Any]]:
    """One change card: ``CardPlan.title`` as the heading over ``CardPlan.body``.

    ``images`` are ``(filename, bug_id)`` for the screenshots actually being
    uploaded with this send, each referenced as ``attachment://`` — the
    filename is how closing the bug finds its pictures again
    (``change_logic.is_bug_image``). ``spoiler`` is set when the card lands in
    a Discord age-gated channel: a screenshot is member-submitted media.
    """
    items: list[discord.ui.Item[Any]] = [_text(f"## {title}\n{body}")]
    if images:
        items.append(
            discord.ui.MediaGallery(
                *(
                    discord.MediaGalleryItem(
                        f"attachment://{filename}",
                        description=screenshot_alt(bug_id),
                        spoiler=spoiler,
                    )
                    for filename, bug_id in images
                )
            )
        )
    return items
