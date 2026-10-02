"""Todo endpoints — shared server todo list, sticky board, recurring tasks.

Also the bug list's (``/bugs``): the same ``todos`` table under ``list='bug'``,
served by the same handlers with a ``list_key``, plus its filing dial. No
recurring routes exist for bugs — the bug list carries no chores.
"""

from __future__ import annotations

import time
from typing import Optional

import discord
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from bot_modules.core.db_utils import (
    get_tz_offset_hours,
    open_db_immediate,
    set_config_value,
)
from bot_modules.core.utils import jump_url
from bot_modules.moderation.settings import staff_ping_service
from bot_modules.todo.recurring_service import (
    KEEP_AUTO_COMPLETE,
    RecurringValidationError,
    create_recurring,
    delete_recurring,
    describe_cadence,
    list_recurring,
    run_now,
    set_status,
    update_recurring,
)
from bot_modules.todo.service import (
    BUG_FILING_KEY,
    FILING_MODES,
    FILING_MODS,
    LIST_BUG,
    LIST_TODO,
    TASK_MAX_LEN,
    bug_filing_mode,
    change_cards_enabled,
    change_cards_key,
    complete_todo,
    create_todo,
    get_board,
    list_todos,
    reset_change_cursors,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.helpers import resolve_names as _resolve_names
from web_server.routes.panel_posting import sticky_conflict

router = APIRouter()

# Tasks and recurring definitions are a moderator worklist end to end. Board
# *placement* is admin — it makes the bot post into an arbitrary channel, which
# is server configuration rather than worklist curation.
_MOD = Depends(require_perms({"moderator"}))
_ADMIN = Depends(require_perms({"admin"}))


class TodoCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str


class BoardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = "0"


class ChangeCardsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class RecurringBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    description: str | None = None
    recurrence: str = "daily"
    time_of_day: int = 0
    recur_days: list[int] = []
    #: Trigger the bot signs this chore off on: "qotd", "spicy_qotd", "game",
    #: or nothing.
    #: Validated in the service, not here, so the picker and any other caller
    #: get the identical sentence back.
    #:
    #: Defaults to the "leave it alone" sentinel rather than None, so a PUT
    #: that omits the field edits the chore's schedule without silently
    #: switching its automation off. On create the sentinel means no trigger.
    auto_complete: str | None = KEEP_AUTO_COMPLETE


def _board_dict(board) -> dict:
    return {
        "channel_id": str(board.channel_id),
        "message_id": str(board.message_id),
        "posted": board.posted,
        "updated_at": board.updated_at,
    }


def _recurring_dict(task) -> dict:
    return {
        "id": task.id,
        "task": task.task,
        "description": task.description,
        "recurrence": task.recurrence,
        "time_of_day": task.time_of_day,
        "recur_days": list(task.recur_days),
        "status": task.status,
        "next_run_at": task.next_run_at,
        "last_run_at": task.last_run_at,
        "last_status": task.last_status,
        "created_by": str(task.created_by),
        "created_at": task.created_at,
        "cadence": describe_cadence(task),
        "auto_complete": task.auto_complete,
    }


class FilingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = FILING_MODS


# What differs between the two lists on the web, and nothing else: the cog that
# owns the board, the board's sticky-registry key, and the noun in an error.
# Every handler below is shared, so a guard added for one list reaches the other.
_LISTS = {
    LIST_TODO: {"cog": "TodoCog", "sticky": "todo-board", "noun": "Task", "missing": "Todo"},
    LIST_BUG: {"cog": "BugCog", "sticky": "bug-board", "noun": "Bug", "missing": "Bug"},
}


@router.get("/todos")
async def list_todos_endpoint(
    request: Request,
    status: Optional[str] = None,
    user: AuthenticatedUser = _MOD,
):
    return await _list_payload(request, user, status, LIST_TODO)


@router.get("/bugs")
async def list_bugs_endpoint(
    request: Request,
    status: Optional[str] = None,
    user: AuthenticatedUser = _MOD,
):
    """The bug list, for moderators — the same shape as ``GET /todos`` plus the
    filing dial. Reading the list stays moderator-only whoever may file: a
    member who can file a bug has no dashboard view of anyone else's."""
    return await _list_payload(request, user, status, LIST_BUG)


async def _list_payload(request: Request, user, status, list_key: str) -> dict:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = list_todos(conn, guild_id, status=status, list_key=list_key)
            board = get_board(conn, guild_id, list_key=list_key)
            change_cards = change_cards_enabled(conn, guild_id, list_key=list_key)
            filing = bug_filing_mode(conn, guild_id) if list_key == LIST_BUG else None

        todos = [
            {
                "id": r["id"],
                "added_by": str(r["added_by"]),
                "added_by_name": "",
                "task": r["task"],
                "description": r["description"],
                "source_message_url": r["source_message_url"],
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
                "completed_by": str(r["completed_by"]) if r["completed_by"] else None,
                "completed_by_name": "",
                "recurring_id": r["recurring_id"],
                # A shop order: the panel files it under Shop orders, apart
                # from ordinary tasks, the way the board marks it 🎁.
                "purchase_id": r["purchase_id"],
                "missed_at": r["missed_at"],
            }
            for r in rows
        ]
        # A row the daily reset wrote off is closed, not outstanding — the
        # dashboard's pending count has to agree with the board's.
        pending = sum(
            1
            for t in todos
            if t["completed_at"] is None and t["missed_at"] is None
        )
        return {
            "pending_count": pending,
            "completed_count": sum(
                1 for t in todos if t["completed_at"] is not None
            ),
            "missed_count": sum(1 for t in todos if t["missed_at"] is not None),
            "todos": todos,
            "board": _board_dict(board),
            "change_cards": change_cards,
            **({"filing": filing} if filing is not None else {}),
        }

    result = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    await _resolve_names(
        ctx, guild, result["todos"],
        ("added_by", "added_by_name"),
        ("completed_by", "completed_by_name"),
    )
    result["can_manage_board"] = "admin" in user.perms
    if guild is not None:
        board = result["board"]
        if board["posted"]:
            board["jump_url"] = jump_url(
                guild_id, int(board["channel_id"]), int(board["message_id"])
            )
    return result


@router.post("/todos")
async def create_todo_endpoint(
    request: Request,
    body: TodoCreateBody,
    user: AuthenticatedUser = _MOD,
):
    return await _create(request, body, user, LIST_TODO)


@router.post("/bugs")
async def create_bug_endpoint(
    request: Request,
    body: TodoCreateBody,
    user: AuthenticatedUser = _MOD,
):
    """File a bug from the dashboard. Moderator-gated, so the filing dial and
    the member cooldown — both about members in Discord — never apply here."""
    return await _create(request, body, user, LIST_BUG)


async def _create(request: Request, body: TodoCreateBody, user, list_key: str) -> dict:
    noun = _LISTS[list_key]["noun"]
    task = (body.task or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail=f"{noun} cannot be empty.")
    if len(task) > TASK_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"{noun} must be {TASK_MAX_LEN} characters or fewer.",
        )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return create_todo(conn, guild_id, user.user_id, task, list_key=list_key)

    todo_id = await run_query(_q)
    await _refresh_board(ctx, guild_id, list_key=list_key)
    return {"ok": True, "id": todo_id}


@router.post("/todos/{todo_id}/complete")
async def complete_todo_endpoint(
    request: Request,
    todo_id: int,
    user: AuthenticatedUser = _MOD,
):
    return await _complete(request, todo_id, user, LIST_TODO)


@router.post("/bugs/{todo_id}/complete")
async def complete_bug_endpoint(
    request: Request,
    todo_id: int,
    user: AuthenticatedUser = _MOD,
):
    return await _complete(request, todo_id, user, LIST_BUG)


async def _complete(request: Request, todo_id: int, user, list_key: str) -> dict:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            # Scoped by list: the two lists share one id sequence, so a
            # hand-typed URL must not close a todo through the bug route.
            return complete_todo(
                conn, todo_id, guild_id, user.user_id, list_key=list_key
            )

    updated = await run_query(_q)
    if not updated:
        missing = _LISTS[list_key]["missing"]
        raise HTTPException(
            status_code=404, detail=f"{missing} not found or already completed."
        )
    await _refresh_board(ctx, guild_id, list_key=list_key)
    return {"ok": True}


# ── Board placement ─────────────────────────────────────────────────────────


def _todo_cog(ctx, list_key: str = LIST_TODO):
    bot = getattr(ctx, "bot", None)
    return bot.get_cog(_LISTS[list_key]["cog"]) if bot else None


async def _refresh_board(ctx, guild_id: int, *, list_key: str = LIST_TODO) -> None:
    """Best-effort in-place repaint of **both** boards after a dashboard mutation.

    A failure here is never fatal to the request — the 60s loop repaints
    anyway, so a missing bot or a Discord hiccup just means a board is up to a
    minute stale rather than the task silently not being saved.

    Also the path a changed *definition* takes. The board is one row per
    definition, so adding, renaming, deleting, pausing or resuming one changes
    what it shows even though no todo row moved — and the 60s loop is not a
    backstop there, since it only repaints guilds where a spawn or a write-off
    happened. Without this an added chore would stay invisible and a deleted
    one leave a ghost row until the next scheduled fire, up to a day away for a
    daily and a week for a weekly. The repaint is signature-guarded, so a call
    that changed nothing costs a DB read and no API call.
    """
    cog = _todo_cog(ctx, list_key)
    if cog is None:
        return
    try:
        await cog.refresh_board(guild_id)
    except Exception:  # noqa: BLE001 — pragma: no cover - defensive: best effort by design (see docstring); the 60s loop repaints anyway
        pass


@router.put("/todos/board")
async def set_board(
    request: Request,
    body: BoardBody,
    _: AuthenticatedUser = _ADMIN,
):
    return await _place(request, body, LIST_TODO)


@router.put("/bugs/board")
async def set_bug_board(
    request: Request,
    body: BoardBody,
    _: AuthenticatedUser = _ADMIN,
):
    return await _place(request, body, LIST_BUG)


async def _place(request: Request, body: BoardBody, list_key: str) -> dict:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    try:
        channel_id = int(body.channel_id or "0")
    except ValueError:
        raise HTTPException(status_code=400, detail="Pick a valid channel.") from None

    cog = _todo_cog(ctx, list_key)
    if cog is None:
        raise HTTPException(
            status_code=503,
            detail="The bot isn't connected right now — try again in a moment.",
        )
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(status_code=503, detail="The bot isn't in this server.")

    if not channel_id:
        await cog.unpost_board(guild)
        return {"ok": True, "posted": False}

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        # Threads and voice channels are filtered out of the picker; a duck-type
        # check here would admit types place_board is not typed to accept.
        raise HTTPException(status_code=400, detail="That channel doesn't exist here.")

    # There is only one todo board since migration 180, so the sibling-board
    # collision this used to refuse with a 409 no longer exists. Every *other*
    # sticky panel still would bury it — the casino hub and the Survivor panel
    # repaint on their own schedule — so the general guard stays. The bug board
    # is one of those others: it is in the registry, so posting either board
    # into the other's channel warns like any other collision.
    warning = await sticky_conflict(
        ctx, guild_id, channel_id, excluding=_LISTS[list_key]["sticky"]
    )

    message = await cog.place_board(guild, channel)
    if message is None:
        raise HTTPException(
            status_code=400,
            detail="I can't post in that channel — check my Send Messages and Embed Links permissions.",
        )
    return {
        "ok": True,
        "posted": True,
        "message_id": str(message.id),
        "warning": warning,
    }


@router.put("/todos/board/change-cards")
async def set_change_cards(
    request: Request,
    body: ChangeCardsBody,
    _: AuthenticatedUser = _ADMIN,
):
    return await _set_change_cards(request, body, LIST_TODO)


@router.put("/bugs/board/change-cards")
async def set_bug_change_cards(
    request: Request,
    body: ChangeCardsBody,
    _: AuthenticatedUser = _ADMIN,
):
    """The bug board's own switch — a separate key, so ticking one never starts
    cards above the other board."""
    return await _set_change_cards(request, body, LIST_BUG)


@router.put("/bugs/filing")
async def set_bug_filing(
    request: Request,
    body: FilingBody,
    _: AuthenticatedUser = _ADMIN,
):
    """Who may file a bug in Discord: moderators, or every member.

    Admin, because the open state is a write surface for every member — the
    same reasoning that makes placement admin. An unknown mode is refused
    rather than stored: the reader treats anything unrecognised as closed, so
    a stored typo would read as saved while enforcing nothing new.
    """
    mode = (body.mode or "").strip().lower()
    if mode not in FILING_MODES:
        raise HTTPException(
            status_code=400, detail="Filing must be 'mods' or 'everyone'."
        )
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _w():
        with open_db_immediate(ctx.db_path) as conn:
            set_config_value(conn, BUG_FILING_KEY, mode, guild_id)

    await run_query(_w)
    return {"ok": True, "mode": mode}


async def _set_change_cards(request: Request, body: ChangeCardsBody, list_key: str) -> dict:
    """Turn one board's change cards on or off for this guild.

    Admin, not moderator, and for the same reason placement is: it decides
    whether the bot posts messages of its own into a channel. Curating the
    worklist is mod work; making the bot talk is server configuration.

    Turning it on posts nothing immediately. The first poll after this banks the
    clock and stays silent, so the first card a channel ever sees is about
    something that arrives *after* the box was ticked, never the queue that was
    already sitting there (see ``plan_arrival_card``).

    Turning it *on* clears both cursors, so every switch-on re-arms that guard —
    not just the first one ever. A guild that ran the feature in March and
    switches it back on in September would otherwise have its first card be
    everything that arrived across those six months. The quiet period is simply
    not announced, which is the honest outcome for a window the feature was off
    for.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _w():
        with open_db_immediate(ctx.db_path) as conn:
            set_config_value(
                conn,
                change_cards_key(list_key),
                "1" if body.enabled else "0",
                guild_id,
            )
            if body.enabled:
                # Re-arm the dark-launch guard. A guild that ran the feature
                # before has a cursor from then; without clearing it, switching
                # back on would announce everything that arrived while it was
                # off as though it had just landed.
                reset_change_cursors(conn, guild_id, list_key=list_key)

    await run_query(_w)
    return {"ok": True, "enabled": body.enabled}


# ── Recurring tasks ─────────────────────────────────────────────────────────


@router.get("/todos/recurring")
async def list_recurring_endpoint(request: Request, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            items = [_recurring_dict(t) for t in list_recurring(conn, guild_id)]
        return {"items": items, "tz_offset_hours": tz}

    return await run_query(_q)


@router.post("/todos/recurring")
async def create_recurring_endpoint(
    request: Request,
    body: RecurringBody,
    user: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            return create_recurring(
                conn,
                guild_id,
                task=body.task,
                description=body.description,
                recurrence=body.recurrence,
                time_of_day=body.time_of_day,
                recur_days=body.recur_days,
                auto_complete=(
                    None if body.auto_complete == KEEP_AUTO_COMPLETE
                    else body.auto_complete
                ),
                created_by=user.user_id,
                offset_hours=tz,
                now_ts=time.time(),
            )

    try:
        new_id = await run_query(_q)
    except RecurringValidationError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    await _refresh_board(ctx, guild_id)
    # An active recurring chore is what puts Staff · Chores on the Mod
    # Settings panel; so do the delete and pause/resume below.
    staff_ping_service.announce_change(ctx.bot, guild_id)
    return {"ok": True, "id": new_id}


@router.put("/todos/recurring/{recurring_id}")
async def update_recurring_endpoint(
    request: Request,
    recurring_id: int,
    body: RecurringBody,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            return update_recurring(
                conn,
                recurring_id,
                guild_id,
                task=body.task,
                description=body.description,
                recurrence=body.recurrence,
                time_of_day=body.time_of_day,
                recur_days=body.recur_days,
                auto_complete=body.auto_complete,
                offset_hours=tz,
                now_ts=time.time(),
            )

    try:
        ok = await run_query(_q)
    except RecurringValidationError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    if not ok:
        raise HTTPException(status_code=404, detail="That recurring task no longer exists.")
    await _refresh_board(ctx, guild_id)
    return {"ok": True}


@router.delete("/todos/recurring/{recurring_id}")
async def delete_recurring_endpoint(
    request: Request,
    recurring_id: int,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return delete_recurring(conn, recurring_id, guild_id)

    if not await run_query(_q):
        raise HTTPException(status_code=404, detail="That recurring task no longer exists.")
    await _refresh_board(ctx, guild_id)
    staff_ping_service.announce_change(ctx.bot, guild_id)
    return {"ok": True}


_NOT_FOUND = "That recurring task no longer exists."


async def _set_recurring_status(request: Request, recurring_id: int, status: str):
    """Shared body for pause/resume — the only thing that differs is the status."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return set_status(
                conn,
                recurring_id,
                guild_id,
                status,
                offset_hours=get_tz_offset_hours(conn, guild_id),
                now_ts=time.time(),
            )

    if not await run_query(_q):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    await _refresh_board(ctx, guild_id)
    staff_ping_service.announce_change(ctx.bot, guild_id)
    return {"ok": True}


@router.post("/todos/recurring/{recurring_id}/pause")
async def pause_recurring(
    request: Request, recurring_id: int, _: AuthenticatedUser = _MOD
):
    return await _set_recurring_status(request, recurring_id, "paused")


@router.post("/todos/recurring/{recurring_id}/resume")
async def resume_recurring(
    request: Request, recurring_id: int, _: AuthenticatedUser = _MOD
):
    return await _set_recurring_status(request, recurring_id, "active")


@router.post("/todos/recurring/{recurring_id}/run-now")
async def run_recurring_now(
    request: Request, recurring_id: int, _: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        # Read-then-write (is the last instance still open?), so take the write
        # lock up front — otherwise this and the background spawner could act
        # on the same snapshot and insert two copies.
        with open_db_immediate(ctx.db_path) as conn:
            return run_now(
                conn,
                recurring_id,
                guild_id,
                now_ts=time.time(),
                offset_hours=get_tz_offset_hours(conn, guild_id),
            )

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    await _refresh_board(ctx, guild_id)
    if result.status != "spawned":
        return {
            "ok": True,
            "spawned": False,
            "detail": "That task is already on the list — nothing new added.",
        }
    return {"ok": True, "spawned": True}
