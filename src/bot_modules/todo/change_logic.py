"""Change cards for the sticky todo board — pure planning, no Discord, no DB.

The board is *edited* in place when it changes, and an edit does not mark a
channel unread. So a mod only learns the board moved by going and looking at
it, which is the complaint this answers (todo #210): post a small card above
the board when something new lands, so the channel shows as modified, and so
the channel itself becomes the history.

Everything here is a decision about **what to say and when**, kept apart from
the cog so it can be tested without a gateway. Three rules shape it:

* **Arrivals only.** A card reports what has *arrived and is still waiting*. It
  is built from the same ``pending_*`` queries the board section uses, so an
  item a mod already handled inside the polling window is never announced —
  "Ada needs a sign-off" posted after Ada was signed off is worse than silence.
  Completions are never announced at all; that was Billy's call, and the board
  showing the row gone is the whole notification.
* **One card per window, not one per item.** The poll runs on the board loop's
  existing 60-second tick, so six sign-offs landing together cost one card and
  one board repost rather than six of each.
* **Nothing is ever edited or corrected.** A card is a log line. When its item
  is later resolved the card stays as it was — the board *beneath* it is the
  live view, and the cards above it are what happened. The board is re-stuck
  under every card it posts, so the bottom of the channel is always the
  current state and everything above it is history. The one exception is a
  bug's **screenshots** (bug list row 244): they ride its card, and closing the
  bug edits them back off it, because the card is the only copy the bot keeps
  and a closed bug's images serve nobody. The card's text is never touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

import discord

from bot_modules.core.db_utils import fixed_offset_tz
from bot_modules.todo.service import CursorColumn
from bot_modules.games.scheduled_service import (
    _epoch_to_local,
    _local_to_epoch,
)
from bot_modules.todo.board_logic import (
    ORDER_MARKER,
    TASK_CLIP,
    _clip,
    _flatten,
    approval_label,
)

#: How many rows a single section's arrival query reads. The window is one
#: 60-second tick, so this is orders of magnitude more than a real burst; it
#: exists so a pathological minute truncates the *count* in the overflow line
#: rather than running an unbounded query.
CHANGE_FETCH = 200

#: Lines a card prints before it defers to the board beneath it. The board itself
#: shows five per section, so a card longer than this is telling a mod less
#: than looking up would.
MAX_CARD_LINES = 15

#: Section markers. Each one is deliberately the emoji on the board **button** a
#: mod presses to act on that item — ✍️ Sign-Offs, 🧾 Approvals, 🕵️ Confessions —
#: so a card line points at its own next action instead of inventing a second
#: visual language for the same five queues.
TASK_MARKER = "➕"
SIGNOFF_MARKER = "✍️"
APPROVAL_MARKER = "🧾"
CONFESSION_MARKER = "🕵️"
CHORE_MARKER = "🔁"
MISSED_MARKER = "⚠️"

BUG_MARKER = "🐛"

ARRIVAL_TITLE_ONE = "📝 1 new item needs a mod"
ARRIVAL_TITLE_MANY = "📝 {n} new items need a mod"
DIGEST_TITLE = "📝 Recurring chores"
BUG_TITLE_ONE = "🐛 1 new bug"
BUG_TITLE_MANY = "🐛 {n} new bugs"

#: Discord's cap on files per message. A card past it drops the rest — the bug
#: text still posts, and the board lists every bug.
MAX_CARD_IMAGES = 10


@dataclass(frozen=True)
class CardImage:
    """One screenshot to post on a bug card."""

    bug_id: int
    url: str
    #: ``bug-<id>-<n>.<ext>`` — the name is how closing the bug finds it again.
    filename: str


@dataclass(frozen=True)
class CardPlan:
    """What to post, and what to write back as the cursor if the post lands.

    ``body`` is None for every outcome that posts nothing, and ``cursor_at`` is
    still meaningful in that case — initialising a NULL cursor writes the clock
    and says nothing, which is what makes turning the dial on silent.
    """

    #: The card's heading, or None when there is nothing to post.
    title: str | None = None
    #: The card's lines, or None when there is nothing to post.
    body: str | None = None
    #: The value to store as the new cursor, or None to leave it alone.
    cursor_at: float | None = None
    #: Bug screenshots to attach; only ever set on a card that posts.
    images: tuple[CardImage, ...] = ()

    @property
    def posts(self) -> bool:
        return self.body is not None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def task_line(row: Mapping[str, Any], *, name: str) -> str:
    """One arrived task, as either a mod's addition or a member's paid order.

    A purchase-spawned row is marked and worded differently on purpose: the
    board already separates the two with ``ORDER_MARKER`` because paid work has
    a member's coins in escrow behind it, and "ordered" is the honest verb —
    nobody on the mod team chose to add it. The stored task text names the item
    only (see ``economy/shop_items.todo_task_text``), so the buyer arrives here
    from the join rather than from the text, and an erased buyer reads as
    "someone" instead of leaking a bare id.
    """
    # task is member/mod-typed free text; clip the raw text to TASK_CLIP
    # first, then escape the short result — the same order board_logic.py's
    # render_rows uses and bug_line below mirrors, so a card and the board it
    # sits above never disagree about the same column's width.
    what = discord.utils.escape_markdown(_clip(_flatten(row.get("task") or ""), TASK_CLIP))
    who = _flatten(name) or "someone"
    if row.get("purchase_id"):
        return f"{ORDER_MARKER} **{who}** ordered {what}"
    return f"{TASK_MARKER} **{who}** added {what}"


def signoff_line(row: Mapping[str, Any], *, name: str) -> str:
    """One quest claim now waiting on a mod."""
    who = _flatten(name) or "someone"
    # quest_title is dashboard-authored free text reaching this line raw, same
    # column as board_logic.render_signoff_rows' title — clip-then-escape,
    # matching that sibling and task_line/approval_line here.
    title = discord.utils.escape_markdown(
        _clip(_flatten(row.get("quest_title") or ""), TASK_CLIP)
    )
    return f"{SIGNOFF_MARKER} **{who}** — {title}"


def approval_line(row: Mapping[str, Any], *, name: str, label: str) -> str:
    """One paid request now waiting on a mod."""
    who = _flatten(name) or "someone"
    # summary is member-paid free text reaching this line raw — clip-then-
    # escape, same TASK_CLIP order as task_line above and board_logic.py's
    # render_approval_rows.
    what = discord.utils.escape_markdown(
        _clip(_flatten(str(row.get("summary") or "")), TASK_CLIP)
    )
    return f"{APPROVAL_MARKER} **{who}** — {label}: {what}"


def confession_line(count: int) -> str:
    """New confessions, as a **count and nothing else** — deliberately.

    Every other section names a member and quotes the thing. This one cannot,
    and the reason is worth stating where somebody tidying up will read it:

    * ``confessions_service.pending_confessions`` returns **no author id at
      all**, by design, so that approving a confession never becomes a second
      way to de-anonymise one. There is no name to print here even if we wanted
      one.
    * The *content* is withheld on top of that, which is stricter than the
      board section immediately above (that one does quote it). A board section
      clears the moment a mod resolves the confession; a change card is
      permanent. Copying confession text into the channel's permanent history
      would therefore widen the disclosure well past the queue that a mod
      actually works — a confession denied and deleted would still be quoted in
      the channel forever.

    So this line says how many are waiting and nothing more. Filling it in with
    the text "for consistency" is a privacy regression, not a polish pass, and
    ``tests/todo/test_todo_change_cards_logic.py`` fails if someone does.
    """
    return f"{CONFESSION_MARKER} {_plural(count, 'new confession')} waiting"


def bug_line(row: Mapping[str, Any]) -> str:
    """One arrived bug, as its text and **never its filer** — deliberately.

    A task line names who added it, because the adder is a moderator doing mod
    work. A bug can be filed by any member, and a change card is permanent: an
    erasure clears ``added_by`` on the row, but it cannot reach a Discord message
    the bot posted weeks ago. Printing the filer's name here would leave it in the
    channel's history for good, which is exactly what the anonymise-on-erasure
    decision promised would not happen. The text itself is kept on erasure anyway
    (see docs/data_register.md), so quoting it widens nothing.

    Adding "**who** filed" for consistency with ``task_line`` is a privacy
    regression; ``tests/todo/test_todo_change_cards_logic.py`` fails if someone does.
    """
    what = discord.utils.escape_markdown(
        _clip(_flatten(row.get("task") or ""), TASK_CLIP)
    )
    return f"{BUG_MARKER} {what}"


def image_filename(bug_id: int, n: int, ext: str) -> str:
    return f"bug-{int(bug_id)}-{int(n)}.{ext}"


def is_bug_image(filename: str, bug_id: int) -> bool:
    """Whether a card attachment is one of this bug's screenshots.

    The trailing dash is what keeps bug 12 from claiming bug 123's images.
    """
    return str(filename).startswith(f"bug-{int(bug_id)}-")


def media_filename(url: str) -> str:
    """The filename at the end of an attachment's CDN link, query dropped.

    A Components V2 card's screenshots sit in a gallery, and a fetched V2
    message reports no ``attachments`` — the gallery item's link is the only
    place its ``bug-<id>-<n>.<ext>`` name can be read back from.
    """
    return str(url).split("?", 1)[0].rsplit("/", 1)[-1]


def survives_strip(filename: str, bug_ids: Iterable[int]) -> bool:
    """Whether a card's image stays once ``bug_ids``' screenshots come off."""
    return not any(is_bug_image(filename, bug_id) for bug_id in bug_ids)


def bug_images(
    rows: Iterable[Mapping[str, Any]], *, limit: int = MAX_CARD_IMAGES
) -> tuple[CardImage, ...]:
    """The screenshots held on these bug rows, in card order, capped at ``limit``.

    A row whose stored list is unreadable contributes nothing rather than
    failing the card: the bug's text matters more than its pictures.
    """
    images: list[CardImage] = []
    for row in rows:
        raw = row.get("attachment_urls")
        if not raw:
            continue
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for n, entry in enumerate(entries if isinstance(entries, list) else (), 1):
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            bug_id = int(row["id"])
            images.append(
                CardImage(
                    bug_id=bug_id,
                    url=str(entry["url"]),
                    filename=image_filename(bug_id, n, str(entry.get("ext") or "png")),
                )
            )
    return tuple(images[:limit])


def strips_by_card(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[int, int, int], list[int]]:
    """``(guild_id, channel_id, message_id) -> bug ids`` to take off that card,
    so two bugs closed together on one card cost one edit."""
    cards: dict[tuple[int, int, int], list[int]] = {}
    for row in rows:
        key = (
            int(row["guild_id"]),
            int(row["card_channel_id"] or 0),
            int(row["card_message_id"]),
        )
        cards.setdefault(key, []).append(int(row["id"]))
    return cards


def _with_overflow(lines: Sequence[str], *, limit: int = MAX_CARD_LINES) -> str:
    """Join lines, collapsing anything past ``limit`` into one more-below line.

    The pointer says *below* because that is where the board is: a card is
    posted and the board is then re-stuck beneath it, so the live view is
    always the last message in the channel and the cards sit above it.
    """
    shown = list(lines[:limit])
    hidden = len(lines) - len(shown)
    if hidden > 0:
        shown.append(f"\n…and **{hidden}** more on the board below.")
    return "\n".join(shown)


def plan_arrival_card(
    *,
    now: float,
    cursor_at: float | None,
    task_lines: Sequence[str] = (),
    signoff_lines: Sequence[str] = (),
    approval_lines: Sequence[str] = (),
    confession_count: int = 0,
    title_one: str = ARRIVAL_TITLE_ONE,
    title_many: str = ARRIVAL_TITLE_MANY,
) -> CardPlan:
    """The card for everything that arrived in this window.

    ``title_one``/``title_many`` let the bug board reuse every guard here under
    its own heading, rather than keeping a second copy of them.

    ``cursor_at is None`` is the **dark-launch guard**: a guild that has just
    had the dial ticked on has no cursor, and the honest reading of "no cursor"
    is "nothing has been announced yet", not "announce the entire backlog". So
    the first tick banks the clock and stays silent. Without this, switching the
    feature on in a guild sitting on a queue would make its first act a wall of
    cards about items that had been waiting for days.

    The new cursor is the **tick's own clock**, not the newest row's timestamp.
    Rows created in the same instant (a transaction inserting two) and rows
    beyond the fetch ceiling would both fall through a max-row-timestamp
    cursor; ``now`` can neither skip a later row nor repeat an earlier one.
    """
    if cursor_at is None:
        return CardPlan(cursor_at=now)

    lines = [
        *task_lines,
        *signoff_lines,
        *approval_lines,
        *([confession_line(confession_count)] if confession_count > 0 else []),
    ]
    if not lines:
        # Nothing arrived. Advance the cursor anyway: leaving it behind would
        # make every later tick re-scan the same widening window for nothing.
        return CardPlan(cursor_at=now)

    # A confession contributes one line but may be several items, so the count
    # in the title is items, not lines.
    items = (
        len(task_lines) + len(signoff_lines) + len(approval_lines) + confession_count
    )
    title = title_one if items == 1 else title_many.format(n=items)
    return CardPlan(title=title, body=_with_overflow(lines), cursor_at=now)


def local_noon_before(now: float, tz_offset_hours: float) -> float:
    """The most recent guild-local noon at or before ``now``, as an epoch time.

    Local rather than UTC because the board is a mod worklist read by people in
    one place, and it is the offset the rest of this feature already schedules
    against (``todo_recurring_service`` spawns a chore at a guild-local time of
    day). Returns yesterday's noon when ``now`` is still in the morning, which
    is what makes the digest fire exactly once per local day.

    The day boundary comes from ``scheduled_games_service``'s two primitives
    rather than epoch arithmetic of its own, for the reason
    ``todo_recurring_service`` gives for borrowing the same pair: this feature
    and the chore scheduler must not hold two opinions about where a guild's day
    starts, or a digest could count a window the spawner thinks is yesterday.
    They are pure functions, so importing them keeps this module free of Discord
    and of the database.
    """
    today = _epoch_to_local(now, tz_offset_hours).date()
    # The guild-local wall clock, in scheduled_games_service's convention: a
    # fixed tz_offset_hours, never UTC and never DST. _local_to_epoch re-stamps
    # that offset regardless, so tagging it here only makes the meaning
    # explicit; it cannot disagree with the conversion.
    local_midnight = datetime(
        today.year, today.month, today.day, tzinfo=fixed_offset_tz(tz_offset_hours)
    )
    noon = _local_to_epoch(local_midnight + timedelta(hours=12), tz_offset_hours)
    if noon > now:
        # Still morning: the most recent noon was yesterday's.
        noon = _local_to_epoch(
            local_midnight + timedelta(hours=12) - timedelta(days=1),
            tz_offset_hours,
        )
    return noon


def plan_digest_card(
    *,
    now: float,
    digest_at: float | None,
    tz_offset_hours: float,
    spawned: int = 0,
    missed: int = 0,
) -> CardPlan:
    """The once-a-day card for the recurring chores.

    Batched rather than live because the chores are the one thing on this board
    that moves on its own: two rows spawn in prod every single day without fail,
    and a card apiece would mean the feature's most reliable output was also its
    least interesting. Billy asked for "a noon batch for the recurring tasks".

    The window is **since the previous digest**, not since local midnight, so a
    chore configured for 18:00 lands in the next day's card rather than being
    dropped for having spawned after the last one went out.

    Same dark-launch guard as the arrivals: a NULL ``digest_at`` banks the clock
    and posts nothing, so ticking the dial on at 15:00 does not immediately fire
    a digest covering a window nobody was watching.
    """
    noon = local_noon_before(now, tz_offset_hours)
    if digest_at is None:
        return CardPlan(cursor_at=now)
    if digest_at >= noon:
        return CardPlan()  # today's has already gone out

    lines: list[str] = []
    if spawned:
        lines.append(f"{CHORE_MARKER} {_plural(spawned, 'chore')} posted")
    if missed:
        lines.append(f"{MISSED_MARKER} {_plural(missed, 'chore')} went unticked")
    if not lines:
        # A guild with no active recurring definitions. Bank the clock so the
        # window does not widen forever, but say nothing — a daily card reading
        # "nothing happened" is the chattiness this design is avoiding.
        return CardPlan(cursor_at=now)
    return CardPlan(title=DIGEST_TITLE, body="\n".join(lines), cursor_at=now)


# ── the pass as a whole ─────────────────────────────────────────────────────
#
# What follows is the rest of the *decisions*, kept out of the cog so they are
# provable without a gateway: which member each row is about, what a whole tick
# should say, and when the watermark may move. The cog's remaining job is to
# resolve names, send a card, and store what it is handed.


def subject_id(row: Mapping[str, Any]) -> int:
    """The member a task row is *about*.

    ``buyer_id`` wins over ``added_by`` because a shop order is entered by the
    machinery on the buyer's behalf: crediting the adder would name the bot's own
    purchase path, or a mod, for something a member paid for. Rows that are not
    orders have no ``buyer_id`` and fall through to whoever added them.
    """
    return int(row.get("buyer_id") or row.get("added_by") or 0)


def subject_ids(data: Mapping[str, Any], *, announce: bool = True) -> list[int]:
    """Every member id a card from ``data`` could name, for one batched lookup.

    Empty when nothing will be announced: the boot pass banks the clock and says
    nothing, so there is nobody to resolve and no reason to pay for a lookup.
    """
    if not announce:
        return []
    ids = [
        *(subject_id(r) for r in data.get("tasks") or ()),
        *(int(r.get("user_id") or 0) for r in data.get("signoffs") or ()),
        *(int(r.get("user_id") or 0) for r in data.get("approvals") or ()),
    ]
    return [i for i in ids if i]


def plan_cards(
    *,
    data: Mapping[str, Any],
    name_fn: Callable[[int], str],
    announce: bool = True,
) -> list[tuple[CardPlan, CursorColumn]]:
    """The whole tick: the cards to post, each with the column it advances.

    Returned in order, arrivals first, so a burst and the noon digest landing on
    the same tick read chronologically in the channel.

    ``announce=False`` is the loop's boot pass, and returns exactly one silent
    plan that banks **only** the arrivals cursor. That cursor is a list of
    individual items and downtime is a window nobody was watching, so replaying
    it would post a wall of cards about rows that sat on the board throughout.
    ``digest_at`` is deliberately left out: the daily card is a pair of *counts*
    over the window since the last digest, so covering the downtime costs one
    line and is simply accurate — whereas re-banking it would mean every restart
    before noon silently shrank that day's window to "since the deploy", and
    since prod's chores spawn in the morning the digest would find nothing to
    report and never post. A NULL ``digest_at`` still banks silently one tick
    later, through ``plan_digest_card``'s own guard.
    """
    now = float(data["now"])
    if not announce:
        return [(CardPlan(cursor_at=now), "change_cursor_at")]

    arrival = plan_arrival_card(
        now=now,
        cursor_at=data.get("cursor_at"),
        task_lines=[
            task_line(row, name=name_fn(subject_id(row)))
            for row in data.get("tasks") or ()
        ],
        signoff_lines=[
            signoff_line(row, name=name_fn(int(row.get("user_id") or 0)))
            for row in data.get("signoffs") or ()
        ],
        approval_lines=[
            approval_line(
                row,
                name=name_fn(int(row.get("user_id") or 0)),
                label=approval_label(str(row.get("kind") or "")),
            )
            for row in data.get("approvals") or ()
        ],
        confession_count=int(data.get("confessions") or 0),
    )
    digest = plan_digest_card(
        now=now,
        digest_at=data.get("digest_at"),
        tz_offset_hours=float(data.get("tz_offset_hours") or 0.0),
        spawned=int(data.get("spawned") or 0),
        missed=int(data.get("missed") or 0),
    )
    return [(arrival, "change_cursor_at"), (digest, "digest_at")]


def plan_bug_cards(
    *, data: Mapping[str, Any], announce: bool = True
) -> list[tuple[CardPlan, CursorColumn]]:
    """The bug board's tick: arrivals only, under the bug heading.

    No digest — the noon card is about recurring chores, and the bug list has
    none — so ``digest_at`` is never read or written for a bug board. No name
    lookup either, because ``bug_line`` names nobody. The boot pass banks the
    arrivals clock and stays quiet, for the reason ``plan_cards`` gives.

    A posting card carries the window's screenshots. A silent one carries none,
    and its bugs' images are dropped with the window rather than held for a
    later card — they belong beside their bug's line, not under someone else's.
    """
    now = float(data["now"])
    if not announce:
        return [(CardPlan(cursor_at=now), "change_cursor_at")]
    bugs = data.get("bugs") or ()
    plan = plan_arrival_card(
        now=now,
        cursor_at=data.get("cursor_at"),
        task_lines=[bug_line(row) for row in bugs],
        title_one=BUG_TITLE_ONE,
        title_many=BUG_TITLE_MANY,
    )
    if plan.posts:
        # Only bugs whose line is on the card may bring a picture: one folded
        # into the "more below" line would post with nothing naming its bug.
        plan = replace(plan, images=bug_images(bugs[:MAX_CARD_LINES]))
    return [(plan, "change_cursor_at")]


#: A send that worked.
SEND_OK = "ok"
#: A send that failed in a way that might work next minute — a 500, a timeout, a
#: gateway hiccup. The cursor stays put so the same card is rebuilt and retried.
SEND_RETRY = "retry"
#: A send that cannot work: the channel is gone, or the bot's Send Messages was
#: taken away. Retrying forever is the wrong answer and so is remembering the
#: card forever, so the cursor is banked and the content is dropped.
SEND_GIVE_UP = "give-up"


def cursor_after(plan: CardPlan, *, outcome: str) -> float | None:
    """The watermark to store after acting on ``plan``; ``None`` to leave it.

    A card that was meant to post and hit a *transient* failure leaves the cursor
    exactly where it was, so the same card is rebuilt on the next tick. Banking
    the clock there would silently drop a history entry, which is the one thing a
    log must not do.

    A **permanent** failure is the opposite call. A channel the bot can no longer
    post in will still be unreachable in a minute, and an unmoved cursor means
    the arrival window widens without bound: after a weekend of a locked channel
    the first successful card would be a wall of everything since Friday, built
    from a query reaching further back every tick. So the cursor is banked and
    that card is given up on — the board itself is still the complete live view,
    which is what makes losing a log entry survivable and losing the dial's
    silence not. ``StickyPanel.max_place_failures`` draws the same line for the
    same reason.

    A plan that never intended to post (a bank-and-stay-quiet tick) stores its
    cursor regardless, because there was nothing to lose.
    """
    if plan.posts and outcome == SEND_RETRY:
        return None
    return plan.cursor_at
