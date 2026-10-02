"""A new guild-scoped table ships with a ``guild_id``-leading index.

Every guild-scoped table is read with ``WHERE guild_id = ?`` and swept the
same way when a server leaves (``guild_purge_service``). Without an index
whose *first* column is ``guild_id``, both are full scans — fine on the day
the table is added, and the reason a query goes quadratic a year later, when
nobody remembers the table was born without one.

**The exemption is derived, not listed.** Twenty-six of these tables declare
``guild_id INTEGER PRIMARY KEY``: one row per guild, and ``guild_id`` *is* the
rowid, so lookups are already btree seeks and a separate index would be dead
weight. ``PRAGMA index_list`` cannot see a rowid alias, which is what made the
standards audit count them as offenders — row 1.12's "65", then "64", was
mostly these. A hand-written exempt list would have gone stale the first time
someone added a config table; this test works the rule out from the schema.

**Born against a measured baseline, shrink-only.** Twenty-nine tables lack an
index today and are pinned below, so the count can only fall. They are pinned
rather than fixed because the fix would be worse. Measured read-only against
production on 2026-09-21: twenty-seven of the twenty-nine hold under 500 rows,
and eleven carry no index of any kind. Twenty-nine indexes over tables that
size buy nothing and cost a write on every insert. The rule earns its keep on
the *next* table, not on these.

**The two that aren't small were checked one at a time**, because pinning the
whole set in one breath is how a real problem gets filed under housekeeping.
``econ_quest_claims`` holds 7,023 rows and grows ~100 a day with no retention
sweep; ``econ_drops`` holds 1,008. Both are already indexed for the queries
they actually serve. The dashboard home counts a guild's pending claims on
every page load, and that rides ``idx_econ_quest_claims_pending`` — a
*partial* index on ``state = 'pending'``, which currently holds no entries at
all because no claim is outstanding, and which is bounded by unresolved claims
rather than by the table's size, so the growth curve never reaches it. The
daily departure sweep deletes by ``(guild_id, user_id)`` and rides
``idx_econ_quest_claims_lookup``.

What is left unindexed is ``DELETE ... WHERE guild_id = ?`` — the guild purge,
an operator action. Adding the index this test asks for makes that *worse*,
which is worth stating precisely because the planner's output alone suggests
the opposite: it does switch from ``SCAN`` to ``SEARCH``, but every row in the
table carries the same ``guild_id`` (``sqlite_stat1`` records ``7023 7023``),
so the seek walks the entire index and then dereferences every row. Timed over
prod's own 7,023 rows: purge 3.6 ms without the index, 5.1 ms with it, and
2,000 inserts 2.9 ms against 3.7 ms. A slower purge and a permanent tax on the
write path is not a fix. That is the argument each of the twenty-nine has to
lose before it is pinned — not one excuse offered for all of them at once.

**Partial indexes count, and sixteen tables lean on one.** ``jails`` is
"indexed" here on the strength of ``idx_jails_active``, which is
``(guild_id, status) WHERE status = 'active'`` — so listing a guild's
*released* jails still scans, and a purge of that table scans too, because a
partial index serves only rows matching its predicate. The same holds for
``casino_baccarat_rounds``, ``casino_blackjack_hands``, ``casino_dice_rounds``,
``casino_keno_rounds``, ``casino_mines_hands``, ``casino_race_rounds``,
``casino_roulette_rounds``, ``casino_war_hands``, ``hidden_channels``,
``inactive_members``, ``intake_cards``, ``mahjong_seats``, ``mahjong_tables``,
``music_playlist_unmatched`` and ``promotion_review_cards``. Each of those
indexes is a deliberate uniqueness constraint on the live row, not an
oversight, so counting them as compliant is right — but "196 indexed" means
180 fully and 16 for one query shape.

**What this cannot see.** It checks that an index exists, never that a query
uses it: a table indexed on ``(guild_id, status)`` still scans for a query
that filters on ``status`` alone. It reads the schema the migrations build, so
an index created by a service module at boot would be invisible — that was
true of twenty of them until migration 235, and
``test_schema_authority_contract.py`` is what keeps it from recurring; the two
tests are a pair, and this one's numbers are only trustworthy while that one
is green. Production also carries two orphan tables whose creator was deleted
(``incident_events``, ``message_velocity_baseline``), which no schema built
from source will ever contain.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.privacy import guild_purge_service as purge
from tests.db_template import migrated_db

#: Guild-scoped tables with no ``guild_id``-leading index, measured 2026-09-21.
#: Shrink only. Adding a name here is not a fix — it is a claim that this
#: table will never be queried per guild, which is false for anything
#: ``guild_purge_service`` sweeps, i.e. all of them.
BASELINE = {
    "announcements",
    "bio_answers",
    "bio_field_values",
    "casino_baccarat_bets",
    "casino_dice_bets",
    "casino_keno_bets",
    "casino_pools_bets",
    "casino_race_bets",
    "casino_roulette_bets",
    "chicken_games",
    "duel_nicks",
    "econ_auction_bids",
    "econ_bounty_contributions",
    "econ_drops",
    "econ_quest_claims",
    "games_active_games",
    "games_scheduled",
    "hot_potato_games",
    "hp_group_games",
    "mc_games",
    "policy_ballot_votes",
    "qa_verdicts",
    "quickdraw_games",
    "risky_active_rounds",
    "risky_pending_questions",
    "risky_posted_questions",
    "survivor_picks",
    "survivor_players",
    "transcripts",
}


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    db = migrated_db(tmp_path_factory.mktemp("guild-index") / "schema.db", reap=False)
    c = sqlite3.connect(db)
    try:
        yield c
    finally:
        c.close()


def _leading_column(conn: sqlite3.Connection, index: str) -> str | None:
    rows = sorted(conn.execute(f"PRAGMA index_info({index!r})"), key=lambda r: r[0])
    return rows[0][2] if rows else None


def _has_guild_leading_index(conn: sqlite3.Connection, table: str) -> bool:
    """Any index — including a PRIMARY KEY or UNIQUE autoindex — starting at ``guild_id``."""
    return any(
        _leading_column(conn, name) == "guild_id"
        for _seq, name, _unique, _origin, _partial in conn.execute(
            f"PRAGMA index_list({table!r})"
        )
    )


def _guild_id_is_the_rowid(conn: sqlite3.Connection, table: str) -> bool:
    """``guild_id INTEGER PRIMARY KEY`` on a rowid table: already a btree key.

    All three conditions matter. A composite primary key is not an alias, a
    ``guild_id TEXT PRIMARY KEY`` is not one either (SQLite aliases the rowid
    only for a declared ``INTEGER``), and in a ``WITHOUT ROWID`` table there is
    no rowid to alias — which is why this asks the database rather than
    pattern-matching the DDL. None of those three is then *reported*: each one
    gets a real autoindex leading with ``guild_id``, so the index check above
    passes them. Only a table whose key starts somewhere else — ``PRIMARY KEY
    (channel_id, guild_id)`` — falls through both and is named.
    """
    info = list(conn.execute(f"PRAGMA table_info({table!r})"))
    keys = [row for row in info if row[5]]
    if len(keys) != 1 or keys[0][1] != "guild_id":
        return False
    if (keys[0][2] or "").upper() != "INTEGER":
        return False
    try:
        conn.execute(f"SELECT rowid FROM {table} LIMIT 0")
    except sqlite3.OperationalError:
        return False
    return True


def _unindexed(conn: sqlite3.Connection) -> set[str]:
    return {
        table
        for table in purge.guild_scoped_tables(conn)
        if not _has_guild_leading_index(conn, table)
        and not _guild_id_is_the_rowid(conn, table)
    }


def test_new_guild_scoped_tables_are_indexed_on_guild_id(conn):
    offenders = sorted(_unindexed(conn) - BASELINE)
    assert not offenders, (
        "These carry a guild_id and no index leading with it, so every "
        f"per-guild read and the guild purge sweep full-scan them: {offenders}. "
        "Add `CREATE INDEX IF NOT EXISTS idx_<table>_guild ON <table> "
        "(guild_id, <whatever you filter on next>)` to the migration that "
        "creates the table. If the table is one row per guild, declare "
        "`guild_id INTEGER PRIMARY KEY` instead and it is exempt automatically."
    )


def test_baseline_only_shrinks(conn):
    """An indexed table left pinned makes the count lie about how much is left."""
    stale = sorted(BASELINE - _unindexed(conn))
    assert not stale, f"Now indexed, or gone — remove from BASELINE: {stale}"


def test_the_rowid_alias_exemption_matches_the_schema(conn):
    """Every exempted table really is one row per guild.

    The exemption is the part that could silently widen: a mistake in
    ``_guild_id_is_the_rowid`` excuses tables instead of failing them, and a
    test that excuses too much passes quietly forever.
    """
    exempt = [t for t in purge.guild_scoped_tables(conn) if _guild_id_is_the_rowid(conn, t)]
    assert exempt, "the rowid-alias rule matched nothing — has it broken?"
    for table in exempt:
        keys = [row[1] for row in conn.execute(f"PRAGMA table_info({table!r})") if row[5]]
        assert keys == ["guild_id"], f"{table} was exempted with primary key {keys}"
