"""Guess cog — DB query layer (sync sqlite3)."""
from __future__ import annotations

import json
import sqlite3
import time

from bot_modules.core.db_utils import get_config_value, set_config_value
from bot_modules.games.guess.models import (
    GuessAuditEvent,
    GuessConfig,
    GuessGuess,
    GuessRound,
)

_CONFIG_DEFAULTS: dict[str, str] = {
    "guess_role_id": "0",
    "guess_channel_id": "0",
    "guess_guess_cooldown_seconds": "60",
    "guess_crop_difficulty": "medium",
    "guess_min_image_dimension_px": "400",
    "guess_max_image_size_mb": "10",
    "guess_prompt_message_id": "0",
    "guess_prompt_channel_id": "0",
    "guess_submit_max_per_window": "5",
    "guess_submit_window_seconds": "3600",
    "guess_max_guesses_per_round": "5",
    "guess_round_lifetime_days": "0",
}


def get_guess_config(conn: sqlite3.Connection, guild_id: int) -> GuessConfig:
    def _get(key: str) -> str:
        return get_config_value(conn, key, _CONFIG_DEFAULTS[key], guild_id)

    return GuessConfig(
        guild_id=guild_id,
        guess_role_id=int(_get("guess_role_id") or 0),
        guess_channel_id=int(_get("guess_channel_id") or 0),
        guess_cooldown_seconds=int(_get("guess_guess_cooldown_seconds") or 60),
        crop_difficulty=_get("guess_crop_difficulty"),
        min_image_dimension_px=int(_get("guess_min_image_dimension_px") or 400),
        max_image_size_mb=int(_get("guess_max_image_size_mb") or 10),
        prompt_message_id=int(_get("guess_prompt_message_id") or 0),
        prompt_channel_id=int(_get("guess_prompt_channel_id") or 0),
        submit_max_per_window=int(_get("guess_submit_max_per_window") or 5),
        submit_window_seconds=int(_get("guess_submit_window_seconds") or 3600),
        max_guesses_per_round=int(_get("guess_max_guesses_per_round") or 5),
        round_lifetime_days=int(_get("guess_round_lifetime_days") or 0),
    )


def set_guess_config_value(
    conn: sqlite3.Connection, guild_id: int, key: str, value: str
) -> None:
    """key is the full config key, e.g. 'guess_crop_difficulty'."""
    set_config_value(conn, key, value, guild_id)


def _row_to_round(row: sqlite3.Row) -> GuessRound:
    return GuessRound(
        id=row["id"],
        guild_id=row["guild_id"],
        submitter_id=row["submitter_id"],
        answer_id=row["answer_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        crop_path=row["crop_path"],
        crop_url=row["crop_url"],
        original_path=row["original_path"],
        difficulty=row["difficulty"],
        candidate_count=row["candidate_count"],
        reroll_count=row["reroll_count"],
        created_at=row["created_at"],
        solved_at=row["solved_at"],
        solver_id=row["solver_id"],
        guesses_to_solve=row["guesses_to_solve"],
        unique_guessers_to_solve=row["unique_guessers_to_solve"],
        answer_optout=bool(row["answer_optout"]),
        deleted_at=row["deleted_at"],
        crop_box_x1=row["crop_box_x1"],
        crop_box_y1=row["crop_box_y1"],
        crop_box_x2=row["crop_box_x2"],
        crop_box_y2=row["crop_box_y2"],
        round_type=row["round_type"],
        confession_text=row["confession_text"],
        confession_prompt_text=row["confession_prompt_text"],
        expired_at=row["expired_at"],
        expiry_cleared_at=row["expiry_cleared_at"],
    )


def insert_round(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    submitter_id: int,
    answer_id: int,
    channel_id: int = 0,
    message_id: int = 0,
    crop_path: str = "",
    crop_url: str = "",
    difficulty: str = "medium",
    candidate_count: int = 0,
    round_type: str = "photo",
    confession_text: str = "",
    confession_prompt_text: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO guess_rounds
            (guild_id, submitter_id, answer_id, channel_id, message_id,
             crop_path, crop_url, difficulty, candidate_count, created_at,
             round_type, confession_text, confession_prompt_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, submitter_id, answer_id, channel_id, message_id,
         crop_path, crop_url, difficulty, candidate_count, time.time(),
         round_type, confession_text, confession_prompt_text),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_round(conn: sqlite3.Connection, round_id: int) -> GuessRound | None:
    row = conn.execute("SELECT * FROM guess_rounds WHERE id = ?", (round_id,)).fetchone()
    return _row_to_round(row) if row else None


def update_round_message(
    conn: sqlite3.Connection, round_id: int, *, message_id: int, crop_url: str, crop_path: str
) -> None:
    conn.execute(
        "UPDATE guess_rounds SET message_id = ?, crop_url = ?, crop_path = ? WHERE id = ?",
        (message_id, crop_url, crop_path, round_id),
    )


def set_round_crop_box(
    conn: sqlite3.Connection, round_id: int, x1: float, y1: float, x2: float, y2: float
) -> None:
    conn.execute(
        "UPDATE guess_rounds SET crop_box_x1=?, crop_box_y1=?, crop_box_x2=?, crop_box_y2=? WHERE id=?",
        (x1, y1, x2, y2, round_id),
    )


def set_round_original_path(
    conn: sqlite3.Connection, round_id: int, original_path: str
) -> None:
    conn.execute(
        "UPDATE guess_rounds SET original_path = ? WHERE id = ?",
        (original_path, round_id),
    )


def list_stale_open_originals(
    conn: sqlite3.Connection, *, older_than: float
) -> list[tuple[int, str]]:
    """Unsolved, undeleted rounds created before *older_than* whose original
    submission is still cached on disk. An **expired** round counts: the expiry
    clean-up deletes its original, but when that step fails after the claim the
    path is still set, and excluding the round here would leave the photo on
    disk for good.

    Solved rounds delete their original at solve time; this is the age-out
    for rounds nobody ever solves, so a member's uncropped submission
    doesn't sit in ``guess_cache/`` forever (2026-08 review, guess G2)."""
    rows = conn.execute(
        """
        SELECT id, original_path FROM guess_rounds
         WHERE solved_at IS NULL AND deleted_at IS NULL
           AND original_path IS NOT NULL AND original_path != ''
           AND created_at < ?
        """,
        (older_than,),
    ).fetchall()
    return [(row["id"], row["original_path"]) for row in rows]


def list_cached_originals_for_user(
    conn: sqlite3.Connection, user_id: int, *, guild_id: int | None = None
) -> list[tuple[int, str]]:
    """Every round *user_id* submitted, or is the answer to, whose original is
    still cached on disk.

    The by-member counterpart of :func:`list_stale_open_originals`, for every
    erasure that takes a member's rounds: the departure auto-anonymise, the
    dashboard's Erase, and the member's own My Data → Guess Who button. No age
    cut. *guild_id* narrows it to one guild; ``None`` means every guild.

    Submitter **or answer**, matching :func:`forget_member`'s round set: until
    2026-09-27 this read the submitter only, so a round somebody else submitted
    with this member as its answer had its row deleted by an erasure while its
    file waited for the orphan sweep.

    Solved rounds clear the path at solve time, so in practice these are the
    unsolved ones. The only filter is that a path is still recorded, though, so
    a round whose solve-time delete failed, or a soft-deleted round, is caught
    too.
    """
    sql = (
        "SELECT id, original_path FROM guess_rounds "
        "WHERE (submitter_id = ? OR answer_id = ?) "
        "AND original_path IS NOT NULL AND original_path != ''"
    )
    params: list[int] = [user_id, user_id]
    if guild_id is not None:
        sql += " AND guild_id = ?"
        params.append(guild_id)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [(row["id"], row["original_path"]) for row in rows]


def list_member_round_posts(
    conn: sqlite3.Connection, user_id: int, *, guild_id: int | None = None
) -> list[tuple[int, int, int]]:
    """``(guild_id, channel_id, message_id)`` of every posted round *user_id*
    submitted or is the answer to — the crops :func:`forget_member` is about to
    orphan in Discord.

    Read **before** the rows go: once :func:`forget_member` has run nothing
    records which posts were theirs, and the crop would stay up in the Guess
    channel with no round behind it. Billy, 2026-09-27: *"Deleting the message
    should clear everything from guess."* Soft-deleted rounds are included —
    ``/guess delete`` tries the post too, but tolerates a failure.
    """
    sql = (
        "SELECT guild_id, channel_id, message_id FROM guess_rounds "
        "WHERE (submitter_id = ? OR answer_id = ?) "
        "AND channel_id IS NOT NULL AND channel_id != 0 "
        "AND message_id IS NOT NULL AND message_id != 0"
    )
    params: list[int] = [user_id, user_id]
    if guild_id is not None:
        sql += " AND guild_id = ?"
        params.append(guild_id)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


def forget_member(conn: sqlite3.Connection, *, guild_id: int, user_id: int) -> None:
    """Delete *user_id*'s Guess Who records in *guild_id*.

    The one deletion engine behind two surfaces: the member's own My Data →
    Guess Who button, and ``privacy_service.purge_user_data`` for an operator
    erasure — the ``whisper_repo.forget_member`` shape, and for the same
    reason (bug #222: a member's button and an operator erasure that delete
    different things are a bug, not a wording problem). It lived inline in the
    purge until 2026-09-27.

    Until 2026-09-20 no ``guess_*`` table but ``guess_consents`` was reached by
    an erasure at all, so the member's submitted photo or confession, every
    guess they ever made, and every guess anyone made naming them, stood
    indefinitely. That was never a decision, and it is the opposite of the
    promise the feature makes, which is that the picture of your face is the
    most sensitive thing the bot holds.

    A round goes WHOLE, taking other players' guesses on it with it (the Risky
    Rolls and Hot Potato ground in the purge): the round *is* the member's
    submission — in prod every round's submitter is also its answer — so there
    is nothing left to keep once their image and confession text go, and a
    round with no subject is not a play record, it is a hole.

    ``guess_consents`` is NOT here. It is the evidence that consent was held,
    kept through an opt-out; only a full erasure clears it, from the purge's
    own table list.

    **Files and posts are the caller's job, first.** An unlink or a Discord
    delete cannot be rolled back and this transaction belongs to the caller —
    ``guess_cleanup_service.clear_member_media`` runs before this, reading
    :func:`list_member_round_posts` and :func:`list_cached_originals_for_user`
    while the rows still say what was theirs. ``sweep_orphan_originals`` is the
    directory-reading backstop for a file whose row went out of band.

    Subqueries rather than a resolved id list, so a prolific player can never
    hit SQLite's bound-variable cap.
    """
    member_rounds = (
        "SELECT id FROM guess_rounds WHERE guild_id = ? "
        "AND (submitter_id = ? OR answer_id = ?)"
    )
    scope = (guild_id, user_id, user_id)
    conn.execute(f"DELETE FROM guess_guesses WHERE round_id IN ({member_rounds})", scope)
    conn.execute(f"DELETE FROM guess_audit_log WHERE round_id IN ({member_rounds})", scope)
    conn.execute(
        "DELETE FROM guess_rounds WHERE guild_id = ? AND (submitter_id = ? OR answer_id = ?)",
        scope,
    )
    # Their play on rounds that survive: guesses they made, and guesses other
    # members made naming them. `guess_guesses` has no guild_id, so the scope
    # comes from the round it hangs off.
    conn.execute(
        "DELETE FROM guess_guesses WHERE (guesser_id = ? OR guessed_user_id = ?) "
        "AND round_id IN (SELECT id FROM guess_rounds WHERE guild_id = ?)",
        (user_id, user_id, guild_id),
    )
    conn.execute(
        "DELETE FROM guess_audit_log WHERE guild_id = ? AND actor_id = ?",
        (guild_id, user_id),
    )
    # A round somebody else submitted that this member solved keeps its row —
    # it is the other member's record — but stops naming them. `solved_at`
    # stays set, and the solver leaderboard already filters
    # `solver_id IS NOT NULL`, so the round reads as solved by nobody rather
    # than as unsolved. The `pen_pals_sessions` seat-scrub ground.
    conn.execute(
        "UPDATE guess_rounds SET solver_id = NULL WHERE guild_id = ? AND solver_id = ?",
        (guild_id, user_id),
    )


def member_guild_ids(conn: sqlite3.Connection, user_id: int) -> list[int]:
    """Every guild where :func:`forget_member` would find something of *user_id*'s.

    For an all-guild anonymisation, which has to run the per-guild deletes in
    each: rounds they submitted, answer or solved, guesses they made or that
    name them, and their Guess-log rows.
    """
    rows = conn.execute(
        "SELECT guild_id FROM guess_rounds "
        "WHERE submitter_id = ? OR answer_id = ? OR solver_id = ? "
        "UNION SELECT r.guild_id FROM guess_guesses g "
        "JOIN guess_rounds r ON r.id = g.round_id "
        "WHERE g.guesser_id = ? OR g.guessed_user_id = ? "
        "UNION SELECT guild_id FROM guess_audit_log WHERE actor_id = ? "
        "ORDER BY 1",
        (user_id,) * 6,
    ).fetchall()
    return [int(r[0]) for r in rows]


def list_all_recorded_originals(conn: sqlite3.Connection) -> list[str]:
    """Every ``original_path`` any round still records — open, solved,
    soft-deleted, every guild.

    The orphan sweep's reference set. Its two siblings above answer "which
    rounds hold a file I should delete?"; this one answers the inverse, "which
    files on disk does anything still point at?", which is what lets the sweep
    delete by directory listing rather than by row (2026-09-17, bug #227).
    """
    rows = conn.execute(
        "SELECT original_path FROM guess_rounds "
        "WHERE original_path IS NOT NULL AND original_path != ''"
    ).fetchall()
    return [row["original_path"] for row in rows]


def clear_round_original_path(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute(
        "UPDATE guess_rounds SET original_path = '' WHERE id = ?", (round_id,)
    )


def mark_round_solved(
    conn: sqlite3.Connection, round_id: int,
    *, solver_id: int, guesses_to_solve: int, unique_guessers_to_solve: int
) -> int:
    """Mark the round solved iff it is currently unsolved. Returns rowcount.

    The ``solved_at IS NULL`` predicate is the race guard for two near-
    simultaneous correct guesses — only the winner sees rowcount == 1.
    """
    cur = conn.execute(
        """
        UPDATE guess_rounds
        SET solved_at = ?, solver_id = ?,
            guesses_to_solve = ?, unique_guessers_to_solve = ?
        WHERE id = ? AND solved_at IS NULL AND expired_at IS NULL
        """,
        (time.time(), solver_id, guesses_to_solve, unique_guessers_to_solve, round_id),
    )
    return cur.rowcount or 0


def list_expirable_rounds(
    conn: sqlite3.Connection, *, guild_id: int, older_than: float
) -> list[sqlite3.Row]:
    """Open rounds in a guild created before *older_than* that are due to expire.

    Open means unsolved, undeleted, not already expired and **not flagged
    ``answer_optout``**: a member who withdrew (or left) has had the round
    closed already. A round never posted (``message_id`` 0) has no card.
    """
    return conn.execute(
        """
        SELECT * FROM guess_rounds
         WHERE guild_id = ? AND created_at < ?
           AND solved_at IS NULL AND deleted_at IS NULL AND expired_at IS NULL
           AND answer_optout = 0
           AND message_id IS NOT NULL AND message_id > 0
         ORDER BY created_at
        """,
        (guild_id, older_than),
    ).fetchall()


def mark_round_expired(conn: sqlite3.Connection, round_id: int, *, when: float) -> int:
    """Resolve an open round as timed out. Returns the rowcount.

    ``solved_at`` and ``solver_id`` stay NULL on purpose: the top-posters query
    counts any row with ``solved_at`` set as a solve, so stamping it here would
    read every timed-out round as one on a public leaderboard (commit
    ``6edc1b55``). The predicate is the race guard against a solve landing at
    the same moment — only one of the two wins.
    """
    cur = conn.execute(
        """
        UPDATE guess_rounds SET expired_at = ?
         WHERE id = ? AND solved_at IS NULL AND deleted_at IS NULL
           AND expired_at IS NULL
        """,
        (when, round_id),
    )
    return cur.rowcount or 0


def list_uncleared_expired_rounds(
    conn: sqlite3.Connection, *, guild_id: int
) -> list[sqlite3.Row]:
    """Expired rounds whose card has not been deleted yet: this sweep's fresh
    claims plus any earlier delete that failed or was cut short by a restart."""
    return conn.execute(
        """
        SELECT * FROM guess_rounds
         WHERE guild_id = ? AND expired_at IS NOT NULL
           AND expiry_cleared_at IS NULL
           AND solved_at IS NULL AND deleted_at IS NULL
         ORDER BY created_at
        """,
        (guild_id,),
    ).fetchall()


def mark_expiry_cleared(conn: sqlite3.Connection, round_id: int, *, when: float) -> None:
    conn.execute(
        "UPDATE guess_rounds SET expiry_cleared_at = ? WHERE id = ?", (when, round_id)
    )


def soft_delete_round(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute(
        "UPDATE guess_rounds SET deleted_at = ? WHERE id = ?", (time.time(), round_id)
    )


def set_round_answer_optout(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute("UPDATE guess_rounds SET answer_optout = 1 WHERE id = ?", (round_id,))


def set_round_reroll_count(conn: sqlite3.Connection, round_id: int, reroll_count: int) -> None:
    conn.execute(
        "UPDATE guess_rounds SET reroll_count = ? WHERE id = ?", (reroll_count, round_id)
    )


def get_active_rounds_for_guild(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 50
) -> list[GuessRound]:
    rows = conn.execute(
        """
        SELECT * FROM guess_rounds
        WHERE guild_id = ? AND deleted_at IS NULL
        ORDER BY created_at DESC LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()
    return [_row_to_round(r) for r in rows]


def _row_to_guess(row: sqlite3.Row) -> GuessGuess:
    return GuessGuess(
        id=row["id"],
        round_id=row["round_id"],
        guesser_id=row["guesser_id"],
        guessed_user_id=row["guessed_user_id"],
        correct=bool(row["correct"]),
        created_at=row["created_at"],
    )


def insert_guess(
    conn: sqlite3.Connection,
    *, round_id: int, guesser_id: int, guessed_user_id: int, correct: bool
) -> int:
    cur = conn.execute(
        """
        INSERT INTO guess_guesses (round_id, guesser_id, guessed_user_id, correct, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (round_id, guesser_id, guessed_user_id, int(correct), time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_guesses_for_round(conn: sqlite3.Connection, round_id: int) -> list[GuessGuess]:
    rows = conn.execute(
        "SELECT * FROM guess_guesses WHERE round_id = ? ORDER BY created_at ASC", (round_id,)
    ).fetchall()
    return [_row_to_guess(r) for r in rows]


def count_guesses_for_round(conn: sqlite3.Connection, round_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guess_guesses WHERE round_id = ?", (round_id,)
    ).fetchone()[0]


def count_unique_guessers_for_round(conn: sqlite3.Connection, round_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT guesser_id) FROM guess_guesses WHERE round_id = ?", (round_id,)
    ).fetchone()[0]


def get_last_guess_by_user_for_round(
    conn: sqlite3.Connection, round_id: int, guesser_id: int
) -> GuessGuess | None:
    row = conn.execute(
        """
        SELECT * FROM guess_guesses
        WHERE round_id = ? AND guesser_id = ?
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (round_id, guesser_id),
    ).fetchone()
    return _row_to_guess(row) if row else None


def count_user_guesses_for_round(
    conn: sqlite3.Connection, round_id: int, guesser_id: int
) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guess_guesses WHERE round_id = ? AND guesser_id = ?",
        (round_id, guesser_id),
    ).fetchone()[0]


def insert_audit_event(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    actor_id: int,
    action: str,
    round_id: int | None = None,
    details: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO guess_audit_log (guild_id, ts, actor_id, action, round_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            time.time(),
            actor_id,
            action,
            round_id,
            json.dumps(details or {}),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_audit_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    limit: int = 100,
    action: str | None = None,
) -> list[GuessAuditEvent]:
    if action is None:
        rows = conn.execute(
            """
            SELECT * FROM guess_audit_log WHERE guild_id = ?
            ORDER BY ts DESC, id DESC LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM guess_audit_log WHERE guild_id = ? AND action = ?
            ORDER BY ts DESC, id DESC LIMIT ?
            """,
            (guild_id, action, limit),
        ).fetchall()
    return [
        GuessAuditEvent(
            id=row["id"],
            guild_id=row["guild_id"],
            ts=row["ts"],
            actor_id=row["actor_id"],
            action=row["action"],
            round_id=row["round_id"],
            details=row["details"],
        )
        for row in rows
    ]


def fresh_rounds(
    conn: sqlite3.Connection, guild_ids, since: float
) -> list[sqlite3.Row]:
    """Rounds posted since *since* that are still worth pointing someone at.

    Event Echo's sweep reads this rather than hooking the two submission
    views, so a third way of posting a round is picked up for free. It is the
    query, not the caller, that decides what "worth pointing at" means, since
    every exclusion here is a Guess Who rule:

    * ``message_id`` unset — the row exists but the card hasn't sent yet, so
      there is nothing to link to. It will be picked up on a later tick.
    * already solved — an echo landing on a finished round is worse than none.
    * ``answer_optout`` — consent was withdrawn and the round is unsolvable;
      advertising it would be pointing a crowd at someone who opted out.
    * ``deleted_at`` — removed.

    Nothing about the submitter or the answer is selected. Callers outside
    Guess Who must not be handed either: the submitter *is* the answer.
    """
    ids = [int(g) for g in guild_ids]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return conn.execute(
        f"""
        SELECT id, guild_id, channel_id, message_id, created_at
        FROM guess_rounds
        WHERE guild_id IN ({marks})
          AND created_at >= ?
          AND message_id IS NOT NULL AND message_id > 0
          AND solved_at IS NULL
          AND expired_at IS NULL
          AND answer_optout = 0
          AND deleted_at IS NULL
        ORDER BY created_at
        """,
        (*ids, since),
    ).fetchall()


def get_all_active_round_ids(conn: sqlite3.Connection) -> list[tuple[int, bool]]:
    rows = conn.execute(
        "SELECT id, solved_at IS NOT NULL AS solved FROM guess_rounds WHERE deleted_at IS NULL"
    ).fetchall()
    return [(row["id"], bool(row["solved"])) for row in rows]


def get_unsolved_round_ids(
    conn: sqlite3.Connection, *, limit: int = 1000
) -> list[int]:
    """Round IDs that still need a persistent GameView at startup.

    Solved rounds are skipped — their 'Guess late' button is fun-loop polish,
    not load-bearing. An expired round whose card is not yet deleted stays, so
    its button answers "round has ended" until the sweep clears it. Newest-first so the cap takes the most recently active.
    """
    rows = conn.execute(
        """
        SELECT id FROM guess_rounds
        WHERE deleted_at IS NULL AND solved_at IS NULL
          AND (expired_at IS NULL OR expiry_cleared_at IS NULL)
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["id"] for row in rows]


def get_top_posters(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 5
) -> list[tuple[int, int, int]]:
    """Return (submitter_id, rounds_posted, rounds_solved) ordered by rounds_posted desc."""
    rows = conn.execute(
        """
        SELECT submitter_id,
               COUNT(*) AS rounds_posted,
               SUM(CASE WHEN solved_at IS NOT NULL THEN 1 ELSE 0 END) AS rounds_solved
        FROM guess_rounds
        WHERE guild_id = ? AND deleted_at IS NULL
        GROUP BY submitter_id
        ORDER BY rounds_posted DESC, rounds_solved DESC
        LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()
    return [(row["submitter_id"], row["rounds_posted"], row["rounds_solved"]) for row in rows]


def get_top_guessers(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 5
) -> list[tuple[int, int]]:
    """Return (solver_id, rounds_solved) ordered by rounds_solved desc."""
    rows = conn.execute(
        """
        SELECT solver_id,
               COUNT(*) AS rounds_solved
        FROM guess_rounds
        WHERE guild_id = ? AND deleted_at IS NULL AND solver_id IS NOT NULL
        GROUP BY solver_id
        ORDER BY rounds_solved DESC
        LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()
    return [(row["solver_id"], row["rounds_solved"]) for row in rows]


def flag_user_open_rounds_optout(
    conn: sqlite3.Connection, *, guild_id: int, user_id: int
) -> int:
    """Mark every open (unsolved, undeleted) round in this guild where *user_id*
    is the answer as ``answer_optout=True``. Returns the number of rounds flagged.

    Called when a user loses the Guess role — their consent has been revoked,
    so existing rounds where they're the answer should become unsolvable.
    Already-flagged rounds are unaffected.
    """
    cur = conn.execute(
        """
        UPDATE guess_rounds
        SET answer_optout = 1
        WHERE guild_id = ? AND answer_id = ?
          AND solved_at IS NULL AND deleted_at IS NULL AND answer_optout = 0
        """,
        (guild_id, user_id),
    )
    return cur.rowcount or 0


# Bump when the Guess consent disclosure changes materially (what is stored,
# for how long, or who can see it). Rows carrying an older version recorded
# agreement to wording the member never saw — which is the whole point of
# storing it, and is not reconstructable after the fact.
# 2 (2026-09-15): unsolved originals cleared after 7 days, not 90.
GUESS_DISCLOSURE_VERSION = 2


#: ``disclosure_version`` of a "legacy role holder" row: the member held the
#: Guess role from before the consent record shipped (2026-08-06) and was never
#: shown the disclosure. It records that the gap exists and since when — it is
#: **not** evidence of consent, and nothing may count it as such.
LEGACY_DISCLOSURE_VERSION = 0

#: Real Discord ids sit above this; below it is a departed-member surrogate
#: (``privacy.service._REAL_MEMBER_ID_FLOOR``), which is not a person to record
#: a consent gap for.
_REAL_MEMBER_ID_FLOOR = 10**16


def list_unevidenced_participants(
    conn: sqlite3.Connection,
) -> list[tuple[int, int, float]]:
    """``(guild_id, user_id, first_seen)`` for every member whose Guess Who data
    is held but who has no ``guess_consents`` row in that guild.

    "Held" means they submitted a round, are a round's answer, or made a guess.
    ``first_seen`` is the earliest of those, the nearest thing the database has to
    "when they started playing under the role". Departed members are not
    separated from current ones here — the database does not know who holds the
    role today; the one-shot script takes an optional list of holders for that.
    """
    rows = conn.execute(
        """
        WITH seen AS (
            SELECT guild_id, submitter_id AS user_id, created_at FROM guess_rounds
            UNION ALL
            SELECT guild_id, answer_id, created_at FROM guess_rounds
            UNION ALL
            SELECT r.guild_id, g.guesser_id, g.created_at
              FROM guess_guesses g JOIN guess_rounds r ON r.id = g.round_id
        )
        SELECT guild_id, user_id, MIN(created_at) AS first_seen
          FROM seen
         WHERE user_id >= ?
           AND NOT EXISTS (
               SELECT 1 FROM guess_consents c
                WHERE c.guild_id = seen.guild_id AND c.user_id = seen.user_id)
         GROUP BY guild_id, user_id
         ORDER BY guild_id, user_id
        """,
        (_REAL_MEMBER_ID_FLOOR,),
    ).fetchall()
    return [(r["guild_id"], r["user_id"], float(r["first_seen"])) for r in rows]


def record_legacy_consents(
    conn: sqlite3.Connection, members: list[tuple[int, int, float]]
) -> int:
    """Write a version-0 row for each ``(guild_id, user_id, first_seen)`` that still
    has no consent row. Returns the number written; running it twice writes
    nothing the second time (the existence check, not the primary key, is what
    makes it idempotent — a member who has since joined properly keeps theirs)."""
    written = 0
    for guild_id, user_id, first_seen in members:
        if get_guess_consent(conn, guild_id=guild_id, user_id=user_id) is not None:
            continue
        record_guess_consent(
            conn, guild_id=guild_id, user_id=user_id,
            consented_at=first_seen, disclosure_version=LEGACY_DISCLOSURE_VERSION,
        )
        written += 1
    return written


def record_guess_consent(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    consented_at: float,
    disclosure_version: int = GUESS_DISCLOSURE_VERSION,
) -> None:
    """Record that *user_id* agreed to the Guess disclosure (GDPR Art 7(1)).

    Art 7(1) puts the burden on the controller to *demonstrate* consent. The
    role alone shows only that someone holds it — not what they were shown or
    when — so the evidence has to be written at the moment of the click.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO guess_consents
            (guild_id, user_id, consented_at, disclosure_version, withdrawn_at)
        VALUES (?, ?, ?, ?, NULL)
        """,
        (guild_id, user_id, consented_at, disclosure_version),
    )


def withdraw_guess_consent(
    conn: sqlite3.Connection, *, guild_id: int, user_id: int, withdrawn_at: float
) -> int:
    """Stamp the member's outstanding consent rows as withdrawn.

    The rows are stamped, not deleted: Art 7(3) makes withdrawal as easy as
    giving consent, but the record that consent *was* held — and for how long —
    is what makes past processing defensible. A full erasure removes them.
    """
    cur = conn.execute(
        """
        UPDATE guess_consents SET withdrawn_at = ?
        WHERE guild_id = ? AND user_id = ? AND withdrawn_at IS NULL
        """,
        (withdrawn_at, guild_id, user_id),
    )
    return cur.rowcount or 0


def get_guess_consent(
    conn: sqlite3.Connection, *, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """Most recent consent row for the member, withdrawn or not."""
    return conn.execute(
        """
        SELECT * FROM guess_consents
        WHERE guild_id = ? AND user_id = ?
        ORDER BY consented_at DESC LIMIT 1
        """,
        (guild_id, user_id),
    ).fetchone()
