"""Discord surface for intake cards (welcome tracker).

Components V2 layout + persistent buttons for the per-newcomer card posted
to greeter chat on join, plus the event-hook handlers ``events_cog`` calls. All decisions
(what a message means, which steps tick) live in
:mod:`bot_modules.onboarding.intake.service`; this module is glue that renders
the result and talks to Discord.

Persistent (``timeout=None``) buttons rendered as
:class:`discord.ui.DynamicItem`, registered in ``__main__`` via
``bot.add_dynamic_items(...)``:

* :class:`IntakeStepButton` — one per manual step without a code (a coded
  step ticks from the greeter's message and draws no button); toggles the
  tick. ``custom_id`` carries the ``intake_cards`` row id + step key.
* :class:`IntakeGrantButton` — one per un-ticked grant step; adds that
  step's role to the member and ticks it (greeters + mods).
* :class:`IntakeDismissButton` — mods close the card without completing.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sqlite3
import time
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any, cast
from functools import partial

import discord

from bot_modules.core.layout import lobby_art
from bot_modules.core.layout import card as layout_card
from bot_modules.economy.intake_rewards import pay_intake_steps
from bot_modules.core.branding_service import resolve_panel_art_conn
from bot_modules.onboarding.intake import service as svc
from bot_modules.panels.title_service import drawable_title, titled_strip_bytes
from bot_modules.shared.name_resolver import NameFn, build_name_fn
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.core.role_safety import role_block_reason
from bot_modules.core.utils import get_bot_member
from bot_modules.moderation.service import write_audit
from bot_modules.roles.grant.logic import (
    GATE_MISSING_PREREQUISITE,
    GATE_PREREQUISITE_DELETED,
    role_prerequisite_verdict,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

_STEP_CID = re.compile(r"intake:step:(?P<cid>\d+):(?P<key>[\w-]{1,64})")
_DISMISS_CID = re.compile(r"intake:dismiss:(?P<cid>\d+)")
_GRANT_CID = re.compile(r"intake:grant:(?P<cid>\d+):(?P<key>[\w-]{1,64})")

#: audit_log action for a Grant press on an intake card. A greeter handing out
#: a role is new, so like Level 5 Grant the row names who pressed.
GRANT_AUDIT_ACTION = "intake_grant"

#: A closed card's ``###`` line: the outcome takes the event's place, so a
#: scan of the channel reads outcomes (design pass, 2026-09-30).
_RESOLVED_HEADLINES = {
    svc.RESOLUTION_COMPLETED: "🎉 Intake complete — welcomed by {by}",
    svc.RESOLUTION_DISMISSED: "🚫 Dismissed by {by}",
    svc.RESOLUTION_LEFT: "👋 They left the server",
    svc.RESOLUTION_BANNED: "🔨 They were banned",
    svc.RESOLUTION_ADVANCED: "➡️ Reached Level 5, approved by {by}; a new card follows",
}

#: An open card's ``###`` line names what happened, not the internal "stage"
#: (design pass, 2026-09-28).
_TITLES = {
    svc.STAGE_JOIN: "🚪 Just joined",
    svc.STAGE_LEVEL5: "🌙 Reached Level 5",
}

#: Subtext prefix: the size a line drops to once it is history (a done or
#: skipped step, the facts), so what is still owed is the only body text.
SUBTEXT = "-# "

#: Button verbs. The buttons sit in a row at the foot, away from their lines,
#: so each label is "verb: step" — the verb alone would not say which step.
LABEL_DONE = "Done"
LABEL_UNDO = "Undo"
LABEL_GRANT = "Give role"

#: The pending glyph. U+2610 BALLOT BOX draws as a thin outline in the text
#: weight, beside ✅; the white-square emoji ⬜ it replaced rendered on the
#: dark theme as a flat grey block that read as a broken image (Ben,
#: 2026-09-30: "this card is pretty ugly").
PENDING = "☐"


# ---------------------------------------------------------------------------
# Card layout (pure)
# ---------------------------------------------------------------------------


def step_line(
    step: Any, name_fn: NameFn, role_names: Mapping[int, str] | None = None
) -> str:
    """One checklist line: ✅ done / ⏭️ skipped / ☐ pending.

    Done lines show who ticked (``done_by`` 0 = the bot's own tick, shown as
    "automatically") and when. A pending grant step names, in brackets, the
    role its button hands out.
    """
    label = str(step["label"])
    if step["skipped"]:
        return f"⏭️ ~~{label}~~ — skipped"
    if step["done_at"] is not None:
        by = "automatically" if not step["done_by"] else name_fn(int(step["done_by"]))
        return f"✅ {label} — {by} <t:{int(step['done_at'])}:R>"
    role = (role_names or {}).get(int(step["auto_role_id"] or 0))
    if str(step["auto_kind"]) == svc.AUTO_GRANT and role:
        return f"{PENDING} {label} ({role})"
    return f"{PENDING} {label}"


def _is_history(step: Any) -> bool:
    """True once the step's line is a record, not a thing owed (see ``_checklist``)."""
    return bool(step["skipped"]) or step["done_at"] is not None


def card_user_ids(
    member_id: int,
    inviter_id: int | None,
    steps: list[Any],
    resolver_id: int = 0,
    opener_id: int = 0,
) -> list[int]:
    """Every id :func:`build_intake_layout` names, for one ``build_name_fn`` call."""
    ids = [member_id, inviter_id or 0, resolver_id, opener_id]
    ids.extend(int(s["done_by"] or 0) for s in steps)
    return [uid for uid in ids if uid]


def step_button(
    card_id: int, step: Any, *, coded: bool = False
) -> discord.ui.Button | None:
    """The step's button, or None when there is nothing to press.

    A pending grant step with a role gets Give role; a manual step toggles
    between Done and Undo; auto steps, skipped steps and a ticked or
    role-less grant step get none. Plain buttons with the persistent ids —
    the registered ``Intake*Button`` dynamic items answer the press. The
    label names its step ("Done: SFW questions asked"): the button sits in
    the foot row, not beside its line.

    A manual step with a code (``coded``) gets no button either way: the
    greeter's canned message ticks it, so the card treats it like an auto
    step — no Done to press, and no Undo, because nothing was pressed (Ben,
    2026-09-30: "We don't need a SFW questions finished button, it's
    automatic"). The step's line is unchanged; only the foot row is.
    """
    kind = str(step["auto_kind"])
    key = str(step["step_key"])
    if step["skipped"]:
        return None
    if kind == svc.AUTO_GRANT:
        if step["done_at"] is not None or int(step["auto_role_id"] or 0) <= 0:
            return None
        verb, cid = LABEL_GRANT, f"intake:grant:{card_id}:{key}"
    elif not kind and not coded:
        verb = LABEL_UNDO if step["done_at"] is not None else LABEL_DONE
        cid = f"intake:step:{card_id}:{key}"
    else:
        return None
    label = f"{verb}: {step['label']}"
    return discord.ui.Button(
        label=label[:80], style=discord.ButtonStyle.secondary, custom_id=cid
    )


def dismiss_button(card_id: int) -> discord.ui.Button:
    # No emoji: nothing else on the card carries one, and the red no-entry
    # sign made the one button that closes the card the loudest thing on it.
    return discord.ui.Button(
        label="Dismiss",
        style=discord.ButtonStyle.secondary,
        custom_id=f"intake:dismiss:{card_id}",
    )


def _checklist(
    card_id: int,
    steps: list[Any],
    name_fn: NameFn,
    role_names: Mapping[int, str] | None,
    *,
    closed: bool,
    coded_keys: Collection[str] = frozenset(),
) -> list[discord.ui.Item[Any]]:
    """The checklist: every line in one text block, then (open cards only)
    the buttons in rows of five at the foot, Dismiss last. Steps whose key
    is in ``coded_keys`` draw their line but no button (see ``step_button``).

    One block rather than a Section per pressable row (which the card wore
    from 2026-09-28 to 09-30): a row with an accessory is taller than a text
    line, so a checklist of four auto steps and one manual one drew four
    tight lines and then one padded row with a lone button floating at its
    right. Lines of equal height, and every button in one place, read as
    one list.

    **A line drops to subtext the moment it is history** (design pass,
    2026-09-30): done and skipped lines take the ``-#`` size, pending lines
    keep the body size, so what is still owed is the only body text on the
    card, a card quiets down as the work gets done, and a completed one is
    visibly a receipt. The order is the procedure's order, untouched.
    """
    # No "Checklist" heading: the ✅/☐ lines say it, under the divider.
    items: list[discord.ui.Item[Any]] = []
    if steps:
        lines = [
            (SUBTEXT if _is_history(s) else "") + step_line(s, name_fn, role_names)
            for s in steps
        ]
        items.append(discord.ui.TextDisplay("\n".join(lines)))
    if closed:
        return items
    buttons = [
        b
        for b in (
            step_button(card_id, s, coded=str(s["step_key"]) in coded_keys) for s in steps
        )
        if b is not None
    ]
    buttons.append(dismiss_button(card_id))
    items.append(discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))
    for i in range(0, len(buttons), 5):
        items.append(discord.ui.ActionRow(*buttons[i : i + 5]))
    return items


def build_intake_layout(
    *,
    card_id: int,
    member_id: int,
    member_display: str,
    account_created_ts: float | None,
    inviter_id: int | None,
    steps: list[Any],
    name_fn: NameFn,
    stage: int = svc.STAGE_JOIN,
    greeter_role_id: int = 0,
    opener_id: int = 0,
    avatar_url: str | None = None,
    role_names: Mapping[int, str] | None = None,
    resolved: tuple[str, int] | None = None,
    header_url: str | None = None,
    footer_url: str | None = None,
    inviter_is_bot: bool = False,
    coded_keys: Collection[str] = frozenset(),
    opened_at: float | None = None,
) -> list[discord.ui.Item[Any]]:
    """The card, as Components V2 items: a work ticket — the event, the
    facts, a checklist, its buttons in one row at the foot (a paradigm-5
    shape, not the row-per-action list it was until 2026-09-30; see
    ``_checklist``). ``resolved`` = ``(resolution, resolver_id)`` once
    closed. ``coded_keys`` names the manual steps a code ticks, which get no
    button (``service.coded_step_keys``, read from the live config at
    render). ``opened_at`` is the card row's creation time — the queue age.

    **The frame is the card** (design pass, 2026-09-30, Ben: "use the small
    header and footer"). The member's name is the title, drawn into the
    games' small lobby strip when ``header_url`` is given (the
    ``attachment://`` of that drawing, see :func:`_header_art`), and the
    guild's footer strip closes the card under the buttons, the way a lobby
    closes: in a channel of a dozen open cards each one ends with art
    instead of a row of grey buttons abutting the next card's strip. Both
    come from ``lobby_art``, so the footer never draws without the header.
    Without art the name is a ``##`` heading and the card is bare.

    Under the title, one text block beside the member's avatar, in the
    order a greeter acts on it: the event as a ``###`` line ("🚪 Just
    joined" / "🌙 Reached Level 5"; once closed the outcome takes its
    place, so a scan of the channel reads outcomes), the greeter ping on its
    own line (open cards only; the one line a PvP challenge gives its
    target), then the facts in subtext — username, how long they have
    waited ("joined ‹pill›", or "approved by ‹name› ‹pill›" for a Level 5
    card), account age, and how they got in ("invited by ‹name›", a listing
    bot's "via ‹bot›", "inviter unknown"). Plain text with the timestamp
    pills; nothing in the text is bold, the drawn name is the card's one
    loud thing. Then the checklist under a rule, and the buttons. A closed
    card has no buttons and no ping.

    Everyone on the card is named through ``name_fn``, never mentioned: greeter
    chat reads it long after the newcomer or their inviter may have left. The
    greeter role is the one deliberate ping, allow-listed at the send. A
    resolver id of 0 credits nobody — only auto steps ever ticked.

    Budget: the fullest card — a 20-step stage, every step pressable, both
    strips and the avatar — is the two galleries, the header, a rule, the
    text block, a spacer and five rows of buttons, well under Discord's 40
    (``test_intake_views`` measures it).
    """
    closed = resolved is not None
    member_name = name_fn(member_id)
    art_top, art_bottom = lobby_art(header_url, footer_url, title=member_name)
    heading = "" if art_top else f"## {member_name}\n"
    if resolved is not None:
        resolution, resolver_id = resolved
        by = name_fn(resolver_id) if resolver_id > 0 else "the checklist"
        event = _RESOLVED_HEADLINES.get(resolution, "Closed").format(by=by)
    else:
        event = _TITLES.get(stage, _TITLES[svc.STAGE_JOIN])
    lines = [f"{heading}### {event}"]
    if greeter_role_id > 0 and not closed:
        lines.append(f"<@&{greeter_role_id}>")
    # No progress bar (Ben, 2026-09-28): the checklist right below says it.
    # The username is member-authored text on a rendered line: escaped, so
    # its underscores stay underscores. (name_fn's names arrive escaped.)
    facts = [discord.utils.escape_markdown(member_display)]
    when = f" <t:{int(opened_at)}:R>" if opened_at is not None else ""
    if stage == svc.STAGE_LEVEL5:
        if opener_id:
            facts.append(f"approved by {name_fn(opener_id)}{when}")
        elif when:
            facts.append(f"promoted{when}")
    elif when:
        facts.append(f"joined{when}")
    if account_created_ts is not None:
        facts.append(f"account created <t:{int(account_created_ts)}:R>")
    if not inviter_id:
        facts.append("inviter unknown")
    elif inviter_is_bot:
        facts.append(f"via {name_fn(inviter_id)}")
    else:
        facts.append(f"invited by {name_fn(inviter_id)}")
    # The facts share the avatar's block so the thumbnail fills its height.
    lines.append(SUBTEXT + " • ".join(facts))
    title = discord.ui.TextDisplay("\n".join(lines))
    header: discord.ui.Item[Any] = (
        discord.ui.Section(title, accessory=discord.ui.Thumbnail(avatar_url))
        if avatar_url
        else title
    )
    return [
        *art_top,
        header,
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.large),
        *_checklist(
            card_id, steps, name_fn, role_names, closed=closed, coded_keys=coded_keys
        ),
        *art_bottom,
    ]


# ---------------------------------------------------------------------------
# Persistent buttons
# ---------------------------------------------------------------------------


class IntakeStepButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_STEP_CID,
):
    """Toggle one manual checklist step (greeters + mods)."""

    def __init__(self, card_id: int, step_key: str, label: str, done: bool) -> None:
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.success if done else discord.ButtonStyle.secondary,
                custom_id=f"intake:step:{card_id}:{step_key}",
            )
        )
        self.card_id = card_id
        self.step_key = step_key

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> IntakeStepButton:
        return cls(int(match["cid"]), match["key"], item.label or match["key"], False)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _toggle_step(interaction, self.card_id, self.step_key)


class IntakeDismissButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_DISMISS_CID,
):
    """Close the card without completing (mods only)."""

    def __init__(self, card_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Dismiss",
                style=discord.ButtonStyle.secondary,
                custom_id=f"intake:dismiss:{card_id}",
            )
        )
        self.card_id = card_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> IntakeDismissButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _dismiss_card(interaction, self.card_id)


class IntakeGrantButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_GRANT_CID,
):
    """Grant a grant step's role to the member (greeters + mods)."""

    def __init__(self, card_id: int, step_key: str, label: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"intake:grant:{card_id}:{step_key}",
            )
        )
        self.card_id = card_id
        self.step_key = step_key

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> IntakeGrantButton:
        return cls(int(match["cid"]), match["key"], item.label or "Grant")

    async def callback(self, interaction: discord.Interaction) -> None:
        await _grant_step(interaction, self.card_id, self.step_key)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _card_render_data(
    ctx: AppContext, guild_id: int, card_id: int, user_id: int
):
    """(steps, inviter_id, greeter_role_id) fetched off-thread for a render."""
    with ctx.open_db() as conn:
        return (
            svc.steps_for(conn, card_id),
            svc.inviter_for(conn, guild_id, user_id),
            svc.greeter_role_id(conn, guild_id),
        )


def _role_names(guild: discord.Guild, steps: list[Any]) -> dict[int, str]:
    """Names of the roles the card's grant steps hand out, for their rows."""
    out: dict[int, str] = {}
    for s in steps:
        role = guild.get_role(int(s["auto_role_id"] or 0))
        if role is not None:
            out[role.id] = role.name
    return out


async def _layout_for_card(
    ctx: AppContext,
    guild: discord.Guild,
    card: Any,
    steps: list[Any],
    inviter_id: int | None,
    greeter_role_id: int,
    resolved: tuple[str, int] | None = None,
) -> tuple[discord.ui.LayoutView, list[discord.File]]:
    """The card's view and the files it references — its drawn header, or
    nothing. Every send and edit passes both: ``attachments=`` replaces what
    the message holds, so the header the layout points at is always there.
    The footer is the guild's hosted strip, so it travels as a URL."""
    user_id = int(card["user_id"])
    member = guild.get_member(user_id)
    inviter = guild.get_member(int(inviter_id)) if inviter_id else None
    opener_id = svc.card_opener(card)
    stage = svc.card_stage(card)
    name_fn = await build_name_fn(
        guild=guild,
        db_path=ctx.db_path,
        guild_id=guild.id,
        user_ids=card_user_ids(
            user_id, inviter_id, steps, resolved[1] if resolved else 0, opener_id
        ),
    )
    header_url, footer_url, files = await _header_art(ctx, guild, name_fn(user_id))
    # Which steps a code ticks comes from the live config, like the code
    # matching itself, so the buttons follow an admin's edit at the next
    # render. A closed card draws no buttons, so it skips the read.
    coded_keys: frozenset[str] = frozenset()
    if resolved is None:
        try:
            coded_keys = await asyncio.to_thread(_coded_step_keys, ctx, guild.id, stage)
        except Exception:
            # Like a failed header drawing: a card with a button too many
            # beats no card at all (the row would be left without a
            # message). The press is refused anyway (step_takes_a_press).
            log.exception("intake: coded-step read failed for card %s", card["id"])
    view = layout_card(
        build_intake_layout(
            card_id=int(card["id"]),
            member_id=user_id,
            member_display=str(member) if member else str(user_id),
            account_created_ts=member.created_at.timestamp() if member else None,
            inviter_id=inviter_id,
            steps=steps,
            name_fn=name_fn,
            stage=stage,
            greeter_role_id=greeter_role_id,
            opener_id=opener_id,
            avatar_url=member.display_avatar.url if member else None,
            role_names=_role_names(guild, steps),
            resolved=resolved,
            header_url=header_url,
            footer_url=footer_url,
            # A listing bot's link (DISBOARD and its kind) reads "via", not
            # "invited by". An inviter who has left is a person by default.
            inviter_is_bot=bool(inviter is not None and inviter.bot),
            coded_keys=coded_keys,
            opened_at=float(card["created_at"]),
        )
    )
    return view, files


def _coded_step_keys(ctx: AppContext, guild_id: int, stage: int) -> frozenset[str]:
    """``service.coded_step_keys`` on a fresh connection, for ``to_thread``."""
    with ctx.open_db() as conn:
        return svc.coded_step_keys(conn, guild_id, stage)


#: The drawn header's file name, which the layout's media item points at.
HEADER_FILE = "intake-header.webp"


async def _header_art(
    ctx: AppContext, guild: discord.Guild, name: str
) -> tuple[str | None, str | None, list[discord.File]]:
    """``(header_url, footer_url, files)`` for a card titled ``name``.

    The guild's lobby strip with the name drawn between its leaves, attached
    rather than hosted: the hosted art directory is public and outlives an
    erasure, so it never holds a file per member
    (``title_service.titled_strip_bytes``). The footer is the guild's own
    strip with no member in it, so it stays the hosted URL. No header art,
    a name with nothing the face can draw, or a failed drawing →
    ``(None, None, [])``, and the card keeps its text heading and no
    footer: the frame is both strips or neither (``lobby_art``).
    """

    def _read() -> tuple[str | None, bytes] | None:
        with ctx.open_db() as conn:
            art = resolve_panel_art_conn(conn, guild.id)
        title = drawable_title(name)
        if not art.header or not title:
            return None
        strip = titled_strip_bytes(art.header, title)
        if strip is None:
            return None
        return art.footer or None, strip

    try:
        read = await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001 — decoration spanning a DB read and a Pillow render; the card must post with its text heading whatever the art does
        log.debug("intake: header art unavailable", exc_info=True)
        return None, None, []
    if read is None:
        return None, None, []
    footer_url, drawn = read
    file = discord.File(io.BytesIO(drawn), filename=HEADER_FILE)
    return f"attachment://{HEADER_FILE}", footer_url, [file]


async def _edit_card_message(
    guild: discord.Guild,
    card: Any,
    view: discord.ui.LayoutView,
    files: list[discord.File],
) -> None:
    channel = guild.get_channel(int(card["channel_id"]))
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        # Cards posted before the Components V2 conversion are embed messages
        # with a content line; an edit that doesn't null both leaves them
        # beside the layout, which Discord refuses. Harmless on a V2 card.
        await channel.get_partial_message(int(card["message_id"])).edit(
            content=None, embed=None, view=view, attachments=files
        )
    except discord.HTTPException:
        log.debug("intake: failed to edit card message", exc_info=True)


def _autocompleted(conn: Any, card: Any, actor_id: int) -> bool:
    """Close the card if that tick was the last one. True when it closed."""
    return (
        svc.autocomplete_if_finished(conn, int(card["id"]), actor_id=actor_id)
        is not None
    )


async def _rerender_card(ctx: AppContext, guild: discord.Guild, card: Any) -> None:
    """Refresh an open card after a tick."""
    steps, inviter_id, greeter_role = await asyncio.to_thread(
        _card_render_data, ctx, guild.id, int(card["id"]), int(card["user_id"])
    )
    view, files = await _layout_for_card(ctx, guild, card, steps, inviter_id, greeter_role)
    await _edit_card_message(guild, card, view, files)


async def _render_resolved(
    ctx: AppContext,
    guild: discord.Guild,
    card: Any,
    resolution: str,
    resolver_id: int,
) -> None:
    """Flip a card message to its closed state, buttons gone.

    ``resolver_id`` 0 credits nobody (left, banned, or only auto steps ticked).
    """
    steps, inviter_id, greeter_role = await asyncio.to_thread(
        _card_render_data, ctx, guild.id, int(card["id"]), int(card["user_id"])
    )
    view, files = await _layout_for_card(
        ctx, guild, card, steps, inviter_id, greeter_role, resolved=(resolution, resolver_id)
    )
    await _edit_card_message(guild, card, view, files)


# ---------------------------------------------------------------------------
# Posting a card (on_member_join)
# ---------------------------------------------------------------------------


async def post_intake_card(ctx: AppContext, member: discord.Member) -> bool:
    """Post a card for a fresh join. Returns True only when a card surface
    actually exists for this member (freshly posted, or a still-open card
    from a previous join — create_card dedupes those).

    Returns False when intake is dark **or the card could not be posted**
    (channel missing, send rejected): the caller then falls back to the
    legacy bare greeter ping, preserving the every-join-gets-announced
    invariant even while the intake channel is broken.
    """
    guild = member.guild
    guild_id = guild.id
    now = time.time()

    def _prepare():
        with ctx.open_db() as conn:
            if not svc.is_enabled(conn, guild_id):
                return None
            card_id = svc.create_card(conn, guild_id, member.id, now)
            if card_id is None:  # still-open card from a previous join
                return ("carded",)
            return (
                "post",
                card_id,
                svc.intake_channel_id(conn, guild_id),
                svc.greeter_role_id(conn, guild_id),
                svc.steps_for(conn, card_id),
                svc.inviter_for(conn, guild_id, member.id),
            )

    try:
        prepared = await asyncio.to_thread(_prepare)
    except Exception:
        log.exception("intake: failed to prepare card for %s", member.id)
        return False
    if prepared is None:
        return False
    if prepared[0] == "carded":
        svc.add_watched(guild_id, member.id)
        return True

    _, card_id, channel_id, greeter_role, steps, inviter_id = prepared
    return await _send_card(
        ctx,
        member,
        card_id=card_id,
        channel_id=channel_id,
        greeter_role=greeter_role,
        steps=steps,
        inviter_id=inviter_id,
    )


async def post_stage2_card(
    ctx: AppContext,
    member: discord.Member,
    approver: discord.Member,
    grant_role_id: int = 0,
) -> str:
    """Open a member's stage 2 card after ``approver`` approved them at Level 5.

    ``grant_role_id`` is the role the card's built-in *NSFW access* line hands
    out (:func:`intake_service.stage2_lead_steps`); pass 0 when it is unset or
    deleted. A card with nothing to grant is refused before anything moves.

    Closes a still-open join card as ``advanced`` first (see
    :func:`intake_service.advance_to_stage2`) and flips its message. Returns
    one of the ``svc.STAGE2_*`` outcomes, which the press turns into its reply
    (:func:`intake_service.stage2_press_reply`) so a skip is never silent.
    Never raises: the caller has already granted the role, and a failure here
    must not report the press as failed.
    """
    guild = member.guild
    guild_id = guild.id
    now = time.time()

    def _prepare():
        with ctx.open_db() as conn:
            if not svc.stage2_enabled(conn, guild_id):
                return svc.STAGE2_DARK
            # Before advancing: a member who never opted in keeps their join
            # card as it is, and the greeters are never pinged about them.
            optin = svc.stage2_optin_role_id(conn, guild_id)
            verdict = svc.stage2_optin_verdict(
                optin,
                {r.id for r in member.roles},
                guild.get_role(optin) is not None,
            )
            if verdict == svc.OPTIN_ROLE_MISSING:
                log.warning(
                    "intake: stage 2 opt-in role %s missing in guild %s — "
                    "opening no Stage 2 cards until it's reset", optin, guild_id
                )
                return svc.STAGE2_ROLE_MISSING
            if verdict == svc.OPTIN_NOT_OPTED_IN:
                log.info(
                    "intake: stage 2 skipped for %s in guild %s — no opt-in role %s",
                    member.id, guild_id, optin,
                )
                return svc.STAGE2_NOT_OPTED_IN
            # Check the channel before advancing: closing the join card and
            # then failing to post would leave the member with no card.
            channel_id = svc.intake_channel_id(conn, guild_id)
            if not isinstance(
                guild.get_channel(channel_id), (discord.TextChannel, discord.Thread)
            ):
                log.warning(
                    "intake: stage 2 channel %s missing in guild %s", channel_id, guild_id
                )
                return svc.STAGE2_NO_CHANNEL
            configured = svc.step_config(conn, guild_id, svc.STAGE_LEVEL5)
            lead = svc.stage2_lead_steps(configured, grant_role_id)
            if not svc.stage2_grants_a_role(lead, configured):
                log.warning(
                    "intake: stage 2 for %s in guild %s has no role to grant",
                    member.id, guild_id,
                )
                return svc.STAGE2_NO_GRANT
            closed, card_id = svc.advance_to_stage2(
                conn, guild_id, member.id, approver.id, now, grant_role_id
            )
            if card_id is None:
                return svc.STAGE2_ALREADY_OPEN
            return (
                closed,
                card_id,
                channel_id,
                svc.greeter_role_id(conn, guild_id),
                svc.steps_for(conn, card_id),
                svc.inviter_for(conn, guild_id, member.id),
            )

    try:
        prepared = await asyncio.to_thread(_prepare)
        if isinstance(prepared, str):
            return prepared
        closed, card_id, channel_id, greeter_role, steps, inviter_id = prepared
        if closed is not None and int(closed["message_id"]) > 0:
            await _render_resolved(
                ctx, guild, closed, svc.RESOLUTION_ADVANCED, approver.id
            )
        sent = await _send_card(
            ctx,
            member,
            card_id=card_id,
            channel_id=channel_id,
            greeter_role=greeter_role,
            steps=steps,
            inviter_id=inviter_id,
        )
        return svc.STAGE2_OPENED if sent else svc.STAGE2_FAILED
    except Exception:
        log.exception("intake: stage 2 card failed for %s", member.id)
        return svc.STAGE2_FAILED


async def _send_card(
    ctx: AppContext,
    member: discord.Member,
    *,
    card_id: int,
    channel_id: int,
    greeter_role: int,
    steps: list[Any],
    inviter_id: int | None,
) -> bool:
    """Post a freshly created card; rolls the row back if Discord refuses.

    When the post fails the card is deleted, so the member isn't left watched
    by a card nobody can see. The caller decides what that means (the join
    path falls back to the legacy ping).
    """
    guild = member.guild
    guild_id = guild.id
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        log.warning("intake: channel %s missing in guild %s", channel_id, guild_id)
        await asyncio.to_thread(_delete_card, ctx, card_id)
        return False  # no card surface — let the legacy ping announce them

    card = await asyncio.to_thread(_fetch_card, ctx, card_id)
    view, files = await _layout_for_card(ctx, guild, card, steps, inviter_id, greeter_role)
    try:
        # The greeter role is pinged from inside the header text; the
        # allow-list is what makes that one mention (and only it) notify.
        posted = await channel.send(
            view=view,
            files=files,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(id=greeter_role)] if greeter_role > 0 else False,
            ),
        )
    except discord.HTTPException:
        log.warning("intake: failed to post card in guild %s", guild_id)
        await asyncio.to_thread(_delete_card, ctx, card_id)
        return False  # no card surface — let the legacy ping announce them

    await asyncio.to_thread(_attach_message, ctx, card_id, channel.id, posted.id)
    svc.add_watched(guild_id, member.id)
    return True


def _fetch_card(ctx: AppContext, card_id: int) -> Any:
    with ctx.open_db() as conn:
        return svc.get_card(conn, card_id)


def _delete_card(ctx: AppContext, card_id: int) -> None:
    with ctx.open_db() as conn:
        svc.delete_card(conn, card_id)


def _attach_message(ctx: AppContext, card_id: int, channel_id: int, message_id: int) -> None:
    with ctx.open_db() as conn:
        svc.set_card_message(conn, card_id, channel_id, message_id)


# ---------------------------------------------------------------------------
# Event-hook handlers (called from events_cog)
# ---------------------------------------------------------------------------


async def handle_intake_message(ctx: AppContext, message: discord.Message) -> None:
    """Greet + step/completion-code detection for a message addressing cards.

    Caller has already pre-filtered with :func:`intake_service.is_watched`.
    """
    guild = message.guild
    author = message.author
    if guild is None or not isinstance(author, discord.Member):
        return
    guild_id = guild.id
    # A reply addresses its target as surely as an @ does — fold it in so a
    # pasted canned reply lands without hand-editing a mention into it.
    mentioned = [u.id for u in message.mentions]
    replied_to = svc.reply_target_id(message)
    if replied_to and replied_to not in mentioned:
        mentioned.append(replied_to)
    author_role_ids = {r.id for r in author.roles}
    author_is_mod = ctx.member_is_mod(author)
    content = message.content
    channel_id = message.channel.id
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            greeter_role = svc.greeter_role_id(conn, guild_id)
            actions = svc.evaluate_message(
                conn,
                guild_id,
                channel_id=channel_id,
                content=content,
                mentioned_ids=mentioned,
                author_is_greeter=greeter_role in author_role_ids,
                author_is_mod=author_is_mod,
            )
            results = []
            for action, uid, step_key in actions:
                if action == svc.ACTION_COMPLETE:
                    completed = svc.complete_card(conn, guild_id, uid, author.id, now)
                    if completed is not None:
                        results.append((svc.ACTION_COMPLETE, completed[0], uid))
                elif action == svc.ACTION_STEP:
                    card = svc.get_open_card(conn, guild_id, uid)
                    if card is not None and svc.set_step_state(
                        conn,
                        int(card["id"]),
                        step_key,
                        done=True,
                        actor_id=author.id,
                        at=now,
                    ):
                        # Pasting a step's code is doing the step — it pays
                        # like the button does, and the same anchor means
                        # re-pasting the message earns nothing more.
                        pay_intake_steps(
                            conn,
                            guild_id,
                            card_id=int(card["id"]),
                            newcomer_id=uid,
                            step_keys=(step_key,),
                            actor_id=author.id,
                            booster=author.premium_since is not None,
                            at=now,
                        )
                        if _autocompleted(conn, card, author.id):
                            results.append((svc.ACTION_COMPLETE, card, uid))
                        else:
                            results.append((svc.ACTION_STEP, card, uid))
                else:
                    card, ticked = svc.auto_tick(
                        conn, guild_id, uid, svc.AUTO_GREETED, now, actor_id=author.id
                    )
                    if card is not None and ticked:
                        # The greeter earns for the step they just ticked —
                        # rides this transaction so the coins commit with the
                        # tick (intake_rewards savepoints each step, so an
                        # economy failure can't roll the tick back).
                        pay_intake_steps(
                            conn,
                            guild_id,
                            card_id=int(card["id"]),
                            newcomer_id=uid,
                            step_keys=ticked,
                            actor_id=author.id,
                            booster=author.premium_since is not None,
                            at=now,
                        )
                        if _autocompleted(conn, card, author.id):
                            results.append((svc.ACTION_COMPLETE, card, uid))
                        else:
                            results.append((svc.ACTION_GREET, card, uid))
            return results

    try:
        results = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: message handling failed in guild %s", guild_id)
        return

    # One message can greet *and* carry step codes for the same card; collapse
    # those into a single re-render instead of editing the message N times.
    rerender: dict[int, sqlite3.Row] = {}
    completed: dict[int, tuple[sqlite3.Row, int]] = {}
    for action, card, uid in results:
        if action == svc.ACTION_COMPLETE:
            completed.setdefault(int(card["id"]), (card, uid))
        else:
            rerender.setdefault(int(card["id"]), card)
    # A message can carry a step code *and* a greeting for the same card; if
    # either closed it, the closed rendering wins — re-rendering afterwards
    # would paint the buttons back onto a resolved card.
    for card_id, (card, uid) in completed.items():
        rerender.pop(card_id, None)
        svc.discard(guild_id, uid)
        await _render_resolved(
            ctx, guild, card, svc.RESOLUTION_COMPLETED, author.id
        )
        try:
            await message.add_reaction("🎉")
        except discord.HTTPException:
            log.debug("intake: completion reaction failed", exc_info=True)
    for card in rerender.values():
        await _rerender_card(ctx, guild, card)


async def handle_role_changes(
    ctx: AppContext,
    member: discord.Member,
    gained_role_ids: list[int],
    unverified_removed: bool,
) -> None:
    """Auto-tick verified / role_gained steps after a role update."""
    guild_id = member.guild.id
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            if not svc.is_enabled(conn, guild_id):
                return None
            card = None
            ticked: list[str] = []
            for rid in gained_role_ids:
                card, keys = svc.auto_tick(
                    conn, guild_id, member.id, svc.AUTO_ROLE_GAINED, now, role_id=rid
                )
                ticked.extend(keys)
            if svc.verification_signalled(
                conn, guild_id, gained_role_ids, unverified_removed
            ):
                card, keys = svc.auto_tick(
                    conn, guild_id, member.id, svc.AUTO_VERIFIED, now
                )
                ticked.extend(keys)
            welcomer = None
            if card is not None and ticked:
                closed = svc.autocomplete_if_finished(conn, int(card["id"]))
                if closed is not None:
                    welcomer = closed[1]
            return (card, ticked, welcomer)

    try:
        result = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: role-change handling failed for %s", member.id)
        return
    if result is None:
        return
    card, ticked, welcomer = result
    if card is None or not ticked:
        return
    if welcomer is None:
        await _rerender_card(ctx, member.guild, card)
        return
    # A role grant can be the last box on the checklist — close it here too,
    # or the queue keeps a finished intake until the sweep catches it.
    svc.discard(guild_id, member.id)
    await _render_resolved(
        ctx,
        member.guild,
        card,
        svc.RESOLUTION_COMPLETED,
        welcomer,
    )


async def close_finished_cards(ctx: AppContext, guild: discord.Guild) -> int:
    """Close every open card whose checklist is already fully ticked.

    The sweep behind :func:`intake_service.autocomplete_if_finished`, run from
    the nudge loop. The live tick paths close a card the instant its last box
    is ticked, so in steady state this finds nothing; it exists for cards
    finished before auto-completion shipped (the queue was full of them) and
    for anything ticked while the bot was down. Returns how many it closed.
    """

    def _work():
        with ctx.open_db() as conn:
            if not svc.is_enabled(conn, guild.id):
                return []
            closed = []
            for card in svc.finished_open_cards(conn, guild.id):
                done = svc.autocomplete_if_finished(conn, int(card["id"]))
                if done is not None:
                    closed.append(done)
            return closed

    try:
        closed = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: finished-card sweep failed in guild %s", guild.id)
        return 0
    for card, welcomer in closed:
        svc.discard(guild.id, int(card["user_id"]))
        if int(card["message_id"]) > 0:
            await _render_resolved(
                ctx,
                guild,
                card,
                svc.RESOLUTION_COMPLETED,
                welcomer,
            )
    if closed:
        log.info(
            "intake: closed %d finished card(s) in guild %s", len(closed), guild.id
        )
    return len(closed)


async def close_member_card(
    ctx: AppContext, guild: discord.Guild, user_id: int, resolution: str
) -> None:
    """Close a member's open card on leave/ban and flip its message."""
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            return svc.close_for_member(conn, guild.id, user_id, resolution, 0, now)

    try:
        card = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: close failed for %s in guild %s", user_id, guild.id)
        return
    svc.discard(guild.id, user_id)
    if card is not None and int(card["message_id"]) > 0:
        await _render_resolved(ctx, guild, card, resolution, 0)


# ---------------------------------------------------------------------------
# Button callbacks
# ---------------------------------------------------------------------------


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="intake")


async def _toggle_step(
    interaction: discord.Interaction, card_id: int, step_key: str
) -> None:
    """Tick/untick a manual step and refresh the card in one response."""
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    actor_is_mod = ctx.member_is_mod(actor)
    actor_role_ids = {r.id for r in actor.roles}
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            card = svc.get_card(conn, card_id)
            if card is None or card["resolved_at"] is not None:
                return ("closed",)
            greeter_role = svc.greeter_role_id(conn, guild.id)
            if greeter_role not in actor_role_ids and not actor_is_mod:
                return ("denied",)
            step = next(
                (s for s in svc.steps_for(conn, card_id) if s["step_key"] == step_key),
                None,
            )
            if step is None or str(step["auto_kind"]):
                return ("closed",)  # unknown or auto step — buttons never carry these
            if not svc.step_takes_a_press(conn, guild.id, svc.card_stage(card), step):
                # A button from before the step had a code: the code ticks
                # this step, so the press changes nothing and the card
                # redraws without the button.
                return (
                    "coded",
                    card,
                    svc.steps_for(conn, card_id),
                    svc.inviter_for(conn, guild.id, int(card["user_id"])),
                    svc.greeter_role_id(conn, guild.id),
                )
            ticking = step["done_at"] is None
            changed = svc.set_step_state(
                conn,
                card_id,
                step_key,
                done=ticking,
                actor_id=actor.id,
                at=now,
            )
            if ticking and changed:
                # Only ticking pays, and only the first time this step is
                # ticked on this card — the anchor in intake_rewards makes
                # toggling the button off and on again worth nothing.
                pay_intake_steps(
                    conn,
                    guild.id,
                    card_id=card_id,
                    newcomer_id=int(card["user_id"]),
                    step_keys=(step_key,),
                    actor_id=actor.id,
                    booster=actor.premium_since is not None,
                    at=now,
                )
            closed = (
                svc.autocomplete_if_finished(conn, card_id, actor_id=actor.id)
                if ticking and changed
                else None
            )
            return (
                "done" if closed is not None else "ok",
                card,
                svc.steps_for(conn, card_id),
                svc.inviter_for(conn, guild.id, int(card["user_id"])),
                svc.greeter_role_id(conn, guild.id),
            )

    try:
        result = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: step toggle failed for card %s", card_id)
        await _safe_ephemeral(interaction, "❌ Something went wrong — try again.")
        return

    if result[0] == "closed":
        await _safe_ephemeral(interaction, "This card is no longer active.")
        return
    if result[0] == "denied":
        await _safe_ephemeral(interaction, "❌ Only greeters and mods can tick steps.")
        return

    status, card, steps, inviter_id, greeter_role = result
    finished = status == "done"
    if finished:
        # That was the last box: the card closes on the same edit rather than
        # sitting in the queue waiting for a completion code nobody posts.
        svc.discard(guild.id, int(card["user_id"]))
    view, files = await _layout_for_card(
        ctx,
        guild,
        card,
        steps,
        inviter_id,
        greeter_role,
        resolved=(
            (svc.RESOLUTION_COMPLETED, actor.id) if finished else None
        ),
    )
    try:
        await interaction.response.edit_message(
            content=None, embed=None, view=view, attachments=files
        )
    except discord.HTTPException:
        log.debug("intake: failed to edit card on toggle", exc_info=True)
        return
    if status == "coded":
        # The redraw above took the stale button away; say why, since the
        # press itself looked like it did nothing.
        await _safe_ephemeral(
            interaction,
            "That step ticks from the greeter's canned message, so its button is gone.",
        )


async def _dismiss_card(interaction: discord.Interaction, card_id: int) -> None:
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not ctx.member_is_mod(actor):
        await _safe_ephemeral(interaction, "❌ Only mods can dismiss an intake card.")
        return
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            card = svc.get_card(conn, card_id)
            if card is None or card["resolved_at"] is not None:
                return None
            svc.resolve_card(conn, card_id, actor.id, now, svc.RESOLUTION_DISMISSED)
            return (
                card,
                svc.steps_for(conn, card_id),
                svc.inviter_for(conn, guild.id, int(card["user_id"])),
                svc.greeter_role_id(conn, guild.id),
            )

    try:
        result = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: dismiss failed for card %s", card_id)
        await _safe_ephemeral(interaction, "❌ Something went wrong — try again.")
        return
    if result is None:
        await _safe_ephemeral(interaction, "Already closed.")
        return

    card, steps, inviter_id, greeter_role = result
    svc.discard(guild.id, int(card["user_id"]))
    view, files = await _layout_for_card(
        ctx, guild, card, steps, inviter_id, greeter_role,
        resolved=(svc.RESOLUTION_DISMISSED, actor.id),
    )
    try:
        await interaction.response.edit_message(
            content=None, embed=None, view=view, attachments=files
        )
    except discord.HTTPException:
        log.debug("intake: failed to edit card on dismiss", exc_info=True)


async def _grant_step(
    interaction: discord.Interaction, card_id: int, step_key: str
) -> None:
    """Grant button: add the step's role, credit the presser, refresh the card.

    The role is safety-checked at press time as well as at save — a role can
    gain permissions after it was picked — and every grant writes an audit row
    naming the presser. Greeters pressing a button that hands out a role is a
    new power, and the audit log is where a moderator looks for who did what.

    The press also honours the role's *Role Required First*, exactly as
    ``/grant`` and the role menus do. The button is a third door to the same
    role, and the production case is the one the gate exists for: main chat
    behind verification.
    """
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("intake: failed to defer grant", exc_info=True)
    actor_is_mod = ctx.member_is_mod(actor)
    actor_role_ids = {r.id for r in actor.roles}

    def _check():
        with ctx.open_db() as conn:
            greeter_role = svc.greeter_role_id(conn, guild.id)
            if greeter_role not in actor_role_ids and not actor_is_mod:
                return ("denied", None, None)
            return svc.grant_step_state(conn, card_id, step_key)

    try:
        status, card, step = await asyncio.to_thread(_check)
    except Exception:
        log.exception("intake: grant check failed for card %s", card_id)
        await _safe_ephemeral(interaction, "❌ Something went wrong — try again.")
        return
    if status == "denied":
        await _safe_ephemeral(interaction, "❌ Only greeters and mods can grant from a card.")
        return
    if status == svc.GRANT_DONE:
        await _safe_ephemeral(interaction, "That's already been granted.")
        return
    if status != svc.GRANT_OK or card is None or step is None:
        await _safe_ephemeral(interaction, "This card is no longer active.")
        return

    target = guild.get_member(int(card["user_id"]))
    if target is None:
        await _safe_ephemeral(interaction, "❌ That member isn't in the server anymore.")
        return
    role = guild.get_role(int(step["auto_role_id"]))
    if role is None:
        await _safe_ephemeral(
            interaction, "❌ That step's role no longer exists — pick one on the dashboard."
        )
        return
    blocked = role_block_reason(role, get_bot_member(guild))
    if blocked is not None:
        await _safe_ephemeral(
            interaction,
            f"❌ I won't grant that: {blocked}. Ask an admin to pick a different role.",
        )
        return
    # Skipped for a member who already holds the role, as /grant does: that
    # press hands out nothing and only ticks the row, and answering it with
    # "they need Verified first" invites someone to hand-add the prerequisite.
    if role not in target.roles:
        role_names = {r.id: r.name for r in guild.roles}
        verdict, required_id = role_prerequisite_verdict(
            ctx.guild_config(guild.id).grant_roles,
            role.id,
            guild_role_ids=role_names.keys(),
            target_role_ids={r.id for r in target.roles},
            actor_is_admin=ctx.is_admin(interaction),
        )
        if verdict == GATE_PREREQUISITE_DELETED:
            log.warning(
                "intake: card %s can't grant %s, its grant requires role %s "
                "which no longer exists in guild %s",
                card_id, role.name, required_id, guild.id,
            )
            await _safe_ephemeral(
                interaction,
                "❌ This grant is misconfigured — the role it requires first no "
                "longer exists. Ask an admin to fix it on the dashboard.",
            )
            return
        if verdict == GATE_MISSING_PREREQUISITE:
            await _safe_ephemeral(
                interaction,
                f"❌ They need **{role_names[required_id]}** before they can be "
                f"given **{role.name}**. Press again once they have it.",
            )
            return
    try:
        await target.add_roles(role, reason=f"Intake card: granted by {actor}")
    except discord.Forbidden:
        await _safe_ephemeral(interaction, "❌ I can't assign that role (check my role position).")
        return
    except discord.HTTPException:
        await _safe_ephemeral(interaction, "❌ Discord hiccup assigning the role — try again.")
        return

    now = time.time()

    def _commit():
        with ctx.open_db() as conn:
            write_audit(
                conn,
                guild_id=guild.id,
                action=GRANT_AUDIT_ACTION,
                actor_id=actor.id,
                target_id=target.id,
                extra={"role_id": role.id, "role_name": role.name, "step": step_key},
            )
            credited = svc.claim_grant_tick(conn, card_id, step_key, actor.id, now)
            if credited:
                pay_intake_steps(
                    conn,
                    guild.id,
                    card_id=card_id,
                    newcomer_id=target.id,
                    step_keys=(step_key,),
                    actor_id=actor.id,
                    booster=actor.premium_since is not None,
                    at=now,
                )
            # The role is on the member, so every other row waiting on it is
            # done as well: ticked as automatic, crediting and paying nobody.
            # The role-update hook would do the same, but it never fires for
            # a member who already held the role, and an automatic row has no
            # button to catch up with.
            svc.auto_tick(
                conn, guild.id, target.id, svc.AUTO_ROLE_GAINED, now, role_id=role.id
            )
            closed = svc.autocomplete_if_finished(conn, card_id, actor_id=actor.id)
            return svc.get_card(conn, card_id), closed

    # The role is on the member now. A DB failure past this point is logged,
    # not reported as a failed press — the card catches up on the next tick.
    try:
        fresh, closed = await asyncio.to_thread(_commit)
    except Exception:
        log.exception("intake: grant bookkeeping failed for card %s", card_id)
        await _safe_ephemeral(interaction, f"Granted {role.name}.")
        return
    if closed is not None:
        svc.discard(guild.id, target.id)
        await _render_resolved(ctx, guild, closed[0], svc.RESOLUTION_COMPLETED, closed[1])
    elif fresh is not None and fresh["resolved_at"] is None:
        await _rerender_card(ctx, guild, fresh)
    await _safe_ephemeral(interaction, f"Granted {role.name}.")
