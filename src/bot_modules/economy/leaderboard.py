"""The economy panel's content — the live, auto-updating channel board.

The top earners over a rolling window, community goal progress with pace,
a per-cadence quest-board summary (members draw personal boards, so no
full-pool menu), an anonymous live feed of today's completions, and the
week's casino highlights. This module is the data and the text
(:func:`panel_sections`); ``economy.bank_panel_layout`` turns it into the
Components V2 hub panel. Placed from the dashboard's panel
poster; refreshed in place by the hourly economy loop AND by the debounced
live loop (``leaderboard_live_loop``) whenever economy activity marks the
guild dirty — so the panel moves within a couple of minutes of the action.

Since 2026-08-18 this is the guild's **only** economy panel: the how-it-works
guide it used to sit alongside became the panel's **Help** button, and the
merged panel kept the guide's home and the guide's stored ids
(``guide_channel_id`` / ``guide_message_id`` in the ``econ_`` config — see
the economy spec for why that pair won). This module
still owns the content and both refresh loops; only the message it edits
changed.

Pure collector + text — all Discord I/O stays in the cog and the loops.
The text takes a ``resolve_name`` callable so it never touches the
gateway itself. The live feed is anonymous by design (2026-07-18 decision):
quest titles, counts, and timestamps — never member names. Countdowns render
as Discord relative timestamps, which tick client-side between edits.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from discord.utils import escape_markdown

from bot_modules.core import meters
from bot_modules.core.branding import code_safe
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy import quests as quest_rules
from bot_modules.economy.logic import local_day_bounds, local_day_for
from bot_modules.economy.quests_service import (
    list_quests,
    spotlight_kind,
)
from bot_modules.economy.service import load_econ_settings
from bot_modules.shared.embeds import pad_cell, rel_ts

if TYPE_CHECKING:
    from collections.abc import Callable

    from bot_modules.economy.service import EconSettings

# Rolling window for the earner ranking, in days.
ROLLING_DAYS = 7

# How many earners make the board.
TOP_N = 5

# How many aggregated completion lines the live feed shows.
FEED_LINES = 5

_MEDALS = ("🥇", "🥈", "🥉", "🏅", "🏅")

# Quest-board display order and per-cadence labels. Event ("Anytime") quests
# aren't board-drawn and don't get a section here — they stay a surprise
# payout rather than a proactively-listed menu.
_QTYPE_LABELS = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}

# Pace rule shared with the Statistics page's "Happening now" card: expected
# progress is linear across the ISO week; under 90% of that reads "behind".
_PACE_OK = 0.9


def bar_fill(current: int, target: int, width: int = 10) -> str:
    """Just the ``▰▱`` meter glyphs (no numbers) for a running total.

    Raw and unwrapped — the login digest and the ``/bank quests`` table both
    compose this into a code span they build themselves. Callers rendering a
    bare bar must wrap it (``meters.mono``) or it renders proportionally and
    wobbles as it fills.
    """
    return meters.fill(current, target, width)


def progress_bar(
    current: int, target: int, width: int = 10, *, code: bool = True
) -> str:
    """A text meter for a community quest's running total.

    Wrapped in a code span by default so the bar keeps one length as it
    fills. Pass ``code=False`` when the result goes inside a code span the
    caller is already building — backticks do not nest.
    """
    if target <= 0:
        return f"{current:,}"
    rendered = f"{bar_fill(current, target, width)} {current:,}/{target:,}"
    return meters.mono(rendered) if code else rendered


def community_progress_bar(current: int, target: int, width: int = 12) -> str:
    """A community goal's meter, split into the 3 tier regions.

    Divides the ``▰▱`` bar at the 40/70/100% tier thresholds
    (``quest_rules.COMMUNITY_TIERS``) with a ``┃`` divider, so members can
    see which milestone region they're in rather than just a flat fill.
    """
    if target <= 0:
        return f"{current:,}"
    bounds = [0]
    for frac in quest_rules.COMMUNITY_TIERS:
        bounds.append(max(bounds[-1] + 1, round(width * frac)))
    bounds[-1] = width
    filled = max(0, min(width, round(width * current / target)))
    segments = []
    for start, end in zip(bounds, bounds[1:]):
        seg_len = end - start
        seg_filled = max(0, min(seg_len, filled - start))
        segments.append(
            meters.BAR_FILLED * seg_filled
            + meters.BAR_EMPTY * (seg_len - seg_filled)
        )
    bar = "┃".join(segments)
    return meters.mono(f"{bar} {current:,}/{target:,}")


# Shared monospace-table helpers (docs/embed_style_guide.md names them).
# Aliased to the historical private names so the ~20 call sites below and the
# modules that import them from here keep working.
_pad = pad_cell
_rel = rel_ts


@dataclass(frozen=True)
class FeedLine:
    """One anonymous live-feed entry: a quest's completions today."""

    title: str
    count: int
    last_ts: float


@dataclass(frozen=True)
class CommunityGoal:
    title: str
    current: int
    target: int | None
    completed: bool
    settled: bool
    # Auto-tracking weekly (trigger_kind set): tier markers, pace, deadline.
    auto: bool = False
    tiers: int = 0
    contributors: int = 0
    # Today's contribution count (None when unknowable — channel-scoped).
    today_delta: int | None = None
    on_track: bool = True
    ends_ts: float | None = None
    # Warm per-kind one-liner (quests.TRIGGER_FLAVOR, falling back to the
    # functional TRIGGER_KINDS label), "" for manual goals — shown next to
    # the title, which stays descriptive on its own.
    kind_flavor: str = ""
    # The goal's trigger channel, None when anything anywhere counts. The
    # same NULL that makes `today_delta` unknowable above: a scoped goal is
    # the one case where "chip in" needs an address.
    channel_id: int | None = None


@dataclass(frozen=True)
class QuestLine:
    qtype: str
    title: str
    reward: int
    reward_xp: int
    spotlight: bool = False


@dataclass(frozen=True)
class LeaderboardData:
    top_earners: list[tuple[int, int]]  # (user_id, amount), ranked
    community: list[CommunityGoal]
    quests: list[QuestLine]
    spotlight_kind: str | None = None
    spotlight_label: str = ""
    today_by_user: dict[int, int] = field(default_factory=dict)
    feed: tuple[FeedLine, ...] = ()
    set_bonuses_today: int = 0
    # Next guild-local day roll (dailies reset) / ISO-week roll (weeklies
    # flip, spotlight changes, community weeklies end). None = omit clocks.
    day_roll_ts: float | None = None
    week_roll_ts: float | None = None
    # Weekly raffle (sinks round 3, stage 5). raffle_on gates the section;
    # last_winner_id is announced BY NAME (the deliberate anonymous-ticker
    # carve-out — buying a ticket is opting in).
    raffle_on: bool = False
    raffle_tickets: int = 0
    raffle_entrants: int = 0
    last_winner_id: int | None = None
    last_winner_week: str = ""
    # Night at the Tables (casino fancy round): this ISO week's biggest
    # single win (user_id, amount) and best multiplier (user_id, ×100).
    # Winners are NAMED — public play is opting in, the raffle rule.
    casino_biggest: tuple[int, int] | None = None
    casino_luckiest: tuple[int, int] | None = None


def collect_leaderboard_data(
    conn: sqlite3.Connection, guild_id: int, now_ts: float
) -> LeaderboardData:
    """Everything the panel shows, in one sync read.

    Earner income matches the Statistics page definition: positive ledger
    amounts excluding ``transfer_in`` (a transfer moves currency between
    members, it isn't earned). "Today" is the guild-local calendar day; the
    week clock is the guild-local ISO week (Monday 00:00), matching every
    quest cadence.
    """
    offset = get_tz_offset_hours(conn, guild_id)
    today = local_day_for(now_ts, offset)
    day_start, day_end = local_day_bounds(today, offset)
    day_obj = date.fromisoformat(today)
    next_monday = day_obj + timedelta(days=7 - day_obj.weekday())
    week_end, _ = local_day_bounds(next_monday.isoformat(), offset)
    # Month clock for monthly guild-wide goals (1st of next month, guild-local).
    first_next_month = date(
        day_obj.year + (1 if day_obj.month == 12 else 0),
        1 if day_obj.month == 12 else day_obj.month + 1,
        1,
    )
    month_end, _ = local_day_bounds(first_next_month.isoformat(), offset)
    days_in_month = (first_next_month - date(day_obj.year, day_obj.month, 1)).days

    settings = load_econ_settings(conn, guild_id)
    this_week = quest_rules.iso_week_for(today)
    raffle_on = bool(settings.raffle_enabled) and settings.price_raffle_ticket > 0
    raffle_tickets = raffle_entrants = 0
    last_winner_id: int | None = None
    last_winner_week = ""
    if raffle_on:
        trow = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS t, COUNT(*) AS e "
            "FROM econ_raffle_tickets "
            "WHERE guild_id = ? AND iso_week = ? AND count > 0",
            (guild_id, this_week),
        ).fetchone()
        raffle_tickets, raffle_entrants = int(trow["t"]), int(trow["e"])
        drow = conn.execute(
            "SELECT iso_week, winner_id FROM econ_raffle_draws "
            "WHERE guild_id = ? AND winner_id IS NOT NULL "
            "ORDER BY drawn_at DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        if drow is not None:
            last_winner_id = int(drow["winner_id"])
            last_winner_week = str(drow["iso_week"])

    from bot_modules.casino.service import (  # noqa: PLC0415
        weekly_table_highlights,
    )

    big_row, lucky_row = weekly_table_highlights(conn, guild_id, this_week)
    casino_biggest = (
        (int(big_row["user_id"]), int(big_row["biggest_win"]))
        if big_row is not None
        else None
    )
    casino_luckiest = (
        (int(lucky_row["user_id"]), int(lucky_row["biggest_mult_x100"]))
        if lucky_row is not None
        else None
    )

    cutoff = now_ts - ROLLING_DAYS * 86400
    earners = [
        (int(r["user_id"]), int(r["s"]))
        for r in conn.execute(
            "SELECT user_id, SUM(amount) AS s FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
            "AND kind != 'transfer_in' AND user_id > 0 "  # <0 = house bots
            "GROUP BY user_id ORDER BY s DESC, user_id LIMIT ?",
            (guild_id, cutoff, TOP_N),
        ).fetchall()
    ]

    today_by_user: dict[int, int] = {}
    if earners:
        marks = ",".join("?" * len(earners))
        today_by_user = {
            int(r["user_id"]): int(r["s"])
            for r in conn.execute(
                "SELECT user_id, SUM(amount) AS s FROM econ_ledger "
                "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
                f"AND kind != 'transfer_in' AND user_id IN ({marks}) "
                "GROUP BY user_id",
                (guild_id, day_start, *[uid for uid, _ in earners]),
            ).fetchall()
        }

    feed = tuple(
        FeedLine(
            title=str(r["title"]),
            count=int(r["n"]),
            last_ts=float(r["last_ts"]),
        )
        for r in conn.execute(
            "SELECT q.title AS title, COUNT(*) AS n, "
            "MAX(c.created_at) AS last_ts "
            "FROM econ_quest_claims c JOIN econ_quests q ON q.id = c.quest_id "
            "WHERE c.guild_id = ? AND c.state = 'paid' AND c.created_at >= ? "
            "GROUP BY c.quest_id ORDER BY last_ts DESC LIMIT ?",
            (guild_id, day_start, FEED_LINES),
        ).fetchall()
    )
    set_bonuses_today = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM econ_ledger "
            "WHERE guild_id = ? AND kind = 'quest_bonus' AND created_at >= ?",
            (guild_id, day_start),
        ).fetchone()["n"]
    )

    week = quest_rules.iso_week_for(today)
    spot = spotlight_kind(conn, guild_id, week)

    # Pace baseline, shared with compute_live: day 1 of the ISO week counts
    # as one elapsed day.
    elapsed_days = day_obj.weekday() + 1

    community: list[CommunityGoal] = []
    quests: list[QuestLine] = []
    for row in list_quests(conn, guild_id, active_only=True):
        if row["qtype"] in ("community", "monthly"):
            # Both are guild-wide, tier-settled goals. Monthly runs on the
            # calendar-month clock; community on the ISO-week clock.
            is_monthly = row["qtype"] == "monthly"
            qid = int(row["id"])
            prog = conn.execute(
                "SELECT current, completed_at, settled_at "
                "FROM econ_community_progress WHERE quest_id = ?",
                (qid,),
            ).fetchone()
            current = int(prog["current"]) if prog else 0
            target = row["community_target"]
            auto = bool(row["trigger_kind"])
            contributors = 0
            today_delta: int | None = None
            on_track = True
            if auto:
                contributors = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM econ_community_contrib "
                        "WHERE quest_id = ? AND count > 0",
                        (qid,),
                    ).fetchone()["n"]
                )
                if row["trigger_channel_id"] is None:
                    # The kind-activity ledger is scope-blind, so today's
                    # delta is only honest for unscoped goals.
                    today_delta = int(
                        conn.execute(
                            "SELECT COALESCE(SUM(count), 0) AS s "
                            "FROM econ_kind_activity WHERE guild_id = ? "
                            "AND kind = ? AND local_day = ?",
                            (guild_id, str(row["trigger_kind"]), today),
                        ).fetchone()["s"]
                    )
                if is_monthly:
                    expected = (target or 0) * day_obj.day / days_in_month
                else:
                    expected = (target or 0) * elapsed_days / 7
                on_track = expected == 0 or current >= _PACE_OK * expected
            community.append(
                CommunityGoal(
                    title=row["title"],
                    current=current,
                    target=target,
                    completed=bool(prog and prog["completed_at"] is not None),
                    settled=bool(prog and prog["settled_at"] is not None),
                    auto=auto,
                    tiers=quest_rules.community_tiers_crossed(
                        current, int(target or 0)
                    ) if auto else 0,
                    contributors=contributors,
                    today_delta=today_delta,
                    on_track=on_track,
                    ends_ts=(month_end if is_monthly else week_end) if auto else None,
                    kind_flavor=(
                        quest_rules.TRIGGER_FLAVOR.get(
                            str(row["trigger_kind"]),
                            quest_rules.TRIGGER_KINDS.get(
                                str(row["trigger_kind"]), ""
                            ),
                        )
                        if auto
                        else ""
                    ),
                    channel_id=(
                        int(row["trigger_channel_id"])
                        if row["trigger_channel_id"] is not None
                        else None
                    ),
                )
            )
        elif row["qtype"] in _QTYPE_LABELS:
            quests.append(
                QuestLine(
                    qtype=row["qtype"],
                    title=row["title"],
                    reward=int(row["reward"]),
                    reward_xp=int(row["reward_xp"]),
                    spotlight=bool(
                        spot and str(row["trigger_kind"] or "") == spot
                    ),
                )
            )
    order = {q: i for i, q in enumerate(_QTYPE_LABELS)}
    quests.sort(key=lambda q: order[q.qtype])
    return LeaderboardData(
        top_earners=earners,
        community=community,
        quests=quests,
        spotlight_kind=spot,
        spotlight_label=quest_rules.TRIGGER_KINDS.get(spot, spot) if spot else "",
        today_by_user=today_by_user,
        feed=feed,
        set_bonuses_today=set_bonuses_today,
        day_roll_ts=day_end,
        week_roll_ts=week_end,
        raffle_on=raffle_on,
        raffle_tickets=raffle_tickets,
        raffle_entrants=raffle_entrants,
        last_winner_id=last_winner_id,
        last_winner_week=last_winner_week,
        casino_biggest=casino_biggest,
        casino_luckiest=casino_luckiest,
    )


# The panel's section headings (Title Case, glyph-led — style guide § Words).
TOP_EARNERS_HEADING = f"🏆 Top Earners (Last {ROLLING_DAYS} Days)"
# One heading over both the community goals and the per-cadence board.
BOARD_HEADING = "📋 Quest Board & Community Goals"
FEED_HEADING = "📰 Live Feed — Today"
TABLES_HEADING = "🎰 Night at the Tables"
RAFFLE_HEADING = "🎟️ Weekly Raffle"

# A Components V2 message has one 4,000-character text budget and no
# per-field cap, so the board — the one section that grows with the guild's
# quest list — is held to a share of it rather than packed into "(cont.)"
# fields the way the embed's 1,024-character fields forced. Everything else on
# the panel is bounded by TOP_N / FEED_LINES.
BOARD_CHAR_BUDGET = 1800


def _fit_blocks(blocks: list[str], limit: int) -> tuple[str, int]:
    """Join as many leading ``blocks`` (blank-line separated) as fit ``limit``.

    A block (one community goal, or the quest-board summary) is never split
    mid-block, and the first always goes in whatever its length — every block
    is already bounded by its own builder. Returns the joined text and how
    many blocks made it in.
    """
    out: list[str] = []
    used = 0
    for block in blocks:
        cost = len(block) + (2 if out else 0)  # 2 = the "\n\n" separator
        if out and used + cost > limit:
            break
        out.append(block)
        used += cost
    return "\n\n".join(out), len(out)


def _community_block(g: CommunityGoal) -> str:
    """One goal's lines: title + flavor, a tier-region bar, then detail.

    A channel-scoped goal names its channel between the bar and the detail.
    This block is the only member-facing surface a *guild-wide* goal has that
    the per-member digest doesn't cover, and the scope is exactly the fact a
    reader needs before they can contribute to one.
    """
    if g.settled:
        state = " — ✅ paid out"
    elif g.completed:
        state = " — 🎉 complete, payout coming"
    else:
        state = ""
    title = (
        f"**{g.title}** — *{g.kind_flavor}*"
        if g.kind_flavor
        else f"**{g.title}**"
    )
    bar = community_progress_bar(g.current, g.target or 0)
    lines = [title, f"{bar}{state}"]
    # Where to chip in, for a channel-scoped goal — dropped once the goal is
    # finished, for the same reason a done quest loses its link in the digest.
    if not g.completed and not g.settled:
        scope = quest_rules.channel_scope_line(g.channel_id)
        if scope:
            lines.append(scope)
    target = int(g.target or 0)
    if g.auto and target > 0 and not g.settled:
        if not g.completed and g.tiers > 0:
            lines.append(f"🏁 tier {g.tiers}/3 secured")
        detail = []
        if g.contributors > 0:
            detail.append(f"👥 {g.contributors} contributing")
        if g.today_delta:
            detail.append(f"+{g.today_delta:,} today")
        if g.ends_ts and not g.completed:
            detail.append(f"ends {_rel(g.ends_ts)}")
        if detail:
            lines.append(" · ".join(detail))
    return "\n".join(lines)


def _feed_lines(data: LeaderboardData) -> str:
    """Today's anonymous completion feed — titles and counts, never names.

    Title | count | when, with the title column tab-aligned (×1 is always
    printed so the count column lines up too).
    """
    lines = []
    if data.feed:
        width = min(max(len(f.title) for f in data.feed), 20)
        lines = [
            f"✅ `{_pad(f.title, width)}` ×{f.count} · {_rel(f.last_ts)}"
            for f in data.feed
        ]
    if data.set_bonuses_today > 0:
        lines.append(
            f"🎁 Full-board bonus paid ×{data.set_bonuses_today} today"
        )
    if not lines:
        return "Quiet so far today — complete a quest to light this board up."
    return "\n".join(lines)


def _earner_lines(
    data: LeaderboardData, resolve_name: Callable[[int], str], emoji: str
) -> str:
    """Medal | name | amount rows, the name and amount as fixed-width cells."""
    if not data.top_earners:
        return "Nobody has earned yet this week — be the first!"
    # code_safe, not escape_markdown: these names render inside a `code
    # span`, where markdown never renders and a stray backtick would close
    # the span early instead.
    names = {uid: code_safe(resolve_name(uid)) for uid, _ in data.top_earners}
    name_w = min(max(len(n) for n in names.values()), 16)
    amount_w = max(len(f"{amt:,}") for _, amt in data.top_earners)
    lines = []
    for i, (uid, amount) in enumerate(data.top_earners):
        today = data.today_by_user.get(uid, 0)
        delta = f" (+{today:,} today)" if today > 0 else ""
        lines.append(
            f"{_MEDALS[i]} `{_pad(names[uid], name_w)}` "
            f"{emoji} `{f'{amount:,}'.rjust(amount_w)}`{delta}"
        )
    return "\n".join(lines)


def _reset_line(data: LeaderboardData) -> str:
    """When the boards next reshuffle, pointing at the member's own list.

    The reset clocks lived in the Today's Pulse section until it came off the
    panel (Billy, 2026-09-26); they are the one pulse fact a member acts on,
    so they moved here, beside the board they reset.
    """
    clocks = []
    if data.day_roll_ts:
        clocks.append(f"dailies reset {_rel(data.day_roll_ts)}")
    if data.week_roll_ts:
        clocks.append(f"new weeklies {_rel(data.week_roll_ts)}")
    # Not str.capitalize(): it would lower the "R" in a <t:…:R> timestamp.
    when = " · ".join(clocks) if clocks else "boards reshuffle each reset"
    when = when[0].upper() + when[1:]
    return f"{when}. Press **Show My Quests** below to see yours."


def _board_text(settings: EconSettings, data: LeaderboardData) -> str:
    """Community goals, then the personal-board summary, in one section.

    Both answer "what is there to do", and two adjacent headings made the
    panel read as two competing boards. Goals lead (they're shared and
    time-boxed). Past :data:`BOARD_CHAR_BUDGET` the trailing blocks drop
    with a count, never mid-goal.
    """
    emoji = settings.currency_emoji
    blocks: list[str] = [_community_block(g) for g in data.community]

    if data.quests:
        quest_lines = []
        if data.spotlight_label:
            until = (
                f" — until {_rel(data.week_roll_ts)}"
                if data.week_roll_ts
                else " this week"
            )
            quest_lines.append(
                f"⚡ **Spotlight:** {data.spotlight_label} pays "
                f"**double**{until}!"
            )
        # Members never face the whole pool: each draws a personal board of
        # board_size quests per cadence. Summarize the draw instead of
        # listing a menu nobody actually has. Monthly has no entry: it is a
        # guild-wide goal, diverted into `community` above, so its pool here
        # is always empty and the row can never render.
        sizes = {
            "daily": settings.quest_board_daily,
            "weekly": settings.quest_board_weekly,
        }
        label_width = max(len(v) for v in _QTYPE_LABELS.values())
        # cadence | description | payment rows; the first two columns are
        # fixed-width code cells so payments line up down the section.
        rows: list[tuple[str, str, str]] = []
        for qtype, qtype_label in _QTYPE_LABELS.items():
            pool = [q for q in data.quests if q.qtype == qtype]
            if not pool:
                continue
            n = min(sizes.get(qtype, 0), len(pool))
            if n <= 0:
                continue
            lo = min(q.reward for q in pool)
            hi = max(q.reward for q in pool)
            reward = f"{emoji} {lo:,}" + (
                f"–{hi:,}" if hi != lo else ""
            ) + " each"
            rows.append(
                (qtype_label, f"{n} yours · pool {len(pool)}", reward)
            )
        if rows:
            desc_width = min(max(len(desc) for _, desc, _ in rows), 24)
            # One monospace cell per row (cadence + description together) rather
            # than two adjacent code spans — a single grey box keeps the columns
            # squared up, while the payment stays outside for its emoji + bold.
            quest_lines.extend(
                f"`{_pad(label, label_width)}  {_pad(desc, desc_width)}` {pay}"
                for label, desc, pay in rows
            )
            quest_lines.append(_reset_line(data))
        if quest_lines:
            blocks.append("\n".join(quest_lines))
    else:
        blocks.append("No quests running right now — check back soon.")

    text, shown = _fit_blocks(blocks, BOARD_CHAR_BUDGET)
    if shown < len(blocks):
        text += f"\n\n_…and {len(blocks) - shown} more_"
    return text


def _tables_text(
    data: LeaderboardData, resolve_name: Callable[[int], str], emoji: str
) -> str | None:
    """This week's casino highlights, or ``None`` on a week with no play.

    Winners are named — public play is opting in, the raffle rule.
    """
    if not (data.casino_biggest or data.casino_luckiest):
        return None
    lines = []
    if data.casino_biggest:
        uid, amount = data.casino_biggest
        lines.append(
            f"💥 Biggest win: **{escape_markdown(resolve_name(uid))}** — "
            f"{emoji} **{amount:,}** in one play"
        )
    if data.casino_luckiest:
        uid, mult_x100 = data.casino_luckiest
        lines.append(
            f"🍀 Luckiest hit: **{escape_markdown(resolve_name(uid))}** — "
            f"**{mult_x100 / 100:g}×** their bet"
        )
    lines.append("Resets with the week — the tables are waiting.")
    return "\n".join(lines)


def _raffle_text(data: LeaderboardData, resolve_name: Callable[[int], str]) -> str | None:
    """This week's raffle tally and last draw's winner, or ``None`` when off.

    The winner is named: buying a ticket is opting in (the raffle rule), and
    until now only the opt-in DM ever said who won.
    """
    if not data.raffle_on:
        return None
    lines = [
        f"🎫 **{data.raffle_tickets:,}** ticket(s) from "
        f"**{data.raffle_entrants:,}** entrant(s) this week"
    ]
    if data.last_winner_id is not None:
        lines.append(
            f"🏆 Last winner: **{escape_markdown(resolve_name(data.last_winner_id))}**"
            + (f" ({data.last_winner_week})" if data.last_winner_week else "")
        )
    return "\n".join(lines)


def panel_sections(
    settings: EconSettings,
    data: LeaderboardData,
    resolve_name: Callable[[int], str],
) -> list[tuple[str, str]]:
    """The economy panel's board, as ``(heading, body)`` pairs in order.

    The text half of the panel, kept apart from the components
    (``economy.bank_panel_layout``) so the content rules are tested as text.
    Each body is a small table: fixed-width inline-code cells align the
    columns (see ``_pad``) while emoji, bold and live timestamps stay outside
    the backticks where Discord still renders them.

    No Today's Pulse: Billy took it off on 2026-09-26, keeping the earners,
    the board, the feed and the tables. Its reset clocks moved into the board
    (:func:`_reset_line`).
    """
    emoji = settings.currency_emoji
    sections = [
        (TOP_EARNERS_HEADING, _earner_lines(data, resolve_name, emoji)),
        (BOARD_HEADING, _board_text(settings, data)),
        (FEED_HEADING, _feed_lines(data)),
    ]
    raffle = _raffle_text(data, resolve_name)
    if raffle is not None:
        sections.append((RAFFLE_HEADING, raffle))
    tables = _tables_text(data, resolve_name, emoji)
    if tables is not None:
        sections.append((TABLES_HEADING, tables))
    return sections
