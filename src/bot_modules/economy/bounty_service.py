"""Community Bounty — crowdfunded task pots (migration 109).

Anyone posts a freeform task and seeds a pot; anyone chips in; the poster or a
mod awards the pot to the winner minus a house rake that evaporates (the poster
may not award it to themselves — :func:`resolve_denial` owns that rule, and it
is the only permission decision this service makes); an unawarded bounty
refunds every contributor. The economy's first *many-payer* mechanic, so each
contribution is its own ``econ_bounty_contributions`` row keyed by bounty — the
pot is ``SUM(non-refunded contributions)`` (never stored, can't drift) and
refunds are per-member and exactly-once.

Pure DB, no Discord — the caller owns the board card and DMs, matching the pin
and sponsor services.

Money::

    contribute  -> apply_debit  (bounty_stake)   escrow into the pot
    award       -> apply_credit (bounty_payout)  winner gets pot - rake
                   (the rake is escrow never credited back = the burn)
    cancel/     -> apply_credit (bounty_refund)  each contribution back,
    expire         exactly-once (refunded_at guard), NEVER raked
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.economy.service import (
    apply_credit,
    apply_debit,
    get_balance,
)
from bot_modules.no_contact.service import (
    is_no_contact_conn,
    no_contact_partners_conn,
)

if TYPE_CHECKING:
    from bot_modules.economy.service import EconSettings

MAX_TITLE_LEN = 100
MAX_DESC_LEN = 500
MIN_TITLE_LEN = 3

STAKE_KIND = "bounty_stake"
PAYOUT_KIND = "bounty_payout"
REFUND_KIND = "bounty_refund"


def bounty_enabled(settings: EconSettings) -> bool:
    """On only when a board channel is set (nowhere to post otherwise)."""
    return int(settings.bounty_channel_id) > 0


def bounty_min_stake(settings: EconSettings) -> int:
    """The floor for the opening stake and each chip-in (at least 1)."""
    return max(1, int(settings.bounty_min_stake))


def bounty_rake_pct(settings: EconSettings) -> int:
    """House cut on award, clamped to 0–100. 0 = the winner takes the whole pot."""
    return max(0, min(100, int(settings.bounty_rake_pct)))


@dataclass(frozen=True)
class BountyOutcome:
    """A freshly-posted bounty: its id and the opening stake escrowed."""

    bounty_id: int
    stake: int


def pot_of(conn: sqlite3.Connection, bounty_id: int) -> int:
    """The live pot: the sum of this bounty's non-refunded contributions."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS pot FROM econ_bounty_contributions "
        "WHERE bounty_id = ? AND refunded_at IS NULL",
        (bounty_id,),
    ).fetchone()
    return int(row["pot"])


def contributor_count(conn: sqlite3.Connection, bounty_id: int) -> int:
    """How many distinct members have live contributions in the pot."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM econ_bounty_contributions "
        "WHERE bounty_id = ? AND refunded_at IS NULL",
        (bounty_id,),
    ).fetchone()
    return int(row["n"])


def open_count_for(conn: sqlite3.Connection, guild_id: int, poster_id: int) -> int:
    """How many open bounties this member currently has posted."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_bounties "
        "WHERE guild_id = ? AND poster_id = ? AND state = 'open'",
        (guild_id, poster_id),
    ).fetchone()
    return int(row["n"])


def _escrow(
    conn: sqlite3.Connection,
    guild_id: int,
    bounty_id: int,
    user_id: int,
    amount: int,
    now: float,
) -> bool:
    """Debit ``amount`` into the pot and record the contribution. False if broke."""
    if not apply_debit(
        conn, guild_id, user_id, amount, STAKE_KIND, meta={"bounty_id": bounty_id}
    ):
        return False
    conn.execute(
        "INSERT INTO econ_bounty_contributions "
        "(bounty_id, guild_id, user_id, amount, created_at) VALUES (?, ?, ?, ?, ?)",
        (bounty_id, guild_id, user_id, amount, now),
    )
    return True


def create_bounty(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    poster_id: int,
    *,
    title: str,
    description: str,
    stake: int,
    now: float | None = None,
) -> BountyOutcome:
    """Open a bounty, escrowing the poster's opening stake. ValueError = member text."""
    if not bounty_enabled(settings):
        raise ValueError("Bounties aren't enabled here.")
    title = " ".join(title.split())
    description = "\n".join(line.rstrip() for line in description.splitlines()).strip()
    if len(title) < MIN_TITLE_LEN:
        raise ValueError("Give your bounty a title (a few words at least).")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"Titles are limited to {MAX_TITLE_LEN} characters.")
    if len(description) > MAX_DESC_LEN:
        raise ValueError(f"Descriptions are limited to {MAX_DESC_LEN} characters.")
    floor = bounty_min_stake(settings)
    if stake < floor:
        raise ValueError(f"The opening stake must be at least {floor}.")
    cap = max(0, int(settings.bounty_max_open))
    if cap and open_count_for(conn, guild_id, poster_id) >= cap:
        raise ValueError(
            f"You already have {cap} open bounties — award or cancel one first."
        )

    # Affordability is checked BEFORE the insert so an unaffordable post is a
    # zero-write (the row would otherwise sit until the caller's transaction
    # unwinds). Within this one transaction the balance can't move between the
    # check and the debit, so the escrow below can't then fail on funds.
    now = time.time() if now is None else now
    have = get_balance(conn, guild_id, poster_id)
    if have < stake:
        unit = settings.currency_plural or "coins"
        raise ValueError(
            f"Posting this bounty costs {stake} {unit} up front — you have {have}."
        )
    days = max(0, int(settings.bounty_expire_days))
    expires_at = now + days * 86400.0 if days > 0 else None
    cur = conn.execute(
        "INSERT INTO econ_bounties "
        "(guild_id, poster_id, title, description, state, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (guild_id, poster_id, title, description, now, expires_at),
    )
    bounty_id = int(cur.lastrowid or 0)
    if not _escrow(conn, guild_id, bounty_id, poster_id, stake, now):
        # Defensive: the balance was just confirmed, so this shouldn't fire — but
        # if it does, raise so the caller's transaction rolls back the insert.
        raise ValueError("Couldn't take the opening stake — try again.")
    # shop_purchase quest trigger (one-time setup kind); deferred import.
    from bot_modules.economy.quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", poster_id, occurrence="set")
    return BountyOutcome(bounty_id=bounty_id, stake=stake)


def contribute(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    bounty_id: int,
    user_id: int,
    amount: int,
    *,
    now: float | None = None,
) -> int:
    """Chip ``amount`` into an open bounty's pot. Returns the new pot. ValueError = text."""
    floor = bounty_min_stake(settings)
    if amount < floor:
        raise ValueError(f"Chip in at least {floor}.")
    row = get_bounty(conn, bounty_id)
    if row is None or int(row["guild_id"]) != guild_id:
        raise ValueError("That bounty no longer exists.")
    if str(row["state"]) != "open":
        raise ValueError(f"That bounty is {row['state']} — you can't add to it now.")
    now = time.time() if now is None else now
    # Re-assert 'open' as this connection's *first* write. The state read above
    # runs in autocommit, so without this an award or cancel committing in
    # between would leave the contribution escrowed on a closed bounty: excluded
    # from the payout snapshot and never refunded, so the coins just vanish.
    # The write is a no-op on success, so the error path needs no unwinding.
    if conn.execute(
        "UPDATE econ_bounties SET state = 'open' WHERE id = ? AND state = 'open'",
        (bounty_id,),
    ).rowcount != 1:
        raise ValueError("That bounty was just closed — nothing was charged.")
    if not _escrow(conn, guild_id, bounty_id, user_id, amount, now):
        have = get_balance(conn, guild_id, user_id)
        unit = settings.currency_plural or "coins"
        raise ValueError(f"That's {amount} {unit} — you have {have}.")
    return pot_of(conn, bounty_id)


# ── who may resolve ──────────────────────────────────────────────────────────

#: Refusal shown to a member who is neither the poster nor an economy manager.
RESOLVE_DENIED_MSG = "❌ You don't have permission to award or cancel bounties."
#: Refusal shown to a poster who picked themselves as the winner.
SELF_AWARD_DENIED_MSG = (
    "❌ You can't award your own bounty to yourself — ask a mod to award it, "
    "or cancel it to refund everyone."
)


def resolve_denial(
    *, poster_id: int, actor_id: int, is_manager: bool, winner_id: int | None = None
) -> str | None:
    """Why ``actor_id`` may not resolve this bounty, or ``None`` if they may.

    ``winner_id`` names the award's winner; ``None`` asks about cancelling.
    Economy managers resolve anything, as they always have. The **poster** may
    cancel their own bounty (everyone is refunded, so nobody can profit) and may
    award it to someone else — but not to themselves: the pot is other members'
    coins, and paying it to yourself is the one move that turns a bounty into a
    self-serve withdrawal. A manager who happens to be the poster keeps the
    manager's rights, self-award included, exactly as they do on anyone else's.

    Lives here rather than in the view because both the Award button's gate and
    the award handler's re-check ask the same question, and because a permission
    rule that can be unit-tested is the only kind that stays true.
    """
    if is_manager:
        return None
    if actor_id != poster_id:
        return RESOLVE_DENIED_MSG
    if winner_id is not None and winner_id == actor_id:
        return SELF_AWARD_DENIED_MSG
    return None


def should_notify_winner(
    conn: sqlite3.Connection, guild_id: int, poster_id: int, winner_id: int
) -> bool:
    """Whether the winner's "you were awarded" DM may be sent.

    False for a poster/winner pair under a no-contact rule: that DM names the
    bounty and links its card, and the card says who posted it. The award itself
    still runs and the coins still land — suppressing the delivery rather than
    refusing the action is the shape ``/bank pay`` set (docs/specs/no_contact_spec.md),
    and it keeps the outcome indistinguishable from a member who simply has bot
    DMs switched off. Keyed on the *pair*, not on who clicked: a mod awarding
    the bounty leaks the same card link, so the rule can't depend on the actor.
    """
    return not is_no_contact_conn(conn, guild_id, poster_id, winner_id)


def notifiable_contributors(
    conn: sqlite3.Connection, guild_id: int, poster_id: int, user_ids: list[int]
) -> list[int]:
    """The refunded contributors who may be told their bounty was cancelled.

    Same reasoning as :func:`should_notify_winner` and the same payload problem:
    the cancellation DM links the card, and the card names the poster. A
    contributor under a no-contact rule with them keeps their refund — the money
    is never held back — and simply isn't sent the note. Order is preserved so
    the caller's "refunded N contributor(s)" count still reads off the full list.

    One query for the poster's partners rather than one per contributor: a
    crowded pot is the case where this runs, and the answer is the same set
    every time.
    """
    if not user_ids:
        return []
    partners = no_contact_partners_conn(conn, guild_id, poster_id)
    return [uid for uid in user_ids if uid not in partners]


def names_two_members(row) -> bool:
    """Whether this card names a second member beside the poster.

    Only an awarded card does. Pure and forgiving of a row that doesn't carry
    the columns (the repaint paths hand this whatever they hold, including a
    bare mapping in tests), because its caller is a never-raises name resolver:
    an unreadable row is one with nobody to protect, not an error.
    """
    try:
        return str(row["state"]) == "awarded" and bool(int(row["winner_id"] or 0))
    except (IndexError, KeyError, TypeError, ValueError):
        return False


def award_pair_no_contact(
    conn: sqlite3.Connection, guild_id: int, row: sqlite3.Row
) -> bool:
    """Whether this awarded card would name a no-contact pair side by side.

    The card prints "Posted by X" and "Winner Y" in the bot's own voice on a
    public board. For a pair that is the bot manufacturing the association the
    list exists to prevent, so both names degrade to a bare ``User <id>`` —
    the same call the Guess solved-reveal makes (``games.guess.layouts.reveal_names``,
    docs/specs/no_contact_spec.md). Only an *awarded* card names two members; an open
    or cancelled one names the poster alone and is left as it is.
    """
    if not names_two_members(row):
        return False
    return is_no_contact_conn(
        conn, guild_id, int(row["poster_id"]), int(row["winner_id"])
    )


@dataclass(frozen=True)
class AwardResult:
    """An awarded bounty: the fresh row, the winner, and how the pot split."""

    bounty: sqlite3.Row
    winner_id: int
    payout: int
    rake: int


def award_bounty(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    bounty_id: int,
    *,
    winner_id: int,
    resolver_id: int,
    now: float | None = None,
) -> AwardResult:
    """Award the pot to ``winner_id`` minus the house rake. ValueError = member text.

    The rake is ``floor(pot × rake_pct / 100)`` and simply isn't credited back to
    anyone — the escrowed coins that fund it evaporate. An empty pot can't be
    awarded (nothing to pay).
    """
    row = get_bounty(conn, bounty_id)
    if row is None or int(row["guild_id"]) != guild_id:
        raise ValueError("That bounty no longer exists.")
    if str(row["state"]) != "open":
        raise ValueError(f"That bounty is already {row['state']}.")
    if pot_of(conn, bounty_id) <= 0:
        raise ValueError("That bounty's pot is empty — nothing to award.")

    now = time.time() if now is None else now
    # Claim the bounty *before* reading the pot it pays out. Closing the state
    # first is what stops a chip-in landing behind the snapshot and being
    # escrowed into a bounty that has already paid. Contributions only ever
    # grow, so the pot re-read below is still non-empty.
    cur = conn.execute(
        "UPDATE econ_bounties SET state = 'awarded', winner_id = ?, "
        "resolver_id = ?, resolved_at = ? WHERE id = ? AND state = 'open'",
        (winner_id, resolver_id, now, bounty_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("That bounty was just resolved by someone else.")
    pot = pot_of(conn, bounty_id)
    rake = pot * bounty_rake_pct(settings) // 100
    payout = pot - rake
    conn.execute(
        "UPDATE econ_bounties SET payout = ?, rake_amount = ? WHERE id = ?",
        (payout, rake, bounty_id),
    )
    if payout > 0:
        apply_credit(
            conn, guild_id, winner_id, payout, PAYOUT_KIND,
            meta={"bounty_id": bounty_id, "rake": rake}, booster=False,
        )
    fresh = get_bounty(conn, bounty_id)
    assert fresh is not None
    return AwardResult(bounty=fresh, winner_id=winner_id, payout=payout, rake=rake)


def _refund_all(conn: sqlite3.Connection, bounty_id: int, reason: str) -> list[int]:
    """Refund every not-yet-refunded contribution exactly once. Returns user ids paid."""
    rows = conn.execute(
        "SELECT * FROM econ_bounty_contributions "
        "WHERE bounty_id = ? AND refunded_at IS NULL",
        (bounty_id,),
    ).fetchall()
    now = time.time()
    paid: list[int] = []
    for row in rows:
        cur = conn.execute(
            "UPDATE econ_bounty_contributions SET refunded_at = ? "
            "WHERE id = ? AND refunded_at IS NULL",
            (now, int(row["id"])),
        )
        if (cur.rowcount or 0) == 0:
            continue
        apply_credit(
            conn, int(row["guild_id"]), int(row["user_id"]), int(row["amount"]),
            REFUND_KIND, meta={"bounty_id": bounty_id, "reason": reason}, booster=False,
        )
        paid.append(int(row["user_id"]))
    return paid


def cancel_bounty(
    conn: sqlite3.Connection,
    guild_id: int,
    bounty_id: int,
    *,
    resolver_id: int,
    now: float | None = None,
) -> tuple[sqlite3.Row, list[int]]:
    """Cancel an open bounty and refund every contributor. Returns (row, refunded ids)."""
    row = get_bounty(conn, bounty_id)
    if row is None or int(row["guild_id"]) != guild_id:
        raise ValueError("That bounty no longer exists.")
    if str(row["state"]) != "open":
        raise ValueError(f"That bounty is already {row['state']}.")
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE econ_bounties SET state = 'cancelled', resolver_id = ?, resolved_at = ? "
        "WHERE id = ? AND state = 'open'",
        (resolver_id, now, bounty_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("That bounty was just resolved by someone else.")
    refunded = _refund_all(conn, bounty_id, "cancelled")
    fresh = get_bounty(conn, bounty_id)
    assert fresh is not None
    return fresh, refunded


@dataclass(frozen=True)
class ExpiredBounty:
    """An expired bounty and the contributors refunded (for post-commit DMs/cards)."""

    bounty: sqlite3.Row
    refunded_user_ids: list[int]


def expire_bounties(
    conn: sqlite3.Connection, settings: EconSettings, guild_id: int, *, now: float
) -> list[ExpiredBounty]:
    """Expire and refund open bounties nobody awarded within the window."""
    days = max(0, int(settings.bounty_expire_days))
    if days <= 0:
        return []
    due = conn.execute(
        "SELECT * FROM econ_bounties WHERE guild_id = ? AND state = 'open' "
        "AND expires_at IS NOT NULL AND expires_at <= ?",
        (guild_id, now),
    ).fetchall()
    out: list[ExpiredBounty] = []
    for row in due:
        cur = conn.execute(
            "UPDATE econ_bounties SET state = 'expired', resolved_at = ? "
            "WHERE id = ? AND state = 'open'",
            (now, int(row["id"])),
        )
        if (cur.rowcount or 0) == 0:
            continue
        refunded = _refund_all(conn, int(row["id"]), "expired")
        fresh = get_bounty(conn, int(row["id"]))
        assert fresh is not None
        out.append(ExpiredBounty(bounty=fresh, refunded_user_ids=refunded))
    return out


def get_bounty(conn: sqlite3.Connection, bounty_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_bounties WHERE id = ?", (bounty_id,)
    ).fetchone()


def recent_bounties(conn: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    """Open bounties posted since ``since``, across guilds — for announcements
    (see event_echo_service).

    Bounded by ``created_at`` so a consumer restarting doesn't re-announce a
    backlog, and by ``card_message_id`` because a bounty whose card hasn't
    posted has nothing to link to. Columns are aliased to the shape
    announcement callers share (``id``, ``channel_id``, ``message_id``).
    """
    return conn.execute(
        "SELECT id, guild_id, title, "
        "       card_channel_id AS channel_id, card_message_id AS message_id "
        "FROM econ_bounties "
        "WHERE state = 'open' AND card_message_id != 0 AND created_at >= ?",
        (since,),
    ).fetchall()


@dataclass(frozen=True)
class BountyBoardEntry:
    """One open bounty as the hub panel lists it: live pot, backers, jump target."""

    bounty_id: int
    title: str
    pot: int
    contributors: int
    card_channel_id: int
    card_message_id: int


#: How many open bounties the hub panel asks for. Component-budget driven
#: since the 2026-09-28 Components V2 conversion: each row is a ``Section``
#: (title + pot line + button = 3 components), so 8 of them plus the blurb,
#: divider, heading and bottom action row stays comfortably under Discord's
#: 40-component ceiling (``core.layout.check_budget``). Never treat this as
#: "what the hub shows" — a hidden-count line covers the rest.
HUB_LIST_LIMIT = 8


def board_entries(
    conn: sqlite3.Connection, guild_id: int, limit: int = HUB_LIST_LIMIT
) -> list[BountyBoardEntry]:
    """Open bounties oldest-first with their live pots — the hub panel's list.

    Oldest-first so the bounty closest to expiring (and to being refunded) is
    the one members see, and so the list doesn't reshuffle under a reader every
    time someone posts. Pots are summed per bounty in SQL rather than by
    calling :func:`pot_of` in a loop, because this runs on every sticky repost.

    Unlike :func:`recent_bounties` this does NOT require a posted card: a
    bounty whose card failed to send still belongs on the board, it just has no
    jump link (ids stay 0 and the caller omits the link).
    """
    limit = min(max(int(limit), 1), 25)
    rows = conn.execute(
        "SELECT b.id, b.title, b.card_channel_id, b.card_message_id, "
        "       COALESCE(SUM(c.amount), 0) AS pot, "
        "       COUNT(DISTINCT c.user_id) AS contributors "
        "FROM econ_bounties AS b "
        "LEFT JOIN econ_bounty_contributions AS c "
        "       ON c.bounty_id = b.id AND c.refunded_at IS NULL "
        "WHERE b.guild_id = ? AND b.state = 'open' "
        "GROUP BY b.id "
        "ORDER BY b.created_at ASC, b.id ASC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [
        BountyBoardEntry(
            bounty_id=int(row["id"]),
            title=str(row["title"]),
            pot=int(row["pot"]),
            contributors=int(row["contributors"]),
            card_channel_id=int(row["card_channel_id"]),
            card_message_id=int(row["card_message_id"]),
        )
        for row in rows
    ]


def open_board_count(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many bounties are open in this guild — the hub's ``+N more`` tail."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_bounties WHERE guild_id = ? AND state = 'open'",
        (guild_id,),
    ).fetchone()
    return int(row["n"])


def set_bounty_card(
    conn: sqlite3.Connection, bounty_id: int, channel_id: int, message_id: int
) -> None:
    conn.execute(
        "UPDATE econ_bounties SET card_channel_id = ?, card_message_id = ? WHERE id = ?",
        (channel_id, message_id, bounty_id),
    )
