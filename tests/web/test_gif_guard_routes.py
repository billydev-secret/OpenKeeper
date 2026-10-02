"""GIF Guard's dashboard route: the channel list round-trips and reaches the bot."""

from __future__ import annotations

from bot_modules.core.db_utils import get_config_id_set, open_db


def test_gif_guard_is_off_until_channels_are_picked(authed_client, fake_ctx):
    body = authed_client.get("/api/config").json()
    assert body["gif_guard"] == {"channel_ids": []}
    assert fake_ctx.guild_config(fake_ctx.guild_id).gif_guard_channel_ids == frozenset()


def test_gif_guard_channels_round_trip_and_reach_the_bot(authed_client, fake_ctx):
    primed = fake_ctx.guild_config(fake_ctx.guild_id)
    big = "1476000000000000484"  # > 2**53: must travel as a string

    resp = authed_client.put("/api/config/gif-guard", json={"channel_ids": ["7001", big]})
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        assert get_config_id_set(conn, "gif_guard_channel_ids", fake_ctx.guild_id) == {
            7001,
            int(big),
        }
    assert authed_client.get("/api/config").json()["gif_guard"]["channel_ids"] == sorted(
        ["7001", big], key=int
    )
    # The cached snapshot on_message reads was dropped, not left stale.
    fresh = fake_ctx.guild_config(fake_ctx.guild_id)
    assert fresh is not primed
    assert fresh.gif_guard_channel_ids == frozenset({7001, int(big)})

    # Clearing the list turns it back off.
    assert authed_client.put("/api/config/gif-guard", json={"channel_ids": []}).status_code == 200
    assert fake_ctx.guild_config(fake_ctx.guild_id).gif_guard_channel_ids == frozenset()


def test_gif_guard_rejects_non_numeric_ids(authed_client):
    resp = authed_client.put("/api/config/gif-guard", json={"channel_ids": ["general"]})
    assert resp.status_code == 400
