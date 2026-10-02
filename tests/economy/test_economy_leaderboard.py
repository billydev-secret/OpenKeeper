"""Economy leaderboard panel — collector, section text, command, loop refresh."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy import live_signal
from bot_modules.economy.leaderboard import (
    CommunityGoal,
    BOARD_CHAR_BUDGET,
    BOARD_HEADING,
    FEED_HEADING,
    FeedLine,
    LeaderboardData,
    QuestLine,
    ROLLING_DAYS,
    TABLES_HEADING,
    RAFFLE_HEADING,
    TOP_EARNERS_HEADING,
    collect_leaderboard_data,
    community_progress_bar,
    panel_sections,
)
from bot_modules.economy.logic import local_day_bounds, local_day_for
from bot_modules.economy import quests_service as quests_svc
from bot_modules.economy.loop import (
    run_guild_leaderboard,
    run_live_tick,
)
from bot_modules.economy.service import (
    EconSettings,
    load_econ_settings,
    save_econ_settings,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
CHANNEL_ID = 111
NOW = 1_700_000_000.0


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


@pytest.fixture(autouse=True)
def _fresh_live_signal():
    live_signal.reset()
    yield
    live_signal.reset()


def _today_bounds(conn) -> tuple[float, float]:
    offset = get_tz_offset_hours(conn, GUILD_ID)
    today = local_day_for(NOW, offset)
    return local_day_bounds(today, offset)


def _paid_claim(conn, quest_id, user_id, *, period, created_at):
    conn.execute(
        "INSERT INTO econ_quest_claims "
        "(quest_id, guild_id, user_id, period, state, created_at) "
        "VALUES (?, ?, ?, ?, 'paid', ?)",
        (quest_id, GUILD_ID, user_id, period, created_at),
    )


def _credit(conn, user_id, amount, *, kind="quest", age_days=0.0):
    conn.execute(
        "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (GUILD_ID, user_id, amount, kind, NOW - age_days * 86400),
    )


def _quest(
    conn, *, qtype, title, reward=10, reward_xp=0, target=None, active=True,
    trigger_kind="",
):
    qid = quests_svc.create_quest(
        conn,
        GUILD_ID,
        title=title,
        description="",
        qtype=qtype,
        reward=reward,
        signoff=0,
        criteria="",
        starts_at=None,
        ends_at=None,
        rotate_tag="",
        community_target=target,
        created_by=None,
        trigger_kind=trigger_kind,
        reward_xp=reward_xp,
    )
    if active:
        quests_svc.set_quest_active(conn, GUILD_ID, qid, True)
    return qid


# ── collector ───────────────────────────────────────────────────────────────


def test_collect_top_earners_window_and_exclusions(db):
    with open_db(db) as conn:
        for uid in range(1, 7):  # six earners, uid == relative rank seed
            _credit(conn, uid, uid * 10)
        _credit(conn, 1, 500, age_days=ROLLING_DAYS + 1)  # outside the window
        _credit(conn, 2, 500, kind="transfer_in")  # moved, not earned
        _credit(conn, 3, -25)  # spending never counts
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert len(data.top_earners) == 5  # six candidates, top five kept
    assert data.top_earners[0] == (6, 60)
    assert data.top_earners[-1] == (2, 20)  # transfer_in ignored
    assert all(uid != 1 or amt == 10 for uid, amt in data.top_earners)


def test_collect_quests_and_community_goals(db):
    with open_db(db) as conn:
        _quest(conn, qtype="weekly", title="Weekly one", reward=30, reward_xp=15)
        _quest(conn, qtype="daily", title="Daily one")
        _quest(
            conn, qtype="monthly", title="Benched", active=False, trigger_kind="quoted"
        )
        cq = _quest(conn, qtype="community", title="Group goal", target=100)
        quests_svc.set_community_progress(conn, cq, 40, target=100)
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert [(q.qtype, q.title) for q in data.quests] == [
        ("daily", "Daily one"),  # cadence order, not insertion order
        ("weekly", "Weekly one"),
    ]
    assert data.quests[1].reward_xp == 15
    goal = data.community[0]
    assert (goal.title, goal.current, goal.target) == ("Group goal", 40, 100)
    assert not goal.completed and not goal.settled
    assert goal.kind_flavor == ""  # manual goal, no flavor line


def test_collect_pulse_and_today_deltas(db):
    with open_db(db) as conn:
        _credit(conn, 1, 100, age_days=2)  # in the 7d window, not today
        _credit(conn, 1, 40)  # written at NOW = inside today
        _credit(conn, 2, 30)
        _credit(conn, 3, 500, kind="transfer_in")  # moved, never earned
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert data.today_by_user == {1: 40, 2: 30}
    assert data.top_earners[0] == (1, 140)  # window total unchanged
    assert data.day_roll_ts is not None and data.day_roll_ts > NOW
    assert data.week_roll_ts is not None and data.week_roll_ts > NOW


def test_collect_feed_aggregates_todays_completions(db):
    with open_db(db) as conn:
        hi = _quest(conn, qtype="daily", title="Say hi")
        bump = _quest(conn, qtype="weekly", title="Bump us")
        day_start, _day_end = _today_bounds(conn)
        _paid_claim(conn, hi, 1, period="p1", created_at=day_start + 10)
        _paid_claim(conn, hi, 2, period="p2", created_at=day_start + 30)
        _paid_claim(conn, bump, 3, period="p3", created_at=day_start + 20)
        _paid_claim(conn, hi, 4, period="p4", created_at=day_start - 10)  # yday
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, "
            "created_at) VALUES (?, ?, ?, 'quest_bonus', ?)",
            (GUILD_ID, 1, 5, day_start + 40),
        )
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert [(f.title, f.count) for f in data.feed] == [
        ("Say hi", 2),  # newest last-completion first
        ("Bump us", 1),
    ]
    assert data.feed[0].last_ts == day_start + 30
    assert data.set_bonuses_today == 1


def test_collect_auto_goal_live_detail(db):
    with open_db(db) as conn:
        qid = quests_svc.create_quest(
            conn,
            GUILD_ID,
            title="Bump week",
            description="",
            qtype="community",
            reward=25,
            signoff=0,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=70,
            created_by=None,
            trigger_kind="message_sent",
        )
        quests_svc.set_quest_active(conn, GUILD_ID, qid, True)
        quests_svc.set_community_progress(conn, qid, 30, target=70)
        conn.executemany(
            "INSERT INTO econ_community_contrib (quest_id, user_id, count) "
            "VALUES (?, ?, ?)",
            [(qid, 1, 20), (qid, 2, 10)],
        )
        offset = get_tz_offset_hours(conn, GUILD_ID)
        today = local_day_for(NOW, offset)
        quests_svc.record_kind_activity(conn, GUILD_ID, 1, "message_sent", today)
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    goal = data.community[0]
    assert goal.auto and goal.tiers == 1  # 30/70 crosses the 40% tier (28)
    assert goal.contributors == 2
    assert goal.today_delta == 1
    assert goal.on_track  # 30 done ≥ 90% of the linear-pace expectation
    assert goal.ends_ts == data.week_roll_ts
    # TRIGGER_FLAVOR[message_sent] — warm copy, not the functional label
    assert goal.kind_flavor == "every word keeps the fire crackling"


def test_collect_carries_a_goals_trigger_channel(db):
    """The scoped-goal id reaches the card, and NULL stays None.

    ``today_delta`` is already suppressed for a scoped goal because the
    kind-activity ledger is scope-blind; this is the other half of the same
    fact, and the half the member can act on.
    """
    with open_db(db) as conn:
        scoped = quests_svc.create_quest(
            conn, GUILD_ID, title="Picture week", description="",
            qtype="community", reward=25, signoff=0, criteria="",
            starts_at=None, ends_at=None, rotate_tag="", community_target=70,
            created_by=None, trigger_kind="photo_post",
            trigger_channel_id=555,
        )
        quests_svc.set_quest_active(conn, GUILD_ID, scoped, True)
        quests_svc.set_community_progress(conn, scoped, 30, target=70)
        loose = _quest(
            conn, qtype="monthly", title="Server buzz", target=100,
            trigger_kind="message_sent",
        )
        quests_svc.set_community_progress(conn, loose, 10, target=100)
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    by_title = {g.title: g for g in data.community}
    assert by_title["Picture week"].channel_id == 555
    assert by_title["Server buzz"].channel_id is None


# ── builder ─────────────────────────────────────────────────────────────────


def _names(mapping):
    return lambda uid: mapping.get(uid, f"User {uid}")


def _sections(data, settings=None, names=None) -> dict[str, str]:
    return dict(panel_sections(settings or EconSettings(), data, _names(names or {})))


def test_earners_ranked_with_names_and_currency():
    settings = EconSettings(currency_plural="Gems", currency_emoji="💎")
    data = LeaderboardData(
        top_earners=[(1, 120), (2, 80)], community=[], quests=[]
    )
    top = _sections(data, settings, {1: "Alice", 2: "Bob"})[TOP_EARNERS_HEADING]
    assert top.index("Alice") < top.index("Bob")
    assert "🥇" in top and "🥈" in top and "💎 `120`" in top


def test_top_earner_name_neutralises_backtick_not_escapes(monkeypatch):
    # names[] renders inside a monospace `code span` (_pad), where markdown
    # never renders and escape_markdown would print literal backslashes —
    # the vulnerability there is a backtick closing the span early, so the
    # fix is code_safe's stand-in character, not a backslash.
    data = LeaderboardData(top_earners=[(1, 120)], community=[], quests=[])
    top = _sections(data, names={1: "a`b"})[TOP_EARNERS_HEADING]
    assert "\\" not in top  # no escape-markdown backslashes leaked in
    assert "`b" not in top  # the raw backtick never reaches the span raw
    assert "a" in top and "b" in top


def test_casino_highlight_names_are_escaped():
    # Unlike the top-earners cell above, these two lines sit inside the
    # bot's own bare ** wrapper (no code span) — a member with ** in their
    # display name can break out of it unless resolve_name's output is
    # escaped at these interpolation sites.
    data = LeaderboardData(
        top_earners=[],
        community=[],
        quests=[],
        casino_biggest=(1, 500),
        casino_luckiest=(2, 3600),
    )
    tables = _sections(data, names={1: "**Root**", 2: "__Mod__"})[TABLES_HEADING]
    assert "\\*\\*Root\\*\\*" in tables
    assert "\\_\\_Mod\\_\\_" in tables
    # The bot's own bold wrapper around the name is untouched.
    assert "**\\*\\*Root\\*\\***" in tables


def test_embed_community_bar_and_states():
    from bot_modules.economy.leaderboard import CommunityGoal

    data = LeaderboardData(
        top_earners=[],
        community=[
            CommunityGoal("Half there", 50, 100, completed=False, settled=False),
            CommunityGoal("Done", 10, 10, completed=True, settled=True),
        ],
        quests=[],
    )
    goals = _sections(data)[BOARD_HEADING]
    assert goals is not None
    assert community_progress_bar(50, 100) in goals
    assert "✅ paid out" in goals


def test_embed_community_goals_separated_by_blank_line():
    # Two goals crammed together with no gap read as one run-on block —
    # a blank line between them keeps each goal's bar/tier/detail trio
    # visually grouped with its own title.
    data = LeaderboardData(
        top_earners=[],
        community=[
            CommunityGoal(
                "Server Buzz", 327, 16635, completed=False, settled=False,
                auto=True, tiers=0, contributors=23, today_delta=1656,
                on_track=False,
            ),
            CommunityGoal(
                "Talk It Out", 108, 10263, completed=False, settled=False,
                auto=True, tiers=0, contributors=19, today_delta=784,
                on_track=False,
            ),
        ],
        quests=[],
    )
    goals = _sections(data)[BOARD_HEADING]
    assert "today\n\n**Talk It Out**" in goals


def test_embed_board_keeps_goals_above_the_cadence_summary():
    # One heading, both bodies, goals first — the merge (#83), not a swap.
    data = LeaderboardData(
        top_earners=[],
        community=[CommunityGoal("Group goal", 5, 10, completed=False, settled=False)],
        quests=[QuestLine("daily", "Chatter", 5, 0)],
    )
    sections = _sections(data)
    board = sections[BOARD_HEADING]
    assert board.index("**Group goal**") < board.index("`Daily")
    assert sum(1 for h in sections if h.startswith("📋")) == 1


def test_board_past_its_budget_drops_whole_goals_with_a_count():
    # A V2 message has one 4,000-character budget and no per-field cap, so
    # the board is held to BOARD_CHAR_BUDGET: trailing goals drop whole, with
    # a count, rather than the "(cont.)" fields the embed's 1,024 cap forced.
    data = LeaderboardData(
        top_earners=[],
        community=[
            CommunityGoal(
                f"Goal number {i} with a long enough title to eat budget",
                i, 100, completed=False, settled=False, auto=True,
                kind_flavor="a flavor line that also takes up meaningful room",
                contributors=12, today_delta=340,
            )
            for i in range(20)
        ],
        quests=[QuestLine("daily", "Chatter", 5, 0)],
    )
    board = _sections(data)[BOARD_HEADING]
    shown = sum(f"Goal number {i} " in board for i in range(20))
    assert 0 < shown < 20
    assert board.endswith(f"_…and {21 - shown} more_")  # 20 goals + the summary
    assert len(board) <= BOARD_CHAR_BUDGET + 30
    # Never mid-goal: every goal shown keeps its bar.
    assert board.count("`▰") + board.count("`▱") == shown


def test_community_progress_bar_marks_tier_regions():
    # width=12 default: tier bounds at 5/8/12 (40/70/100% of target).
    assert community_progress_bar(0, 10) == "`▱▱▱▱▱┃▱▱▱┃▱▱▱▱ 0/10`"
    assert community_progress_bar(4, 10) == "`▰▰▰▰▰┃▱▱▱┃▱▱▱▱ 4/10`"  # tier 1 boundary
    assert community_progress_bar(7, 10) == "`▰▰▰▰▰┃▰▰▰┃▱▱▱▱ 7/10`"  # tier 2 boundary
    assert community_progress_bar(10, 10) == "`▰▰▰▰▰┃▰▰▰┃▰▰▰▰ 10/10`"
    assert community_progress_bar(5, 0) == "5"  # no target, plain count


def test_embed_goal_title_shows_kind_flavor():
    data = LeaderboardData(
        top_earners=[],
        community=[
            CommunityGoal(
                "Server Buzz", 10, 100, completed=False, settled=False,
                auto=True, kind_flavor="every word keeps the fire crackling",
            ),
            CommunityGoal("Manual goal", 1, 10, completed=False, settled=False),
        ],
        quests=[],
    )
    goals = _sections(data)[BOARD_HEADING]
    assert "**Server Buzz** — *every word keeps the fire crackling*" in goals
    assert "**Manual goal**\n" in goals  # no dangling "—" without flavor


def test_sections_in_order_without_the_pulse():
    # Billy, 2026-09-26: Today's Pulse came off; the earners, the board and
    # the feed stay, and the tables join only on a week with play.
    data = LeaderboardData(
        top_earners=[(1, 10)],
        community=[CommunityGoal("Goal", 1, 10, completed=False, settled=False)],
        quests=[QuestLine("daily", "Q", 5, 0)],
    )
    assert list(_sections(data)) == [TOP_EARNERS_HEADING, BOARD_HEADING, FEED_HEADING]


def test_embed_quest_board_summarizes_per_cadence():
    # Members draw personal boards, so the panel shows one summary line per
    # cadence (draw count vs pool, reward range) — never the full pool.
    quests = [
        QuestLine("daily", f"Quest {i}", reward=40 if i == 0 else 5, reward_xp=0)
        for i in range(14)
    ] + [
        QuestLine("weekly", "Solo weekly", reward=30, reward_xp=0),
        QuestLine("monthly", "Big month", reward=100, reward_xp=0),
    ]
    settings = EconSettings(quest_board_daily=3)
    board = _sections(LeaderboardData([], [], quests), settings)[BOARD_HEADING]
    assert "`Daily    3 yours · pool 14` 🪙 5–40 each" in board
    # A pool smaller than the configured size clamps to the pool.
    assert "`Weekly   1 yours · pool 1 ` 🪙 30 each" in board
    # Monthly has no board size at all: it became one guild-wide goal, so even
    # a hand-built monthly quest line can never summarise as a personal draw.
    assert "Monthly" not in board
    # No individual titles leak into the summary.
    assert "Quest 0" not in board and "Solo weekly" not in board
    # Points at the panel's button, not the command the explainer used to name.
    assert "reshuffle each reset" in board and "Show My Quests" in board


def test_embed_quest_board_omits_event_quests():
    # "Anytime" (event) quests are a surprise payout, not a proactively-listed
    # menu — the board summary never gets an "Anytime" section for them.
    quests = [
        QuestLine("daily", "Chatter", reward=5, reward_xp=0),
        QuestLine("event", "Secret Santa", reward=25, reward_xp=10),
    ]
    board = _sections(LeaderboardData([], [], quests))[BOARD_HEADING]
    assert "Anytime" not in board
    assert "Secret Santa" not in board


def test_empty_states():
    sections = _sections(LeaderboardData([], [], []))
    assert "be the first" in sections[TOP_EARNERS_HEADING]
    assert "No quests running" in sections[BOARD_HEADING]
    assert "Quiet so far today" in sections[FEED_HEADING]
    # Goals get no heading of their own — they share the board's.
    assert not any(h == "🎯 Community goals" for h in sections)
    # The old "/bank quests … /bank wallet …" explainer never comes back:
    # the panel's buttons are the affordance.
    assert not any("/bank wallet" in v for v in sections.values())


# ── settings round-trip ─────────────────────────────────────────────────────


def test_panel_ids_round_trip(db):
    """The merged panel stores its ids in the *guide* pair (2026-08-18).

    Named for the guide because that is the message the merge kept, not
    because the guide is what the panel renders.
    """
    with open_db(db) as conn:
        save_econ_settings(
            conn,
            GUILD_ID,
            {"guide_channel_id": 123, "guide_message_id": 456},
        )
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.guide_channel_id == 123
    assert settings.guide_message_id == 456


# ── posting the panel ───────────────────────────────────────────────────────


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db_path=db, open_db=lambda: open_db(db))


@pytest.fixture(autouse=True)
def _patch_accents():
    with (
        patch(
            "bot_modules.core.branding.resolve_accent_color",
            new=AsyncMock(return_value=discord.Color(0x123456)),
        ),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.economy.economy_cog import EconomyCog

    _bot = MagicMock()
    _bot.ctx = ctx
    return EconomyCog(_bot)


def _channel(channel_id: int) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    ch.send = AsyncMock(return_value=MagicMock(id=8888))
    ch.fetch_message = AsyncMock()
    # core.sticky edits and deletes through a partial message — one REST call
    # instead of fetch + edit.
    partial = MagicMock()
    partial.edit = AsyncMock()
    partial.delete = AsyncMock()
    ch.get_partial_message = MagicMock(return_value=partial)
    return ch


def _admin() -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = 500
    m.guild_permissions = MagicMock(administrator=True)
    m.roles = []
    return m


def _stored(db) -> tuple[int, int]:
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    return s.guide_channel_id, s.guide_message_id


def _layout_text(view) -> str:
    return "\n".join(
        i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)
    )


@pytest.mark.asyncio
async def test_post_leaderboard_posts_and_saves_ids(ctx, db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": True})
        # The command windows on wall-clock time, so seed at real now.
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (GUILD_ID, 42, 99, "quest", time.time()),
        )
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    interaction.user = _admin()
    interaction.channel = channel

    await cog.post_economy_panel(interaction.guild, channel)

    text = _layout_text(channel.send.await_args.kwargs["view"])
    assert "🪙 `99`" in text
    assert "User 42" in text  # not a guild member, known_users empty → fallback
    assert _stored(db) == (CHANNEL_ID, 8888)


# Edit-in-place, moving channel, a 403, and the disabled gate are the same one
# method for the same one panel since the merge; they live with the ids they
# read, in tests/economy/test_economy_guide.py. What stays here is what is specific to
# this half: the content the panel renders, and the loops that refresh it.


# ── hourly loop refresh ─────────────────────────────────────────────────────


def _loop_world(ctx, guild):
    """A bot whose ``get_cog`` returns a real EconomyCog.

    ``run_guild_leaderboard`` delegates to that cog's ``StickyPanel`` now, so
    there is one renderer and one per-guild lock shared with the sticky repost
    and the dashboard poster — rather than the second hand-rolled ``refresh``
    this used to be (2026-08-06 review, F9).
    """
    from bot_modules.economy.economy_cog import EconomyCog

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    bot.ctx = ctx
    cog = EconomyCog(bot)
    bot.get_cog = MagicMock(return_value=cog)
    return bot, cog


def _seed_panel(db, *, message_id: int = 4444) -> None:
    with open_db(db) as conn:
        save_econ_settings(
            conn,
            GUILD_ID,
            {
                "enabled": True,
                "guide_channel_id": CHANNEL_ID,
                "guide_message_id": message_id,
            },
        )


@pytest.mark.asyncio
async def test_loop_refresh_edits_panel_in_place(db, ctx):
    channel = _channel(CHANNEL_ID)
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    _seed_panel(db)
    with open_db(db) as conn:
        _credit(conn, 42, 30)
    bot, _cog = _loop_world(ctx, guild)

    # The panel's build callback sources its own clock (which is why
    # run_guild_leaderboard no longer takes now_ts seriously), so the ledger's
    # week window has to be measured against the same one.
    with patch("bot_modules.economy.economy_cog.time.time", return_value=NOW):
        await run_guild_leaderboard(bot, db, GUILD_ID, NOW)

    partial = channel.get_partial_message.return_value
    text = _layout_text(partial.edit.await_args.kwargs["view"])
    assert "🪙 `30`" in text
    channel.send.assert_not_awaited()  # an edit never moves the panel


@pytest.mark.asyncio
async def test_loop_refresh_skips_without_panel(db, ctx):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": True})
    channel = _channel(CHANNEL_ID)
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    bot, _cog = _loop_world(ctx, guild)

    await run_guild_leaderboard(bot, db, GUILD_ID, NOW)

    # load_ids reports (0, 0) for an unposted panel, so nothing is issued.
    channel.get_partial_message.assert_not_called()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_refresh_clears_ids_when_panel_deleted(db, ctx):
    """Deleting the message is how staff retire *this* panel, so a 404 must
    clear the ids rather than repost — which is why the loop passes
    ``repost_if_missing=False`` (core's default heals instead)."""
    channel = _channel(CHANNEL_ID)
    channel.get_partial_message.return_value.edit.side_effect = discord.NotFound(
        MagicMock(status=404), "gone"
    )
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    _seed_panel(db)
    bot, _cog = _loop_world(ctx, guild)

    await run_guild_leaderboard(bot, db, GUILD_ID, NOW)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.guide_channel_id == 0
    assert settings.guide_message_id == 0
    channel.send.assert_not_awaited()  # retired, not resurrected


@pytest.mark.asyncio
async def test_loop_refresh_keeps_ids_on_transient_error(db, ctx):
    channel = _channel(CHANNEL_ID)
    channel.get_partial_message.return_value.edit.side_effect = (
        discord.HTTPException(MagicMock(status=500), "boom")
    )
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    _seed_panel(db)
    bot, cog = _loop_world(ctx, guild)

    await run_guild_leaderboard(bot, db, GUILD_ID, NOW)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.guide_message_id == 4444  # untouched, retried next tick
    # And the guild is queued, so a caller with a loop can re-apply the edit.
    assert cog.economy_panel.take_retries() == {GUILD_ID}


def test_embed_auto_goal_tier_marker():
    data = LeaderboardData(
        top_earners=[],
        community=[
            # 75/100 on an auto weekly = tiers 1+2 banked mid-run.
            CommunityGoal(
                "Weekly", 75, 100, completed=False, settled=False,
                auto=True, tiers=2,
            ),
        ],
        quests=[],
    )
    goals = _sections(data)[BOARD_HEADING]
    assert goals is not None
    assert "🏁 tier 2/3 secured" in goals


# ── live content: pulse, feed, clocks ───────────────────────────────────────


def test_earner_deltas_feed_and_reset_clocks():
    data = LeaderboardData(
        top_earners=[(1, 120), (2, 80)],
        community=[],
        quests=[QuestLine("daily", "Chatter", 5, 0)],
        today_by_user={1: 40},
        feed=(
            FeedLine("Say hi", 3, NOW - 60),
            FeedLine("Bump us", 1, NOW - 600),
        ),
        set_bonuses_today=1,
        day_roll_ts=NOW + 3600,
        week_roll_ts=NOW + 7200,
    )
    sections = _sections(data, names={1: "Alice", 2: "Bob"})

    top = sections[TOP_EARNERS_HEADING]
    # name and amount are fixed-width cells so the columns align
    assert "🥇 `Alice` 🪙 `120` (+40 today)" in top
    assert top.splitlines()[1] == "🥈 `Bob  ` 🪙 ` 80`"  # padded, no delta

    # The reset clocks outlived the pulse section: they sit under the board
    # they reset, and the "R" of a relative timestamp survives the capital.
    board = sections[BOARD_HEADING]
    assert (
        f"Dailies reset <t:{int(NOW + 3600)}:R> · "
        f"new weeklies <t:{int(NOW + 7200)}:R>. Press **Show My Quests** below to see yours."
    ) in board

    feed = sections[FEED_HEADING]
    assert f"✅ `Say hi ` ×3 · <t:{int(NOW - 60)}:R>" in feed
    assert f"✅ `Bump us` ×1 · <t:{int(NOW - 600)}:R>" in feed
    assert "🎁 Full-board bonus paid ×1 today" in feed


def test_tables_section_names_the_week_winners():
    data = LeaderboardData(
        [], [], [], casino_biggest=(1, 3000), casino_luckiest=(2, 1250),
    )
    tables = _sections(data, names={1: "Alice", 2: "Bob"})[TABLES_HEADING]
    assert "💥 Biggest win: **Alice** — 🪙 **3,000** in one play" in tables
    assert "🍀 Luckiest hit: **Bob** — **12.5×** their bet" in tables


@pytest.mark.parametrize(
    "kwargs, expect, absent",
    [
        pytest.param(
            dict(raffle_on=True, raffle_tickets=7, raffle_entrants=3,
                 last_winner_id=1, last_winner_week="2026-W39"),
            ["**7** ticket(s) from **3** entrant(s)", "🏆 Last winner: **Alice** (2026-W39)"],
            [],
            id="winner-named-not-mentioned",
        ),
        pytest.param(
            dict(raffle_on=True, raffle_tickets=0, raffle_entrants=0),
            ["**0** ticket(s)"], ["Last winner"], id="no-draw-yet",
        ),
        pytest.param(dict(raffle_on=False, last_winner_id=1), None, None, id="off-hides"),
    ],
)
def test_raffle_section_names_the_last_winner(kwargs, expect, absent):
    secs = _sections(LeaderboardData([], [], [], **kwargs), names={1: "Alice"})
    if expect is None:
        assert RAFFLE_HEADING not in secs
        return
    body = secs[RAFFLE_HEADING]
    assert "<@" not in body
    assert all(e in body for e in expect)
    assert not any(a in body for a in absent)


def test_embed_community_pace_crowd_and_deadline():
    goal = CommunityGoal(
        "Bump week", 30, 70, completed=False, settled=False,
        auto=True, tiers=1, contributors=4, today_delta=12,
        on_track=False, ends_ts=NOW + 5000,
    )
    goals = _sections(LeaderboardData([], [goal], []))[BOARD_HEADING]
    assert "🏁 tier 1/3 secured" in goals
    assert "👥 4 contributing" in goals and "+12 today" in goals
    assert f"ends <t:{int(NOW + 5000)}:R>" in goals


@pytest.mark.parametrize(
    "completed, settled, shown",
    [
        pytest.param(False, False, True, id="running-shows-where"),
        # A finished goal drops the address: inviting people to chip in to
        # something already paid out is the same mistake the login digest
        # avoids by blanking a done quest's blurb.
        pytest.param(True, False, False, id="complete-drops-it"),
        pytest.param(True, True, False, id="settled-drops-it"),
    ],
)
def test_embed_community_goal_names_its_channel(completed, settled, shown):
    goal = CommunityGoal(
        "Picture week", 30, 70, completed=completed, settled=settled,
        auto=True, tiers=1, contributors=4, channel_id=555,
    )
    goals = _sections(LeaderboardData([], [goal], []))[BOARD_HEADING]
    assert ("📍 Only counts in <#555>" in goals) is shown


def test_embed_community_goal_with_no_channel_says_nothing():
    """An unscoped goal gets no line at all — not "any channel"."""
    goal = CommunityGoal(
        "Server buzz", 30, 70, completed=False, settled=False,
        auto=True, contributors=4,
    )
    goals = _sections(LeaderboardData([], [goal], []))[BOARD_HEADING]
    assert "Only counts in" not in goals
    assert "<#" not in goals


def test_embed_spotlight_gets_countdown():
    data = LeaderboardData(
        top_earners=[],
        community=[],
        quests=[QuestLine("daily", "Chatter", 5, 0, spotlight=True)],
        spotlight_kind="message_sent",
        spotlight_label="Send messages",
        week_roll_ts=NOW + 7200,
    )
    board = _sections(data)[BOARD_HEADING]
    assert f"pays **double** — until <t:{int(NOW + 7200)}:R>!" in board
    # The banner names the doubled kind; the summary no longer lists titles.
    assert "Chatter" not in board


# ── live signal + debounced refresh loop ────────────────────────────────────


def test_live_signal_debounce_and_requeue():
    live_signal.mark_dirty(GUILD_ID)
    live_signal.mark_dirty(GUILD_ID)  # coalesces
    assert live_signal.take_ready(NOW, 120.0) == [GUILD_ID]
    assert live_signal.take_ready(NOW + 1, 120.0) == []  # clock restarted

    live_signal.mark_dirty(GUILD_ID)
    assert live_signal.take_ready(NOW + 60, 120.0) == []  # still cooling
    assert live_signal.pending_count() == 1  # kept pending, not dropped
    assert live_signal.take_ready(NOW + 121, 120.0) == [GUILD_ID]


def test_apply_credit_marks_guild_dirty(db):
    from bot_modules.economy.service import apply_credit

    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 1, 10, "quest")
    assert live_signal.take_ready(NOW, 0.0) == [GUILD_ID]


def test_community_bump_marks_guild_dirty(db):
    with open_db(db) as conn:
        qid = quests_svc.create_quest(
            conn,
            GUILD_ID,
            title="Bump week",
            description="",
            qtype="community",
            reward=25,
            signoff=0,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=70,
            created_by=None,
            trigger_kind="message_sent",
        )
        quests_svc.set_quest_active(conn, GUILD_ID, qid, True)
        live_signal.reset()
        quests_svc._bump_community_kind(conn, GUILD_ID, "message_sent", 1, None)
    assert live_signal.pending_count() == 1


def test_dashboard_progress_edit_marks_guild_dirty(db):
    with open_db(db) as conn:
        qid = _quest(conn, qtype="community", title="Manual goal", target=100)
        live_signal.reset()
        quests_svc.set_community_progress(conn, qid, 10, target=100)
    assert live_signal.pending_count() == 1


@pytest.mark.asyncio
async def test_live_tick_refreshes_dirty_guild_with_debounce(db, ctx):
    channel = _channel(CHANNEL_ID)
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    _seed_panel(db)
    bot, _cog = _loop_world(ctx, guild)
    edit = channel.get_partial_message.return_value.edit
    live_signal.reset()

    await run_live_tick(bot, db, NOW)  # nothing dirty → no edit
    edit.assert_not_awaited()

    live_signal.mark_dirty(GUILD_ID)
    await run_live_tick(bot, db, NOW)
    edit.assert_awaited_once()

    live_signal.mark_dirty(GUILD_ID)  # burst right after the repaint
    await run_live_tick(bot, db, NOW + 30)
    edit.assert_awaited_once()  # debounce holds it back

    await run_live_tick(bot, db, NOW + 130)
    assert edit.await_count == 2  # lands once the cooldown is up


@pytest.mark.asyncio
async def test_live_tick_survives_refresh_failure(db, ctx):
    channel = _channel(CHANNEL_ID)
    channel.get_partial_message.return_value.edit.side_effect = RuntimeError(
        "gateway hiccup"
    )
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    _seed_panel(db)
    bot, _cog = _loop_world(ctx, guild)
    live_signal.reset()
    live_signal.mark_dirty(GUILD_ID)

    await run_live_tick(bot, db, NOW)  # must not raise

    assert live_signal.pending_count() == 0  # consumed; hourly tick backstops


# ── Night at the Tables (casino fancy round) ────────────────────────────────


def test_collect_casino_week_highlights(db):
    from bot_modules.core.db_utils import get_tz_offset_hours as _tz
    from bot_modules.economy.quests import iso_week_for
    from bot_modules.casino.service import record_play

    with open_db(db) as conn:
        offset = _tz(conn, GUILD_ID)
        record_play(conn, GUILD_ID, 11, "slots", 10, 150, now=NOW)
        record_play(conn, GUILD_ID, 12, "roulette", 10, 360, now=NOW)
        record_play(conn, GUILD_ID, 13, "coinflip", 200, 380, now=NOW)
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    # biggest single return is 13's 380; luckiest multiplier is 12's 36x
    assert data.casino_biggest == (13, 380)
    assert data.casino_luckiest == (12, 3600)
    # sanity: the week key used matches the collector's clock
    assert iso_week_for(local_day_for(NOW, offset))


def test_collect_casino_section_absent_without_play(db):
    with open_db(db) as conn:
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)
    assert data.casino_biggest is None
    assert data.casino_luckiest is None
