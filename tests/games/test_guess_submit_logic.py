"""``games/guess/submit_logic.submit_refusal`` — the one check both submit
doors (``/guess submit`` and the prompt's Submit form) run.

The doors had drifted once already — the form skipped the rate limit — so
the order matters as much as each refusal: the limiter records an attempt
when it runs, and must only run once the member could actually submit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot_modules.games.guess.models import GuessConfig
from bot_modules.games.guess.submit_logic import submit_refusal, too_large_text

ROLE = 7001


def _cfg(**kw) -> GuessConfig:
    base = dict(guild_id=1, guess_role_id=ROLE, guess_channel_id=8001, max_image_size_mb=5)
    base.update(kw)
    return GuessConfig(**base)


def _member(*role_ids: int):
    return SimpleNamespace(roles=[SimpleNamespace(id=r) for r in role_ids])


class _Limiter:
    def __init__(self, limited: bool = False) -> None:
        self.limited = limited
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.limited


@pytest.mark.parametrize(
    ("cfg", "member", "limited", "upload", "expected", "limiter_runs"),
    [
        pytest.param(_cfg(), _member(ROLE), False, ("image/png", 1024), None, 1, id="ok_upload"),
        pytest.param(_cfg(), _member(ROLE), False, None, None, 1, id="ok_link"),
        pytest.param(
            _cfg(guess_role_id=0), _member(ROLE), False, None,
            "Guess role is not configured", 0, id="role_unset",
        ),
        pytest.param(
            _cfg(guess_channel_id=0), _member(ROLE), False, None,
            "Guess channel is not configured", 0, id="channel_unset",
        ),
        pytest.param(_cfg(), None, False, None, "need the Guess role", 0, id="no_member"),
        pytest.param(_cfg(), _member(1), False, None, "need the Guess role", 0, id="no_role"),
        pytest.param(
            _cfg(), _member(ROLE), True, ("image/png", 1), "submission limit", 1,
            id="rate_limited",
        ),
        pytest.param(
            _cfg(), _member(ROLE), False, ("application/pdf", 1), "image file", 1,
            id="not_an_image",
        ),
        pytest.param(
            _cfg(), _member(ROLE), False, (None, 1), "image file", 1, id="no_type",
        ),
        pytest.param(
            _cfg(), _member(ROLE), False, ("image/png", 6 * 1024 * 1024), "too large", 1,
            id="too_large",
        ),
    ],
)
def test_submit_refusal(cfg, member, limited, upload, expected, limiter_runs):
    limiter = _Limiter(limited)
    content_type, size = upload if upload else (None, None)
    refusal = submit_refusal(
        cfg, member, rate_limited=limiter, content_type=content_type, size=size
    )
    if expected is None:
        assert refusal is None
    else:
        assert refusal is not None and refusal.startswith("❌") and expected in refusal
    # The limiter records an attempt, so it runs only once the member could
    # really submit — never for a refusal that comes before it.
    assert limiter.calls == limiter_runs


def test_role_zero_never_grants_even_to_a_role_with_id_zero():
    """Fail closed: an unconfigured role must not match anything."""
    assert submit_refusal(
        _cfg(guess_role_id=0), _member(0), rate_limited=_Limiter()
    ) is not None


def test_too_large_text_names_the_configured_cap():
    assert too_large_text(_cfg(max_image_size_mb=12)) == "❌ Image too large. Maximum is 12 MB."


@pytest.mark.parametrize(
    ("partners", "guesser", "expected"),
    [
        pytest.param([], 1, [2, 3], id="guesser_excluded"),
        pytest.param([2], 1, [3], id="partner_and_guesser_excluded"),
        pytest.param([], 99, [1, 2, 3], id="guesser_not_in_pool"),
    ],
)
def test_picker_candidates(partners, guesser, expected):
    from bot_modules.games.guess.submit_logic import picker_candidates

    members = [SimpleNamespace(id=i) for i in (1, 2, 3)]
    assert [m.id for m in picker_candidates(members, partners, guesser)] == expected
