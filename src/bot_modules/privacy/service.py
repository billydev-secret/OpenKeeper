"""Privacy paths: erasure, anonymisation, and subject access.

Three answers to "this member's data must stop being theirs", and they are not
interchangeable:

- :func:`purge_user_data` — hard erasure (Art 17). Destroys the rows.
- :func:`anonymize_user_data` — rewrites the member id to a small surrogate and
  destroys the names, so **the activity statistics survive intact** while the
  data stops identifying anyone. The answer for a departed member when the
  server still needs to be comparable against its own past.
  :func:`anonymize_user_guild_data` is the same rewrite narrowed to one guild,
  for a member who left one server but is still in another.
- :func:`export_user_data` — the read half (Art 15/20), deliberately a superset
  of the purge.

Extracted from privacy_cog for testability. Operator procedures for all three
live in ``docs/gdpr_runbook.md``.
"""

from __future__ import annotations

import logging
import json
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import islice

from bot_modules.privacy import guild_purge_service
from bot_modules.economy.service import (
    apply_debit as econ_apply_debit,
    get_balance as econ_get_balance,
)
from bot_modules.economy.wager_service import (
    refund_game as wager_refund_game,
)

log = logging.getLogger("dungeonkeeper.privacy")

# SQLite's default variable cap is 32,766; stay far below it so the purge can
# never fail on a heavy poster (the accounts most likely to file an erasure
# request are exactly the ones with the most rows — 2026-08 review, A1).
_ID_CHUNK = 500

# Children of ``messages``, keyed on message_id rather than on the author. They
# carry no subject column of their own, so the export's column-discovery pass
# cannot find them — both the purge and the export reach them by joining
# through the author's message ids. Shared so the two can never drift.
_MESSAGE_CHILD_TABLES = (
    "message_mentions",
    "message_reactions",
    "message_sentiment",
)


def _chunks(ids: list[int], size: int = _ID_CHUNK):
    it = iter(ids)
    while chunk := list(islice(it, size)):
        yield chunk


def _delete(
    conn: sqlite3.Connection, sql: str, params: tuple, *, table: str
) -> None:
    """One tolerated delete: schema drift (a table missing on an older guild
    deployment) logs and moves on rather than aborting the erasure midway."""
    try:
        conn.execute(sql, params)
    except sqlite3.Error as exc:
        log.warning("Purge: failed on %s (%s)", table, exc)


def _scrub(
    conn: sqlite3.Connection, sql: str, params: tuple, *, table: str
) -> None:
    """One tolerated anonymising UPDATE.

    Same schema-drift tolerance as ``_delete``; separate name because erasing a
    member *from* a shared row is a different act from removing the row, and a
    reader scanning this file for what the purge deletes should not have to
    read the SQL to find out that these two are not deletions.
    """
    _delete(conn, sql, params, table=table)


#: The games family's own records (review H1, 2026-09-14):
#: ``(table, key, seat columns, JSON columns, also reaches guild_id 0)``.
#: The shared record's two tables take ``guild_id = 0`` too — rows archived
#: before migration 204 gave them a guild, which a guild-scoped match never
#: reaches; the match is on the member's own id, so those rows are theirs.
_GAME_RECORD_TABLES: tuple[
    tuple[str, str, tuple[str, ...], tuple[str, ...], bool], ...
] = (
    ("hot_potato_games", "id",
     ("challenger_id", "target_id", "holder_id", "winner_id", "loser_id"),
     ("pass_log",), False),
    ("pressure_games", "id",
     ("challenger_id", "target_id", "active_player", "winner_id", "loser_id"),
     ("pumps_json",), False),
    ("quickdraw_games", "id",
     ("challenger_id", "target_id", "winner_id", "loser_id"), (), False),
    ("mc_games", "id", ("host_id", "winner_id", "loser_id"),
     ("roster", "alive", "elimination_order", "seated"), False),
    ("chicken_games", "id", ("host_id", "winner_id", "loser_id"),
     ("roster", "alive", "elimination_order", "bail_log"), False),
    ("games_active_games", "game_id", ("host_id",), ("payload",), True),
    ("games_game_history", "history_id", ("host_id",), ("payload",), True),
)


def _delete_game_records(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> None:
    """Delete every game record naming *user_id* in a seat or inside its JSON.

    A record goes whole — the group Hot Potato call: a game with a hole in it
    is incoherent, and the alternative is leaving the id in the JSON. The JSON
    match is digit-bounded, so a list entry, a nested ``holder_id`` and a
    ``scores`` key keyed by the id all count while 10011 never matches 1001.
    ``instr`` is only the prefilter; the regex is the answer.
    """
    needle = str(user_id)
    token = re.compile(rf"(?<!\d){needle}(?!\d)")
    for table, key, seats, json_cols, reaches_guild_zero in _GAME_RECORD_TABLES:
        guilds = (guild_id, 0) if reaches_guild_zero else (guild_id,)
        seat_sql = " OR ".join(f"{c} = ?" for c in seats)
        text_sql = " OR ".join(f"instr(COALESCE({c}, ''), ?) > 0" for c in json_cols)
        match_sql = " OR ".join(s for s in (seat_sql, text_sql) if s)
        try:
            rows = conn.execute(
                f"SELECT {key}, ({seat_sql}), {', '.join(json_cols) or 'NULL'} "
                f"FROM {table} WHERE guild_id IN ({', '.join('?' * len(guilds))}) "
                f"AND ({match_sql})",
                (*[user_id] * len(seats), *guilds,
                 *[user_id] * len(seats), *[needle] * len(json_cols)),
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("Purge: failed on %s (%s)", table, exc)
            continue
        doomed = [
            row[0] for row in rows
            if row[1] or any(v is not None and token.search(str(v)) for v in row[2:])
        ]
        for chunk in _chunks(doomed):
            _delete(
                conn,
                f"DELETE FROM {table} WHERE {key} IN ({', '.join('?' * len(chunk))})",
                tuple(chunk),
                table=table,
            )


@dataclass(frozen=True)
class PurgeResult:
    """What one hard erasure did: the message rows it deleted and the total
    number of rows it deleted or scrubbed across every table."""

    message_rows: int
    total_rows: int


def purge_user_data_counted(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    keep_messages: bool = False,
) -> PurgeResult:
    """:func:`purge_user_data`, also reporting the rows it changed in total.

    ``purge_user_data`` returns only the message-row count, so a caller that
    wanted to say how much was erased could only report messages. The total is
    SQLite's own count of rows changed on this connection across the call
    (every DELETE, plus the UPDATEs that scrub a member out of a shared row),
    so it needs no per-table bookkeeping and cannot drift from the statements.
    The caller owns the transaction, as for ``purge_user_data``.
    """
    before = conn.total_changes
    message_rows = purge_user_data(
        conn, guild_id, user_id, keep_messages=keep_messages
    )
    return PurgeResult(
        # With keep_messages the message rows were counted but left in place;
        # report only what was erased, so the share never exceeds the total.
        message_rows=0 if keep_messages else message_rows,
        total_rows=conn.total_changes - before,
    )


def purge_user_data(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    keep_messages: bool = False,
) -> int:
    """Delete all DB records for *user_id* in *guild_id*. Returns the count of
    message rows that exist for the user (and were removed unless
    *keep_messages* is set).

    NOTE: this is the genuine hard-erasure path and is deliberately **not wired
    to any command** — ``/mydata`` only clears the member's Discord messages
    (and, store by store, what they can erase themselves) and otherwise
    retains server-side data for moderation. Its surface is the dashboard's Member
    Data panel (``web_server/routes/privacy.py``, admin only), which shows the
    export's counts as a preview first; the same run by hand, and every
    procedure around it, lives in ``docs/gdpr_runbook.md``.

    *keep_messages*: when True, the messages table and its child tables
    (mentions, reactions, sentiment, processed_messages)
    are left untouched. It is the Member Data panel's Erase-tab checkbox
    ("Keep their message activity log"), passed through the purge route;
    ``/mydata`` never reaches this function. Other PII (XP, activity,
    profile, wellness) is still cleared.

    Only a full erasure reaches this function — a partial ``mode`` scrub skips
    the purge entirely rather than passing flags here.

    Every per-table delete tolerates schema drift (logged warning, sweep
    continues). The caller owns the transaction: run this on one connection
    and commit at the end, so a hard failure rolls the whole erasure back
    instead of leaving partial state.
    """
    msg_ids = [
        r[0]
        for r in conn.execute(
            "SELECT message_id FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        ).fetchall()
    ]

    if msg_ids and not keep_messages:
        # Chunked: one IN (…) per _ID_CHUNK ids, so a heavy poster can never
        # blow SQLite's bound-variable cap mid-erasure.
        for chunk in _chunks(msg_ids):
            ph = ",".join("?" * len(chunk))
            for table in _MESSAGE_CHILD_TABLES:
                _delete(
                    conn,
                    f"DELETE FROM {table} WHERE message_id IN ({ph})",
                    tuple(chunk),
                    table=table,
                )

        _delete(
            conn,
            "DELETE FROM processed_messages WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table="processed_messages",
        )
        _delete(
            conn,
            "DELETE FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
            table="messages",
        )

    for table in (
        "member_xp",
        "voice_sessions",
        "member_activity",
        "member_events",
        # Slash-command and dashboard-panel usage telemetry. Retained
        # indefinitely for reporting (no routine pruning), so this hard-erasure
        # path is the only thing that clears it.
        "usage_events",
        "known_users",
        "xp_events",
        # The retired daily aggregate of xp_events (migration 186; no writer
        # or reader since 2026-09-30, kept until a migration drops it). Purged
        # with its source rather than preserved: it is the same per-member
        # XP, only summed.
        "xp_daily",
        # Added by the 2026-08 review (previously missed — register rows):
        "xp_reaction_awards",
        "member_birthdays",
        "voice_master_profiles",
        "bios",
        "bio_answers",
        "bio_field_values",
        # Guess consent evidence. Kept through an optout (the record that
        # consent was held is the point), but a full erasure clears it.
        "guess_consents",
        # Pen Pals pool movement (migration 160). Operational history — who
        # joined the matching pool and why they left it. Unlike
        # pen_pals_sessions (the no-repeat memory) and pen_pals_blocks (a
        # protective record), nothing reads this back, so there is no ground
        # to hold it against an erasure request.
        "pen_pals_pool_events",
        # Pen Pals opt-out (migration 174). A bare preference — the state
        # behind a Leave Pool press — held in its own table only because the
        # pool row it belongs to keeps being deleted. Nothing but the requeue
        # paths and _eligible_pool reads it, and an erasure means the member
        # wants their record gone, not curated. Note this leaves an erased
        # member's preserved session able to re-pool them at expiry (the pool
        # and session rows survive the purge by an older decision, register
        # row above) — accepted: an erasure is an out-of-band operator act,
        # and one Leave press puts the flag back.
        "pen_pals_optouts",
        # ── Added by the 2026-09-02 GDPR review ──────────────────────────
        # Found by `scripts/privacy_coverage.py`, which reads a live database
        # and reports every table whose column *values* are real member ids.
        # Each of these named a member and had neither a purge statement nor a
        # register row explaining a preserve, which is the combination the
        # register exists to make impossible. Register rows land with them.
        #
        # Who starred a message and who was starred. The starboard post itself
        # is an ordinary Discord message the purge cannot reach, the same
        # documented limit as a published Flash Theme announcement.
        "starboard_reactors",
        # Members held back from, or exempted from, an inactivity sweep. Bare
        # preferences with no Art 17(3) ground; note the pen_pals_optouts
        # consequence applies here too — erasing the exemption exposes the
        # member to the next sweep, and re-adding it is one mod action.
        "inactivity_prune_exceptions",
        "inactive_sweep_exemptions",
        # Which roles the member picked from a role menu, and the grant log.
        "role_menu_grants",
        # The level-5 congratulation card. Part of the XP family, which is
        # purged in full.
        "xp_level_5_cards",
        # Queued "this member is ready for promotion" posts.
        "pending_promotion_posts",
        # That the member's birthday was announced on a given date — the
        # dedup marker behind `member_birthdays`, which is already purged.
        "birthday_announcements",
        # Which member bumped the server on a listing site.
        "bump_tracker_log",
        # A queued departure auto-anonymise (migration 231). Nothing to keep:
        # an erasure goes further than the anonymisation the row is waiting
        # for, and a surviving row would hold the real id after it.
        "departure_erasure_queue",
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )

    # Anonymous-features audit trail. Keyed on actor_id/target_id rather than
    # user_id, so it needs its own statements. Routinely pruned by the
    # retention sweep (default 90 days), but a hard-erasure request must not
    # have to wait for that — these rows are precisely the deanonymising ones.
    for col in ("actor_id", "target_id"):
        _delete(
            conn,
            f"DELETE FROM anon_audit_log WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
            table=f"anon_audit_log.{col}",
        )
    _delete(
        conn,
        "DELETE FROM role_events WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="role_events",
    )

    # Risky Rolls (migration 019): every row naming the member goes.
    #
    # These are ephemeral party-game rows — a round deletes itself on close and
    # a posted question is swept at 7 days — and nothing downstream reads them
    # back, so there is no Art 17(3) ground to hold one against an erasure.
    # Rows are deleted WHOLE rather than scrubbed: a round whose winner no
    # longer exists, or a question whose asker was erased, is incoherent state
    # that the cog would then try to re-attach a view to on next boot. That
    # does take an in-flight round away from the other players in it; erasure
    # is rare, out-of-band, and the alternative is retaining the id.
    #
    # `risky_round_rolls` has no `guild_id` and reaches its round by FK, so the
    # game ids are resolved first: this connection is the caller's and does not
    # promise `PRAGMA foreign_keys = ON` (the feature's own store sets it), so
    # the cascade cannot be relied on to take the rolls with the round.
    try:
        risky_game_ids = [
            r[0]
            for r in conn.execute(
                "SELECT game_id FROM risky_active_rounds WHERE guild_id = ? AND ("
                "opener_id = ? OR highest_user = ? OR lowest_user = ? OR "
                "second_lowest_user = ? OR second_highest_user = ? OR game_id IN "
                "(SELECT game_id FROM risky_round_rolls WHERE user_id = ?))",
                (guild_id, user_id, user_id, user_id, user_id, user_id, user_id),
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        log.warning("Purge: failed on risky_active_rounds lookup (%s)", exc)
        risky_game_ids = []

    if risky_game_ids:
        ph = ",".join("?" for _ in risky_game_ids)
        _delete(
            conn,
            f"DELETE FROM risky_round_rolls WHERE game_id IN ({ph})",
            tuple(risky_game_ids),
            table="risky_round_rolls",
        )
        _delete(
            conn,
            f"DELETE FROM risky_active_rounds WHERE game_id IN ({ph})",
            tuple(risky_game_ids),
            table="risky_active_rounds",
        )

    # The CSV columns need exact membership, not a bare LIKE: '%123%' also
    # matches 1234. Wrapping both sides in commas makes ',123,' the needle.
    _delete(
        conn,
        "DELETE FROM risky_pending_questions WHERE guild_id = ? AND ("
        "winner_id = ? OR extra_questioner_id = ? "
        "OR (',' || COALESCE(participant_user_ids, '') || ',') LIKE ? "
        "OR (',' || COALESCE(lowest_tie_user_ids, '') || ',') LIKE ? "
        "OR (',' || COALESCE(questioners_asked, '') || ',') LIKE ?)",
        (guild_id, user_id, user_id, *([f"%,{user_id},%"] * 3)),
        table="risky_pending_questions",
    )

    # question_text is the member's own words, so a posted question they asked
    # goes with them; one they were merely a recipient of names them in
    # allowed_replier_ids and goes too.
    _delete(
        conn,
        "DELETE FROM risky_posted_questions WHERE guild_id = ? AND ("
        "asker_id = ? "
        "OR (',' || COALESCE(allowed_replier_ids, '') || ',') LIKE ?)",
        (guild_id, user_id, f"%,{user_id},%"),
        table="risky_posted_questions",
    )

    # Ping Response (migration 198): the erased member's own role pings.
    # Purged, not preserved — this is an analytics table about how a ping
    # landed, and "we wanted the numbers" is not an Art 17(3) ground. Their
    # pings drop out of the report; everyone else's turnout is unaffected,
    # since turnout is counted from messages and reactions at read time and
    # never denormalized onto the ping row.
    _delete(
        conn,
        "DELETE FROM ping_events WHERE guild_id = ? AND author_id = ?",
        (guild_id, user_id),
        table="ping_events",
    )

    # Survivor (migration 167): purged across every season, live or archived
    # — a game record has no Art 17(3) ground to outlive the member (register
    # rows: docs/data_register.md). guild_id is denormalized onto both tables
    # (so the export's standard guild scoping covers them too); coin
    # movements live in econ_ledger, which is preserved separately.
    for table in ("survivor_picks", "survivor_players"):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )
    # Survivor hosts (migration 221): purged. A host entry is an access
    # grant, not a record anyone relies on — erasing it only revokes the
    # panel and the host DMs. A host the member *added* keeps their entry
    # with added_by zeroed: the grant belongs to the host, and nothing reads
    # added_by back.
    _delete(
        conn,
        "DELETE FROM survivor_hosts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="survivor_hosts",
    )
    _scrub(
        conn,
        "UPDATE survivor_hosts SET added_by = 0 WHERE guild_id = ? AND added_by = ?",
        (guild_id, user_id),
        table="survivor_hosts.added_by",
    )

    # Meadow Mahjong (migration 175; register rows in docs/data_register.md).
    # A LIVE seat can't just be deleted — the table dies with it: close the
    # table (the service's next timer fire sees status != 'live' and stands
    # down), free every seat, and refund all escrow still held for it, so the
    # other players get their coins back rather than a wedged hand. Escrow
    # rides econ_game_wagers at game_id = table_id·100000 + hand_no, so the
    # whole range is swept rather than trusting the state JSON to parse.
    try:
        live_tables = [
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT t.id FROM mahjong_tables t "
                "JOIN mahjong_seats s ON s.table_id = t.id "
                "WHERE t.status = 'live' AND s.guild_id = ? AND s.user_id = ? "
                "AND s.live = 1",
                (guild_id, user_id),
            ).fetchall()
        ]
        for table_id in live_tables:
            for r in conn.execute(
                "SELECT DISTINCT game_id FROM econ_game_wagers "
                "WHERE game_type = 'mahjong' AND state = 'held' "
                "AND game_id >= ? AND game_id < ?",
                (table_id * 100_000, (table_id + 1) * 100_000),
            ).fetchall():
                wager_refund_game(conn, "mahjong", int(r[0]))
            conn.execute(
                "UPDATE mahjong_tables SET status = 'closed', "
                "closed_reason = 'purged', deadline_at = NULL WHERE id = ?",
                (table_id,),
            )
            conn.execute(
                "UPDATE mahjong_seats SET live = 0 WHERE table_id = ?",
                (table_id,),
            )
            # a fill table's house bots just got their escrow refunded —
            # burn the synthetic wallets back so a purge can't strand
            # house coins (bots plan B6; negative user_id = house bot)
            for r in conn.execute(
                "SELECT DISTINCT user_id FROM mahjong_seats "
                "WHERE table_id = ? AND user_id < 0", (table_id,),
            ).fetchall():
                bot_id = int(r[0])
                balance = econ_get_balance(conn, guild_id, bot_id)
                if balance > 0:
                    econ_apply_debit(
                        conn, guild_id, bot_id, balance,
                        "mahjong_house_settle", meta={"table_id": table_id},
                    )
    except sqlite3.Error as exc:
        log.warning("Purge: failed dissolving mahjong tables (%s)", exc)

    # The member's own rows go; results they WON are anonymised instead of
    # deleted (winner_id → NULL) because the row also carries the other
    # seats' hand history via mahjong_result_seats.
    for table in (
        "mahjong_seats",
        "mahjong_result_seats",
        "mahjong_stats",
        "mahjong_prefs",
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )
    _scrub(
        conn,
        "UPDATE mahjong_results SET winner_id = NULL "
        "WHERE guild_id = ? AND winner_id = ?",
        (guild_id, user_id),
        table="mahjong_results.winner_id",
    )
    # A closed table the member sat at (bug #222, item 6). The row itself
    # cannot go: `mahjong_results` is preserved game history and points at it
    # by foreign key, so deleting it would take the other players' hand
    # records with it. What it holds of the member is the `state` snapshot —
    # serialized engine state naming seat member ids, and the documented
    # list-column blind spot — so the snapshot is blanked instead and the
    # shell of the row stands. Nothing reads a closed table's state back: the
    # service refuses any member action on a non-live row, the boot restore
    # and the dashboard both select `status = 'live'`, and the readable
    # history is `mahjong_results`/`mahjong_result_seats`.
    #
    # Matched through `mahjong_seats`, which is deleted just above — so this
    # runs against the seats of a table the member sat at, resolved before
    # that delete lands, and on `host_id` directly for a table they only
    # hosted. Digit-bounded, so a seat id of 1001 never matches 10011.
    try:
        needle = re.compile(rf"(?<!\d){user_id}(?!\d)")
        closed = [
            (int(r[0]), r[1])
            for r in conn.execute(
                "SELECT id, state FROM mahjong_tables "
                "WHERE guild_id = ? AND status != 'live' "
                "AND (host_id = ? OR instr(COALESCE(state, ''), ?) > 0)",
                (guild_id, user_id, str(user_id)),
            ).fetchall()
        ]
        for table_id, state in closed:
            if state is None or needle.search(str(state)):
                _scrub(
                    conn,
                    "UPDATE mahjong_tables SET state = '{}' WHERE id = ?",
                    (table_id,),
                    table="mahjong_tables.state",
                )
    except sqlite3.Error as exc:
        log.warning("Purge: failed blanking closed mahjong state (%s)", exc)
    _scrub(
        conn,
        "UPDATE mahjong_tables SET host_id = 0 "
        "WHERE guild_id = ? AND host_id = ? AND status != 'live'",
        (guild_id, user_id),
        table="mahjong_tables.host_id",
    )

    # ── The five decisions the 2026-09-02 GDPR review left open, settled by
    # Billy on 2026-09-02. Each register row carries the reasoning.

    # Quote cards. A row says "X quoted Y's message" and names two members, so
    # it goes whole from either side — with one of them removed it records
    # nothing coherent. Decided as purge rather than preserve: it is named an
    # audit log, but it logs a decorative feature, not a moderation decision,
    # so the sanctions ground does not reach it.
    _delete(
        conn,
        "DELETE FROM quote_audit_log WHERE guild_id = ? AND "
        "(quoter_id = ? OR quoted_user_id = ?)",
        (guild_id, user_id, user_id),
        table="quote_audit_log",
    )

    # Inactivity records and promotion review cards each name a member in two
    # different roles, and the two roles get different treatment — the same
    # split `todos` makes.
    #
    # As the SUBJECT (`user_id`), the row is a record about them and goes.
    # As the ACTOR (the `moderator_id` who set them inactive, the
    # `resolved_by` who closed the card), the row belongs to somebody else:
    # deleting it would take an unrelated member's inactivity record or review
    # card away because a mod asked to be erased. So the actor id is blanked
    # and the row stands — `moderator_id` to 0 (its own NOT NULL default) and
    # `resolved_by` to NULL, in both cases the "unknown member" every surface
    # already renders.
    # An open break (break_holds) holds the member's roles for "I'm back" and
    # nothing else. Erasure takes it: the member keeps whatever roles they
    # wear, the break role included, and a mod restores by hand if asked.
    _delete(
        conn,
        "DELETE FROM break_holds WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="break_holds",
    )

    for table in ("inactive_members", "promotion_review_cards"):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )
    _scrub(
        conn,
        "UPDATE inactive_members SET moderator_id = 0 "
        "WHERE guild_id = ? AND moderator_id = ?",
        (guild_id, user_id),
        table="inactive_members.moderator_id",
    )
    _scrub(
        conn,
        "UPDATE promotion_review_cards SET resolved_by = NULL "
        "WHERE guild_id = ? AND resolved_by = ?",
        (guild_id, user_id),
        table="promotion_review_cards.resolved_by",
    )

    # QA tracker. The verdict is the member's own — their pass/fail call, the
    # note they wrote, what they were paid — and goes. The test itself is the
    # project's work product like a `todos` row, so the signature on it is
    # blanked rather than the test deleted: removing it would take a recorded
    # sign-off off the board because the tester left. `voided_by` names a
    # different mod on someone else's verdict and is blanked for the same
    # reason. The coins already paid live on in the preserved `econ_ledger`.
    _delete(
        conn,
        "DELETE FROM qa_verdicts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="qa_verdicts",
    )
    _scrub(
        conn,
        "UPDATE qa_verdicts SET voided_by = NULL "
        "WHERE guild_id = ? AND voided_by = ?",
        (guild_id, user_id),
        table="qa_verdicts.voided_by",
    )
    _scrub(
        conn,
        "UPDATE qa_tests SET verified_by = NULL "
        "WHERE guild_id = ? AND verified_by = ?",
        (guild_id, user_id),
        table="qa_tests.verified_by",
    )

    # Voice Master per-room block lists — ASYMMETRIC, deliberately.
    #
    # The member's OWN list goes: it is their preference about their own room,
    # with no ground to outlive them. Rows where they are the `target_id` are
    # KEPT: that entry is somebody else's protection, and erasing it at the
    # request of the person it excludes is exactly what Art 17(3)'s "rights of
    # others" carve-out exists to prevent — the `no_contact_pairs` ground.
    #
    # This is neither the symmetric delete `voice_master_trusted` does nor a
    # blanket preserve, so it is a deliberate call rather than an oversight:
    # a trust list confers something and can be dropped freely, a block list
    # withholds something and cannot.
    _delete(
        conn,
        "DELETE FROM voice_master_blocked WHERE guild_id = ? AND owner_id = ?",
        (guild_id, user_id),
        table="voice_master_blocked.owner_id",
    )

    # Shared todo list — ANONYMISED, NOT DELETED, and deliberately so.
    #
    # A todos row is two different things at once: the task text, which is the
    # mod team's shared work product and belongs to the server, and the two
    # Discord ids naming who added it and who ticked it, which are the member's
    # personal data. Deleting the row to erase the ids would take real
    # outstanding work off other people's list — a task someone else is
    # part-way through vanishing because an unrelated member left. Clearing the
    # ids erases everything that identifies a person while leaving the work
    # standing, which is the minimisation the erasure right actually asks for.
    #
    # `added_by` becomes 0 and `completed_by` NULL — the same "unknown" the
    # board and the dashboard already render for an unresolvable member, so no
    # surface needs to learn a new state. `missed_at` rows name nobody by
    # definition and are untouched.
    #
    # A bug's held screenshot links go first, while `added_by` still says
    # whose they are: they are the member's own uploads, not the team's work.
    # Screenshots already posted live on the bug card in Discord, which this
    # synchronous path cannot reach — blanking `added_by` is what makes the bug
    # loop edit them off that card within a minute
    # (`todo_service.bug_cards_to_strip`).
    _scrub(
        conn,
        "UPDATE todos SET attachment_urls = NULL"
        " WHERE guild_id = ? AND added_by = ? AND attachment_urls IS NOT NULL",
        (guild_id, user_id),
        table="todos.attachment_urls",
    )
    for col, blank in (("added_by", 0), ("completed_by", None)):
        _scrub(
            conn,
            f"UPDATE todos SET {col} = ? WHERE guild_id = ? AND {col} = ?",
            (blank, guild_id, user_id),
            table=f"todos.{col}",
        )

    # …and the recurring definitions behind them, or the erasure undoes itself.
    # `_spawn_one` stamps the definition's `created_by` onto every row it
    # materialises, so a member who set up "Post QOTD" and then asked to be
    # erased would have their id written straight back into `todos.added_by` at
    # the next fire, and every day after. Blanking the definition is what makes
    # the scrub above durable rather than a one-off.
    _scrub(
        conn,
        "UPDATE todo_recurring SET created_by = 0 WHERE guild_id = ? AND created_by = ?",
        (guild_id, user_id),
        table="todo_recurring.created_by",
    )

    # Mention Awards: a `from_user` condition chip names the member inside the
    # rule's conditions JSON — the list-column blind spot, so SUBJECT_ID_COLUMNS
    # can't see it. Strip the member's chips; a rule left with no chips is
    # deleted outright (an empty chip list is the matcher's fail-closed
    # "matches nothing" state, and a husk that existed only because of the
    # erased member serves nobody). Other chips, and other members' chips,
    # survive. Tolerates schema drift like every other step.
    try:
        needle = str(user_id)
        rows = conn.execute(
            "SELECT id, conditions FROM mention_award_rules "
            "WHERE guild_id = ? AND conditions LIKE ?",
            (guild_id, f'%"{needle}"%'),
        ).fetchall()
        for rule_id, raw in rows:
            try:
                chips = json.loads(raw or "[]")
            except json.JSONDecodeError:
                continue
            kept = [
                c for c in chips
                if not (
                    isinstance(c, dict)
                    and c.get("kind") == "from_user"
                    and str(c.get("value", "")) == needle
                )
            ]
            if len(kept) == len(chips):
                continue
            if kept:
                conn.execute(
                    "UPDATE mention_award_rules SET conditions = ? WHERE id = ?",
                    (json.dumps(kept), rule_id),
                )
            else:
                conn.execute(
                    "DELETE FROM mention_award_rules WHERE id = ?", (rule_id,)
                )
    except sqlite3.Error as exc:
        log.warning("Purge: failed on mention_award_rules (%s)", exc)

    # Pair tables: clear whichever side the erased user is on.
    for table, col_a, col_b in (
        ("watched_users", "watched_user_id", "watcher_user_id"),
        ("voice_master_trusted", "owner_id", "target_id"),
        ("invite_edges", "inviter_id", "invitee_id"),
        # 2026-09-02 GDPR review. A duel row names exactly two members and
        # means nothing with one of them removed, so it goes whole from either
        # side — the same call the Risky Rolls rounds made.
        ("duel_cooldowns", "player_a", "player_b"),
        ("duel_nicks", "loser_id", "winner_id"),
        # Who tipped whom. The coins live on in the preserved `econ_ledger`.
        ("reaction_tip_awards", "user_id", "author_id"),
    ):
        for col in (col_a, col_b):
            _delete(
                conn,
                f"DELETE FROM {table} WHERE guild_id = ? AND {col} = ?",
                (guild_id, user_id),
                table=f"{table}.{col}",
            )

    # Confessions held by mod-approve mode (migration 200). Deleted outright,
    # and it is the one confessions table that is: `confession_threads` relies
    # on its fourteen-day TTL because it holds routing metadata, but a pending row
    # holds the member's confession *text*, and it is queued to be published.
    # Erasing somebody and then posting what they wrote — because a moderator
    # got to the queue before the sweep did — is not a defensible outcome, so
    # the row goes now. Nothing is announced to the mods. Note this path cannot
    # repaint the board — erasure is run out-of-band, with no bot in hand, and
    # the board's own loop only repaints guilds where a recurring chore spawned
    # — so a clipped copy of the confession stays rendered in the sticky embed
    # until something else moves it. The stored row is gone either way, which is
    # what the erasure owes; the stale pixels are a display lag, and pressing
    # the board's Confessions button will already find nothing there.
    _delete(
        conn,
        "DELETE FROM confession_pending WHERE guild_id = ? AND author_id = ?",
        (guild_id, user_id),
        table="confession_pending",
    )
    # The member's anonymous aliases (2026-09-04, anon-tail-69): one row per
    # confession thread or Anonymous Truth or Dare prompt they replied under,
    # user id beside the pseudonym. Routing metadata like `confession_threads`
    # — no text — and since migration 211 it shares that table's fourteen-day
    # sweep, but it goes at erasure too: an alias row is exactly what would
    # tie a still-visible pseudonymous reply back to an erased member.
    _delete(
        conn,
        "DELETE FROM confession_emoji_assignments WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="confession_emoji_assignments",
    )
    # Confession rate-limit state (bug #222, item 4). The register called it
    # "operational, no content" and preserved it, which is an engineering
    # reason, not an Art 17(3) ground — and the row is not as harmless as the
    # cell implied: `last_confess_at` is a timestamp saying *this member
    # posted an anonymous confession at that moment*, which is exactly what
    # correlates an erased member against a confession still visible in the
    # channel. It also outlives every other confessions row, because it is
    # keyed per member rather than per thread and nothing swept it.
    _delete(
        conn,
        "DELETE FROM confession_rate_limits WHERE guild_id = ? AND author_id = ?",
        (guild_id, user_id),
        table="confession_rate_limits",
    )
    # Auto-Thread "Replies to Your Threads" opt-in (migration 244). A
    # preference, not a record anyone else relies on: an erased member who
    # comes back gets no reply DMs until they switch it on again.
    _delete(
        conn,
        "DELETE FROM needle_reply_optins WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="needle_reply_optins",
    )
    # Notifications waiting to be sent as one batched DM (migration 243):
    # the member as recipient or as the one whose reply is being reported.
    # Minutes-lived; an erased member's reply is simply never announced.
    _delete(
        conn,
        "DELETE FROM dm_batch_pending WHERE guild_id = ? AND (user_id = ? OR actor_id = ?)",
        (guild_id, user_id, user_id),
        table="dm_batch_pending",
    )

    # ── 2026-09-02 GDPR review: tables whose member column is not `user_id`,
    # or which are not guild-scoped and must reach their guild through a
    # parent. Same schema-drift tolerance as every step above.
    for table, column in (
        # Who was starred. The starboard message in Discord is an ordinary
        # message the purge cannot reach.
        ("starboard_posts", "author_id"),
        ("duel_group_cooldowns", "player_id"),
        # A live temporary voice room owned by the member. Live state, not
        # history — the room itself is torn down by the voice cog.
        ("voice_master_channels", "owner_id"),
        # LegitLibs: a template and a revision are the member's own writing.
        ("legitlibs_templates", "author_id"),
        # Auto-react bookkeeping naming whose message was reacted to.
        ("auto_react_placements", "author_id"),
        # How the member voted in a community policy ballot. An activity
        # record with no Art 17(3) ground, the same call `policy_votes` gets
        # one block below — and safe to take because a closed ballot's result
        # is frozen onto `policy_ballots` at close, so removing a vote cannot
        # move a decision that was already announced. Removing one from an
        # *open* ballot does change the live tally, which is correct: the
        # member is no longer a participant. `policy_ballots` itself is
        # preserved — it is a record about the server's governance, and it
        # names no voter.
        ("policy_ballot_votes", "user_id"),
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND {column} = ?",
            (guild_id, user_id),
            table=f"{table}.{column}",
        )

    # Not guild-scoped: each reaches its guild through a parent row, the same
    # shape `survivor_*` uses. Scoping matters — a bare `user_id` match would
    # delete the member's rows in every guild the bot serves, not just this one.
    for table, column, parent, parent_key, child_key in (
        ("role_menu_bindings", "user_id", "role_menus", "id", "menu_id"),
        ("policy_votes", "user_id", "policy_tickets", "id", "policy_id"),
        ("legitlibs_revisions", "editor_id", "legitlibs_templates",
         "template_id", "template_id"),
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE {column} = ? AND {child_key} IN "
            f"(SELECT {parent_key} FROM {parent} WHERE guild_id = ?)",
            (user_id, guild_id),
            table=f"{table}.{column}",
        )

    # `legitlibs_reports` keys on `game_id`, which belongs to an in-memory
    # game rather than a table, so there is no parent to scope through. The
    # report names its reporter and nothing else about the guild; it is
    # deleted across guilds rather than left standing, since a report is a
    # small row whose only personal datum is the id being erased.
    _delete(
        conn,
        "DELETE FROM legitlibs_reports WHERE reporter_id = ?",
        (user_id,),
        table="legitlibs_reports.reporter_id",
    )

    # A group Hot Potato row names its host, holder, winner and loser in
    # columns and its whole roster in JSON lists (`roster`, `alive`,
    # `elimination_order`, `pass_log`) — a list-valued blind spot. The row goes
    # whole from any named seat: a party game with a hole in it is incoherent
    # state the cog would try to re-attach a view to on the next boot, and the
    # alternative is retaining the id inside the JSON.
    _delete(
        conn,
        "DELETE FROM hp_group_games WHERE guild_id = ? AND ("
        "host_id = ? OR holder_id = ? OR winner_id = ? OR loser_id = ? "
        "OR (',' || COALESCE(roster, '') || ',') LIKE ? "
        "OR (',' || COALESCE(alive, '') || ',') LIKE ?)",
        (guild_id, user_id, user_id, user_id, user_id,
         *([f"%,{user_id},%"] * 2)),
        table="hp_group_games",
    )

    # The rest of the games family (review H1, 2026-09-14): the three duels,
    # Musical Chairs, Chicken, and the shared live/archived games record, on
    # the same whole-record rule. The register said these were purged for
    # months; they were named in no statement. Deleting a `games_game_history`
    # row lowers that guild's games-played figures — accepted, since a party
    # game's statistics are no Art 17(3) ground.
    _delete_game_records(conn, guild_id, user_id)
    _delete(
        conn,
        "DELETE FROM hot_potato_style WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="hot_potato_style",
    )

    # Wellness counter children, deleted BEFORE their parents — they key on
    # `cap_id`/`blackout_id` and have no `guild_id` or `user_id` of their own,
    # so once the parent row is gone there is nothing left to find them by.
    #
    # All three were listed in the plain `guild_id`+`user_id` sweep below from
    # the day they shipped, where every erasure failed on them with "no such
    # column: guild_id", logged a warning and moved on — the register recorded
    # the wellness family as fully purged throughout (2026-09-02 GDPR review).
    # Production holds no rows in any of the three, so nothing was retained in
    # error; the defect was that the erasure could not have cleared them.
    for table, child_key, parent, parent_key in (
        ("wellness_cap_counters", "cap_id", "wellness_caps", "id"),
        ("wellness_cap_overages", "cap_id", "wellness_caps", "id"),
        ("wellness_blackout_overages", "blackout_id", "wellness_blackouts", "id"),
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE {child_key} IN "
            f"(SELECT {parent_key} FROM {parent} "
            f"WHERE guild_id = ? AND user_id = ?)",
            (guild_id, user_id),
            table=table,
        )

    for table in (
        "wellness_users",
        "wellness_caps",
        "wellness_blackouts",
        "wellness_blackout_active",
        "wellness_slow_mode",
        "wellness_streaks",
        "wellness_streak_history",
        "wellness_away_rate_limit",
        "wellness_weekly_reports",
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )

    # Economy + casino per-member state (the ledger is deliberately kept —
    # see economy_service._PURGE_USER_ID_TABLES for the list and the rule).
    from bot_modules.economy.service import econ_purge_user

    econ_purge_user(conn, guild_id, user_id)

    # Music playlist: `added_by` rows in tracks + unmatched, and reviewer
    # references nulled — the member column is `added_by`, so the generic
    # user_id sweep above can't reach it (register: docs/data_register.md).
    from bot_modules.music.playlist.music_playlist_store import purge_member_rows

    try:
        purge_member_rows(conn, guild_id, user_id)
    except sqlite3.Error as exc:
        log.warning("Purge: failed on music_playlist tables (%s)", exc)

    # ── Bug #222: register rows that kept member data and named no Art 17(3)
    # ground. Each cell held an engineering reason instead ("authority grant",
    # "matchmaking memory", "operational"), which is not a lawful basis for
    # keeping a name against an erasure request. Re-decided 2026-09-17; the
    # register rows carry the grounds, these comments the consequences.

    # The *Who Can Hand This Out* allow-list. An entry is an authority the
    # member holds, not a record about them that anyone relies on afterwards,
    # so nothing is lost by revoking it — the effect is identical to an admin
    # removing the row on the Role Grants panel. Matched on `entity_type` as
    # well as the value, because `entity_id` holds a role id for a role row
    # and the two numeric spaces overlap.
    _delete(
        conn,
        "DELETE FROM grant_role_permissions "
        "WHERE guild_id = ? AND entity_type = 'user' AND entity_id = ?",
        (guild_id, user_id),
        table="grant_role_permissions",
    )

    # Pen Pals. Three tables, three different answers.
    #
    # The pool is "who is waiting to be matched right now" — a live queue, not
    # a record — and leaving an erased member in it was a defect as much as a
    # register gap: the matcher would pair them and open a private channel for
    # an account that is not supposed to exist.
    _delete(
        conn,
        "DELETE FROM pen_pals_pool WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="pen_pals_pool",
    )
    # A live session is closed before anything else touches it. The row cannot
    # simply be deleted: the orphan-channel sweep on boot keys on a *closed*
    # session, so a deleted row leaves the private channel standing with
    # nothing left that knows to remove it, and the phase timer keeps firing
    # at a member who is gone. Closed with a reason of its own, so the sweep
    # takes the channel and the cog stands down. Nobody is notified and the
    # surviving member is not re-pooled — an erasure runs out-of-band with no
    # bot in hand, and one Join press puts them back (the same accepted
    # consequence as `pen_pals_optouts`).
    _scrub(
        conn,
        "UPDATE pen_pals_sessions SET state = 'closed', closed_at = ?, "
        "close_reason = 'purged' WHERE guild_id = ? AND state = 'active' "
        "AND (user1_id = ? OR user2_id = ?)",
        (time.time(), guild_id, user_id, user_id),
        table="pen_pals_sessions.state",
    )
    # Then the member's id leaves the pairing record, seat by seat, and the
    # row stands. The row is also the OTHER member's no-repeat memory — a
    # pairing is one-per-lifetime and `_past_partners` reads it all-time — so
    # deleting it would take a second member's record away because the first
    # asked to be erased: Art 17(3)(e), applied to the column rather than the
    # row. The `mahjong_results.winner_id` and `survivor_hosts.added_by`
    # precedent. Consequence, accepted: a returning member is matchable with
    # their old pen pal again, which is right — an erased member has no
    # history to repeat.
    for col in ("user1_id", "user2_id"):
        _scrub(
            conn,
            f"UPDATE pen_pals_sessions SET {col} = 0 WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
            table=f"pen_pals_sessions.{col}",
        )
    # Blocks split on who the record protects. The member's own `member`-source
    # blocks are their preference and go with them (the `pen_pals_optouts`
    # precedent). Everything else stays on Art 17(3)(e): a block somebody else
    # placed against them, and an admin separation in either direction, are
    # protective records about another member or a moderator decision — the
    # `voice_master_blocked` ground, and an erasure must not be a way to clear
    # one. See docs/specs/no_contact_spec.md for the same rule stated for contact.
    _delete(
        conn,
        "DELETE FROM pen_pals_blocks "
        "WHERE guild_id = ? AND user_id = ? AND source = 'member'",
        (guild_id, user_id),
        table="pen_pals_blocks",
    )

    # Guess Who: rounds the member submitted or is the answer to go whole, with
    # every guess and log row on them; their play on surviving rounds goes;
    # their solver credit on somebody else's round is scrubbed. The engine is
    # shared with the member's own My Data → Guess Who button, so the two can
    # never delete different things — its docstring owns the reasoning.
    #
    # The Discord posts and the on-disk originals are the route's job, not
    # this function's: neither can be rolled back and this transaction belongs
    # to the caller. See `web_server/routes/privacy.py`, which clears them
    # first through `guess_cleanup_service.clear_member_media`, and
    # `guess_cleanup_service.sweep_orphan_originals`, the directory-reading
    # backstop for exactly the case of a row deleted out of band.
    from bot_modules.games.guess.repo import forget_member as guess_forget_member

    try:
        guess_forget_member(conn, guild_id=guild_id, user_id=user_id)
    except sqlite3.Error as exc:
        log.warning("Purge: failed on guess tables (%s)", exc)

    # The welcome card the greeters worked through for this member, and its
    # steps. The card is about them — when they arrived, who greeted them, how
    # far the checklist got — so it goes with them. `intake_card_steps` has no
    # guild_id and reaches its card by id, so the ids are resolved first.
    #
    # What stays: this member's id as the *greeter* on somebody else's card
    # (`intake_cards.resolved_by`, `intake_card_steps.done_by`). That is the
    # other member's welcome record and the audit of who admitted them —
    # Art 17(3)(e), the `mahjong_results.winner_id` precedent.
    try:
        card_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM intake_cards WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        ]
    except sqlite3.Error as exc:
        log.warning("Purge: failed reading intake_cards (%s)", exc)
        card_ids = []
    for chunk in _chunks(card_ids):
        ph = ",".join("?" * len(chunk))
        _delete(
            conn,
            f"DELETE FROM intake_card_steps WHERE card_id IN ({ph})",
            tuple(chunk),
            table="intake_card_steps",
        )
    _delete(
        conn,
        "DELETE FROM intake_cards WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="intake_cards",
    )

    # Whispers. The deletes already existed; only `/whisper forget-me` could
    # reach them, so this path left every whisper standing while the member
    # could clear the lot themselves (bug #222, item 3 — the whole reason the
    # engine moved to whisper_repo). Reports the member filed on somebody
    # else's whisper, and guesses naming them on a third party's, survive on
    # Art 17(3)(e); the function's docstring owns that list.
    from bot_modules.whisper.repo import forget_member as whisper_forget_member

    try:
        whisper_forget_member(conn, guild_id=guild_id, user_id=user_id)
    except sqlite3.Error as exc:
        log.warning("Purge: failed on whisper tables (%s)", exc)

    return len(msg_ids)



# ── Anonymisation (GDPR Art 4(5) pseudonymisation) ───────────────────────────
#
# The third path, beside the hard erasure above and the export below. It exists
# because the two existing answers to "this member has left" are both wrong for
# the common case: keeping everything retains a named person who is gone, and
# `purge_user_data` destroys 163,701 of 184,009 rows — the entire XP timeline
# included — so the server can no longer be compared against its own past.
#
# THE IDEA. Every DK metric is an aggregate over event rows keyed by a member
# id. Aggregation needs that key to be *stable and distinct*, not *meaningful*.
# So the id is rewritten to a small surrogate integer — far below the snowflake
# range, so it cannot collide with a real account and reads as obviously not a
# person — and every count, sum, rate and distribution is mathematically
# unchanged. The separate identifying surfaces (names, the member's own message
# text, the id embedded in JSON and prose) are then destroyed.
#
# NOT A MAPPING TABLE. Nothing is written that maps the surrogate back to the
# real id; that is the whole point. A retained key would make this merely
# pseudonymisation under Recital 26 and the result would remain personal data.
# `known_users` keeps one row per surrogate — `1 → "Cleared user 1"` — which is
# a label, not a link, and is what stops every embed and leaderboard rendering
# a bare `<@1>` (`name_resolver.resolve_name_from` falls back to a mention).
#
# HONEST LIMIT, stated because overclaiming here would be the worst failure
# this module could have: this is strong pseudonymisation, NOT mathematical
# anonymity. The subject's 18,654 messages keep their timestamps and channels,
# so someone holding Discord scrollback for those channels can still line the
# surrogate's activity up against a real account and re-identify it. What the
# rewrite removes is the ability to do that *from this database alone*.

#: Surrogates live below this. Discord snowflakes are ~10^17-10^18 and every
#: member id in production is >= 10^16, so a value under a million cannot
#: collide with a real account, survives JSON round-tripping, and is instantly
#: recognisable as not-a-person in a raw row. Negative ids are already taken:
#: `mahjong.bot_logic.is_bot_id` uses them for synthetic house seats.
SURROGATE_ID_CEILING = 1_000_000

#: The floor a real member id must clear. Guards against anonymising an
#: already-anonymised surrogate — which would be irreversible nonsense, since
#: the surrogate's rows would merge into another surrogate's.
_REAL_MEMBER_ID_FLOOR = 10**16

#: Tables that KEEP the real member id, decided by Billy on 2026-09-11 table by
#: table. These are the Art 17(3) preserved categories from
#: `docs/data_register.md`, and the split is deliberate in BOTH directions —
#: a later cleanup pass that makes the sweep uniform would silently break
#: something, so `tests/privacy/test_privacy_anonymize.py` fails if this set changes
#: shape.
#:
#: WHY EACH ONE IS HERE. The common thread is that the row exists in order to
#: name a person, so a surrogate empties it of its purpose:
#:
#: - Sanctions (`jails`, `warnings`, `tickets`, `ticket_participants`,
#:   `policy_tickets`). A warning's entire payload is "this member did X";
#:   anonymised it says "someone did X", which is unusable. Warning counts also
#:   drive escalation, so a rewritten history hands a returning account a clean
#:   sheet.
#: - `dm_audit_log`, `dm_consent_pairs`. Consent forensics, and the party it
#:   mostly protects is the OTHER one — it is what a member shows when accused
#:   of unsolicited DMs. Erasing the subject's identity from it destroys
#:   somebody else's defence.
#: - `no_contact_pairs`, `no_contact_events`. The only rows here that exist to
#:   be acted on against a live id in FUTURE. A rewritten pair stops matching,
#:   and the failure is silent by design — the gate must refuse in a way the
#:   blocked party cannot distinguish from an ordinary outcome (CLAUDE.md,
#:   `docs/specs/no_contact_spec.md`), so nothing anywhere would report that the
#:   protection had stopped working. Splitting by side does not help: the gate
#:   matches both directions, so rewriting either `user_low` or `user_high`
#:   kills the pair. This is the `voice_master_blocked.target_id` asymmetry the
#:   purge already makes, applied to a bigger surface.
#:
#: What is NOT here, though `purge_user_data` preserves it: `econ_ledger` (a
#: balance is a SUM over user_id, so the whole balance moves intact to the
#: surrogate and no counterparty moves by a coin) and `audit_log` (pure
#: statistics, which survive a rewrite). `reaction_log` and `voice_follow_log`
#: were rewritten on the same reasoning until migration 229 dropped them.
#:
#: Known inconsistency, accepted: `audit_log` is rewritten while `warnings` is
#: kept, so a `warning_delete` entry and the warning it describes now disagree
#: about who the member was. That is a display oddity on an admin-only surface,
#: not a loss — the warning row itself still names them.
ANONYMIZE_KEEP_TABLES = frozenset(
    {
        "dm_audit_log",
        "dm_consent_pairs",
        "jails",
        "no_contact_events",
        "no_contact_pairs",
        "policy_tickets",
        "ticket_participants",
        "tickets",
        "warnings",
    }
)

#: Free text that is the member's OWN expression or self-description, cleared
#: outright: `(table, text column, the column naming the subject)`.
#:
#: The row is KEPT and only the text goes, which is what makes this consistent
#: with the premise. `messages.content` was the largest entry until migration
#: 228 dropped the column (2026-09-13): the first production run nulled 17,226
#: contents at exactly zero metric loss, because a message row keeps its `ts`,
#: `channel_id`, `sentiment`, `emotion`, `media_kind` and `reply_to_id`.
#:
#: NOTE the third element is the column naming the AUTHOR of the text, never
#: merely a column naming the member. `whispers` is keyed on `sender_id`, not
#: `target_id`: the 94 whispers they sent are theirs to lose, the 59 they received
#: are someone else's words and stay. Same for `whisper_replies.from_user_id`.
#:
#: TWO TABLES DELIBERATELY NOT HERE, each of which a naive "every text column
#: on a row naming this member" sweep would have taken:
#:
#: - `tickets.description` — `tickets` is in `ANONYMIZE_KEEP_TABLES`. Keeping
#:   the id as moderation evidence and then destroying the text of the
#:   complaint would leave evidence that records nothing.
#: - `music_playlist_tracks.title` / `.artist` — measured before writing this:
#:   the rows hold `'Medieval Tavern Joy'` by `'Celestial Aeon Project, …'`.
#:   That is third-party song metadata sitting on a row the member happened to
#:   add, not text that identifies them, and clearing it would damage a shared
#:   playlist for no privacy gain. The `added_by` id is rewritten like any
#:   other; the song keeps its name.
ANONYMIZE_FREE_TEXT_COLUMNS = (
    ("whispers", "message", "sender_id"),
    ("whisper_replies", "content", "from_user_id"),
    ("bio_answers", "answer", "user_id"),
    ("bio_field_values", "value", "user_id"),
    # A bounty the member wrote.
    ("econ_bounties", "title", "poster_id"),
    ("econ_bounties", "description", "poster_id"),
    # The member's saved voice-room name and their pre-duel nickname.
    ("voice_master_profiles", "saved_name", "user_id"),
    ("duel_nicks", "original_nick", "loser_id"),
    # A normalised copy of their last message, kept for repeat detection.
    ("member_xp", "last_message_norm", "user_id"),
    # A QA tester's free-text note on a test they ran. Missed by the first
    # production run (2026-09-11) and found by the post-condition sweep: the
    # table was in the id rewrite from the start, so the row said "surrogate 1
    # blocked this test" while the note beside it was still the member's own
    # sentence. `verdict` and `paid_amount` stay, so the QA statistics and the
    # payout record are untouched.
    ("qa_verdicts", "note", "user_id"),
)

#: JSON blobs where ONE key holds the member's own words and the rest is
#: telemetry: ``(table, column, json path)``. The key is removed with
#: ``json_remove``; the rest of the object is left exactly as it was.
#:
#: `audit_log.extra` is the measured case. 108 of the subject's rows carry an
#: `extra` object and clearing the column outright would throw away 108
#: `channel_id`s, `reason`s and `remember` flags to remove text from 38 of
#: them. Those 38 are `vm_channel_create` / `vm_channel_rename` entries whose
#: `$.name` is the room title the member typed — `"lark's Room"`, but also
#: `"Film Night w Rook"` and `"Fern Appreciation Night"`, which name other
#: people. It is the same category as `voice_master_profiles.saved_name`,
#: which the free-text clear already takes; this is that name's audit trail.
ANONYMIZE_JSON_TEXT_KEYS = (("audit_log", "extra", "$.name"),)

_DRIFT_MARKERS = ("no such table", "no such column")


@dataclass
class AnonymizeReport:
    """What an anonymisation did, or (from :func:`plan_anonymization`) would do.

    The same object either way, built by the same operation list, so a dry run
    cannot quietly disagree with the apply it is rehearsing.
    """

    user_id: int
    surrogate_id: int
    username: str = ""
    display_name: str = ""
    #: "table.column" → rows, for each of the three sweeps.
    id_rewrites: dict[str, int] = field(default_factory=dict)
    text_rewrites: dict[str, int] = field(default_factory=dict)
    text_cleared: dict[str, int] = field(default_factory=dict)
    #: What this run did not touch: tables absent from this deployment's schema
    #: (tolerated drift), and any whose name no statement can safely carry.
    skipped: list[str] = field(default_factory=list)
    #: Tables that still hold the real id, and by decision — see
    #: :data:`ANONYMIZE_KEEP_TABLES`.
    kept: list[str] = field(default_factory=list)
    #: The guild a guild-scoped run was narrowed to; ``None`` for all guilds.
    scope_guild_id: int | None = None
    #: "table.column" → rows that still name the member because they lie
    #: outside a guild-scoped run (bot-wide tables, other guilds' channels).
    #: Left alone, and not counted in :attr:`total_rows`.
    out_of_scope: dict[str, int] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return (
            sum(self.id_rewrites.values())
            + sum(self.text_rewrites.values())
            + sum(self.text_cleared.values())
        )


def _is_schema_drift(exc: sqlite3.Error) -> bool:
    return any(marker in str(exc).lower() for marker in _DRIFT_MARKERS)


def _anon_execute(
    conn: sqlite3.Connection, sql: str, params: tuple, *, table: str
) -> int | None:
    """One anonymising statement. Returns rows affected, or None on drift.

    **This is deliberately stricter than the purge's ``_delete``, which
    tolerates every ``sqlite3.Error``.** The two paths fail differently: a
    statement swallowed during an erasure leaves a row undeleted, which the
    runbook's schema-drift section tells the operator how to find. A statement
    swallowed here leaves **the real member id in place while the report says
    the sweep succeeded** — the feature failing silently into the exact state it
    exists to prevent. So only genuine schema drift (a table or column this
    deployment does not have) is tolerated; a constraint violation, a locked
    database or a type error propagates, and since the caller owns the
    transaction the whole anonymisation rolls back rather than half-applying.
    """
    try:
        return conn.execute(sql, params).rowcount
    except sqlite3.Error as exc:
        if _is_schema_drift(exc):
            log.warning("Anonymise: skipping %s (%s)", table, exc)
            return None
        raise


def _anon_count(
    conn: sqlite3.Connection, sql: str, params: tuple, *, table: str
) -> int | None:
    """The read-only half of :func:`_anon_execute`, for :func:`plan_anonymization`."""
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error as exc:
        if _is_schema_drift(exc):
            return None
        raise


def _text_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, bool]]:
    """Text-affinity columns of *table*, as ``(name, not_null)``.

    Affinity rather than a hand-kept list: the id turns up inside JSON blobs,
    CSV id lists and ordinary prose in fifteen different columns, and the set
    grows every time a feature stores a payload. An empty declared type is
    included — SQLite gives it BLOB affinity, but such columns hold strings in
    practice here and a missed one keeps the id.
    """
    out: list[tuple[str, bool]] = []
    for row in conn.execute(f'PRAGMA table_info("{table}")'):
        declared = (row[2] or "").upper()
        if declared == "" or any(
            marker in declared for marker in ("CHAR", "TEXT", "CLOB")
        ):
            out.append((row[1], bool(row[3])))
    return out


def next_surrogate_id(conn: sqlite3.Connection) -> int:
    """The lowest unused surrogate, starting at 1.

    Allocated from ``known_users`` rather than from a table of its own, which
    is the point: that row already has to exist so embeds have something to
    render, it holds ``1 → "Cleared user 1"`` and nothing pointing back at the
    real id, and so it is a registry of *which surrogates are taken* without
    being a mapping. Adding a table that recorded the pairing would turn this
    from anonymisation into pseudonymisation under Recital 26.
    """
    row = conn.execute(
        "SELECT MAX(user_id) FROM known_users WHERE user_id > 0 AND user_id < ?",
        (SURROGATE_ID_CEILING,),
    ).fetchone()
    return int(row[0] or 0) + 1


def _anon_subject_columns(
    conn: sqlite3.Connection,
) -> list[tuple[str, list[str]]]:
    """Every ``(table, [subject id columns])`` the rewrite should touch.

    Discovered from the live schema against :data:`SUBJECT_ID_COLUMNS` rather
    than hand-listed, exactly as ``export_user_data`` does and unlike
    ``purge_user_data``'s 130 hand-written statements. A curated list is what
    goes stale, and a stale anonymisation leaves the real id in the table
    somebody added last month. Measured against production: this reaches 89
    tables and 113 columns for a heavy member, and 209 tables / 295 columns
    across the whole schema.

    ``ANONYMIZE_KEEP_TABLES`` is subtracted here, so it covers the id sweep,
    the text sweep and the free-text clear in one place.
    """
    out: list[tuple[str, list[str]]] = []
    names = guild_purge_service.table_names(conn)
    for table in names:
        if table in ANONYMIZE_KEEP_TABLES:
            continue
        try:
            cols = _table_columns(conn, table)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            log.warning("Anonymise: cannot introspect %s (%s)", table, exc)
            continue
        matched = sorted(set(cols) & SUBJECT_ID_COLUMNS)
        if matched:
            out.append((table, matched))
    return out


def _validate_surrogate(
    conn: sqlite3.Connection, user_id: int, surrogate_id: int
) -> None:
    if user_id < _REAL_MEMBER_ID_FLOOR:
        raise ValueError(
            f"user_id {user_id} is below the snowflake floor — refusing to "
            "anonymise what is already a surrogate (its rows would merge into "
            "another surrogate's and the result is not recoverable)"
        )
    if not 0 < surrogate_id < SURROGATE_ID_CEILING:
        raise ValueError(
            f"surrogate_id {surrogate_id} out of range: must be 1.."
            f"{SURROGATE_ID_CEILING - 1}. Negative ids are mahjong house seats "
            "and anything larger risks colliding with a real account."
        )
    taken = conn.execute(
        "SELECT 1 FROM known_users WHERE user_id = ? LIMIT 1", (surrogate_id,)
    ).fetchone()
    if taken:
        raise ValueError(
            f"surrogate_id {surrogate_id} is already in use — reusing it would "
            "merge two anonymised members into one, silently corrupting every "
            "per-member metric for both. Use next_surrogate_id()."
        )


def _anon_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table the sweeps may touch — all of them bar the KEEP set."""
    return [
        name
        for name in guild_purge_service.table_names(conn)
        if name not in ANONYMIZE_KEEP_TABLES
    ]


def _anon_text_clear_operations(
    conn: sqlite3.Connection, user_id: int
) -> list[tuple[str, str, str, str, tuple, str, tuple]]:
    """The two text-destroying sweeps, for a subject identified by *user_id*.

    Split out of :func:`_anon_operations` so it can also be run on its own
    against a surrogate that has ALREADY been anonymised — see
    :func:`anonymize_missed_text`. Every one of these finds its rows by
    ``subject_col = user_id``, so within a full anonymisation they must all run
    BEFORE the id sweep overwrites that column.
    """
    ops: list[tuple[str, str, str, str, tuple, str, tuple]] = []

    # 1. The member's own words, cleared. Row kept, text gone.
    for table, col, subject_col in ANONYMIZE_FREE_TEXT_COLUMNS:
        info = {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if col not in info or subject_col not in info:
            continue  # drift: recorded by the caller
        blank = "" if info[col][3] else None
        where = f'"{subject_col}" = ? AND "{col}" IS NOT NULL AND "{col}" != \'\''
        ops.append(
            (
                "text_cleared",
                f"{table}.{col}",
                table,
                f'SELECT COUNT(*) FROM "{table}" WHERE {where}',
                (user_id,),
                f'UPDATE "{table}" SET "{col}" = ? WHERE {where}',
                (blank, user_id),
            )
        )

    # 1b. One key lifted out of a JSON blob, the rest of the object untouched.
    for table, col, path in ANONYMIZE_JSON_TEXT_KEYS:
        info = {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if col not in info:
            continue  # drift: recorded by the caller
        subject_cols = sorted(set(info) & SUBJECT_ID_COLUMNS)
        if not subject_cols:
            continue
        named = " OR ".join(f'"{c}" = ?' for c in subject_cols)
        # `json_valid` guards a column that is only *mostly* JSON: json_remove
        # raises on malformed input, and a raise here aborts the whole
        # anonymisation (see _anon_execute) rather than skipping one row.
        where = (
            f'({named}) AND "{col}" IS NOT NULL AND json_valid("{col}") '
            f'AND json_extract("{col}", ?) IS NOT NULL'
        )
        ids = tuple(user_id for _ in subject_cols)
        ops.append(
            (
                "text_cleared",
                f"{table}.{col}{path[1:]}",
                table,
                f'SELECT COUNT(*) FROM "{table}" WHERE {where}',
                (*ids, path),
                f'UPDATE "{table}" SET "{col}" = json_remove("{col}", ?) '
                f"WHERE {where}",
                (path, *ids, path),
            )
        )

    return ops


#: Wraps a handle parked out of the way for the length of one statement. The
#: ASCII record separator cannot occur in a Discord message, a username, or any
#: JSON this bot writes, so a value hidden under it cannot collide with real
#: content and is restored byte-for-byte by the same statement.
_SHIELD = "\x1e"


def _username_shields(
    conn: sqlite3.Connection, username: str
) -> list[tuple[str, str]]:
    """Other members' usernames that CONTAIN *username*, longest first.

    The username sweep is a plain substring ``REPLACE``, and Discord handles are
    unique but **not prefix-free**. Anonymising a departed ``lark_hollow`` would
    rewrite a *current* member's ``lark_hollow92`` to ``cleared_user_192`` — in
    ``known_users.username`` itself, destroying a live member's roster row, and
    in every message that ever mentioned them. Anonymising one member must never
    touch another member's record, so each colliding handle is parked under a
    sentinel for the length of the statement and put back afterwards::

        REPLACE(REPLACE(REPLACE(col, 'lark_hollow92', S), 'lark_hollow', R),
                S, 'lark_hollow92')

    Longest first, so two colliding handles that overlap each other shield in
    the right order. The count uses the *same* shielded expression, so the dry
    run still agrees with the apply: a row holding only the other member's
    handle is neither counted nor changed.

    The id needle needs no equivalent — a snowflake would have to be a literal
    substring of another snowflake, and in practice they are the same width.
    """
    if not username:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT username FROM known_users "
            "WHERE username IS NOT NULL AND username != '' "
            "AND username != ? AND instr(username, ?) > 0",
            (username, username),
        ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        log.warning("Anonymise: cannot read handles to shield (%s)", exc)
        return []
    others = sorted({str(r[0]) for r in rows}, key=len, reverse=True)
    if others:
        log.warning(
            "Anonymise: %d other handle(s) contain %r and are shielded from "
            "the username sweep: %s",
            len(others),
            username,
            ", ".join(others[:5]),
        )
    return [(other, f"{_SHIELD}{i}{_SHIELD}") for i, other in enumerate(others)]


def _shielded_rewrite(
    col: str, needle: str, replacement: str, shields: list[tuple[str, str]]
) -> tuple[str, tuple, str, tuple]:
    """``(where_sql, where_params, set_expr, set_params)`` for one text rewrite.

    With no shields this is exactly ``REPLACE(col, needle, replacement)`` under
    ``instr(col, needle) > 0`` — the shape this had before
    :func:`_username_shields` existed, and the shape every id rewrite still
    takes. Both halves are built from the same shielded expression so the plan
    and the apply cannot disagree about which rows move.
    """
    guarded = f'"{col}"'
    guard_params: list[str] = []
    for original, sentinel in shields:
        guarded = f"REPLACE({guarded}, ?, ?)"
        guard_params.extend((original, sentinel))

    where = f"instr({guarded}, ?) > 0"
    where_params = (*guard_params, needle)

    set_expr = f"REPLACE({guarded}, ?, ?)"
    set_params: list[str] = [*guard_params, needle, replacement]
    for original, sentinel in reversed(shields):
        set_expr = f"REPLACE({set_expr}, ?, ?)"
        set_params.extend((sentinel, original))

    return where, where_params, set_expr, tuple(set_params)


def _anon_operations(
    conn: sqlite3.Connection, user_id: int, surrogate_id: int, username: str
) -> list[tuple[str, str, str, str, tuple, str, tuple]]:
    """Every statement the anonymisation runs, as
    ``(bucket, label, table, count_sql, count_params, apply_sql, apply_params)``.

    Built once and consumed by both :func:`anonymize_user_data` and
    :func:`plan_anonymization`, so the dry run an operator reads before pressing
    the button is generated by the same code as the run itself — a rehearsal
    that can drift from the thing it rehearses is worse than none.

    ORDER IS LOAD-BEARING. The free-text clear runs FIRST, because it finds its
    rows by ``subject_col = user_id`` and the id sweep is about to overwrite
    that. The text sweep runs before the id sweep only for tidiness (it works on
    string content and is independent of both).
    """
    ops: list[tuple[str, str, str, str, tuple, str, tuple]] = []

    ops.extend(_anon_text_clear_operations(conn, user_id))

    # 2. The id, and the username, wherever they are embedded in text.
    #
    # `instr` rather than LIKE '%…%' deliberately: LIKE is case-insensitive for
    # ASCII but REPLACE is case-sensitive, so a LIKE-driven count would report
    # rows that the UPDATE then leaves untouched. Matching the two keeps the
    # dry run's numbers true. Discord usernames are lowercase, so nothing is
    # lost by the case-sensitivity.
    #
    # The DISPLAY NAME is deliberately not swept — see the docstring of
    # :func:`anonymize_user_data`.
    #
    # The username needle carries a shield list: handles are unique but not
    # prefix-free, and without it a departed `lark_hollow` rewrites a LIVE
    # member's `lark_hollow92`. See :func:`_username_shields`.
    needles: list[tuple[str, str, list[tuple[str, str]]]] = [
        (str(user_id), str(surrogate_id), [])
    ]
    if username:
        needles.append(
            (
                username,
                f"cleared_user_{surrogate_id}",
                _username_shields(conn, username),
            )
        )
    for table in _anon_tables(conn):
        for col, _not_null in _text_columns(conn, table):
            for needle, replacement, shields in needles:
                where, where_params, set_expr, set_params = _shielded_rewrite(
                    col, needle, replacement, shields
                )
                ops.append(
                    (
                        "text_rewrites",
                        f"{table}.{col}",
                        table,
                        f'SELECT COUNT(*) FROM "{table}" WHERE {where}',
                        where_params,
                        f'UPDATE "{table}" SET "{col}" = {set_expr} '
                        f"WHERE {where}",
                        (*set_params, *where_params),
                    )
                )

    # 3. The id itself, in every column the schema says names a member.
    for table, cols in _anon_subject_columns(conn):
        for col in cols:
            ops.append(
                (
                    "id_rewrites",
                    f"{table}.{col}",
                    table,
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" = ?',
                    (user_id,),
                    f'UPDATE "{table}" SET "{col}" = ? WHERE "{col}" = ?',
                    (surrogate_id, user_id),
                )
            )
    return ops


def _read_identity(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    """The member's username and display name, read BEFORE anything is rewritten.

    The text sweep needs the username string, and the roster row that holds it
    is one of the rows the sweep is about to overwrite.
    """
    row = conn.execute(
        "SELECT username, display_name FROM known_users "
        "WHERE user_id = ? AND username != '' ORDER BY updated_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not row:
        return "", ""
    return str(row[0] or ""), str(row[1] or "")


_Operation = tuple[str, str, str, str, tuple, str, tuple]


def _guild_scope(
    conn: sqlite3.Connection, guild_id: int, channel_ids: Sequence[int]
) -> Callable[[str], tuple[str, tuple] | None]:
    """A per-table WHERE fragment selecting *guild_id*'s rows, or ``None``.

    The same three routes ``guild_purge_service`` uses to delete a whole
    guild: a ``guild_id`` column, a parent chain for child tables, and the
    channel list for channel-keyed tables. ``None`` means the table is not
    this guild's data in any way the schema can show — a bot-wide table, or a
    channel-keyed one when no channels were given.
    """
    channels = tuple(channel_ids)
    cache: dict[str, tuple[str, tuple] | None] = {}

    def where_for(table: str) -> tuple[str, tuple] | None:
        if table in cache:
            return cache[table]
        scope: tuple[str, tuple] | None = None
        if "guild_id" in _table_columns(conn, table):
            scope = ('"guild_id" = ?', (guild_id,))
        elif table in guild_purge_service.CHILD_TABLES:
            cond, params = guild_purge_service.child_where(table, guild_id)
            scope = (cond, tuple(params))
        elif table in guild_purge_service.CHANNEL_TABLES and channels:
            col = guild_purge_service.CHANNEL_TABLES[table]
            marks = ", ".join("?" for _ in channels)
            scope = (f'"{col}" IN ({marks})', channels)
        cache[table] = scope
        return scope

    return where_for


def _scoped_operations(
    conn: sqlite3.Connection,
    user_id: int,
    surrogate_id: int,
    username: str,
    *,
    guild_id: int | None,
    channel_ids: Sequence[int],
) -> tuple[list[_Operation], list[_Operation]]:
    """:func:`_anon_operations`, split into ``(to_run, out_of_scope)``.

    With no guild this is the full list and nothing is out of scope. With one,
    each operation gains ``AND (<scope>)``. That is safe to append because
    every statement the builders emit ends in its WHERE clause, which is either
    a single predicate or an AND chain with any OR already parenthesised. The
    count and the apply are narrowed identically, so the dry run still agrees
    with the run. The scope never reads a column the sweep rewrites (it reads
    ``guild_id``, a parent's key or a channel id), so rewriting one table
    cannot move another table's rows in or out of scope mid-run.
    """
    ops = _anon_operations(conn, user_id, surrogate_id, username)
    if guild_id is None:
        return ops, []
    where_for = _guild_scope(conn, guild_id, channel_ids)
    inside: list[_Operation] = []
    outside: list[_Operation] = []
    for op in ops:
        bucket, label, table, count_sql, count_params, apply_sql, apply_params = op
        scope = where_for(table)
        if scope is None:
            outside.append(op)
            continue
        cond, params = scope
        inside.append(
            (
                bucket,
                label,
                table,
                f"{count_sql} AND ({cond})",
                (*count_params, *params),
                f"{apply_sql} AND ({cond})",
                (*apply_params, *params),
            )
        )
    return inside, outside


def _note_out_of_scope(
    conn: sqlite3.Connection, report: AnonymizeReport, ops: list[_Operation]
) -> None:
    """Count what a guild-scoped run leaves naming the member, and say so."""
    for _bucket, label, table, count_sql, count_params, _a, _p in ops:
        rows = _anon_count(conn, count_sql, count_params, table=table)
        if rows:
            report.out_of_scope[label] = report.out_of_scope.get(label, 0) + rows


def _note_refused_tables(conn: sqlite3.Connection, report: AnonymizeReport) -> None:
    """Put any table the schema walk refused to name onto the report.

    ``table_names`` skips a name ``sql_identifier`` rejects, which keeps one odd
    table from blocking every erasure — but the sweeps then miss it silently,
    and the operator reads this report, not the log, before telling a subject
    their id is gone.
    """
    refused: list[str] = []
    guild_purge_service.table_names(conn, refused=refused)
    for name in refused:
        report.skipped.append(f"{name} (name is not a plain SQL identifier)")


def plan_anonymization(
    conn: sqlite3.Connection, user_id: int, *, surrogate_id: int | None = None
) -> AnonymizeReport:
    """What :func:`anonymize_user_data` would change. Writes nothing.

    Read-only, so it is safe against the live database — open it ``mode=ro``
    and this is the dry run ``scripts/anonymize_user_data.py`` prints.
    """
    return _plan_anonymization(
        conn, user_id, surrogate_id, guild_id=None, channel_ids=()
    )


def plan_guild_anonymization(
    conn: sqlite3.Connection,
    user_id: int,
    guild_id: int,
    *,
    channel_ids: Sequence[int] = (),
    surrogate_id: int | None = None,
) -> AnonymizeReport:
    """What :func:`anonymize_user_guild_data` would change. Writes nothing."""
    return _plan_anonymization(
        conn, user_id, surrogate_id, guild_id=guild_id, channel_ids=channel_ids
    )


def _plan_anonymization(
    conn: sqlite3.Connection,
    user_id: int,
    surrogate_id: int | None,
    *,
    guild_id: int | None,
    channel_ids: Sequence[int],
) -> AnonymizeReport:
    surrogate = (
        surrogate_id if surrogate_id is not None else next_surrogate_id(conn)
    )
    username, display_name = _read_identity(conn, user_id)
    report = AnonymizeReport(
        user_id=user_id,
        surrogate_id=surrogate,
        username=username,
        display_name=display_name,
        kept=sorted(ANONYMIZE_KEEP_TABLES),
        scope_guild_id=guild_id,
    )
    _note_refused_tables(conn, report)
    ops, unscoped = _scoped_operations(
        conn, user_id, surrogate, username,
        guild_id=guild_id, channel_ids=channel_ids,
    )
    for bucket, label, table, count_sql, count_params, _a, _p in ops:
        rows = _anon_count(conn, count_sql, count_params, table=table)
        if rows is None:
            report.skipped.append(label)
        elif rows:
            getattr(report, bucket)[label] = (
                getattr(report, bucket).get(label, 0) + rows
            )
    _note_out_of_scope(conn, report, unscoped)
    return report


def anonymize_user_data(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    surrogate_id: int,
    roster_guild_id: int = 0,
) -> AnonymizeReport:
    """Rewrite *user_id* to *surrogate_id* everywhere, and destroy the names.

    The third privacy path, beside ``purge_user_data`` (hard erasure) and
    ``export_user_data`` (access request). Use it when a member has left and
    their data must stop identifying them **while the server's activity
    statistics stay comparable over time** — the case the purge answers badly,
    since it destroys 163,701 of 184,009 rows including the whole XP timeline.

    Every metric here is an aggregate over rows keyed by a member id, and
    aggregation needs that key to be stable and distinct, not meaningful. So
    the id becomes a small integer far below the snowflake range and **every
    count, sum, rate and distribution is mathematically unchanged** —
    ``tests/privacy/test_privacy_anonymize.py`` computes a battery of them before and
    after and asserts equality, because that is the whole claim of this
    function and a docstring is not enforcement.

    **ALL GUILDS AT ONCE**, which is why there is no ``guild_id`` parameter.
    Anonymising one guild while the member's roster row survives in another
    leaves their real name beside timestamps that line up exactly with the
    surrogate's activity, so a partial run is trivially reversible by anyone
    holding the database. A pleasant side effect: the 22 tables with no
    ``guild_id`` column need no parent join, unlike in the purge.
    :func:`anonymize_user_guild_data` exists for the one case where all guilds
    is wrong — the member is still in another — and its docstring states the
    weaker result.

    **WHAT THIS IS NOT.** Strong pseudonymisation, not mathematical anonymity.
    The subject's 18,654 messages keep their timestamps and channel ids, so
    somebody with Discord scrollback for those channels can still correlate the
    surrogate back to a real account. What the rewrite removes is the ability
    to do that *from this database alone*. State that plainly to anyone who
    asks rather than calling the result anonymous.

    **What survives on purpose.** ``ANONYMIZE_KEEP_TABLES`` holds the real id
    in the sanction record, the DM consent evidence and the no-contact orders —
    rows whose entire payload is naming a person, and in the last case rows
    that must keep matching a live id if the account returns. They are listed
    in the returned report's ``kept``, and each has its reasoning at the
    constant. The retained ids are isolated: every *other* table now holds the
    surrogate, so a kept row still names the person in that record but can no
    longer be joined to their 48,646 XP events.

    **The display name is not swept, and that is deliberate.** The numeric id
    and the *username* are replaced everywhere, including inside JSON and other
    members' message text. The display name is left alone in prose, because it
    is free-form and routinely a common word: sweeping ``Lark`` across
    production would have rewritten 246 question-bank routing tags, a
    ``cdn.discordapp.com`` attachment URL, other members' channel and role
    names, and an ordinary phrase that happened to contain the same word — while ``"I have a crush on cleared
    user 1"`` still identifies them from context. A username is a globally
    unique handle with no spaces; a display name is not. Handles are unique but
    **not prefix-free**, so the username needle is not unconditionally safe
    either — ``lark_hollow`` is a substring of the live member's
    ``lark_hollow92`` — and :func:`_username_shields` parks every colliding
    handle out of the way for the length of the statement so one member's
    anonymisation can never rewrite another's record. Where the display name is
    genuinely the member's *own record* it is cleared by
    ``ANONYMIZE_FREE_TEXT_COLUMNS`` or overwritten on the roster row below.

    The decisive case, measured on 2026-09-11: production holds a **second,
    current member** whose display name is ``Lark Hollow`` and whose username
    is ``lark_hollow92``. A ``lark`` needle does not merely damage prose, it
    rewrites a different living person's name — so the sweep cannot be made
    safe by narrowing it to a speaker-prefix pattern either. This is the cost
    of the decision and it is a real residue, not a rounding error: after the
    transcript clear, roughly 500 ``rules_events.window_json`` blobs belonging
    to *other* members' rows still quoted the subject's lines under their display
    name, because they were simply talking in the channel when somebody else's
    message was flagged. Erasing those would have meant erasing other members'
    evidence windows, and no needle can tell their name from Lark Locket's. That
    table, and the transcript clear with it, went when Rules Watch was dropped
    (2026-09-13, migration 227); the limit itself stands for any text that
    quotes a member by display name. The honest statement is that this
    database no longer identifies them *by id or username*, and that a reader
    who already knows the display name can still find them in other people's
    text.

    **No mapping table is written.** Retaining a surrogate↔real-id key would
    make this pseudonymisation under Recital 26 and the result would still be
    personal data. One-way is the point, so this cannot be undone — take a
    backup first if you want the option.

    Transaction contract, as ``purge_user_data``: the caller owns it. Run this
    on one connection and commit at the end. Unlike the purge, only genuine
    schema drift is tolerated per statement (see :func:`_anon_execute`) — a
    constraint failure propagates and rolls the whole thing back rather than
    leaving the member half-rewritten.

    Raises ``ValueError`` if the surrogate is out of range, already in use, or
    if *user_id* is itself below the snowflake floor.

    *roster_guild_id* is only used when the member has no ``known_users`` row
    at all: the surrogate is then registered under that guild so it counts as
    taken (see :func:`_apply_anonymization`).
    """
    return _apply_anonymization(
        conn,
        user_id,
        surrogate_id,
        guild_id=None,
        channel_ids=(),
        roster_guild_id=roster_guild_id,
    )


def anonymize_user_guild_data(
    conn: sqlite3.Connection,
    user_id: int,
    guild_id: int,
    *,
    surrogate_id: int,
    channel_ids: Sequence[int] = (),
) -> AnonymizeReport:
    """:func:`anonymize_user_data` narrowed to the rows of one guild.

    For a member who has left *guild_id* but is still in another guild the bot
    shares. The all-guild rewrite would anonymise them in a server they are
    still part of. The caller is the departure worker
    (``departure_erasure_service``), which chooses between the two.

    **WEAKER, AND SAID SO.** The all-guild docstring's reason for taking no
    ``guild_id`` still holds. The member's roster row in the other guild keeps
    their real name, and activity timestamps line up with the surrogate's, so
    anyone holding the database can link the two. What this buys is that the
    left guild's rows stop naming them by id or username. Never describe it as
    the all-guild result.

    Every statement :func:`_anon_operations` builds is narrowed by one extra
    condition. The operation list, its order and the KEEP set are exactly the
    all-guild ones:

    - a table with a ``guild_id`` column → ``guild_id = ?``;
    - a child table (``guild_purge_service.CHILD_TABLES``) → its parent's
      guild, resolved through as many parents as it has;
    - a channel-keyed table → ``channel_id IN (channel_ids)``, the guild's
      channels as the caller sees them. Left alone when none are given;
    - anything else is not this guild's data and is left alone. That covers
      the bot-wide ``GLOBAL_TABLES``, such as the shared question bank, and
      any unclassified table.

    Rows left alone that DO name the member are counted in the report's
    ``out_of_scope``, so the gap is visible rather than implied. The roster
    relabel touches only this guild's ``known_users`` row; the member's roster
    row in another guild is unchanged.
    """
    return _apply_anonymization(
        conn,
        user_id,
        surrogate_id,
        guild_id=guild_id,
        channel_ids=tuple(channel_ids),
        roster_guild_id=guild_id,
    )


def _apply_anonymization(
    conn: sqlite3.Connection,
    user_id: int,
    surrogate_id: int,
    *,
    guild_id: int | None,
    channel_ids: Sequence[int],
    roster_guild_id: int,
) -> AnonymizeReport:
    _validate_surrogate(conn, user_id, surrogate_id)
    username, display_name = _read_identity(conn, user_id)

    report = AnonymizeReport(
        user_id=user_id,
        surrogate_id=surrogate_id,
        username=username,
        display_name=display_name,
        kept=sorted(ANONYMIZE_KEEP_TABLES),
        scope_guild_id=guild_id,
    )
    _note_refused_tables(conn, report)

    ops, unscoped = _scoped_operations(
        conn, user_id, surrogate_id, username,
        guild_id=guild_id, channel_ids=channel_ids,
    )
    _note_out_of_scope(conn, report, unscoped)
    for bucket, label, table, _c, _cp, apply_sql, apply_params in ops:
        rows = _anon_execute(conn, apply_sql, apply_params, table=table)
        if rows is None:
            report.skipped.append(label)
        elif rows:
            getattr(report, bucket)[label] = (
                getattr(report, bucket).get(label, 0) + rows
            )

    # The roster row, last — by now its `user_id` is the surrogate, rewritten
    # by the sweep like any other subject column.
    #
    # Rewritten rather than deleted, which matters more than it looks:
    # `name_resolver.resolve_name_from` falls back to `mention(user_id)`, so
    # deleting the row would leave every embed, report and leaderboard
    # rendering a bare `<@1>`. One row, and each of those surfaces gets a clean
    # label with no other code change. `is_bot = 0` keeps the surrogate
    # *counted* — `core/bot_exclusion` filters on `is_bot = 1`, so flagging it
    # as a bot would drop it from the very metrics this function exists to
    # preserve. `current_member = 0` is the truth (they left) and is what
    # `health_metrics` already filters on.
    #
    # A guild-scoped run relabels only that guild's row: the member's roster
    # row in a guild they are still in is theirs, not this run's.
    label = f"Cleared user {surrogate_id}"
    handle = f"cleared_user_{surrogate_id}"
    relabel_sql = (
        "UPDATE known_users SET display_name = ?, username = ?, "
        "is_bot = 0, current_member = 0 WHERE user_id = ?"
    )
    relabel_params: tuple = (label, handle, surrogate_id)
    if guild_id is not None:
        relabel_sql += " AND guild_id = ?"
        relabel_params = (*relabel_params, guild_id)
    labelled = _anon_execute(conn, relabel_sql, relabel_params, table="known_users")
    if labelled:
        report.id_rewrites["known_users.label"] = labelled
    elif labelled == 0:
        # No roster row to relabel, so REGISTER one. This used to be reported
        # and left, which quietly broke the surrogate allocator:
        # `next_surrogate_id` reads `known_users`, so a surrogate with rows
        # everywhere but no roster row was never marked as taken, and the next
        # anonymisation was handed the same number. Two departed members would
        # then share one surrogate, their metrics merged for good. The
        # registered row is the same label the relabel writes and points
        # nowhere, so it is no more a mapping than the relabelled row is.
        conn.execute(
            "INSERT INTO known_users (guild_id, user_id, username, display_name, "
            "updated_at, is_bot, current_member) VALUES (?, ?, ?, ?, ?, 0, 0)",
            (roster_guild_id, surrogate_id, handle, label, time.time()),
        )
        report.id_rewrites["known_users.registered"] = 1
        log.warning(
            "Anonymise: no known_users row for surrogate %s — registered one "
            "under guild %s so the number is never handed out again",
            surrogate_id,
            roster_guild_id,
        )
    else:
        report.skipped.append("known_users.label")

    log.info(
        "Anonymise: user %s → surrogate %s (%s); %d rows across %d id columns, "
        "%d text rewrites, %d texts cleared",
        user_id,
        surrogate_id,
        "all guilds" if guild_id is None else f"guild {guild_id}",
        report.total_rows,
        len(report.id_rewrites),
        sum(report.text_rewrites.values()),
        sum(report.text_cleared.values()),
    )
    return report


def anonymize_missed_text(
    conn: sqlite3.Connection, surrogate_id: int, *, dry_run: bool = False
) -> AnonymizeReport:
    """Run the text clears against a surrogate that is ALREADY anonymised.

    This exists because the clear lists are the part of this module that grows.
    An id rewrite is complete the moment it runs — the schema tells it every
    column — but "which text is the member's own words" is a judgement made
    table by table, and the 2026-09-11 production run shipped with three of
    those judgements missing. The id sweep had already reached
    ``rules_events``, ``audit_log`` and ``qa_verdicts``, so the report said the
    member was anonymised while a transcript quoting them, the voice-room names
    they typed and a QA note in their own words sat beside surrogate 1.

    :func:`anonymize_user_data` cannot be re-run to fix that: it refuses a
    subject below the snowflake floor, because rewriting one surrogate into
    another would silently merge two anonymised members' metrics. So this runs
    the text half only — no id rewrite, no roster relabel, nothing that could
    move a number — and takes the **inverse** guard: the subject must be a
    surrogate, never a real account. Pointed at a live member it would destroy
    their words while leaving them fully identified, which is the one outcome
    worse than doing nothing.

    Transaction contract as elsewhere: the caller owns it.
    """
    if not 0 < surrogate_id < SURROGATE_ID_CEILING:
        raise ValueError(
            f"{surrogate_id} is not a surrogate (expected 1.."
            f"{SURROGATE_ID_CEILING - 1}). This clears a member's text without "
            "anonymising them, so it must never be pointed at a real account."
        )

    report = AnonymizeReport(user_id=surrogate_id, surrogate_id=surrogate_id)
    report.kept = sorted(ANONYMIZE_KEEP_TABLES)

    for bucket, label, table, count_sql, count_params, sql, params in (
        _anon_text_clear_operations(conn, surrogate_id)
    ):
        if dry_run:
            rows = _anon_count(conn, count_sql, count_params, table=table)
        else:
            rows = _anon_execute(conn, sql, params, table=table)
        if rows is None:
            report.skipped.append(label)
        elif rows:
            # Accumulate: two operations can share a label when one table's
            # text is reached by more than one clear, and assigning would
            # report only the last of them. plan_anonymization already sums
            # this way — the two must agree or the dry run understates.
            bucket_counts = getattr(report, bucket)
            bucket_counts[label] = bucket_counts.get(label, 0) + rows

    log.info(
        "Anonymise: text remediation for surrogate %s %s %d rows",
        surrogate_id,
        "would clear" if dry_run else "cleared",
        report.total_rows,
    )
    return report


# ── Subject access / portability (GDPR Art 15 + Art 20) ──────────────────────

# Column names that identify a member. The export finds a subject's rows by
# intersecting each table's columns with this set rather than from a curated
# table list: the curated list is what goes stale, and a stale access export is
# an incomplete answer to a statutory request. A new feature's table is covered
# the day it lands, provided it names its member column conventionally.
#
# The convention is only as good as the sweep that checks it, which is what
# ``scripts/privacy_coverage.py`` does: it reads a live database and reports
# every column whose values are real member ids but whose name is not in here.
# The 2026-09-02 GDPR review ran it against production and added seven such
# names — ``hidden_by``, ``labeled_by``, ``player_a``/``player_b``,
# ``posted_by``, ``updated_by`` and ``verified_by`` — each of which named a
# member in every table carrying it while being invisible to an access request.
# A second pass added four more the same way: ``active_player``,
# ``answer_id`` (the member a Guess round is *about*), ``closed_by`` and
# ``resolved_by``. The last three matter even where the table was already
# exported through some other column: a member who only ever *closed* a
# ticket or *resolved* an intake card matched nothing, so their rows were
# silently absent from an answer that looked complete.
SUBJECT_ID_COLUMNS = frozenset(
    {
        "active_player", "actor_id", "added_by", "answer_id", "approved_by",
        "asker_id", "author_id",
        "beneficiary_id", "blocked_user_id", "challenger_id", "claimed_by",
        "claimer_id", "closed_by", "completed_by", "created_by", "creator_id",
        "done_by",
        "editor_id",
        # grant_role_permissions (and its dead prod-only predecessor
        # give_role_permissions) name a member as "entity_id" alongside an
        # entity_type of 'user' or 'role' — a role id can never collide with a
        # member id, so matching on it is safe and it is the only way those
        # keeper allow-lists reach an access export at all.
        "entity_id",
        "extra_questioner_id", "from_user_id", "guessed_id", "guessed_user_id",
        "guesser_id", "hidden_by", "high_bidder_id", "highest_user",
        "holder_id", "host_id",
        "invitee_id", "inviter_id", "labeled_by", "last_winner_id", "loser_id",
        "lowest_user",
        "member_id", "mod_id", "moderator_id", "opened_by", "opener_id",
        "original_author_id",
        "owner_id", "partner_id", "player_a", "player_b", "player_id",
        "posted_by", "poster_id", "protected_user_id",
        "quoted_id", "quoted_user_id", "quoter_id", "reactor_id", "recipient_id",
        "replier_id", "reporter_id", "requester_id", "resolved_by",
        "resolver_id",
        "reviewed_by",
        "second_highest_user", "second_lowest_user", "sender_id", "set_by",
        "solver_id", "subject_id", "submitter_id",
        "target_author_id", "target_id", "to_user_id", "updated_by",
        "updated_by_user_id",
        "user1_id", "user2_id", "user_a", "user_a_id", "user_b", "user_b_id",
        "user_high", "user_id", "user_low", "verified_by", "voided_by",
        "voter_id",
        "watched_user_id", "watcher_user_id", "winner_id",
    }
)

# Known blind spot: a handful of columns store a *list* of member ids as JSON or
# CSV (``econ_demurrage_sweeps.taxed_members``,
# ``risky_pending_questions.participant_user_ids`` / ``lowest_tie_user_ids`` /
# ``questioners_asked``, ``risky_posted_questions.allowed_replier_ids``,
# ``confession_config.blocked_user_ids``, ``revive_events.follow_authors``). A subject inside one of those lists is not
# found by an equality match and will not appear in the export. The volumes are
# small and the content is incidental, but it is a gap, not an absence — the
# runbook tells the operator to grep them by hand.
LIST_VALUED_MEMBER_COLUMNS = (
    ("confession_config", "blocked_user_ids"),
    # A group Hot Potato row keeps its whole roster in JSON/CSV lists. The
    # purge matches `roster` and `alive` by exact membership and takes the row
    # whole; the export cannot match inside a list, so the gap is disclosed
    # here (2026-09-02 GDPR review).
    ("hp_group_games", "alive"),
    ("hp_group_games", "elimination_order"),
    ("hp_group_games", "pass_log"),
    ("hp_group_games", "roster"),
    # from_user chip values live in the conditions JSON (purge strips them;
    # export can only disclose the gap).
    ("mention_award_rules", "conditions"),
    ("econ_demurrage_sweeps", "taxed_members"),
    ("revive_events", "follow_authors"),
    ("risky_pending_questions", "lowest_tie_user_ids"),
    ("risky_pending_questions", "participant_user_ids"),
    ("risky_pending_questions", "questioners_asked"),
    ("risky_posted_questions", "allowed_replier_ids"),
    # The rest of the games family (review H1, 2026-09-14). The purge takes
    # each row whole on a digit-bounded match (`_delete_game_records`); the
    # payloads also key members' scores and rolls by id.
    ("chicken_games", "alive"),
    ("chicken_games", "bail_log"),
    ("chicken_games", "elimination_order"),
    ("chicken_games", "roster"),
    ("games_active_games", "payload"),
    ("games_game_history", "payload"),
    ("hot_potato_games", "pass_log"),
    ("mc_games", "alive"),
    ("mc_games", "elimination_order"),
    ("mc_games", "roster"),
    ("mc_games", "seated"),
    ("pressure_games", "pumps_json"),
)

# Tables whose rows name a *second* member, where that person's identity is the
# payload rather than incidental. The subject is entitled to their own data, but
# Art 15(4) says an access request must not adversely affect others' rights — so
# these are surfaced for operator review before disclosure rather than silently
# included or silently dropped. Redacting automatically would corrupt the
# record; deciding for the operator would hide the tension.
THIRD_PARTY_TABLES = frozenset(
    {
        "anon_audit_log", "audit_log", "confession_threads", "dm_audit_log",
        "dm_batch_pending",
        "dm_consent_pairs", "dm_requests", "econ_msg_replies", "guess_audit_log",
        "guess_guesses", "guess_rounds", "inactive_members", "invite_edges",
        "jails", "no_contact_events", "no_contact_pairs", "pen_pals_blocks",
        "pen_pals_sessions", "quote_audit_log",
        "reaction_tip_awards", "starboard_reactors",
        "mahjong_tables",
        "tickets", "warnings",
        "watched_users", "whisper_guesses", "whisper_replies",
        "whisper_reports", "whispers",
    }
)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def export_user_data(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> dict:
    """Collect every row naming *user_id* into a JSON-ready dict.

    This is the read half of the erasure path — the answer to a subject access
    request (Art 15) and a portability request (Art 20). The operator procedure
    is in ``docs/gdpr_runbook.md``; ``scripts/export_user_data.py`` is
    the CLI.

    **The export is deliberately a superset of the purge.** ``purge_user_data``
    skips categories the server keeps under Art 17(3) — the ``econ_ledger``
    double-entry record, sanction history, consent audit, no-contact orders.
    Retaining data does not exempt it from disclosure, so those rows are
    exported even though they are never deleted. Anything scoped narrower than
    "every table naming this member" would answer the wrong question.

    Read-only: opens no transaction and writes nothing.

    Returns ``{"subject", "tables", "counts", "review_required", "notes"}``.
    ``tables`` maps table name → ``{"matched_columns", "guild_scoped", "rows"}``.
    ``review_required`` lists the tables that name a second member.
    """
    tables: dict[str, dict] = {}
    notes: list[str] = []

    refused: list[str] = []
    names = guild_purge_service.table_names(conn, refused=refused)
    for name in refused:
        # An access answer that quietly omits a table is the wrong answer. The
        # name cannot go into a statement, so say on the export itself that it
        # was not searched rather than leaving it to the log.
        notes.append(
            f"{name}: not searched — its name is not a plain SQL identifier, "
            "so no query can safely name it. Check it by hand."
        )

    for table in names:
        if table in _MESSAGE_CHILD_TABLES:
            continue  # reached below, through the author's message ids
        try:
            cols = _table_columns(conn, table)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            log.warning("Export: cannot introspect %s (%s)", table, exc)
            continue

        matched = sorted(set(cols) & SUBJECT_ID_COLUMNS)
        if not matched:
            continue

        guild_scoped = "guild_id" in cols
        where = " OR ".join(f'"{c}" = ?' for c in matched)
        params: list[int] = [user_id] * len(matched)
        sql = f'SELECT * FROM "{table}" WHERE ({where})'
        if guild_scoped:
            sql += " AND guild_id = ?"
            params.append(guild_id)

        try:
            cur = conn.execute(sql, tuple(params))
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            # Same schema-drift tolerance as the purge: one unreadable table
            # must not cost the subject the rest of their answer.
            log.warning("Export: failed on %s (%s)", table, exc)
            notes.append(f"{table}: unreadable ({exc})")
            continue

        if rows and not guild_scoped:
            # Only worth saying when it actually affects this subject's answer.
            notes.append(
                f"{table}: has no guild_id — its rows span every guild, not just {guild_id}"
            )
        if rows:
            tables[table] = {
                "matched_columns": matched,
                "guild_scoped": guild_scoped,
                "rows": rows,
            }

    _export_message_children(conn, guild_id, user_id, tables)

    present = {
        f"{t}.{c}"
        for t, c in LIST_VALUED_MEMBER_COLUMNS
        if t in {n for n in names}
    }
    if present:
        notes.append(
            "list-valued member columns are not searchable by equality and are "
            "NOT covered by this export — grep them by hand: "
            + ", ".join(sorted(present))
        )

    return {
        "subject": {"guild_id": guild_id, "user_id": user_id},
        "tables": tables,
        "counts": {t: len(v["rows"]) for t, v in sorted(tables.items())},
        "review_required": sorted(set(tables) & THIRD_PARTY_TABLES),
        "notes": notes,
    }


def _export_message_children(
    conn: sqlite3.Connection, guild_id: int, user_id: int, tables: dict
) -> None:
    """Add the message children, joined through the author's message ids.

    Chunked exactly as the purge is: a heavy poster's id list would otherwise
    blow SQLite's bound-variable cap, and the accounts most likely to file an
    access request are the ones with the most rows.
    """
    msg_ids = [
        r[0]
        for r in conn.execute(
            "SELECT message_id FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        ).fetchall()
    ]
    if not msg_ids:
        return

    for table in _MESSAGE_CHILD_TABLES:
        try:
            cols = _table_columns(conn, table)
        except sqlite3.Error:  # pragma: no cover - defensive
            continue
        if not cols:
            continue
        rows: list[dict] = []
        try:
            for chunk in _chunks(msg_ids):
                ph = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f'SELECT * FROM "{table}" WHERE message_id IN ({ph})',
                    tuple(chunk),
                )
                rows.extend(dict(zip(cols, r)) for r in cur.fetchall())
        except sqlite3.Error as exc:
            log.warning("Export: failed on %s (%s)", table, exc)
            continue
        if rows:
            tables[table] = {
                "matched_columns": ["message_id (via messages.author_id)"],
                "guild_scoped": True,
                "rows": rows,
            }
