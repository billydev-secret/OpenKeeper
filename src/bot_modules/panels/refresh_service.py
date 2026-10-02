"""Redraw every posted channel panel in place — on boot and on an art change.

The runner behind ``core.panel_refresh``: it walks every registered refresher
for every guild, one edit at a time, and owns the one refresher that isn't a
cog's own — the posted-once panels recorded in ``posted_panels``. Plan:
``docs/plans/panel-refresh.md``.

**What a refresh never does.** It never posts a panel that isn't there. A
sticky whose message is gone is left to its own rules (it may heal on its next
event, or treat the delete as retirement); a recorded posted-once message that
404s has its row deleted, because a moderator deleting a panel is how a panel
is retired. It never moves a panel either. An edit keeps the message where it
is and pings nobody.

**Failures stay local.** One panel's error is logged and counted, and the walk
carries on to the next panel and the next guild. A recorded panel whose edit
fails for any reason other than a 404 keeps its row for the next pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

import discord

from bot_modules.core import panel_refresh
from bot_modules.core.sticky import PanelContent

log = logging.getLogger(__name__)

#: The registry name of the posted-once panels' refresher.
POSTED_PANELS = "posted panels"

#: Pause between two edits, so a guild with twenty panels doesn't spend its
#: whole edit bucket in one burst.
EDIT_SPACING_SECONDS = 1.0


# ── posted_panels store ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PostedPanel:
    id: int
    guild_id: int
    panel_key: str
    channel_id: int
    message_id: int
    options: dict[str, Any]


def _row(r: sqlite3.Row | tuple) -> PostedPanel:
    try:
        options = json.loads(r[5] or "{}")
    except ValueError:
        options = {}
    return PostedPanel(
        id=int(r[0]), guild_id=int(r[1]), panel_key=str(r[2]),
        channel_id=int(r[3]), message_id=int(r[4]),
        options=options if isinstance(options, dict) else {},
    )


_COLUMNS = "id, guild_id, panel_key, channel_id, message_id, options_json"


def record_post(
    conn: sqlite3.Connection,
    guild_id: int,
    panel_key: str,
    channel_id: int,
    message_id: int,
    options: Optional[dict[str, Any]] = None,
) -> None:
    """Remember a panel just posted. A second post of the same panel into the
    same channel replaces the first row — ``post_and_record`` moves that
    copy (posts the new one, then deletes the old), so one row per channel."""
    conn.execute(
        "INSERT INTO posted_panels"
        " (guild_id, panel_key, channel_id, message_id, options_json, posted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (guild_id, panel_key, channel_id) DO UPDATE SET"
        " message_id = excluded.message_id, options_json = excluded.options_json,"
        " posted_at = excluded.posted_at",
        (guild_id, panel_key, channel_id, message_id,
         json.dumps(options or {}, sort_keys=True), time.time()),
    )


def find_post(
    conn: sqlite3.Connection, guild_id: int, panel_key: str, channel_id: int
) -> Optional[PostedPanel]:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM posted_panels"
        " WHERE guild_id = ? AND panel_key = ? AND channel_id = ?",
        (guild_id, panel_key, channel_id),
    ).fetchone()
    return _row(row) if row else None


def list_posts(conn: sqlite3.Connection, guild_id: int) -> list[PostedPanel]:
    return [
        _row(r)
        for r in conn.execute(
            f"SELECT {_COLUMNS} FROM posted_panels WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ).fetchall()
    ]


def delete_post(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute("DELETE FROM posted_panels WHERE id = ?", (post_id,))


# ── posted-once panels ────────────────────────────────────────────────────

#: ``(guild, panel_key, options) -> PanelContent``, or None when the panel
#: can't be built right now (its cog isn't loaded, its feature was switched
#: off). None leaves the message and its row alone.
Renderer = Callable[[discord.Guild, str, dict[str, Any]], Awaitable[Optional[PanelContent]]]


async def refresh_posted(
    guild: discord.Guild,
    posts: Iterable[PostedPanel],
    render: Renderer,
    forget: Callable[[int], Awaitable[None]],
    *,
    spacing: float = 0.0,
) -> int:
    """Edit each recorded panel in ``guild`` to what the running code draws.

    Returns how many edits were issued. A message that is gone, or a channel
    that is gone, calls ``forget(post_id)``. Any other failure is logged and
    the row kept.
    """
    edited = 0
    for post in posts:
        channel = guild.get_channel(post.channel_id)
        if channel is None:
            await forget(post.id)
            continue
        try:
            content = await render(guild, post.panel_key, post.options)
        except Exception:
            log.exception(
                "panel refresh: couldn't build %s in guild %s", post.panel_key, guild.id
            )
            continue
        if content is None:
            continue
        try:
            await channel.get_partial_message(post.message_id).edit(  # type: ignore[union-attr]
                **content.edit_kwargs()
            )
        except discord.NotFound:
            await forget(post.id)
            continue
        except discord.HTTPException:
            log.warning(
                "panel refresh: Discord refused the edit of %s in guild %s",
                post.panel_key, guild.id, exc_info=True,
            )
            continue
        edited += 1
        if spacing:
            await asyncio.sleep(spacing)
    return edited


def _registry_renderer(bot: Any) -> Renderer:
    """Render a recorded row through its spec's ``render`` method on the
    owning cog (``panel_registry``); a spec without one, or a cog that isn't
    loaded, leaves the message as it is."""
    from bot_modules.panels.registry import get_panel_spec  # noqa: PLC0415

    async def render(guild: discord.Guild, key: str, options: dict[str, Any]) -> Optional[PanelContent]:
        spec = get_panel_spec(key)
        if spec is None or not spec.render:
            return None
        cog = bot.get_cog(spec.cog)
        build = getattr(cog, spec.render, None) if cog is not None else None
        if build is None:
            return None
        return await build(guild, **options)

    return render


async def _refresh_recorded(
    bot: Any,
    db_path: Any,
    guild: discord.Guild,
    *,
    panel_key: Optional[str],
    spacing: float,
) -> int:
    from bot_modules.core.db_utils import open_db  # noqa: PLC0415

    def _list() -> list[PostedPanel]:
        with open_db(db_path) as conn:
            posts = list_posts(conn, guild.id)
        return [p for p in posts if panel_key is None or p.panel_key == panel_key]

    def _delete(post_id: int) -> None:
        with open_db(db_path) as conn:
            delete_post(conn, post_id)

    async def forget(post_id: int) -> None:
        await asyncio.to_thread(_delete, post_id)

    posts = await asyncio.to_thread(_list)
    if not posts:
        return 0
    return await refresh_posted(guild, posts, _registry_renderer(bot), forget, spacing=spacing)


def posted_panels_refresher(bot: Any, db_path: Any, *, spacing: float = EDIT_SPACING_SECONDS) -> panel_refresh.Refresher:
    """The ``posted_panels`` rows as one registry entry, each rendered through
    its spec (:func:`_registry_renderer`)."""

    async def refresh(guild: discord.Guild) -> int:
        return await _refresh_recorded(bot, db_path, guild, panel_key=None, spacing=spacing)

    return refresh


async def refresh_panel_key(bot: Any, db_path: Any, guild: discord.Guild, panel_key: str) -> int:
    """Redraw every recorded copy of one panel in ``guild`` now — for a panel
    whose content follows settings saved elsewhere (the Mod Settings panel
    after a dashboard save), rather than waiting for the next boot. Same rules
    as the boot refresh: a gone message forgets its row, a failure keeps it.
    Returns how many edits were issued."""
    return await _refresh_recorded(bot, db_path, guild, panel_key=panel_key, spacing=0.0)


# ── posting ───────────────────────────────────────────────────────────────


async def post_and_record(
    db_path: Any,
    guild: discord.Guild,
    channel: Any,
    panel_key: str,
    options: dict[str, Any],
    *,
    post: Callable[..., Awaitable[Any]],
) -> Any:
    """Post a posted-once panel and record it, moving any copy already there.

    The dashboard's Post button for a spec with ``render``. The new copy goes
    up first and is recorded, with its options, so the refresher can redraw
    it; only then is the copy this channel already had deleted — posting again
    is how an admin brings a panel that scrolled off back to the bottom, so
    two copies in one channel are never the intent. A failed post leaves the
    old copy and its row alone. Copies in other channels are untouched: a
    panel members may have bookmarked is only removed by a moderator. Returns
    what ``post`` returned (None when Discord refused it).
    """
    from bot_modules.core.db_utils import open_db  # noqa: PLC0415

    def _find() -> Optional[PostedPanel]:
        with open_db(db_path) as conn:
            return find_post(conn, guild.id, panel_key, channel.id)

    def _record(message_id: int) -> None:
        with open_db(db_path) as conn:
            record_post(conn, guild.id, panel_key, channel.id, message_id, options)

    existing = await asyncio.to_thread(_find)
    message = await post(guild, channel, **options)
    if message is None:
        return None
    await asyncio.to_thread(_record, message.id)
    if existing is not None and existing.message_id != message.id:
        try:
            await channel.get_partial_message(existing.message_id).delete()
        except (discord.HTTPException, TimeoutError):
            # Already gone, not ours to delete, or a timeout: the new copy is
            # posted and recorded either way, so never abort the post here.
            log.warning(
                "panel post: could not delete the old %s copy %s in guild %s",
                panel_key, existing.message_id, guild.id, exc_info=True,
            )
    return message


# ── the runner ────────────────────────────────────────────────────────────


@dataclass
class RefreshReport:
    edited: int = 0
    failed: list[str] = field(default_factory=list)


async def refresh_guild(
    guild: discord.Guild,
    refreshers: Iterable[tuple[str, panel_refresh.Refresher]],
    *,
    spacing: float = 0.0,
) -> RefreshReport:
    """Run every refresher for one guild. One refresher raising never stops
    the rest."""
    report = RefreshReport()
    for name, fn in refreshers:
        try:
            edits = int(await fn(guild))
            report.edited += edits
            if edits and spacing:
                await asyncio.sleep(spacing)
        except Exception:
            log.exception("panel refresh: %s failed in guild %s", name, guild.id)
            report.failed.append(name)
    return report


async def refresh_all(
    guilds: Iterable[discord.Guild], *, reason: str, spacing: float = EDIT_SPACING_SECONDS
) -> RefreshReport:
    """Every registered panel in every guild, one guild after another."""
    total = RefreshReport()
    for guild in guilds:
        report = await refresh_guild(guild, panel_refresh.registered(), spacing=spacing)
        total.edited += report.edited
        total.failed.extend(f"{name} ({guild.id})" for name in report.failed)
    log.info(
        "panel refresh (%s): %d panel(s) redrawn%s",
        reason, total.edited,
        f", failed: {', '.join(total.failed)}" if total.failed else "",
    )
    return total
