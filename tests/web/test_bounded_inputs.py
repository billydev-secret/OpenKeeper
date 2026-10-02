"""Out-of-range numbers are refused, not quietly rewritten.

Before this sweep the dashboard did both, depending on which route you hit.
Most bounded inputs clamped: ``?limit=100000`` came back 200 with 500 rows,
and a duel dial typed as 1s was stored as 5s under a green "Saved" while the
form still showed 1. A few refused, with a hand-rolled 400. The house rule is
now one answer everywhere — **422, never clamp** — because a clamp tells the
admin their number was accepted when a different number was stored, and the
panel has no way to know it should re-read the form.

Bounds are declared where the value enters (``Query(ge=, le=)`` or a
``Field`` on the body model) so FastAPI raises them; ``require_in_range``
covers the handful whose bounds are table-driven. These tests pin the
*contract* on a representative route of each kind — the per-route tests
alongside them cover their own dials — plus the two properties a clamp never
had to have: nothing is written, and the edges are inclusive.
"""

from __future__ import annotations

import pytest


# ── Query-parameter bounds ───────────────────────────────────────────

@pytest.mark.parametrize(
    ("path", "params"),
    [
        pytest.param("/api/reports/usage", {"days": 0}, id="usage-days-low"),
        pytest.param("/api/reports/usage", {"days": 366}, id="usage-days-high"),
        pytest.param("/api/reports/intake-report", {"days": 0}, id="intake-days-low"),
        pytest.param("/api/reports/inactive-report", {"limit": 0}, id="inactive-limit-low"),
        pytest.param("/api/reports/inactive-report", {"limit": 2001}, id="inactive-limit-high"),
        pytest.param("/api/meta/members", {"limit": 0}, id="members-limit-low"),
        pytest.param("/api/help/suggestions", {"limit": 41}, id="suggestions-limit-high"),
    ],
)
def test_a_query_bound_is_refused_not_clamped(open_client, path, params):
    resp = open_client.get(path, params=params)
    assert resp.status_code == 422, f"{path} {params} -> {resp.status_code}"


def test_the_refusal_names_the_parameter(open_client):
    """FastAPI's own shape: ``detail`` is a list whose ``loc`` says which
    input was wrong. ``api.js`` joins the ``msg`` fields, so the admin gets a
    sentence rather than a blank failure."""
    body = open_client.get("/api/reports/usage", params={"days": 0}).json()
    assert isinstance(body["detail"], list)
    entry = body["detail"][0]
    assert "days" in entry["loc"]
    assert entry["msg"]


@pytest.mark.parametrize(
    ("path", "params"),
    [
        pytest.param("/api/reports/usage", {"days": 1}, id="usage-floor"),
        pytest.param("/api/reports/usage", {"days": 365}, id="usage-ceiling"),
        pytest.param("/api/meta/members", {"limit": 1}, id="members-floor"),
    ],
)
def test_the_edges_are_inclusive(open_client, path, params):
    """A bound the panel offers must be selectable. ``days=0`` on the inactive
    report is a documented value ("list the whole scope"), not a floor
    violation, and it survived the sweep."""
    assert open_client.get(path, params=params).status_code == 200


def test_zero_days_on_the_inactive_report_is_a_real_value(open_client, live_guild):
    """``days=0`` means "list the whole scope" — the one bound in the sweep
    whose floor is 0 rather than 1. It is a documented choice on that report,
    so the sweep had to keep it selectable rather than round every floor up.
    """
    live_guild()
    assert open_client.get(
        "/api/reports/inactive-report", params={"days": 0}
    ).status_code == 200
    assert open_client.get(
        "/api/reports/inactive-report", params={"days": -1}
    ).status_code == 422


# ── Body-model bounds ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("path", "payload"),
    [
        pytest.param("/api/config/inactive", {"threshold_days": 0}, id="inactive-threshold"),
        pytest.param("/api/config/inactive", {"sweep_cap": 201}, id="inactive-cap"),
        pytest.param("/api/config/whisper", {"guesses_per_whisper": 11}, id="whisper-guesses"),
        pytest.param("/api/config/whisper", {"cooldown_seconds": -1}, id="whisper-cooldown"),
        pytest.param("/api/config/guess", {"guess_cooldown_seconds": 3601}, id="guess-cooldown"),
        pytest.param("/api/config/guess", {"min_image_dimension_px": 0}, id="guess-min-px-low"),
        pytest.param("/api/config/guess", {"min_image_dimension_px": 4097}, id="guess-min-px-high"),
        pytest.param("/api/config/guess", {"max_image_size_mb": 0}, id="guess-max-mb-low"),
        pytest.param("/api/config/guess", {"max_image_size_mb": 101}, id="guess-max-mb-high"),
        pytest.param("/api/config/birthday/settings", {"birthday_announce_hour": 24}, id="birthday-hour"),
        pytest.param("/api/config/greeting-watch", {"window_minutes": 1441}, id="greeting-window"),
    ],
)
def test_a_dial_outside_its_bound_is_refused(open_client, path, payload):
    resp = open_client.put(path, json=payload)
    assert resp.status_code == 422, f"{path} {payload} -> {resp.status_code}"


def test_a_refused_dial_writes_nothing(open_client):
    """The bound is checked before the handler opens the database, so a form
    with one bad number stores none of its fields — not even the good ones."""
    open_client.put("/api/config/inactive", json={"threshold_days": 90, "sweep_cap": 25})
    before = open_client.get("/api/config").json()["inactive"]

    resp = open_client.put(
        "/api/config/inactive", json={"threshold_days": 45, "sweep_cap": 9999}
    )
    assert resp.status_code == 422
    after = open_client.get("/api/config").json()["inactive"]
    assert after["threshold_days"] == before["threshold_days"]
    assert after["sweep_cap"] == before["sweep_cap"]


# ── The table-driven half ────────────────────────────────────────────

def test_require_in_range_speaks_pydantic():
    """``require_in_range`` exists so the duel rule table stays the one place
    a duel bound is written. Its refusal has to be indistinguishable from a
    declared one, or the panel would need a second error branch for six
    routes out of forty-odd.
    """
    from fastapi import HTTPException

    from web_server.helpers import require_in_range

    assert require_in_range(5, 1, 10, field="x") == 5
    assert require_in_range(9999, 0, None, field="open_top") == 9999

    with pytest.raises(HTTPException) as low:
        require_in_range(0, 1, 10, field="min_timer")
    assert low.value.status_code == 422
    entry = low.value.detail[0]
    assert entry["type"] == "greater_than_equal"
    assert entry["loc"] == ["body", "min_timer"]
    assert entry["msg"] == "Input should be greater than or equal to 1"

    with pytest.raises(HTTPException) as high:
        require_in_range(11, 1, 10, field="min_timer")
    assert high.value.detail[0]["type"] == "less_than_equal"


# ── What deliberately still clamps ───────────────────────────────────

def test_derived_numbers_are_still_clamped():
    """Not every ``min(max(...))`` in the route layer was a caller's input.

    Little Libs derives its blank count from the paragraph count and its token
    budget from the blank count; ``meta/members`` widens its page budget to
    fit an explicit id list. Nobody typed those numbers, so there is nobody to
    refuse — clamping is the whole point of them, and a sweep that converted
    them would 422 an admin over arithmetic they never saw.
    """
    import inspect

    from web_server.routes import games, meta

    assert "min(max(4, paragraph_count * 2), 24)" in inspect.getsource(games)
    assert "min(max(budget, len(want_ids)), MEMBER_PAGE_MAX)" in inspect.getsource(meta)
