"""Tests for /api/privacy — Moderation → Member Data.

The service layer already proves what each erasure *does*
(``tests/privacy/test_privacy_service.py``, ``tests/privacy/test_privacy_anonymize.py``). What
only the route can prove is the part that is new here: that the receipts these
endpoints write cannot re-identify the member they were written about, that
anonymising is refused from a guild that does not own the decision, and that
message deletion — the one operation that must never touch the database — still
doesn't when driven from the web.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from web_server.auth import SESSION_COOKIE, DiscordOAuthAuth
from web_server.server import create_app

# A real Discord snowflake: above 2^53, and above the anonymiser's floor for
# "this is a member id and not a surrogate".
SUBJECT = 203899999999999916


def _seed(db, guild_id: int, user_id: int = SUBJECT) -> None:
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO known_users (guild_id, user_id, username, display_name)"
            " VALUES (?, ?, ?, ?)",
            (guild_id, user_id, "departed", "Departed"),
        )
        conn.execute(
            "INSERT INTO member_xp (guild_id, user_id, total_xp) VALUES (?, ?, ?)",
            (guild_id, user_id, 500),
        )


def _audit(db) -> list[dict]:
    with open_db(db) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT action, actor_id, target_id, extra FROM audit_log"
                " ORDER BY id"
            ).fetchall()
        ]


# ── export ────────────────────────────────────────────────────────────────


def test_the_export_summary_counts_rows_without_returning_them(
    authed_client, fake_ctx
):
    """The payload behind these counts is the whole of a member's data. The
    page shows counts; the file is a separate, deliberate press."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    body = authed_client.get(f"/api/privacy/export?user_id={SUBJECT}").json()

    assert body["counts"]["member_xp"] == 1
    assert body["total_rows"] >= 1
    assert "review_required" in body
    assert "rows" not in body, "the summary must not carry row content"
    assert "tables" not in body


def test_ids_leave_as_strings(authed_client, fake_ctx):
    """A snowflake past 2^53 is mangled by JS's Number."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    subject = authed_client.get(f"/api/privacy/export?user_id={SUBJECT}").json()[
        "subject"
    ]
    assert subject["user_id"] == str(SUBJECT)
    assert subject["guild_id"] == str(fake_ctx.guild_id)


def test_the_download_arrives_as_an_attachment(authed_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    resp = authed_client.get(f"/api/privacy/export/download?user_id={SUBJECT}")

    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert str(SUBJECT) in resp.headers["content-disposition"]
    assert "member_xp" in resp.json()["tables"]


def test_the_export_writes_no_receipt(authed_client, fake_ctx):
    """Reading is not an event worth an audit row, and Art 15 does not make it
    one — the file itself is the record."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    authed_client.get(f"/api/privacy/export?user_id={SUBJECT}")
    authed_client.get(f"/api/privacy/export/download?user_id={SUBJECT}")

    assert _audit(fake_ctx.db_path) == []


# ── receipts ──────────────────────────────────────────────────────────────


def test_a_purge_receipt_cannot_re_identify_the_member(authed_client, fake_ctx):
    """``purge_user_data`` does not clear plain ``audit_log``, so a receipt
    naming the member would be the one row that outlives their erasure."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    resp = authed_client.post(
        "/api/privacy/purge", json={"user_id": str(SUBJECT)}
    )

    assert resp.status_code == 200
    rows = _audit(fake_ctx.db_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "privacy_purge"
    assert rows[0]["actor_id"] == 1, "who ran it is the part worth keeping"
    assert rows[0]["target_id"] is None
    assert str(SUBJECT) not in rows[0]["extra"]


def test_the_purge_response_reports_every_row_erased(authed_client, fake_ctx):
    """The panel's toast used to say "Erased N rows" with N the member's
    *message* rows only, so erasing a member with no message log read as
    "Erased 0 rows" while their XP and profile rows went."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    body = authed_client.post(
        "/api/privacy/purge", json={"user_id": str(SUBJECT)}
    ).json()

    assert body["ok"] is True
    assert body["message_rows"] == 0
    assert body["rows"] >= 2, "the known_users and member_xp rows both went"
    extra = _audit(fake_ctx.db_path)[0]["extra"]
    assert f'"rows": {body["rows"]}' in extra


def test_erasing_takes_the_members_guess_photo_off_the_disk(
    authed_client, fake_ctx, tmp_path
):
    """The one thing the service layer cannot do. ``purge_user_data`` deletes
    the round row, but an unlink cannot be rolled back inside the caller's
    transaction — so the route unlinks first, and once the row is gone nothing
    knows the file was theirs. Without this the erasure reports success with
    the member's full-size photo still on disk."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)
    photo = tmp_path / "7.jpg"
    photo.write_bytes(b"jpeg")
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO guess_rounds (id, guild_id, submitter_id, answer_id,"
            " channel_id, message_id, created_at, original_path)"
            " VALUES (7, ?, ?, ?, 1, 1, 1.0, ?)",
            (fake_ctx.guild_id, SUBJECT, SUBJECT, str(photo)),
        )

    cog = MagicMock(delete_guess_post=AsyncMock())
    fake_ctx.bot = MagicMock(get_cog=MagicMock(return_value=cog))

    resp = authed_client.post("/api/privacy/purge", json={"user_id": str(SUBJECT)})

    assert resp.status_code == 200
    assert not photo.exists()
    # And out of Discord: "Deleting the message should clear everything from
    # guess" (Billy, 2026-09-27) — the crop post goes before the row.
    cog.delete_guess_post.assert_awaited_once_with(fake_ctx.guild_id, 1, 1)
    with open_db(fake_ctx.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM guess_rounds").fetchone()[0] == 0


def test_an_erasure_with_no_guess_post_runs_with_the_bot_disconnected(
    authed_client, fake_ctx
):
    """The bot is needed only to take a post down, so an erasure with none
    to take down must not 503 on a disconnected bot."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)
    fake_ctx.bot = None
    resp = authed_client.post("/api/privacy/purge", json={"user_id": str(SUBJECT)})
    assert resp.status_code == 200


def test_an_anonymise_receipt_cannot_re_identify_the_member(
    authed_client, fake_ctx
):
    """The sharper case: ``audit_log`` is *not* in ``ANONYMIZE_KEEP_TABLES``, so
    a receipt written after the run puts the real id straight back into the
    database the run just cleaned."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    resp = authed_client.post(
        "/api/privacy/anonymize",
        json={"user_id": str(SUBJECT), "surrogate_id": 1},
    )

    assert resp.status_code == 200, resp.text
    rows = _audit(fake_ctx.db_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "privacy_anonymize"
    assert rows[0]["target_id"] is None
    assert str(SUBJECT) not in rows[0]["extra"]
    assert '"surrogate": 1' in rows[0]["extra"], "the surrogate is safe to keep"


def test_the_anonymise_plan_writes_nothing(authed_client, fake_ctx):
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    body = authed_client.get(
        f"/api/privacy/anonymize/plan?user_id={SUBJECT}"
    ).json()

    assert body["user_id"] == str(SUBJECT)
    assert body["total_rows"] >= 1
    assert _audit(fake_ctx.db_path) == []
    with open_db(fake_ctx.db_path) as conn:
        still_there = conn.execute(
            "SELECT COUNT(*) FROM member_xp WHERE user_id = ?", (SUBJECT,)
        ).fetchone()[0]
    assert still_there == 1


def test_a_sweep_dry_run_writes_neither_rows_nor_a_receipt(
    authed_client, fake_ctx
):
    _seed(fake_ctx.db_path, fake_ctx.guild_id)
    authed_client.post(
        "/api/privacy/anonymize",
        json={"user_id": str(SUBJECT), "surrogate_id": 1},
    )

    resp = authed_client.post(
        "/api/privacy/anonymize/sweep?dry_run=true", json={"surrogate_id": 1}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is True
    assert [r["action"] for r in _audit(fake_ctx.db_path)] == ["privacy_anonymize"]


# ── guards ────────────────────────────────────────────────────────────────


def test_anonymising_is_refused_from_a_secondary_guild(fake_ctx):
    """It rewrites the member's id in *every* guild the bot serves, so the
    decision belongs to the guild that runs the bot — not to whichever admin
    happens to have another server selected."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    other = fake_ctx.guild_id + 1
    client = TestClient(create_app(fake_ctx, auth=auth))
    client.cookies.set(
        SESSION_COOKIE,
        auth.create_session_cookie(
            user_id=1,
            username="tester",
            access_token="token",
            permission_bits=0x8,
            guild_id=other,
            guilds=[{"id": other, "name": "Other", "icon": None}],
        ),
    )
    try:
        assert client.get(f"/api/privacy/anonymize/plan?user_id={SUBJECT}").status_code == 403
        assert client.post(
            "/api/privacy/anonymize",
            json={"user_id": str(SUBJECT), "surrogate_id": 1},
        ).status_code == 403
        # Guild-scoped paths are unaffected — they only ever touch this guild.
        assert client.get(f"/api/privacy/export?user_id={SUBJECT}").status_code == 200
    finally:
        client.close()


def test_a_moderator_is_refused_everywhere_on_this_page(fake_ctx):
    """The page is admin-only, and that is a deliberate narrowing: clearing a
    member's Discord messages used to be open to moderators via /delete_user."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth))
    client.cookies.set(
        SESSION_COOKIE,
        auth.create_session_cookie(
            user_id=7,
            username="mod",
            access_token="token",
            permission_bits=0x2000,  # MANAGE_MESSAGES → moderator, not admin
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
        ),
    )
    try:
        assert client.get(f"/api/privacy/export?user_id={SUBJECT}").status_code == 403
        assert client.get(
            f"/api/privacy/export/download?user_id={SUBJECT}"
        ).status_code == 403
        assert client.get(
            f"/api/privacy/anonymize/plan?user_id={SUBJECT}"
        ).status_code == 403
        assert client.post(
            "/api/privacy/purge", json={"user_id": str(SUBJECT)}
        ).status_code == 403
        assert client.post(
            "/api/privacy/anonymize",
            json={"user_id": str(SUBJECT), "surrogate_id": 1},
        ).status_code == 403
        assert client.post(
            "/api/privacy/messages/delete", json={"user_id": str(SUBJECT)}
        ).status_code == 403
    finally:
        client.close()


@pytest.mark.parametrize("raw", ["", "0", "-1", "not-an-id", "12x"])
def test_a_junk_member_id_is_refused_before_anything_runs(
    authed_client, fake_ctx, raw
):
    assert authed_client.get(f"/api/privacy/export?user_id={raw}").status_code == 422
    assert (
        authed_client.post("/api/privacy/purge", json={"user_id": raw}).status_code
        == 422
    )
    assert _audit(fake_ctx.db_path) == []


def test_anonymising_without_a_surrogate_is_refused(authed_client, fake_ctx):
    """The plan names the surrogate to use; a caller that skipped it must not
    get one picked silently underneath them.

    The refusal is the service's — the route deliberately has no check of its
    own — so this asserts the operator-facing message reaches the panel rather
    than just the status code, which is all a second guard would have pinned.
    """
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    resp = authed_client.post(
        "/api/privacy/anonymize", json={"user_id": str(SUBJECT)}
    )

    assert resp.status_code == 422
    assert "out of range" in resp.json()["detail"]
    assert _audit(fake_ctx.db_path) == []


def test_a_surrogate_already_in_use_is_refused(authed_client, fake_ctx):
    """Reusing one merges two anonymised members into a single key and silently
    corrupts every per-member metric for both."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO known_users (guild_id, user_id, username, display_name)"
            " VALUES (?, ?, ?, ?)",
            (fake_ctx.guild_id, 1, "surrogate", "Surrogate"),
        )

    resp = authed_client.post(
        "/api/privacy/anonymize",
        json={"user_id": str(SUBJECT), "surrogate_id": 1},
    )

    assert resp.status_code == 422
    assert "already in use" in resp.json()["detail"]
    assert _audit(fake_ctx.db_path) == []


def test_the_service_refusal_becomes_a_422_not_a_500(authed_client, fake_ctx):
    """``anonymize_missed_text`` refuses a subject that is not a surrogate —
    that is a bad request, not a server fault."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)

    resp = authed_client.post(
        "/api/privacy/anonymize/sweep?dry_run=true",
        json={"surrogate_id": SUBJECT},
    )

    assert resp.status_code == 422
    assert "surrogate" in resp.json()["detail"].lower()


# ── Discord messages ──────────────────────────────────────────────────────


def test_deleting_messages_reports_503_when_the_bot_is_down(
    authed_client, fake_ctx
):
    fake_ctx.bot = None

    resp = authed_client.post(
        "/api/privacy/messages/delete", json={"user_id": str(SUBJECT)}
    )

    assert resp.status_code == 503


def test_a_second_deletion_for_the_same_member_is_refused(
    authed_client, fake_ctx
):
    cog = MagicMock()
    cog.start_message_deletion = AsyncMock(
        return_value={"started": False, "reason": "already_running"}
    )
    fake_ctx.bot = MagicMock(get_cog=MagicMock(return_value=cog))

    resp = authed_client.post(
        "/api/privacy/messages/delete", json={"user_id": str(SUBJECT)}
    )

    assert resp.status_code == 409


def test_an_unknown_deletion_mode_is_refused(authed_client, fake_ctx):
    cog = MagicMock()
    cog.start_message_deletion = AsyncMock(return_value={"started": True})
    fake_ctx.bot = MagicMock(get_cog=MagicMock(return_value=cog))

    resp = authed_client.post(
        "/api/privacy/messages/delete",
        json={"user_id": str(SUBJECT), "mode": "everything"},
    )

    assert resp.status_code == 422
    cog.start_message_deletion.assert_not_awaited()


def test_deleting_messages_writes_no_receipt_and_no_row(authed_client, fake_ctx):
    """The one operation here that must leave the database completely alone."""
    _seed(fake_ctx.db_path, fake_ctx.guild_id)
    cog = MagicMock()
    cog.start_message_deletion = AsyncMock(return_value={"started": True})
    fake_ctx.bot = MagicMock(get_cog=MagicMock(return_value=cog))

    assert authed_client.post(
        "/api/privacy/messages/delete", json={"user_id": str(SUBJECT)}
    ).status_code == 200

    assert _audit(fake_ctx.db_path) == []
    with open_db(fake_ctx.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM member_xp WHERE user_id = ?", (SUBJECT,)
        ).fetchone()[0] == 1


def test_status_is_idle_for_a_member_with_no_run(authed_client, fake_ctx):
    cog = MagicMock()
    cog.deletion_status = MagicMock(return_value=None)
    fake_ctx.bot = MagicMock(get_cog=MagicMock(return_value=cog))

    body = authed_client.get(
        f"/api/privacy/messages/status?user_id={SUBJECT}"
    ).json()

    assert body == {"phase": "idle"}


# ── When members leave ────────────────────────────────────────────────────


def _queue(db, guild_id: int, user_id: int = SUBJECT, *, state: str = "pending") -> None:
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO departure_erasure_queue (guild_id, user_id, left_at, next_at, state)"
            " VALUES (?, ?, 1000.0, 1300.0, ?)",
            (guild_id, user_id, state),
        )


def test_the_departure_dial_ships_off_with_an_empty_queue(authed_client):
    body = authed_client.get("/api/privacy/departures").json()
    assert body["enabled"] is False
    assert body["jobs"] == []
    assert body["primary"] is True


def test_switching_the_dial_writes_this_guilds_config(authed_client, fake_ctx):
    assert authed_client.put("/api/privacy/departures", json={"enabled": True}).status_code == 200
    assert authed_client.get("/api/privacy/departures").json()["enabled"] is True
    with open_db(fake_ctx.db_path) as conn:
        [(guild,)] = conn.execute(
            "SELECT guild_id FROM config WHERE key = 'departure_auto_anonymise'"
        ).fetchall()
    assert guild == fake_ctx.guild_id


def test_switching_off_empties_the_queue(authed_client, fake_ctx):
    _queue(fake_ctx.db_path, fake_ctx.guild_id)
    authed_client.put("/api/privacy/departures", json={"enabled": True})

    body = authed_client.put("/api/privacy/departures", json={"enabled": False}).json()

    assert body["dropped"] == 1
    assert authed_client.get("/api/privacy/departures").json()["jobs"] == []


def test_queued_ids_leave_as_strings(authed_client, fake_ctx):
    _queue(fake_ctx.db_path, fake_ctx.guild_id)
    [job] = authed_client.get("/api/privacy/departures").json()["jobs"]
    assert job["user_id"] == str(SUBJECT)


def test_retry_revives_only_a_failed_job(authed_client, fake_ctx):
    _queue(fake_ctx.db_path, fake_ctx.guild_id)
    body = {"user_id": str(SUBJECT)}
    assert authed_client.post("/api/privacy/departures/retry", json=body).status_code == 404

    with open_db(fake_ctx.db_path) as conn:
        conn.execute("UPDATE departure_erasure_queue SET state = 'failed', attempts = 5")
    assert authed_client.post("/api/privacy/departures/retry", json=body).status_code == 200
    [job] = authed_client.get("/api/privacy/departures").json()["jobs"]
    assert (job["state"], job["attempts"]) == ("pending", 0)


def test_drop_removes_the_job_and_404s_when_there_is_none(authed_client, fake_ctx):
    _queue(fake_ctx.db_path, fake_ctx.guild_id)
    body = {"user_id": str(SUBJECT)}
    assert authed_client.post("/api/privacy/departures/drop", json=body).status_code == 200
    assert authed_client.post("/api/privacy/departures/drop", json=body).status_code == 404


def test_a_moderator_cannot_see_or_touch_the_departure_queue(fake_ctx):
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth))
    client.cookies.set(
        SESSION_COOKIE,
        auth.create_session_cookie(
            user_id=7,
            username="mod",
            access_token="token",
            permission_bits=0x2000,
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
        ),
    )
    try:
        assert client.get("/api/privacy/departures").status_code == 403
        assert client.put("/api/privacy/departures", json={"enabled": True}).status_code == 403
        assert client.post(
            "/api/privacy/departures/drop", json={"user_id": str(SUBJECT)}
        ).status_code == 403
    finally:
        client.close()
