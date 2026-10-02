"""Greeting Watch — flag "good morning" / "hello" messages that go unanswered.

Detection (:func:`is_greeting`) runs live in ``on_message`` because the default
``"none"`` storage level drops message text before it ever reaches the DB. The
DB helpers here persist a lightweight watch row; the same ingest path marks it
acknowledged the moment someone answers the greeter in that channel — a reply,
an @mention, a hello of their own, or a reaction on the greeting — and the
background loop gives the verdict once the window has closed.
Who answered is never stored — only that someone did.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable
from functools import lru_cache
from typing import NamedTuple

# Matched against the start of a stripped message. A greeting is a short message
# that OPENS with a hello-ish token — "good morning everyone", "gm", "hey all",
# "hiya 👋" — or a check-in phrase standing in for one, like "how's everyone's
# evening" or "good timezone". The trailing ``\b`` stops prefixes like
# "history" / "gaming" / "morningstar" from matching. This is a heuristic
# dial, not a classifier: widen the vocabulary as real misses surface rather
# than treating it as exhaustive — and per-guild additions come in through
# ``is_greeting``'s ``extra_words`` (dashboard-configured), so server in-jokes
# don't need a code change.
_WORD_TOKENS = (
    r"g(?:ood)?\s*mornin[g']?"
    # Bare "evening"/"afternoon" open greetings just like bare "morning" does
    # ("Evening frands") — the good/g prefix is optional for all three.
    r"|(?:g(?:ood)?\s*)?afternoon"
    r"|(?:g(?:ood)?\s*)?evenin[g']?"
    # "good timezone" — a jokey stand-in for "good morning/afternoon/evening"
    # in servers spread across timezones, so nobody's greeting reads as wrong
    # for the reader's local time.
    r"|g(?:ood)?\s*time\s*zone"
    r"|g'?day"
    r"|mornin[g']?"
    r"|gm"
    r"|hey+o*"  # hey / heyyy / heyo / heyooo
    r"|hi+"
    r"|hai+"
    r"|hello+"
    r"|henlo+"  # meme-speak hello ("henlo frands")
    r"|hewwo+"
    r"|ello+"
    r"|heya+"
    r"|hiya+"
    r"|howdy"
    r"|greetings"
    r"|yo+"
    r"|oi+"
    r"|hola"
    r"|bonjour"
    r"|ciao"
    r"|aloha"
    r"|salut(?:ations)?"
    r"|what'?s\s*up"
    r"|wass?up"
    r"|sup"
    # "how's everyone's evening" / "how's your day going" — a check-in framed
    # as a question rather than a bare salutation. Anchored to a plural/2nd-
    # person subject + a time-of-day-or-doing word right after, so generic
    # "how's this bug" questions don't match.
    r"|(?:how'?s|how\s+is)\s+(?:everyone|everybody|every1|y'?all|your)'?s?\s+"
    r"(?:mornin[g']?|afternoon|evening|day|night|weekend|doing|going)"
)

# Tokens that end in a non-word character can't take the trailing ``\b`` (there
# is no word boundary after "/" or an emoji), so they live in a second
# alternation: the "o/" wave (also catches "\o/") and a bare 👋.
_SYMBOL_TOKENS = (r"o/", "👋")


@lru_cache(maxsize=128)
def _greeting_re(extra_words: tuple[str, ...]) -> re.Pattern[str]:
    """Compile the greeting matcher with a guild's extra tokens folded in.

    Extras are matched literally (``re.escape``) so a token like "what?" can't
    smuggle in regex syntax. Each goes to the word-boundary group or the
    symbol group by its final character, mirroring the built-in split. Cached
    per distinct token tuple — one entry per configured guild in practice.
    """
    bounded: list[str] = [_WORD_TOKENS]
    symbols: list[str] = list(_SYMBOL_TOKENS)
    for tok in extra_words:
        (bounded if re.match(r"\w", tok[-1]) else symbols).append(re.escape(tok))
    return re.compile(
        r"^\W*(?:(?:" + "|".join(bounded) + r")\b|" + "|".join(symbols) + r")",
        re.IGNORECASE,
    )


def parse_extra_words(raw: str | None) -> tuple[str, ...]:
    """Parse the ``greeting_watch_extra_words`` CSV into a clean tuple.

    Comma- or newline-separated; entries are trimmed, lowercased, de-duplicated
    preserving first-seen order. The tuple form keeps GuildConfig hashable and
    feeds ``_greeting_re``'s cache key directly.
    """
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for part in re.split(r"[,\n]", raw):
        tok = part.strip().lower()
        if tok:
            seen.setdefault(tok, None)
    return tuple(seen)


# A greeting to the room is short; a longer message is a conversation that
# merely opens with "hey", and we don't want to babysit those.
_MAX_GREETING_WORDS = 8


def is_greeting(content: str, extra_words: tuple[str, ...] = ()) -> bool:
    """True if *content* reads as a greeting addressed to the channel."""
    if not content:
        return False
    text = content.strip()
    if not text or len(text.split()) > _MAX_GREETING_WORDS:
        return False
    return _greeting_re(extra_words).match(text) is not None


class PendingGreeting(NamedTuple):
    message_id: int
    channel_id: int
    author_id: int
    created_ts: int
    acknowledged: bool = False


def has_pending_greeting(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, author_id: int
) -> bool:
    """True if this author already has an unresolved greeting in the channel."""
    row = conn.execute(
        """
        SELECT 1 FROM greeting_watch
        WHERE guild_id = ? AND channel_id = ? AND author_id = ?
          AND resolved_at IS NULL
        LIMIT 1
        """,
        (guild_id, channel_id, author_id),
    ).fetchone()
    return row is not None


def record_greeting(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    channel_id: int,
    author_id: int,
    created_ts: int,
) -> bool:
    """Persist a greeting to watch.

    A hello to the room is also a hello back: every pending greeting by someone
    else in the channel is stamped acknowledged, and this one is born
    acknowledged when it follows one — the two have greeted each other. In the
    30 days to 2026-09-30 that exchange, never a reply or a tag, was behind 95
    of 242 "unanswered" alerts. Rows stay pending either way, so a reply to
    either greeter still earns ``greeting_answered``.

    Returns ``False`` (nothing inserted) when this author already has an
    unresolved greeting in the channel — one open watch per person per channel
    stops a "gm 🙂 … hey all" double-post from queuing two alerts for the same
    silence.
    """
    follows_a_hello = (
        conn.execute(
            """
            SELECT 1 FROM greeting_watch
            WHERE guild_id = ? AND channel_id = ? AND author_id != ?
              AND resolved_at IS NULL
            LIMIT 1
            """,
            (guild_id, channel_id, author_id),
        ).fetchone()
        is not None
    )
    if follows_a_hello:
        conn.execute(
            """
            UPDATE greeting_watch SET outcome = 'acknowledged'
            WHERE guild_id = ? AND channel_id = ? AND author_id != ?
              AND resolved_at IS NULL AND outcome IS NULL
            """,
            (guild_id, channel_id, author_id),
        )
    if has_pending_greeting(conn, guild_id, channel_id, author_id):
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO greeting_watch
            (guild_id, message_id, channel_id, author_id, created_ts, outcome)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id, message_id, channel_id, author_id, created_ts,
            "acknowledged" if follows_a_hello else None,
        ),
    )
    return cur.rowcount > 0


def guilds_with_pending(conn: sqlite3.Connection) -> list[int]:
    """Distinct guilds that have at least one unresolved greeting."""
    rows = conn.execute(
        "SELECT DISTINCT guild_id FROM greeting_watch WHERE resolved_at IS NULL"
    ).fetchall()
    return [int(r[0]) for r in rows]


def list_due_greetings(
    conn: sqlite3.Connection, guild_id: int, cutoff_ts: int
) -> list[PendingGreeting]:
    """Unresolved greetings whose window has closed (created at/before cutoff)."""
    rows = conn.execute(
        """
        SELECT message_id, channel_id, author_id, created_ts, outcome
        FROM greeting_watch
        WHERE guild_id = ? AND resolved_at IS NULL AND created_ts <= ?
        ORDER BY created_ts
        """,
        (guild_id, cutoff_ts),
    ).fetchall()
    return [
        PendingGreeting(
            int(r[0]), int(r[1]), int(r[2]), int(r[3]), r[4] == "acknowledged"
        )
        for r in rows
    ]


def directed_target_ids(
    author_id: int,
    reply_author: tuple[int, bool] | None,
    mentions: Iterable[tuple[int, bool]],
) -> tuple[int, ...]:
    """Who a message is aimed at: the reply partner, then the @mentioned members.

    Each argument is ``(user_id, is_bot)``, resolved from Discord objects by the
    caller; ``reply_author`` is None when the reference did not resolve to a
    message. Bots and the author themself are never targets, and a member both
    replied to and mentioned counts once, reply first. Shared by Greeting
    Watch's ingest acknowledgment and the ``greeting_answered`` /
    ``birthday_wish`` quest detectors, so the two cannot drift on who counts.
    """
    candidates = ([reply_author] if reply_author is not None else []) + list(mentions)
    return tuple(
        dict.fromkeys(
            uid for uid, is_bot in candidates if not is_bot and uid != author_id
        )
    )


def acknowledge_greetings(
    conn: sqlite3.Connection,
    guild_id: int,
    author_id: int,
    channel_ids: tuple[int, ...],
    target_ids: tuple[int, ...],
) -> int:
    """Mark pending greetings by ``target_ids`` in these channels as answered.

    Called at ingest for a reply/@mention: the directed targets are the reply
    partner and the mentioned members. The row stays *pending* — ``outcome`` is
    stamped but ``resolved_at`` is left NULL — so the window still runs its
    course: a later answerer can still earn the ``greeting_answered`` quest
    (:func:`pending_greetings_for` keys on ``resolved_at``), and the loop
    closes the row as ``acknowledged`` instead of pinging anyone. Nothing about
    the answerer is written. The author's own id is dropped from the targets,
    so a greeter replying to themselves never counts. Returns rows marked.
    """
    targets = tuple(t for t in target_ids if t != author_id)
    if not channel_ids or not targets:
        return 0
    ch = ",".join("?" * len(channel_ids))
    tg = ",".join("?" * len(targets))
    cur = conn.execute(
        f"""
        UPDATE greeting_watch SET outcome = 'acknowledged'
        WHERE guild_id = ? AND channel_id IN ({ch}) AND author_id IN ({tg})
          AND resolved_at IS NULL AND outcome IS NULL
        """,
        (guild_id, *channel_ids, *targets),
    )
    return cur.rowcount or 0


def acknowledge_by_reaction(
    conn: sqlite3.Connection, guild_id: int, message_id: int, reactor_id: int
) -> int:
    """Mark a pending greeting answered because someone reacted to it.

    A wave under a "good morning" is an answer. The greeter's own reaction
    never counts; the caller drops bots. Like :func:`acknowledge_greetings`
    the row stays pending and nothing about the reactor is written. Returns
    rows marked.
    """
    cur = conn.execute(
        """
        UPDATE greeting_watch SET outcome = 'acknowledged'
        WHERE guild_id = ? AND message_id = ? AND author_id != ?
          AND resolved_at IS NULL AND outcome IS NULL
        """,
        (guild_id, message_id, reactor_id),
    )
    return cur.rowcount or 0


def is_acknowledged(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> bool:
    """Fresh read of whether this row has since been stamped acknowledged.

    ``list_due_greetings`` takes one snapshot per tick before the loop starts
    posting pings; an answer can land (via :func:`acknowledge_greetings`)
    while earlier rows in that same snapshot are still being processed. The
    loop re-checks with this immediately before notifying, the same way it
    checked against the live interaction log before the edge stores were
    dropped, so a late-arriving acknowledgment during a tick isn't overwritten
    by a false "unanswered" verdict and ping.
    """
    row = conn.execute(
        """
        SELECT 1 FROM greeting_watch
        WHERE guild_id = ? AND message_id = ? AND resolved_at IS NULL
          AND outcome = 'acknowledged'
        """,
        (guild_id, message_id),
    ).fetchone()
    return row is not None


def pending_greetings_for(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_ids: tuple[int, ...],
    target_ids: tuple[int, ...],
) -> list[tuple[int, int]]:
    """Unresolved greetings by any of ``target_ids`` in these channels.

    The `greeting_answered` quest detector: a reply/mention landing on a
    member with a pending greeting in the same channel is "answering the
    hello". Pending ≈ within the window — the background loop resolves rows
    shortly after their window closes, so anything still open is answerable.
    Returns ``(message_id, author_id)`` pairs (the message id keys the quest
    occurrence, so each greeting credits an answerer at most once).
    """
    if not channel_ids or not target_ids:
        return []
    ch = ",".join("?" * len(channel_ids))
    tg = ",".join("?" * len(target_ids))
    rows = conn.execute(
        f"""
        SELECT message_id, author_id FROM greeting_watch
        WHERE guild_id = ? AND channel_id IN ({ch}) AND author_id IN ({tg})
          AND resolved_at IS NULL
        """,
        (guild_id, *channel_ids, *target_ids),
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def mark_resolved(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    outcome: str,
    now_ts: int,
) -> None:
    """Close a greeting with a verdict; a no-op if it was already resolved.

    An ``acknowledged`` stamp from ingest wins: a reply that lands after the
    loop's fresh check (:func:`is_acknowledged`) but before this write closes
    the row as acknowledged rather than being overwritten by ``unanswered``.
    """
    conn.execute(
        """
        UPDATE greeting_watch
        SET resolved_at = ?,
            outcome = CASE WHEN outcome = 'acknowledged' THEN outcome ELSE ? END
        WHERE guild_id = ? AND message_id = ? AND resolved_at IS NULL
        """,
        (now_ts, outcome, guild_id, message_id),
    )


RESOLVED_RETENTION_SECONDS = 30 * 86400


def gc_resolved_watches(
    conn: sqlite3.Connection, *, older_than_seconds: int = RESOLVED_RETENTION_SECONDS
) -> int:
    """Delete resolved watch rows past the retention window. Returns rows
    deleted. A verdicted row (acknowledged / unanswered / skipped) has no
    ongoing use — the alert already fired or was never needed — so keeping
    ids+timestamps forever is pure residue (2026-08 review, intake G1)."""
    cutoff = time.time() - older_than_seconds
    cur = conn.execute(
        "DELETE FROM greeting_watch WHERE resolved_at IS NOT NULL AND resolved_at < ?",
        (cutoff,),
    )
    return cur.rowcount or 0
