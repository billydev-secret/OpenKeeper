"""The headless deletion engine, and the dashboard bridge that drives it.

``/delete_me`` and Moderation → Member Data → Delete Messages run the *same*
scan-and-delete; only the reporting differs. These tests guard the three things
that split can quietly break:

* the engine still refuses to touch the database (the guarantee that let the
  card skip a branding lookup in the first place);
* the two surfaces cannot run at once against the same member — the per-target
  lock used to be enforced by two commands sharing one set, and now has to hold
  across a command and a web route;
* a failed run ends in a terminal state the panel can render, rather than
  polling a counter that stopped moving.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bot_modules.privacy import cog as privacy_cog
from bot_modules.privacy import deletion_service
from bot_modules.privacy.deletion_service import DeletionResult, run_deletion
from bot_modules.privacy.logic import MODE_ALL, MODE_MEDIA


@pytest.fixture
def _engine(monkeypatch):
    """Stub the two Discord seams; record what the engine asked them for."""
    calls: dict[str, list] = {"scanned_with": [], "deleted": []}

    async def _find(guild, user_id, *, on_progress=None, predicate=None):
        calls["scanned_with"].append(predicate)
        if on_progress:
            await on_progress(2, 2, 3)
        return [(11, 22), (12, 22), (13, 23)]

    async def _delete(guild, user_id, msg_rows, on_progress=None):
        calls["deleted"].append(list(msg_rows))
        if on_progress:
            await on_progress(3, 0, 0)
        return (3, 0, 0)

    monkeypatch.setattr(deletion_service, "find_user_messages", _find)
    monkeypatch.setattr(deletion_service, "delete_discord_messages", _delete)
    return calls


# ── the engine ──────────────────────────────────────────────────────────


async def test_a_run_reports_what_it_scanned_and_deleted(_engine) -> None:
    result = await run_deletion(MagicMock(), 42)
    assert result == DeletionResult(
        mode=MODE_ALL, found=3, deleted=3, failed=0, replaced=0
    )
    assert not result.empty


async def test_a_mode_other_than_all_filters_during_the_scan(_engine) -> None:
    """The returned ids carry no attachment detail, so a content-selecting mode
    has to decide while the Message is still in hand — the predicate is the only
    place that can happen."""
    await run_deletion(MagicMock(), 42, mode=MODE_MEDIA)
    assert _engine["scanned_with"][-1] is not None

    await run_deletion(MagicMock(), 42, mode=MODE_ALL)
    assert _engine["scanned_with"][-1] is None


async def test_an_empty_scan_deletes_nothing_at_all(monkeypatch) -> None:
    """`empty` is what the callers branch on. If the engine fell through to the
    deleter with an empty list it would still report zeros, so the counters
    alone cannot tell the two apart."""
    deleted: list = []

    async def _find(guild, user_id, *, on_progress=None, predicate=None):
        return []

    async def _delete(guild, user_id, msg_rows, on_progress=None):
        deleted.append(msg_rows)
        return (0, 0, 0)

    monkeypatch.setattr(deletion_service, "find_user_messages", _find)
    monkeypatch.setattr(deletion_service, "delete_discord_messages", _delete)

    result = await run_deletion(MagicMock(), 42)
    assert result.empty
    assert deleted == [], "an empty scan must not reach the deleter"


def test_the_engine_never_opens_the_database() -> None:
    """The guarantee that survives the move out of the cog.

    Read statically, not by patching a DB seam and watching it stay untouched:
    the engine does not import one, so such a probe compares nothing to nothing
    and passes whatever the code does. Naming the module's own text is the only
    check that fails when somebody adds a read.
    """
    src = Path(deletion_service.__file__).read_text(encoding="utf-8")
    for forbidden in ("open_db", "db_path", "sqlite3", "conn"):
        assert forbidden not in src, (
            f"deletion_service touches the database ({forbidden!r}) — a deletion "
            "run must not, and the Discord card relies on it"
        )


async def test_progress_reaches_the_sink_for_both_phases(_engine) -> None:
    seen: list[tuple[str, int, int]] = []

    class _Sink:
        async def scanning(self, done, total, found):
            seen.append(("scanning", done, total))

        async def deleting(self, done, total):
            seen.append(("deleting", done, total))

    await run_deletion(MagicMock(), 42, progress=_Sink())
    assert [phase for phase, _, _ in seen] == ["scanning", "deleting"]
    assert seen[-1] == ("deleting", 3, 3)


# ── the dashboard bridge ────────────────────────────────────────────────


def _cog(guild=None):
    bot = MagicMock()
    bot.get_guild.return_value = guild if guild is not None else MagicMock()
    return privacy_cog.PrivacyCog(bot)


async def test_the_dashboard_run_reports_a_finished_summary(_engine) -> None:
    cog = _cog()
    assert (await cog.start_message_deletion(1, 42))["started"] is True
    await cog._web_runs[(1, 42)].task

    status = cog.deletion_status(1, 42)
    assert status["phase"] == "done"
    assert status["deleted"] == 3
    # Same renderer as the Discord card, so both surfaces say the same thing.
    assert "3" in status["summary"]


async def test_a_member_and_an_admin_cannot_delete_at_once(_engine) -> None:
    """The lock used to be two commands sharing ``_active_deletions``. It now has
    to hold between a slash command and a web route, which is the case a split
    engine makes easy to lose."""
    cog = _cog()
    cog._active_deletions.add(42)  # as /delete_me would while it runs

    outcome = await cog.start_message_deletion(1, 42)
    assert outcome == {"started": False, "reason": "already_running"}
    assert cog.deletion_status(1, 42) is None


async def test_the_lock_is_released_when_the_run_finishes(_engine) -> None:
    cog = _cog()
    await cog.start_message_deletion(1, 42)
    await cog._web_runs[(1, 42)].task
    assert 42 not in cog._active_deletions


async def test_a_run_that_raises_ends_in_a_terminal_phase(monkeypatch) -> None:
    """A panel polling a counter that stopped moving cannot tell a hung scan
    from a crashed one, so a failure has to become a phase."""

    async def _boom(*a, **k):
        raise RuntimeError("Discord said no")

    monkeypatch.setattr(deletion_service, "find_user_messages", _boom)

    cog = _cog()
    await cog.start_message_deletion(1, 42)
    await cog._web_runs[(1, 42)].task

    assert cog.deletion_status(1, 42)["phase"] == "failed"
    assert 42 not in cog._active_deletions, "a failed run must release the lock"


async def test_a_deletion_is_refused_when_the_bot_cannot_see_the_guild() -> None:
    bot = MagicMock()
    bot.get_guild.return_value = None
    cog = privacy_cog.PrivacyCog(bot)

    outcome = await cog.start_message_deletion(1, 42)
    assert outcome == {"started": False, "reason": "guild_unavailable"}


def test_status_is_none_for_a_member_with_no_run() -> None:
    assert _cog().deletion_status(1, 999) is None


# ── the finished-run table is bounded ────────────────────────────────────────
#
# ``_web_runs`` is deliberately in memory rather than a table (see the bridge
# comment on PrivacyCog), which makes it append-only unless something evicts.
# One entry per member ever cleared from the dashboard is small, but it never
# stops growing in a process that stays up for months.


def test_a_live_run_is_never_evicted_however_old() -> None:
    """The cap must not be able to pull the entry out from under a panel that
    is still polling it — a full-server scan can outlast any age cut."""
    cog = _cog()
    cog._web_runs[(1, 7)] = privacy_cog._WebRun(mode=MODE_ALL)  # finished_at None
    for i in range(privacy_cog._WEB_RUN_KEEP + 10):
        done = privacy_cog._WebRun(mode=MODE_ALL)
        done.finished_at = 0.0  # ancient
        cog._web_runs[(1, 1000 + i)] = done

    cog._prune_web_runs()

    assert (1, 7) in cog._web_runs, "a run with no finished_at is still live"


def test_a_finished_run_ages_out() -> None:
    cog = _cog()
    stale = privacy_cog._WebRun(mode=MODE_ALL)
    stale.finished_at = time.monotonic() - privacy_cog._WEB_RUN_TTL_SECONDS - 1
    fresh = privacy_cog._WebRun(mode=MODE_ALL)
    fresh.finished_at = time.monotonic()
    cog._web_runs[(1, 1)] = stale
    cog._web_runs[(1, 2)] = fresh

    cog._prune_web_runs()

    assert (1, 1) not in cog._web_runs
    assert (1, 2) in cog._web_runs, "a just-finished run is still readable"


def test_a_burst_inside_the_ttl_is_capped_oldest_first() -> None:
    """The age cut alone can't bound a burst; the count cap is the backstop."""
    cog = _cog()
    now = time.monotonic()
    for i in range(privacy_cog._WEB_RUN_KEEP + 5):
        state = privacy_cog._WebRun(mode=MODE_ALL)
        state.finished_at = now - (privacy_cog._WEB_RUN_KEEP + 5 - i)  # all fresh
        cog._web_runs[(1, i)] = state

    cog._prune_web_runs()

    assert len(cog._web_runs) == privacy_cog._WEB_RUN_KEEP
    assert (1, 0) not in cog._web_runs, "the oldest finished run goes first"
    assert (1, privacy_cog._WEB_RUN_KEEP + 4) in cog._web_runs, "the newest stays"


async def test_a_completed_run_is_marked_finished() -> None:
    """Nothing can be evicted if the run never records that it stopped."""
    cog = _cog()
    await cog.start_message_deletion(1, 42)
    await cog._web_runs[(1, 42)].task

    assert cog._web_runs[(1, 42)].finished_at is not None


def test_the_privacy_cog_offers_no_commands() -> None:
    """``/delete_user`` is deleted on purpose, not lost — and so, since
    2026-09-17, are ``/delete_me`` and ``/whisper forget-me``, and since
    2026-09-26 ``/mydata`` itself.

    An admin erasing someone else's data does it from the dashboard's Member
    Data panel, which is admin-only — so restoring ``/delete_user`` would both
    duplicate a surface CLAUDE.md says belongs on the web and silently hand
    message deletion back to every moderator. A stranger tidying up and
    "restoring" the missing half of the pair is exactly what this catches.

    The My Data panel is the member's self-service surface and the *only*
    one: the message modes it absorbed from ``/delete_me`` and the whisper
    erasure it absorbed from ``/whisper forget-me`` are buttons on it (bug #222
    item 3, ``tests/privacy/test_privacy_member_data_panel.py``). It opens from the
    Server Features hub and ``/info`` through ``PrivacyCog.open_panel`` —
    ``/mydata`` was folded into the hub (bug-list row 259).
    """
    names = {c.name for c in privacy_cog.PrivacyCog.__cog_app_commands__}
    assert names == set(), f"unexpected privacy commands: {sorted(names)}"
    assert callable(privacy_cog.PrivacyCog.open_panel)
