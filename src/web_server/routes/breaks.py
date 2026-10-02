"""Take a Break API — the page's settings, its setup button and the "On a
break" list.

Every knob is here and nowhere in Discord (CLAUDE.md): the break channel is
made by **Set up break channel**, the Spicy Lite role is picked here, and a
mod ends someone's break from the list. The "Take a Break" panel itself is
posted through the shared ``/api/panels/break-panel/post`` route.

Snowflakes go out as strings (the dashboard's snowflake-precision sweep).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from bot_modules.breaks import service
from bot_modules.breaks.store import SPICY_LITE_ROLE_KEY, list_breaks, retire_hold_role
from bot_modules.core.db_utils import set_config_value
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.helpers import refuse_ungrantable_role
from web_server.routes.panel_posting import (
    ChannelIdBody,
    guild_or_503,
    text_channel_or_400,
)

router = APIRouter()


class BreakConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: "0" = no swap: Spicy comes off with everything else.
    spicy_lite_role_id: str = "0"


@router.get("/breaks")
async def get_breaks(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            cfg = service.read_break_config(ctx, conn, guild_id)
            rows = list_breaks(conn, guild_id)
        return {
            "config": {
                "break_role_id": str(cfg.break_role_id),
                "break_channel_id": str(cfg.break_channel_id),
                "spicy_role_id": str(cfg.spicy_role_id),
                "spicy_lite_role_id": str(cfg.spicy_lite_role_id),
                "enabled": cfg.enabled,
            },
            "breaks": [
                {
                    "user_id": str(r.user_id),
                    "started_at": r.started_at,
                    "roles": len(r.stored_roles),
                    "swapped_spicy": r.swapped_spicy,
                }
                for r in rows
            ],
        }

    return await run_query(_q)


@router.put("/breaks/config")
async def put_break_config(
    request: Request,
    body: BreakConfigBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    try:
        lite = int(body.spicy_lite_role_id or 0)
    except ValueError:
        raise HTTPException(400, "Spicy Lite role must be a role id.")
    if lite < 0:
        raise HTTPException(400, "Spicy Lite role must be a role id.")
    # Members on a break are handed this role, so it has to be one the bot may
    # grant — refused here, while the admin is still looking at the form.
    refuse_ungrantable_role(ctx, guild_id, lite, setting="Spicy Lite role")

    def _q():
        with ctx.open_db() as conn:
            cfg = service.read_break_config(ctx, conn, guild_id)
            if lite and lite in (cfg.break_role_id, cfg.spicy_role_id):
                raise HTTPException(
                    400, "Spicy Lite has to be its own role, not the break or Spicy role."
                )
            # Members already wearing the old one keep counting as on a break
            # (Grant Audit, jail snapshots, a stray I'm back) — breaks.store.
            retire_hold_role(conn, guild_id, cfg.spicy_lite_role_id, lite)
            set_config_value(conn, SPICY_LITE_ROLE_KEY, str(lite), guild_id)
        return {"ok": True}

    return await run_query(_q)


@router.post("/breaks/setup")
async def setup_break_channel_route(
    request: Request,
    body: ChannelIdBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Make the break role, hide the server from it, post "I'm back" here.

    Writing the channel is what switches breaks on, so this is the feature's
    on switch as well as its setup.
    """
    ctx = get_ctx(request)
    guild = guild_or_503(ctx, get_active_guild_id(request))
    channel = text_channel_or_400(guild, body.channel_id)
    cog = ctx.bot.get_cog("BreaksCog")
    if cog is None:
        raise HTTPException(503, "The Take a Break module isn't loaded.")
    ok, note = await service.setup_break_channel(
        ctx, guild, channel, return_panel=await cog.return_panel_view(guild)
    )
    if not ok:
        raise HTTPException(400, note)
    return {"ok": True, "note": note}


@router.post("/breaks/{user_id}/end")
async def end_break_route(
    request: Request,
    user_id: int,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """A mod ends someone's break: roles back if they're here, row gone either way."""
    ctx = get_ctx(request)
    guild = guild_or_503(ctx, get_active_guild_id(request))
    member = guild.get_member(user_id)
    if member is None:
        if not await service.end_break_absent(ctx, guild.id, user_id, actor_id=user.user_id):
            raise HTTPException(404, "That member isn't on a break.")
        return {"ok": True, "note": "They've left the server, so there were no roles to give back."}
    result = await service.end_break(ctx, member, actor_id=user.user_id)
    if not result.ok:
        raise HTTPException(400, result.message)
    return {"ok": True, "note": result.message}
