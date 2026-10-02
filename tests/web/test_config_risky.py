"""The Risky Rolls panel's two payoff dials (`/api/config/risky`).

Both ship at 0 (off). The chaser reads the config rows fresh each tick, so
the route's job is only to store them within bounds — no in-memory cache to
update, unlike the three older dials tested in test_config_routes.py.
"""

from __future__ import annotations

import pytest

from bot_modules.games.risky_roll.logic import PAYOFF_HOURS_MAX, PayoffDials, SpecialRollOdds
from bot_modules.games.risky_roll.store import StateStore


def _dials(fake_ctx) -> dict[int, PayoffDials]:
    return StateStore(fake_ctx.db_path)._load_payoff_dials()


def test_risky_section_ships_both_payoff_dials_off(authed_client):
    r = authed_client.get("/api/config").json()["risky"]
    assert r["chase_hours"] == 0
    assert r["fallback_hours"] == 0


def test_update_risky_persists_the_payoff_dials(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/risky", json={"chase_hours": 4, "fallback_hours": 24}
    )
    assert resp.status_code == 200

    r = authed_client.get("/api/config").json()["risky"]
    assert r["chase_hours"] == 4
    assert r["fallback_hours"] == 24
    # The chaser reads the same rows the panel wrote.
    assert _dials(fake_ctx)[fake_ctx.guild_id] == PayoffDials(chase_hours=4, fallback_hours=24)


def test_update_risky_zero_clears_a_payoff_dial(authed_client, fake_ctx):
    authed_client.put("/api/config/risky", json={"chase_hours": 4, "fallback_hours": 24})
    resp = authed_client.put("/api/config/risky", json={"chase_hours": 0})
    assert resp.status_code == 200

    r = authed_client.get("/api/config").json()["risky"]
    assert r["chase_hours"] == 0
    assert r["fallback_hours"] == 24  # untouched by a partial update
    assert _dials(fake_ctx)[fake_ctx.guild_id] == PayoffDials(chase_hours=0, fallback_hours=24)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"chase_hours": -1}, id="negative chase"),
        pytest.param({"fallback_hours": -1}, id="negative fallback"),
        pytest.param({"chase_hours": PAYOFF_HOURS_MAX + 1}, id="chase over a week"),
        pytest.param({"fallback_hours": PAYOFF_HOURS_MAX + 1}, id="fallback over a week"),
    ],
)
def test_update_risky_rejects_out_of_range_payoff_hours(authed_client, fake_ctx, body):
    resp = authed_client.put("/api/config/risky", json=body)
    assert resp.status_code == 400
    assert fake_ctx.guild_id not in _dials(fake_ctx)


# ── special-roll odds ────────────────────────────────────────────────


def _odds(fake_ctx) -> SpecialRollOdds:
    return StateStore(fake_ctx.db_path)._load_special_roll_odds(fake_ctx.guild_id)


def _odds_rows(fake_ctx) -> int:
    with StateStore(fake_ctx.db_path)._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM config WHERE guild_id = ? AND key LIKE 'risky_odds_%'",
            (fake_ctx.guild_id,),
        ).fetchone()[0]


def test_risky_section_ships_the_flat_die(authed_client):
    r = authed_client.get("/api/config").json()["risky"]
    assert (r["odds_69"], r["odds_100"], r["odds_1"]) == (100, 100, 100)


def test_update_risky_persists_the_special_roll_odds(authed_client, fake_ctx):
    resp = authed_client.put(
        "/api/config/risky", json={"odds_69": 500, "odds_100": 5, "odds_1": 0}
    )
    assert resp.status_code == 200

    r = authed_client.get("/api/config").json()["risky"]
    assert (r["odds_69"], r["odds_100"], r["odds_1"]) == (500, 5, 0)
    # The Roll button reads the same rows the panel wrote.
    assert _odds(fake_ctx) == SpecialRollOdds(sixty_nine=500, hundred=5, one=0)


def test_update_risky_flat_odds_store_no_row(authed_client, fake_ctx):
    authed_client.put("/api/config/risky", json={"odds_69": 500, "odds_1": 0})
    resp = authed_client.put("/api/config/risky", json={"odds_69": 100, "odds_100": 100})
    assert resp.status_code == 200

    assert _odds(fake_ctx) == SpecialRollOdds(one=0)  # untouched by a partial update
    assert _odds_rows(fake_ctx) == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"odds_69": 4}, id="commoner than 1 in 5"),
        pytest.param({"odds_100": 1}, id="every roll"),
        pytest.param({"odds_1": 1001}, id="rarer than 1 in 1000"),
        pytest.param({"odds_69": -1}, id="negative"),
    ],
)
def test_update_risky_rejects_out_of_range_odds(authed_client, fake_ctx, body):
    resp = authed_client.put("/api/config/risky", json=body)
    assert resp.status_code == 400
    assert _odds_rows(fake_ctx) == 0


def test_risky_section_odds_ignore_a_legacy_guild_zero_row(authed_client, fake_ctx):
    # The Roll button reads only the guild's own rows, so the panel must not
    # show a guild-0 value the die would ignore.
    with StateStore(fake_ctx.db_path)._connect() as conn:
        conn.execute("INSERT INTO config (guild_id, key, value) VALUES (0, 'risky_odds_69', '5')")
    r = authed_client.get("/api/config").json()["risky"]
    assert r["odds_69"] == 100
    assert _odds(fake_ctx).is_flat
