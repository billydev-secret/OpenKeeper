"""Member Data — the three GDPR paths and the Discord-message deletion.

Every operation here was previously reachable only from ``scripts/`` (export,
purge, anonymise) or from a slash command that no longer exists
(``/delete_user``). They are gathered behind one admin-gated page because they
answer the same question — "what does this server hold about this member, and
what do we do about it" — and because running an erasure from a shell on the
production box, by hand, is the worst place for it.

Three things about this module are load-bearing:

* **Receipts never name the subject.** ``audit_log`` is not in
  ``ANONYMIZE_KEEP_TABLES``, so the anonymise sweep rewrites its ids like any
  other table: a receipt written *after* a run puts the real id back into the
  database the run just cleaned, and one written *before* is rewritten to the
  surrogate and records nothing. Purge has the mirror problem — it does not
  clear plain ``audit_log``, so a receipt naming the erased member outlives the
  erasure. So every receipt here carries ``target_id=None`` and counts only.
  Actor and timestamp are real, which is what accountability actually needs.

* **Anonymise applies from the primary guild only.** It deliberately runs
  across *every* guild at once (a surrogate has to be consistent or the
  aggregates it protects stop adding up), and the bot serves a guild whose
  economy someone else runs. Export and purge are guild-scoped and work from
  anywhere.

* **Deleting Discord messages writes nothing server-side.** It is the one
  operation on this page that does not touch the database at all — see
  ``privacy.deletion_service``. It therefore gets no receipt either.

The fifth tab, *When members leave*, is not about the chosen member: it holds
the per-guild dial for the departure auto-anonymise and that guild's queue
(``privacy/departure_erasure_service.py``). The auto path is allowed from any
guild because it only goes bot-wide for a member leaving the primary guild —
see ``choose_scope``.

Snowflakes go out as strings: a member id past 2^53 loses precision as a bare
JSON number (the dashboard's snowflake sweep fails on it).
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from bot_modules.core.db_utils import open_db_immediate, set_config_value
from bot_modules.privacy.logic import MODE_ALL, MODE_MEDIA, MODE_TEXT
from bot_modules.privacy import departure_erasure_service as departure
from bot_modules.games.guess import cleanup_service as guess_cleanup_service
from bot_modules.privacy import service as privacy_service
from bot_modules.moderation.service import write_audit
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

_ADMIN = Depends(require_perms({"admin"}))

_MODES = (MODE_ALL, MODE_MEDIA, MODE_TEXT)


class SubjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Snowflake as a string — a bare number above 2^53 is rounded by JS.
    user_id: str = "0"


class PurgeBody(SubjectBody):
    model_config = ConfigDict(extra="forbid")

    keep_messages: bool = False


class AnonymizeBody(SubjectBody):
    model_config = ConfigDict(extra="forbid")

    #: Omitted on the first call; the plan names the one that will be used.
    surrogate_id: int = 0


class SweepBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surrogate_id: int = 0


class DeleteMessagesBody(SubjectBody):
    model_config = ConfigDict(extra="forbid")

    mode: str = MODE_ALL


class DepartureDialBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


def _subject_id(raw: str) -> int:
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "That is not a member id.") from None
    if user_id <= 0:
        raise HTTPException(422, "That is not a member id.")
    return user_id


def _require_primary_guild(ctx, guild_id: int) -> None:
    """Refuse an anonymisation from anywhere but the primary guild.

    ``require_perms({"admin"})`` only proves the caller administers the guild
    they currently have selected, and an anonymisation rewrites the member's id
    in *every* guild the bot serves. The panel hides the button off-primary,
    but that flag is client-side.
    """
    if int(guild_id) != int(ctx.guild_id):
        raise HTTPException(
            403,
            "Anonymising runs across every server the bot is in — "
            "start it from the primary server.",
        )


def _report_json(report: privacy_service.AnonymizeReport) -> dict:
    return {
        "user_id": str(report.user_id),
        "surrogate_id": report.surrogate_id,
        "username": report.username,
        "display_name": report.display_name,
        "id_rewrites": report.id_rewrites,
        "text_rewrites": report.text_rewrites,
        "text_cleared": report.text_cleared,
        "skipped": report.skipped,
        "kept": report.kept,
        "total_rows": report.total_rows,
    }


# ── Export (Art 15 / 20) ──────────────────────────────────────────────────


@router.get("/privacy/export")
async def export_summary(
    request: Request,
    user_id: str = "0",
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """Row counts for an access request — never the rows themselves.

    The payload behind these counts is the whole of a member's data; a browser
    is the wrong place for it and the page has no use for the content. Press
    Download for the file.
    """
    ctx = get_ctx(request)
    subject = _subject_id(user_id)

    def _q():
        with ctx.open_db() as conn:
            return privacy_service.export_user_data(conn, guild_id, subject)

    data = await run_query(_q)
    return {
        "subject": {
            "guild_id": str(guild_id),
            "user_id": str(subject),
        },
        "counts": data["counts"],
        "total_rows": sum(data["counts"].values()),
        "review_required": data["review_required"],
        "notes": data["notes"],
    }


@router.get("/privacy/export/download")
async def export_download(
    request: Request,
    user_id: str = "0",
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """The full export as a JSON attachment.

    Read-only, so it writes no receipt. The tables in ``review_required`` name
    a second member: Art 15(4) makes that a redaction decision a person has to
    take **before the file leaves the building**, and this endpoint does not
    take it for them.
    """
    ctx = get_ctx(request)
    subject = _subject_id(user_id)

    def _q():
        with ctx.open_db() as conn:
            return privacy_service.export_user_data(conn, guild_id, subject)

    data = await run_query(_q)
    body = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="export-{subject}-{guild_id}.json"'
            )
        },
    )


# ── Erase (Art 17) ────────────────────────────────────────────────────────


@router.post("/privacy/purge")
async def purge(
    request: Request,
    body: PurgeBody,
    guild_id: int = Depends(get_active_guild_id),
    user: AuthenticatedUser = _ADMIN,
):
    """Delete this guild's rows for the member. Irreversible.

    The preview the panel showed is the export's counts, which are a deliberate
    *superset*: the export discloses rows the purge keeps under Art 17(3)
    (the double-entry ledger, sanction history, consent audit, no-contact
    orders). ``rows`` in the response is every row actually deleted or scrubbed,
    across all tables; ``message_rows`` is the message-log share of that.
    """
    ctx = get_ctx(request)
    subject = _subject_id(body.user_id)

    # The member's Guess Who rounds come out of Discord and off the disk FIRST
    # — each crop post, then each cached original in its own transaction —
    # before the rows that record them are deleted. Neither can be rolled back,
    # so neither can live inside the purge's transaction; and once
    # `purge_user_data` has deleted the rounds, nothing knows which posts and
    # files were theirs. A post or file that refuses raises here and the
    # erasure does not run, which is the right order: better to retry than to
    # report an erasure with the photo still up. The bot is only needed when
    # there is a post to take down, so an erasure with no Guess rounds still
    # runs while it is disconnected.
    async def _delete_post(post_guild_id: int, channel_id: int, message_id: int) -> None:
        await _privacy_cog(ctx).delete_guess_post(post_guild_id, channel_id, message_id)

    await guess_cleanup_service.clear_member_media(
        ctx.db_path, subject, guild_id=guild_id, delete_post=_delete_post
    )

    def _q():
        with ctx.open_db() as conn:
            result = privacy_service.purge_user_data_counted(
                conn, guild_id, subject, keep_messages=body.keep_messages
            )
            # No target_id and no name: see the module docstring. A receipt for
            # an erasure must not be the one row that survives it.
            write_audit(
                conn,
                guild_id=guild_id,
                action="privacy_purge",
                actor_id=user.user_id,
                target_id=None,
                extra={
                    "rows": result.total_rows,
                    "message_rows": result.message_rows,
                    "keep_messages": body.keep_messages,
                },
            )
            return result

    result = await run_query(_q)
    return {"ok": True, "rows": result.total_rows, "message_rows": result.message_rows}


# ── Anonymise ─────────────────────────────────────────────────────────────


@router.get("/privacy/anonymize/plan")
async def anonymize_plan(
    request: Request,
    user_id: str = "0",
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """What an anonymisation would rewrite. Writes nothing."""
    ctx = get_ctx(request)
    subject = _subject_id(user_id)
    _require_primary_guild(ctx, guild_id)

    def _q():
        with ctx.open_db() as conn:
            return privacy_service.plan_anonymization(conn, subject)

    try:
        report = await run_query(_q)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return _report_json(report)


@router.post("/privacy/anonymize")
async def anonymize(
    request: Request,
    body: AnonymizeBody,
    guild_id: int = Depends(get_active_guild_id),
    user: AuthenticatedUser = _ADMIN,
):
    """Replace the member's id with a surrogate everywhere. Irreversible.

    Cross-guild by design, which is why it is primary-guild only.
    """
    ctx = get_ctx(request)
    subject = _subject_id(body.user_id)
    _require_primary_guild(ctx, guild_id)
    # No surrogate check here: ``_validate_surrogate`` already refuses a missing
    # one, a taken one and one out of range, with a message written for an
    # operator. A second check in front of it would only be able to disagree.

    def _q():
        # ``open_db_immediate`` takes the write lock before the read: two
        # admins anonymising different members at once could otherwise both
        # read the surrogate as free (a plain ``open_db`` starts a DEFERRED
        # transaction, so ``_validate_surrogate``'s "taken" check runs before
        # either writer is serialised) and both proceed to write it, silently
        # merging two members' rows under one surrogate — exactly the
        # corruption ``_validate_surrogate`` exists to prevent. BEGIN
        # IMMEDIATE makes the second caller block until the first commits and
        # then re-read the now-current state, so it gets a clean refusal
        # instead of a race.
        with open_db_immediate(ctx.db_path) as conn:
            report = privacy_service.anonymize_user_data(
                conn, subject, surrogate_id=body.surrogate_id
            )
            # The surrogate is safe to record; the real id is the whole point
            # of the run and must not survive in its own receipt.
            write_audit(
                conn,
                guild_id=guild_id,
                action="privacy_anonymize",
                actor_id=user.user_id,
                target_id=None,
                extra={
                    "surrogate": report.surrogate_id,
                    "rows": report.total_rows,
                    "tables": len(report.id_rewrites),
                },
            )
            return report

    try:
        report = await run_query(_q)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return _report_json(report)


@router.post("/privacy/anonymize/sweep")
async def anonymize_sweep(
    request: Request,
    body: SweepBody,
    dry_run: bool = True,
    guild_id: int = Depends(get_active_guild_id),
    user: AuthenticatedUser = _ADMIN,
):
    """Clear text an earlier anonymisation's clear list did not name.

    The id sweep is complete by construction — the schema names every column.
    Which text is the member's *own words* is a per-table judgement, and the
    first production run got three of them wrong, so the report said
    "anonymised" with her words sitting beside the surrogate. This re-runs the
    text half against a surrogate that has already been rewritten.
    """
    ctx = get_ctx(request)
    _require_primary_guild(ctx, guild_id)

    def _q():
        with ctx.open_db() as conn:
            report = privacy_service.anonymize_missed_text(
                conn, body.surrogate_id, dry_run=dry_run
            )
            if not dry_run:
                write_audit(
                    conn,
                    guild_id=guild_id,
                    action="privacy_anonymize_sweep",
                    actor_id=user.user_id,
                    target_id=None,
                    extra={
                        "surrogate": report.surrogate_id,
                        "rows": report.total_rows,
                    },
                )
            return report

    try:
        report = await run_query(_q)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {**_report_json(report), "dry_run": dry_run}


# ── Discord messages ──────────────────────────────────────────────────────


def _privacy_cog(ctx):
    bot = getattr(ctx, "bot", None)
    get_cog = getattr(bot, "get_cog", None)
    cog = get_cog("PrivacyCog") if get_cog else None
    if cog is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Bot is not connected — try again once it's back up.",
        )
    return cog


@router.post("/privacy/messages/delete")
async def delete_messages(
    request: Request,
    body: DeleteMessagesBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """Start clearing the member's Discord messages, and return immediately.

    A full-server scan runs for minutes, so the run is a task on the bot and
    the panel polls ``/privacy/messages/status``. Nothing server-side is
    touched and no receipt is written — see the module docstring.
    """
    ctx = get_ctx(request)
    subject = _subject_id(body.user_id)
    if body.mode not in _MODES:
        raise HTTPException(422, "Unknown deletion mode.")

    outcome = await _privacy_cog(ctx).start_message_deletion(
        guild_id, subject, mode=body.mode
    )
    if not outcome.get("started"):
        reason = outcome.get("reason")
        if reason == "already_running":
            raise HTTPException(
                409, "A deletion is already running for that member."
            )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Bot can't see that server right now — try again once it's back up.",
        )
    return {"ok": True}


@router.get("/privacy/messages/status")
async def delete_messages_status(
    request: Request,
    user_id: str = "0",
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """Progress for a run started above, or ``{"phase": "idle"}``."""
    ctx = get_ctx(request)
    subject = _subject_id(user_id)
    status_ = _privacy_cog(ctx).deletion_status(guild_id, subject)
    return status_ or {"phase": "idle"}


# ── When members leave ────────────────────────────────────────────────────


@router.get("/privacy/departures")
async def departures(
    request: Request,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """This guild's auto-anonymise dial and its queue.

    The queue holds real member ids — it is the list of who is about to be
    anonymised — so it is admin-only like the rest of the page, and ids go out
    as strings.
    """
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            return (
                departure.is_enabled(conn, guild_id),
                departure.list_jobs(conn, guild_id),
            )

    enabled, jobs = await run_query(_q)
    return {
        "enabled": enabled,
        "primary": int(guild_id) == int(ctx.guild_id),
        "settle_seconds": departure.SETTLE_SECONDS,
        "max_attempts": departure.MAX_ATTEMPTS,
        "jobs": jobs,
    }


@router.put("/privacy/departures")
async def set_departures(
    request: Request,
    body: DepartureDialBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """Turn the auto-anonymise on or off for this guild.

    Off also empties the queue: a member who left while it was on but had not
    been reached yet must not be anonymised by a later switch-on.
    """
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            set_config_value(
                conn, departure.DIAL_KEY, "1" if body.enabled else "0", guild_id
            )
            return 0 if body.enabled else departure.clear_queue(conn, guild_id)

    dropped = await run_query(_q)
    return {"ok": True, "enabled": body.enabled, "dropped": dropped}


@router.post("/privacy/departures/retry")
async def retry_departure(
    request: Request,
    body: SubjectBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """Put a job that ran out of attempts back in line."""
    ctx = get_ctx(request)
    subject = _subject_id(body.user_id)

    def _q():
        with ctx.open_db() as conn:
            return departure.retry_job(conn, guild_id, subject, now=time.time())

    if not await run_query(_q):
        raise HTTPException(404, "There is no failed job for that member here.")
    return {"ok": True}


@router.post("/privacy/departures/drop")
async def drop_departure(
    request: Request,
    body: SubjectBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = _ADMIN,
):
    """Take a member off the queue with nothing done. Leaving again re-queues."""
    ctx = get_ctx(request)
    subject = _subject_id(body.user_id)

    def _q():
        with ctx.open_db() as conn:
            return departure.cancel(conn, guild_id, subject)

    if not await run_query(_q):
        raise HTTPException(404, "That member is not waiting to be anonymised here.")
    return {"ok": True}
