"""Wellness panel FastAPI dependencies.

The auth guards are the dashboard's own ``web_server.deps.require_perms`` —
this module only names the two tiers the wellness routes use. It once kept a
private ``require_user``/``require_perms`` pair that raised a bare 401/403 and
logged nothing, so a refused wellness login was invisible to the operator
while every other panel's refusal left a line under ``dungeonkeeper.web.auth``.
"""

from __future__ import annotations

from fastapi import Request

from web_server.deps import get_active_guild_id, get_ctx, require_perms

__all__ = ["get_ctx", "get_guild_id", "require_manage_server", "require_user"]


def get_guild_id(request: Request) -> int:
    """Return the active guild_id from the session, with fallback."""
    return get_active_guild_id(request)


# Any signed-in member: their own wellness data.
require_user = require_perms(set())
# The admin half of the panel.
require_manage_server = require_perms({"manage_server"})
