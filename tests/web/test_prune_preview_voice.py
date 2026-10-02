"""The Check Now dry run counts voice time exactly as the daily prune does."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from bot_modules.core.db_utils import open_db

ROLE_ID = 555
DAYS = 30


def _member(uid: int, *, in_voice: bool = False):
    m = MagicMock()
    m.id = uid
    m.bot = False
    m.display_name = f"member-{uid}"
    m.voice = MagicMock(channel=object()) if in_voice else None
    return m


def test_preview_keeps_members_with_recent_voice_or_in_voice_now(
    authed_client, fake_ctx
):
    stale, voiced, live = _member(401), _member(402), _member(403, in_voice=True)
    role = MagicMock()
    role.name = "NSFW"
    role.members = [stale, voiced, live]
    guild = MagicMock()
    guild.get_role = MagicMock(return_value=role)
    fake_ctx.bot = MagicMock()
    fake_ctx.bot.get_guild = MagicMock(return_value=guild)

    now = time.time()
    old = now - (DAYS + 10) * 86400
    with open_db(fake_ctx.db_path) as conn:
        for uid in (401, 402, 403):
            conn.execute(
                "INSERT INTO member_activity (guild_id, user_id, last_channel_id, "
                "last_message_id, last_message_at) VALUES (?, ?, 1, 1, ?)",
                (fake_ctx.guild_id, uid, old),
            )
        conn.execute(
            "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at) "
            "VALUES (?, 402, 'voice', 1, ?)",
            (fake_ctx.guild_id, now - 86400),
        )

    resp = authed_client.post(
        "/api/config/prune/preview",
        json={"role_id": str(ROLE_ID), "inactivity_days": DAYS},
    )

    assert resp.status_code == 200
    assert [c["id"] for c in resp.json()["candidates"]] == ["401"]
