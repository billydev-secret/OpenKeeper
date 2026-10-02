"""Stopped loops and paused deliveries, as Config Problems card rows."""

from __future__ import annotations

import pytest

from bot_modules.core.background import GaveUp, Stall
from bot_modules.analytics.delivery_health_logic import delivery_problems

NAMES = {71: "general"}


def _stall(**kw) -> Stall:
    base = dict(
        label="coin drop", guild_id=7, channel_id=71, failures=5, retrying=True,
        paused_note="drops there are paused, with one attempt an hour",
    )
    return Stall(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stall", "severity", "title"),
    [
        pytest.param(_stall(), "warning", "Coin drop can't post in #general", id="probed"),
        pytest.param(
            _stall(label="econ panel", retrying=False), "error",
            "Econ panel can't post in #general", id="waits-for-proof",
        ),
        pytest.param(
            _stall(channel_id=99), "warning", "Coin drop can't post in its channel",
            id="channel-gone",
        ),
        pytest.param(
            _stall(channel_id=None), "warning", "Coin drop can't post in its channel",
            id="channel-unknown",
        ),
    ],
)
def test_a_paused_delivery(stall, severity, title):
    [row] = delivery_problems([], [stall], NAMES.get)
    assert (row["severity"], row["title"]) == (severity, title)
    assert row["message"].startswith("The last 5 tries to post in")
    assert "Drops there are paused, with one attempt an hour." in row["message"]


def test_a_stopped_loop_comes_first_and_says_when():
    loop = GaveUp(name="coin_drops_loop", at=0.0, crashes=11, error="RuntimeError: x")
    rows = delivery_problems([loop], [_stall()], NAMES.get)
    assert [r["code"] for r in rows] == [
        "loop_stopped:coin_drops_loop", "delivery_paused:coin drop:71",
    ]
    assert rows[0]["severity"] == "error"
    assert rows[0]["title"] == "Background job coin_drops_loop has stopped"
    assert "crashed 11 times" in rows[0]["message"]
    assert "at 1970-01-01 00:00 UTC" in rows[0]["message"]
    assert rows[0]["message"].endswith("Last error: RuntimeError: x")


def test_nothing_stopped_is_no_rows():
    assert delivery_problems([], [], NAMES.get) == []
