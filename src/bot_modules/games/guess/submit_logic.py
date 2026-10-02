"""The checks a Guess Who submission passes before the crop editor opens.

There are two ways in — ``/guess submit`` and the channel prompt's Submit
form — and until 2026-09-30 each carried its own copy of this chain. The
copies had already drifted once (the form skipped the rate limit, and its
"not configured" refusal read differently), so both doors now ask
:func:`submit_refusal` and the order and wording live here.

Pure apart from ``rate_limited``, which the caller passes as a callable: the
limiter records the attempt as it checks, so it must only run once every
check before it has passed — exactly where it always ran.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from bot_modules.games.guess.models import GuessConfig
from bot_modules.no_contact.logic import candidate_members_for


def _has_guess_role(member: Any, guess_role_id: int) -> bool:
    """Fail closed: an unconfigured role (id 0) must never grant access."""
    if guess_role_id == 0:
        return False
    return any(r.id == guess_role_id for r in member.roles)


def _validate_mime(content_type: str | None) -> bool:
    return content_type is not None and content_type.startswith("image/")


def _validate_size(byte_count: int, max_mb: int) -> bool:
    return byte_count <= max_mb * 1024 * 1024


def too_large_text(config: GuessConfig) -> str:
    return f"❌ Image too large. Maximum is {config.max_image_size_mb} MB."


def submit_refusal(
    config: GuessConfig,
    member: Any | None,
    *,
    rate_limited: Callable[[], bool],
    content_type: str | None = None,
    size: int | None = None,
) -> str | None:
    """The refusal for this submission, or ``None`` when it may go ahead.

    ``member`` is the submitter as a guild member (``None`` when they can't
    be resolved). ``content_type``/``size`` describe an uploaded file; leave
    them unset for a link, whose type is checked by the image decoder and
    whose size is only known once it has been downloaded
    (:func:`too_large_text` is that later refusal).
    """
    if config.guess_role_id == 0:
        return (
            "❌ Guess role is not configured. Ask an admin to set it in the web dashboard."
        )
    if config.guess_channel_id == 0:
        return (
            "❌ Guess channel is not configured. Ask an admin to set it in the web dashboard."
        )
    if member is None or not _has_guess_role(member, config.guess_role_id):
        return "❌ You need the Guess role to submit."
    if rate_limited():
        return (
            f"❌ You've hit the submission limit ({config.submit_max_per_window} per "
            f"{config.submit_window_seconds}s). Please wait a bit before submitting again."
        )
    if size is not None or content_type is not None:
        if not _validate_mime(content_type):
            return "❌ Please submit an image file."
        if size is not None and not _validate_size(size, config.max_image_size_mb):
            return too_large_text(config)
    return None


def picker_candidates(
    members: Iterable[Any], partners: Iterable[int], guesser_id: int
) -> list[Any]:
    """Who the guess picker offers: the role holders minus the guesser (a
    self-pick would burn one of their guesses and start the cooldown) and
    minus their no-contact partners."""
    return candidate_members_for(members, {*partners, guesser_id})
