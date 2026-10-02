"""Tests for the member-facing GDPR disclosure report.

The report is sent to a member, so the tests that matter most are the ones
about what must never reach it: another member's identity, a false promise of
deletion, or the register's own engineering notes.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.privacy import disclosure_service as ds
from bot_modules.privacy.disclosure_copy import (
    CATEGORIES,
    FEATURE_TO_CATEGORY,
    composed_values,
)
from bot_modules.privacy.service import THIRD_PARTY_TABLES
from tests.paths import REPO_ROOT

REGISTER_HEAD = (
    "| Table / store | Feature | Data class | Retention | Purge? | Processor | Notes |\n"
    "|---|---|---|---|---|---|---|\n"
)


def _register(*rows: str) -> str:
    return REGISTER_HEAD + "".join(rows)


# ---------------------------------------------------------------------------
# register parsing
# ---------------------------------------------------------------------------


def test_escaped_pipes_do_not_shift_columns():
    """A cell containing ``\\|`` must not push Retention onto the wrong column.

    ``risky_pending_questions`` documents its CSV matching as SQL string
    concatenation — ``',' \\|\\| col \\|\\| ','`` — and a naive split on "|" turned
    that 7-column row into 11 fields. Retention and Purge survived by luck; a
    row escaping a pipe any earlier would have reported one table's retention
    as another's.
    """
    row = (
        "| risky_pending_questions | Risky Rolls | CSV lists | 7 days | "
        "**YES — purge** | — | matched with `',' \\|\\| col \\|\\| ','` |\n"
    )
    parsed = ds.parse_register(_register(row))
    assert len(parsed) == 1
    assert parsed[0].tables == ("risky_pending_questions",)
    assert parsed[0].retention == "7 days"
    assert parsed[0].purge == "**YES — purge**"
    assert parsed[0].processor == "—"
    assert "||" in parsed[0].notes  # unescaped back to real pipes


def test_rows_that_cannot_be_read_by_column_are_skipped_not_guessed():
    good = "| a_table | Telemetry | x | 30 days | YES | — | n |\n"
    short = "| b_table | only three | fields |\n"
    parsed = ds.parse_register(_register(good, short))
    assert [r.tables for r in parsed] == [("a_table",)]
    assert ds.parse_register.malformed == ["b_table"]


def test_parsing_stops_at_the_next_heading():
    """The register continues with narrower tables that are not processing rows."""
    text = _register(
        "| a_table | Telemetry | x | 30 days | YES | — | n |\n"
    ) + "\n## Deliberately not personal data\n\n| c_table | no member id | n/a |\n"
    parsed = ds.parse_register(text)
    assert [r.tables for r in parsed] == [("a_table",)]
    assert ds.parse_register.malformed == []


def test_glob_and_exact_resolution():
    rows = ds.parse_register(
        _register(
            "| econ_* (49 tables) | Economy | x | permanent — Art 17(3)(e) | YES | — | n |\n"
            "| econ_wallets | Economy | x | 30 days | YES | — | n |\n"
        )
    )
    index = ds.build_table_index(rows)
    assert ds.resolve_row("econ_wallets", rows, index).retention == "30 days"
    assert ds.resolve_row("econ_ledger", rows, index).retention.startswith("permanent")
    assert ds.resolve_row("unrelated_table", rows, index) is None


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("permanent — Art 17(3)(e) legal claims", "permanent"),
        ("180 days", "period"),
        ("7-day TTL (threads), verified live", "period"),
        ("12-month TTL", "period"),
        ("until purged", "event"),
        ("until the member rejoins", "event"),
        ("**undecided** — no decision has been taken", "undecided"),
        ("?", "undecided"),
        ("n/a", "undecided"),
        ("", "undecided"),
        # Legacy vocabulary: ambiguous by design, so never read as a decision.
        ("indefinite", "undecided"),
    ],
)
def test_classify_retention(cell, expected):
    assert ds.classify_retention(cell) == expected


def test_a_bounded_period_never_hides_an_unbounded_one():
    """The regression that mattered: a false promise of erasure.

    The XP row reads "indefinite, except xp_events: kept 90 days once an admin
    enables it". Testing for the duration first classified the whole category
    as a clean 90-day deletion — telling a member their activity history
    expires when in fact most of it is kept forever.
    """
    cell = (
        "indefinite, except xp_events: individual events are kept 90 days and "
        "then replaced by a daily total, once an admin enables it (off by default)"
    )
    assert ds.classify_retention(cell) == "mixed"

    cat = ds.CategoryReport("k", "T", "b", "p")
    cat.retention_kinds.update({"permanent", "period"})
    cat.durations.append((90, "90 days"))
    sentence = ds._retention_sentence(cat)
    assert "Mixed" in sentence
    assert "90 days" in sentence
    assert "permanently" in sentence


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("**YES — purge** (migration 198)", "yes"),
        ("**DECIDED — preserve**, Art 17(3)(e) legal claims", "no"),
        ("**STILL NO** — no guess_* table is in purge_user_data", "no"),
        ("**SPLIT.** from_user chips: **YES** — purge strips them", "partial"),
        ("role_events YES, others **NO**", "partial"),
        ("**ASYMMETRIC — purge the owner's side, preserve the target's**", "partial"),
        ("needs decision — low priority", "unknown"),
        ("—", "unknown"),
        # A cell that purges nearly everything but names an Art 17(3) preserve
        # must read as partial, not as a refusal. Bug #222's whispers row is
        # this shape: almost all of it goes, and reports the member filed on
        # somebody else's whisper stay.
        (
            "**YES — purged**, and **Member-erasable**. **Preserved, "
            "Art 17(3)(e) rights of others:** a report this member filed on "
            "somebody else's whisper",
            "partial",
        ),
    ],
)
def test_purge_verdict(cell, expected):
    assert ds.purge_verdict(cell) == expected


def test_every_register_row_this_bug_touched_classifies_truthfully():
    """The cells are read by a member-facing report, so their wording is load
    bearing (bug #222, 2026-09-17).

    Two of these went wrong while the bug was being fixed, both silently.
    ``**partial**`` in lower case never matched the verdict pattern, so the
    confessions row scored on the stray word "preserved" elsewhere in the
    cell — and once that word was edited out the row flipped to a clean YES,
    which would have told a member their confession routing rows are deleted
    when they are left to a TTL. The whispers row, rewritten to say the purge
    now reaches it, still read as a blanket NO.
    """
    register = REPO_ROOT / "docs" / "data_register.md"
    expected = {
        "whispers": "partial",                 # reports the member filed survive
        "confession_threads": "partial",       # routing rows are left to the TTL
        "mahjong_tables": "partial",           # the closed row's shell stays
        "pen_pals_pool": "yes",
        "pen_pals_sessions": "partial",        # the other member's seat stays
        "pen_pals_blocks": "partial",          # blocks protecting someone else stay
        "grant_role_permissions": "yes",       # role rows are not personal data
    }
    seen: dict[str, str] = {}
    for line in register.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        first = cells[0].split(",")[0].strip().strip("`").strip()
        if first in expected:
            seen[first] = ds.purge_verdict(cells[4])

    assert seen == expected


def test_erasure_sentence_never_claims_erasure_when_the_register_never_said_so():
    """A category built entirely from ambiguous Purge? cells must not read as

    a clean deletion. Every real path into this state has a table whose
    register cell reads as neither yes nor no (``games_external_messages``,
    ``greeting_watch``, ``voice_transcription_config`` all do today) — a
    member whose only rows in that category live there would otherwise be told
    "most of this is erased" on the strength of nothing.
    """
    cat = ds.CategoryReport("k", "T", "b", "p", preserved="Some reason.")
    cat.purge_unknown = 1
    sentence = ds._erasure_sentence(cat)
    assert "most of this is erased" not in sentence
    assert "all of this is erased" not in sentence
    assert "has not been recorded" in sentence


def test_an_unknown_beside_the_yeses_is_not_an_unqualified_promise():
    """A yes-and-unknown category must not read "all of this is erased".

    The unqualified sentence is the only promise of deletion this report makes,
    so one table nobody has recorded a decision for is enough to disqualify it —
    the same refusal to guess as the all-unknown case above, which the
    surrounding yeses would otherwise hide. ``joining`` is the live shape:
    three YES rows beside ``intake_cards``, which no code clears.
    """
    cat = ds.CategoryReport("k", "T", "b", "p", preserved="Some reason.")
    cat.purge_yes = 3
    cat.purge_unknown = 1
    sentence = ds._erasure_sentence(cat)
    assert "all of this is erased" not in sentence
    assert "has not been recorded" in sentence


def test_purge_verdict_is_case_sensitive_about_its_verdict():
    """"No Art 17(3) ground worth claiming" justifies a purge, it is not a refusal.

    Lower-casing before matching read that prose "no" as a NO verdict and would
    have told a member their data is retained when it is in fact erased.
    """
    cell = (
        "**YES — purge** (migration 198). No Art 17(3) ground worth claiming: "
        "it is analytics, and 'we wanted the numbers' is not a ground"
    )
    assert ds.purge_verdict(cell) == "yes"


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1784776342.41046, "2026-07-23"),          # REAL epoch seconds
        (1785684983, "2026-08-02"),                # INTEGER epoch seconds
        (1785684983660, "2026-08-02"),             # milliseconds
        ("2026-07-24", "2026-07-24"),              # local_day text
        ("2026-07-24T11:02:13+00:00", "2026-07-24"),  # ISO text
        (0.0, None),
        (None, None),
        ("not a date", None),
    ],
)
def test_timestamp_shapes(raw, expected):
    assert ds._to_date(raw) == expected


# ---------------------------------------------------------------------------
# copy coverage
# ---------------------------------------------------------------------------


def test_every_category_has_authored_copy():
    for cat in CATEGORIES:
        assert cat.blurb and cat.purpose and cat.preserved, cat.key


def test_every_feature_maps_to_a_real_category():
    keys = {c.key for c in CATEGORIES}
    for feature, key in FEATURE_TO_CATEGORY.items():
        assert key in keys, f"{feature} -> unknown category {key}"


def test_register_features_are_all_mapped():
    """A new register Feature must not reach a member as an uncategorised table.

    This is the gate CLAUDE.md's per-commit register contract implies: a table
    added to the register with a new Feature name would otherwise silently
    vanish from every report, which is exactly the invisibility the register
    exists to prevent.
    """
    from pathlib import Path

    register = Path("docs/data_register.md").read_text(encoding="utf-8")
    features = {r.feature for r in ds.parse_register(register) if r.feature}
    missing = sorted(features - set(FEATURE_TO_CATEGORY))
    assert not missing, (
        "register Features with no member-facing category: "
        + ", ".join(missing)
        + " — add them to disclosure_copy.FEATURE_TO_CATEGORY"
    )


def test_birthday_is_composed_not_printed_as_two_numbers():
    assert composed_values("member_birthdays", {"birth_month": 9, "birth_day": 5}) == [
        ("Birthday you set", "5 September")
    ]
    assert composed_values("member_birthdays", {"birth_month": None}) == []


# ---------------------------------------------------------------------------
# the two things that must never reach a member's copy
# ---------------------------------------------------------------------------


def _tiny_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE starboard_reactors (guild_id INT, reactor_id INT, "
        "author_id INT, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO starboard_reactors VALUES (1, 100, 999, 1784776342.0)"
    )
    conn.execute("CREATE TABLE member_xp (guild_id INT, user_id INT, total_xp REAL, level INT)")
    conn.execute("INSERT INTO member_xp VALUES (1, 100, 41391.9, 46)")
    return conn


def _export() -> dict:
    return {
        "subject": {"guild_id": 1, "user_id": 100},
        "tables": {
            "starboard_reactors": {
                "matched_columns": ["reactor_id"],
                "guild_scoped": True,
                "rows": [
                    {
                        "guild_id": 1,
                        "reactor_id": 100,
                        "author_id": 999,
                        "created_at": 1784776342.0,
                    }
                ],
            },
            "member_xp": {
                "matched_columns": ["user_id"],
                "guild_scoped": True,
                "rows": [{"guild_id": 1, "user_id": 100, "total_xp": 41391.9, "level": 46}],
            },
        },
        "counts": {"starboard_reactors": 1, "member_xp": 1},
        "review_required": ["starboard_reactors"],
        "notes": [],
    }


REGISTER = _register(
    "| starboard_reactors | Starboard | reactor/author pairs | 180 days | "
    "**NO** — evidence path | — | biggest unpurged table |\n"
    "| member_xp, xp_events | XP/activity | xp totals | "
    "permanent — Art 17(3)(e) | YES — purge | — | ✓ |\n"
)


def test_a_second_member_is_never_named_in_the_report():
    """Art 15(4), satisfied by construction rather than by an operator.

    ``starboard_reactors`` names the member who was reacted to. The report must
    contribute its count and say a second person is involved, without ever
    printing that person's id — so the file needs no redaction pass before it
    is sent.
    """
    conn = _tiny_db()
    report = ds.build_report(conn, _export(), REGISTER, 1, 100)
    rendered = ds.render_markdown(report, "Test Server")

    assert "999" not in rendered
    assert "starboard_reactors" in THIRD_PARTY_TABLES
    assert "name another member" in rendered
    social = next(c for c in report.categories if c.key == "social")
    assert social.rows == 1
    assert social.third_party_tables == 1


def test_no_register_internals_reach_the_member_copy():
    """The register's cells carry commit hashes, table names and notes-to-self.

    An earlier draft pasted them through, and produced a document telling the
    member "econ_purge_user shipped 6ac71558" and "make preserve explicit".
    """
    conn = _tiny_db()
    report = ds.build_report(conn, _export(), REGISTER, 1, 100)
    rendered = ds.render_markdown(report, "Test Server")

    for leak in (
        "starboard_reactors",
        "member_xp",
        "xp_events",
        "purge",
        "Art 17(3)",
        "evidence path",
        "unpurged",
        "✓",
    ):
        assert leak not in rendered, f"register internals leaked: {leak!r}"


def test_self_authored_values_are_printed_back():
    conn = _tiny_db()
    report = ds.build_report(conn, _export(), REGISTER, 1, 100)
    rendered = ds.render_markdown(report, "Test Server")
    assert "Level: 46" in rendered
    assert "41391.9" in rendered


def test_list_valued_scan_finds_an_id_equality_cannot():
    """The export's documented blind spot, closed rather than declared."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE hp_group_games (roster TEXT)")
    conn.execute('INSERT INTO hp_group_games VALUES (\'[100, 200, 300]\')')
    conn.execute('INSERT INTO hp_group_games VALUES (\'[200, 300]\')')
    hits = ds.scan_list_valued(conn, 100)
    assert hits == {"hp_group_games.roster": 1}


def test_list_valued_scan_does_not_match_a_substring():
    """``1001`` contains ``100`` — a LIKE prefilter must not become the answer."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE hp_group_games (roster TEXT)")
    conn.execute('INSERT INTO hp_group_games VALUES (\'[1001, 2002]\')')
    assert ds.scan_list_valued(conn, 100) == {}


def test_list_valued_scan_finds_an_id_used_as_a_payload_key():
    """Game payloads key scores and rolls by member id (``{"100": 200}``)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE games_game_history (payload TEXT)")
    conn.execute('INSERT INTO games_game_history VALUES (\'{"scores": {"100": 200}}\')')
    conn.execute('INSERT INTO games_game_history VALUES (\'{"scores": {"1001": 50}}\')')
    assert ds.scan_list_valued(conn, 100) == {"games_game_history.payload": 1}


def test_a_declared_mixed_cell_yields_no_duration():
    """The register marks multi-period cells "Mixed."; take no number from them.

    This column produced two cells where the number present described something
    other than the store's own retention — ``xp_daily``'s "90-day" governed the
    table it *replaces*, and ``whispers``' "30d age-lock" was a visibility rule
    that deletes nothing. Both would have promised a member a deletion that
    never happens. A number is unsafe in a field meaning "a number" even when
    the surrounding prose disclaims it, so the mark is read structurally.
    """
    cell = "Mixed. `member_xp` permanent; `xp_events` 90 days once enabled"
    assert ds.classify_retention(cell) == "mixed"
    assert ds._is_declared_mixed(cell)

    register = _register(
        f"| member_xp, xp_events | XP/activity | totals | {cell} | YES | — | n |\n"
    )
    conn = _tiny_db()
    report = ds.build_report(conn, _export(), register, 1, 100)
    activity = next(c for c in report.categories if c.key == "activity")
    assert activity.durations == []
    assert "90 days" not in ds._retention_sentence(activity)


def test_no_category_promises_data_a_migration_destroyed():
    """The report must not describe stores the bot no longer has.

    The register→category guard above runs in one direction only: it fails
    when a register Feature maps to no category. A category whose *blurb*
    still describes tables that were dropped is invisible to it, and that is
    the failure this pins.

    Both cases below were live until 2026-09-19. ``messages`` told a member
    "the text of messages you have sent … any attachments or links they
    carried" and that deleted messages "stay readable to moderators" —
    migration 228 dropped ``messages.content`` (600,799 rows of text),
    ``message_attachments`` and ``message_embeds``. ``social`` promised "who
    followed whom between voice channels" and a record of "who you talk to
    and react to" — migration 229 dropped ``user_interactions``,
    ``user_interactions_log``, ``voice_follow``, ``voice_follow_log`` and
    ``reaction_log``.

    This is the one document whose entire job is to be accurate: it is what a
    member is handed when they exercise their right of access, so a sentence
    describing a store that no longer exists is worse here than anywhere else
    in the codebase.
    """
    by_key = {c.key: c for c in CATEGORIES}

    messages = by_key["messages"]
    text = (messages.blurb + " " + messages.preserved).lower()
    # Migration 228: no text, no attachments, no embeds.
    assert "the text of messages you have sent" not in text
    assert "stay readable to moderators" not in text
    assert "attachments or links they carried" not in text
    # It must still say plainly that the text is *not* kept.
    assert "does not keep the text" in messages.blurb.lower()

    social = by_key["social"]
    text = (social.blurb + " " + social.preserved).lower()
    # Migration 229: the member-to-member edge stores are gone.
    assert "who followed whom" not in text or "no longer keeps" in text
    assert "a record of who you talk to and react to" not in text
    assert "records of who reacted to whom" not in text

    # The voice category promised the same dropped voice-follow log as the
    # thing it preserves, which would have survived a fix aimed only at the
    # two categories the scan named.
    voice = by_key["voice"]
    assert "followed whom between voice rooms" not in voice.preserved.lower()


def test_the_report_does_not_claim_to_hold_voice_transcripts():
    """Over-disclosure is a defect too, and this one ran the other way.

    Until 2026-09-19 the voice category listed "transcribed speech" among the
    things the server holds about a member. It does not: transcription posts
    the text into the channel as an ordinary Discord message and keeps no
    copy (``voice_transcription_cog`` — "the transcript's only home is the
    Discord message"), and ``docs/data_register.md`` records the store as
    "config only — transcripts never stored".

    A report that overstates what is held is not the safe direction to be
    wrong in either: it invites a member to ask for the deletion of something
    that was never there, and the answer makes the rest of the document look
    less trustworthy than it is.
    """
    voice = next(c for c in CATEGORIES if c.key == "voice")
    assert "keeps no copy" in voice.blurb.lower()
    # The bare claim, without the correction that follows it, must not return.
    assert "and — where a channel has transcription switched on — transcribed speech" not in (
        voice.blurb.lower()
    )
