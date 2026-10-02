"""Pure-helper tests for promotion_review_views (embed + prune-line rendering).

The interactive button/post flow is Discord glue tested via the service layer;
here we only pin the pure formatting branches.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.xp import promotion_review_service as svc
from bot_modules.xp import promotion_review_views as views
from bot_modules.xp.promotion_review_views import (
    GRANT_LABEL_DEFAULT,
    Level5PromotionView,
    ReviewCardView,
    build_review_embed,
    format_prune_lines,
    grant_label,
    ping_send_kwargs,
    refresh_level_5_cards,
    refresh_spicy_field,
)

GUILD_ID = 42
MEMBER_ID = 7
CARD_CHANNEL = 555
PING_ROLE = 902


# ── the review-card ping allow-list ───────────────────────────────────


def test_ping_kwargs_allow_list_exactly_the_ping_role():
    kwargs = ping_send_kwargs(PING_ROLE)
    # The mention must be in content — inside an embed it renders but never pings.
    assert kwargs["content"] == f"<@&{PING_ROLE}>"
    assert kwargs["allowed_mentions"].to_dict() == {"roles": [PING_ROLE], "parse": []}


def test_ping_kwargs_stay_silent_when_the_role_is_unset():
    kwargs = ping_send_kwargs(0)
    assert "content" not in kwargs
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}


def _level_5_embed(spicy_value: str | None) -> discord.Embed:
    """A Level 5 card as xp_service.maybe_log_level_5 renders it."""
    embed = discord.Embed(title="🎉 Level 5 reached", description="<@7> just reached level 5.")
    embed.add_field(name="Total XP", value="326.56", inline=True)
    if spicy_value is not None:
        embed.add_field(name=svc.SPICY_FIELD_NAME, value=spicy_value, inline=True)
    embed.add_field(name="Joined", value="<t:1784741107:F>", inline=False)
    return embed


class _FakeGuild:
    def __init__(self, roles):
        self._roles = roles  # {role_id: mention_str}

    def get_role(self, rid):
        m = self._roles.get(rid)
        return SimpleNamespace(mention=m) if m is not None else None


def test_format_prune_lines_known_unknown_and_undated():
    guild = _FakeGuild({900: "@NSFW"})
    lines = format_prune_lines(guild, [(900, 1_700_000_000.0), (901, None)])
    assert lines[0] == "@NSFW — removed <t:1700000000:D>"
    # Unknown role falls back to a code-formatted id, undated says so.
    assert lines[1] == "role `901` — removed (date unknown)"


# A dict lookup, not a default: an id the card shouldn't be naming raises.
_NAMES = {7: "Ghost", 99: "Mod"}


def test_build_embed_pruned_return_title_and_fields():
    embed = build_review_embed(
        discord.Color.blurple(),
        kind=svc.KIND_PRUNED_RETURN,
        member_id=7,
        member_display="ghost#1",
        level=5,
        prune_lines=["@NSFW — removed <t:1:D>"],
        action_hint="do the thing",
        name_fn=_NAMES.__getitem__,
    )
    assert "returned" in embed.title.lower()
    assert (embed.description or "").startswith("Ghost lost access")
    fields = {f.name: f.value for f in embed.fields}
    assert {"Member", "Level", "Access a Sweep Removed"} <= fields.keys()
    assert fields["Member"] == "Ghost (`ghost#1`)"


def test_build_embed_member_display_backtick_cannot_break_the_code_span():
    """`member_display` renders inside an inline code span, which shows
    markdown characters literally -- what it's vulnerable to is a backtick
    of its own closing the span early and putting the rest of the field back
    outside it, where markdown renders again."""
    embed = build_review_embed(
        discord.Color.blurple(),
        kind=svc.KIND_PRUNED_RETURN,
        member_id=7,
        member_display="ghost`1",
        level=5,
        prune_lines=[],
        action_hint="do the thing",
        name_fn=_NAMES.__getitem__,
    )
    fields = {f.name: f.value for f in embed.fields}
    # The span still opens/closes exactly once -- the member's own backtick
    # was substituted, not left to close the span early.
    assert fields["Member"].count("`") == 2
    assert "ghost`1" not in fields["Member"]


def test_build_embed_sleeper_title_no_prune_field_when_empty():
    embed = build_review_embed(
        discord.Color.blurple(),
        kind=svc.KIND_SLEEPER,
        member_id=7,
        member_display="ghost#1",
        level=0,
        prune_lines=[],
        action_hint="reactivate them",
        name_fn=_NAMES.__getitem__,
    )
    assert "sleeper" in embed.title.lower()
    assert "Access a Sweep Removed" not in {f.name for f in embed.fields}


@pytest.mark.parametrize(
    ("current", "has_nsfw", "expected"),
    [
        # The reported bug: card says ❌, access was granted afterwards.
        pytest.param(svc.SPICY_NOT_GRANTED, True, svc.SPICY_GRANTED, id="stale-to-granted"),
        pytest.param(svc.SPICY_GRANTED, False, svc.SPICY_NOT_GRANTED, id="revoked"),
        pytest.param(svc.SPICY_GRANTED, True, None, id="already-granted"),
        pytest.param(svc.SPICY_NOT_GRANTED, False, None, id="already-not-granted"),
        # A card posted while nsfw_role_id was unset has no Spicy field at all,
        # and ordinary level-up posts share this channel — neither is touched.
        pytest.param(None, True, None, id="no-spicy-field"),
    ],
)
def test_refresh_spicy_field_moves_only_a_stale_value(current, has_nsfw, expected):
    embed = _level_5_embed(current)
    updated = refresh_spicy_field(embed, has_nsfw)
    if expected is None:
        assert updated is None
        return
    assert updated is not None
    spicy = next(f for f in updated.fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == expected
    # Only that field moves: the rest is an at-promotion snapshot, same layout.
    assert [(f.name, f.inline) for f in updated.fields] == [
        (f.name, f.inline) for f in embed.fields
    ]
    assert next(f for f in updated.fields if f.name == "Total XP").value == "326.56"
    assert updated.title == embed.title


def test_refresh_spicy_field_ignores_an_unrelated_embed():
    assert refresh_spicy_field(discord.Embed(title="something else"), True) is None


def test_refresh_spicy_field_does_not_mutate_the_original():
    """Embed.copy() is shallow and shares field dicts — we must deep-copy."""
    embed = _level_5_embed(svc.SPICY_NOT_GRANTED)
    refresh_spicy_field(embed, True)
    spicy = next(f for f in embed.fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == svc.SPICY_NOT_GRANTED


# ── refresh_level_5_cards: stored card → Discord edit ─────────────────


SPICY_ROLE = 4242


@pytest.fixture
def ctx(sync_db_path):
    """An AppContext stand-in over a real migrated DB."""
    stub = MagicMock()
    stub.db_path = sync_db_path
    stub.open_db = lambda: open_db(sync_db_path)
    return stub


def _record(ctx, *, message_id: int = 9001, channel_id: int = CARD_CHANNEL) -> None:
    with ctx.open_db() as conn:
        svc.record_level_5_card(
            conn, GUILD_ID, MEMBER_ID, channel_id, message_id, 100.0
        )


def _stored(ctx) -> list[sqlite3.Row]:
    with ctx.open_db() as conn:
        return svc.level_5_cards_for(conn, GUILD_ID, MEMBER_ID)


def _member(guild) -> MagicMock:
    member = MagicMock()
    member.id = MEMBER_ID
    member.guild = guild
    return member


def _guild(channel) -> MagicMock:
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_channel_or_thread = MagicMock(return_value=channel)
    guild.fetch_channel = AsyncMock(return_value=channel)
    return guild


def _channel(message) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=message)
    return channel


def _message(embed: discord.Embed | None) -> MagicMock:
    message = MagicMock()
    message.id = 9001
    message.embeds = [embed] if embed is not None else []
    message.edit = AsyncMock()
    return message


@pytest.mark.parametrize(
    ("stored", "swapped", "expected"),
    [
        pytest.param([SPICY_ROLE], True, svc.SPICY_ON_BREAK_LITE, id="lite"),
        pytest.param([SPICY_ROLE], False, svc.SPICY_ON_BREAK, id="no-lite"),
        # Spicy wasn't in the snapshot: the break took nothing, so ❌ is true.
        pytest.param([], False, None, id="never-had-it"),
    ],
)
async def test_refresh_reads_a_break_as_held_not_stripped(ctx, stored, swapped, expected):
    """The break row is written before the strip, so the Spicy-removed diff
    finds it and the card says the role is held."""
    from bot_modules.breaks.store import create_break

    _record(ctx)
    with ctx.open_db() as conn:
        create_break(
            conn, guild_id=GUILD_ID, user_id=MEMBER_ID, stored_roles=stored,
            swapped_spicy=swapped,
        )
    ctx.guild_config = MagicMock(
        return_value=SimpleNamespace(grant_roles={}, promotion_review_grant_role_id=SPICY_ROLE)
    )
    message = _message(_level_5_embed(svc.SPICY_GRANTED))
    await refresh_level_5_cards(ctx, _member(_guild(_channel(message))), False)

    edited = message.edit.await_args.kwargs["embed"]
    spicy = next(f for f in edited.fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == (expected or svc.SPICY_NOT_GRANTED)


async def test_refresh_edits_a_stored_stale_card(ctx):
    """End to end: the reported bug, from stored row to the Discord edit."""
    _record(ctx)
    message = _message(_level_5_embed(svc.SPICY_NOT_GRANTED))
    member = _member(_guild(_channel(message)))

    await refresh_level_5_cards(ctx, member, True)

    message.edit.assert_awaited_once()
    edited = message.edit.await_args.kwargs["embed"]
    spicy = next(f for f in edited.fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == svc.SPICY_GRANTED


@pytest.mark.parametrize(
    "embed",
    [
        pytest.param(svc.SPICY_GRANTED, id="already-correct"),
        pytest.param("no-embeds", id="no-embeds"),
    ],
)
async def test_refresh_skips_the_edit_but_keeps_the_row(ctx, embed):
    _record(ctx)
    message = _message(None if embed == "no-embeds" else _level_5_embed(embed))
    member = _member(_guild(_channel(message)))

    await refresh_level_5_cards(ctx, member, True)

    message.edit.assert_not_awaited()
    assert len(_stored(ctx)) == 1  # still tracked


@pytest.mark.parametrize(
    ("error", "still_stored"),
    [
        # A gone message is forgotten; anything else keeps the row for next time,
        # and an unexpected error must not escape into the listener.
        pytest.param(discord.NotFound(MagicMock(), "gone"), 0, id="deleted-forgotten"),
        pytest.param(discord.HTTPException(MagicMock(), "boom"), 1, id="transient-kept"),
        pytest.param(sqlite3.OperationalError("locked"), 1, id="unexpected-never-raises"),
    ],
)
async def test_refresh_handles_a_failed_message_fetch(ctx, error, still_stored):
    _record(ctx)
    channel = _channel(None)
    channel.fetch_message = AsyncMock(side_effect=error)
    member = _member(_guild(channel))

    await refresh_level_5_cards(ctx, member, True)  # must not raise

    assert len(_stored(ctx)) == still_stored


async def test_refresh_forgets_a_card_whose_channel_is_deleted(ctx):
    _record(ctx)
    guild = _guild(None)
    guild.get_channel_or_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))

    await refresh_level_5_cards(ctx, _member(guild), True)

    assert _stored(ctx) == []


async def test_refresh_forgets_a_card_whose_channel_cant_hold_it(ctx):
    """Exists but isn't text-like — as unrefreshable as a deletion."""
    _record(ctx)
    guild = _guild(None)
    guild.get_channel_or_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock(return_value=MagicMock(spec=discord.VoiceChannel))

    await refresh_level_5_cards(ctx, _member(guild), True)

    assert _stored(ctx) == []


async def test_refresh_keeps_a_card_in_an_uncached_thread(ctx):
    """A cache miss isn't proof the channel is gone — an archived thread misses too."""
    _record(ctx)
    message = _message(_level_5_embed(svc.SPICY_NOT_GRANTED))
    channel = _channel(message)
    guild = _guild(channel)
    guild.get_channel_or_thread = MagicMock(return_value=None)  # not cached
    guild.fetch_channel = AsyncMock(return_value=channel)  # but it exists

    await refresh_level_5_cards(ctx, _member(guild), True)

    assert len(_stored(ctx)) == 1
    message.edit.assert_awaited_once()


async def test_concurrent_refreshes_settle_on_the_latest_state(ctx):
    """Two quick toggles must not leave the card showing the older state.

    Unserialized, the grant task's slow edit lands *after* the revoke task has
    already no-opped on the still-stale card, so the card ends up claiming access
    the member no longer has — and nothing reconciles it.
    """
    _record(ctx)
    embed = _level_5_embed(svc.SPICY_NOT_GRANTED)
    message = MagicMock()
    message.id = 9001
    message.embeds = [embed]

    async def _slow_edit(*, embed):
        await asyncio.sleep(0.01)  # stand in for the edit rate limit
        message.embeds = [embed]

    message.edit = AsyncMock(side_effect=_slow_edit)
    member = _member(_guild(_channel(message)))

    # Role added, then removed a moment later.
    await asyncio.gather(
        refresh_level_5_cards(ctx, member, True),
        refresh_level_5_cards(ctx, member, False),
    )

    spicy = next(f for f in message.embeds[0].fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == svc.SPICY_NOT_GRANTED
    assert message.edit.await_count == 2  # the second task corrected the first


async def test_refresh_lock_registry_does_not_leak(ctx):
    """The per-member lock entry is dropped once the last refresh finishes."""
    from bot_modules.xp import promotion_review_views as views

    _record(ctx)
    message = _message(_level_5_embed(svc.SPICY_NOT_GRANTED))
    member = _member(_guild(_channel(message)))

    await refresh_level_5_cards(ctx, member, True)

    assert views._refresh_locks == {}
    assert views._refresh_lock_users == {}


async def test_refresh_continues_past_one_broken_card(ctx):
    """A card that blows up doesn't stop the member's other cards."""
    _record(ctx, message_id=9001)
    _record(ctx, message_id=9002)
    good = _message(_level_5_embed(svc.SPICY_NOT_GRANTED))
    channel = _channel(None)
    channel.fetch_message = AsyncMock(
        side_effect=[sqlite3.OperationalError("locked"), good]
    )
    member = _member(_guild(channel))

    await refresh_level_5_cards(ctx, member, True)

    good.edit.assert_awaited_once()


async def test_refresh_is_a_noop_when_no_card_is_stored(ctx):
    message = _message(_level_5_embed(svc.SPICY_NOT_GRANTED))
    member = _member(_guild(_channel(message)))

    await refresh_level_5_cards(ctx, member, True)

    message.edit.assert_not_awaited()


def test_build_embed_resolved_verbs():
    for resolution, needle in [
        (svc.RESOLUTION_GRANTED, "granted"),
        (svc.RESOLUTION_REACTIVATED, "reactivated"),
        (svc.RESOLUTION_DISMISSED, "dismissed"),
    ]:
        embed = build_review_embed(
            discord.Color.blurple(),
            kind=svc.KIND_PRUNED_RETURN,
            member_id=7,
            member_display="ghost#1",
            level=1,
            prune_lines=[],
            action_hint="",
            name_fn=_NAMES.__getitem__,
            resolved=(resolution, 99),
        )
        resolved_field = next(f for f in embed.fields if f.name == "Resolved")
        assert needle in resolved_field.value.lower()
        assert resolved_field.value.endswith("by Mod")
        # An embed <@id> shows as digits to a reviewer who never saw the member.
        rendered = [embed.description or ""] + [f.value for f in embed.fields]
        assert not any("<@" in text for text in rendered)


# ── the Grant button names the role it hands out ──────────────────────


def _role(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def test_grant_label_names_the_configured_role():
    assert grant_label(_role("UNLOCK")) == "Grant UNLOCK"


@pytest.mark.parametrize("role", [None])
def test_grant_label_falls_back_when_no_role_resolves(role):
    # No dial set, or the dial points at a role that has since been deleted.
    assert grant_label(role) == GRANT_LABEL_DEFAULT


@pytest.mark.parametrize(
    "stage2_on, label",
    [
        # With Stage 2 on the press approves and grants nothing (2026-10-01).
        pytest.param(True, views.APPROVE_LABEL, id="stage2-approves"),
        pytest.param(False, "Grant UNLOCK", id="stage2-off-grants"),
    ],
)
def test_level_5_button_label_says_what_the_press_does(stage2_on, label):
    assert views.level_5_button_label(_role("UNLOCK"), stage2_on=stage2_on) == label


def test_grant_label_truncates_past_the_discord_ceiling():
    label = grant_label(_role("R" * 200))
    assert len(label) == 80
    assert label.endswith("\u2026")


def test_grant_label_keeps_a_role_that_exactly_fits():
    # "Grant " is six characters, so a 74-character name lands on the ceiling.
    label = grant_label(_role("R" * 74))
    assert len(label) == 80
    assert "\u2026" not in label


def test_views_carry_the_label_onto_the_button():
    # DynamicItem wraps the real button; the label lives on the wrapped item.
    assert ReviewCardView(1, "Grant UNLOCK").children[0].item.label == "Grant UNLOCK"
    assert Level5PromotionView(2, "Grant UNLOCK").children[0].item.label == "Grant UNLOCK"


def test_views_default_to_the_generic_label():
    assert ReviewCardView(1).children[0].item.label == GRANT_LABEL_DEFAULT
    assert Level5PromotionView(2).children[0].item.label == GRANT_LABEL_DEFAULT


# ── the Grant press: role safety gate + audit row ─────────────────────
#
# 2026-09-03: a Level 5 Grant press handed out STAFF because the grant dial
# pointed at it, and the only trace was Discord's own audit log. The press now
# refuses a role no member should get by routine approval, and leaves a row.

GRANT_ROLE_ID = 777
ACTOR_ID = 11


@dataclass(order=True)
class _GrantRole:
    position: int
    id: int = field(default=GRANT_ROLE_ID, compare=False)
    name: str = field(default="UNLOCK", compare=False)
    managed: bool = field(default=False, compare=False)
    default: bool = field(default=False, compare=False)
    perms: dict = field(default_factory=dict, compare=False)

    def __post_init__(self):
        self.permissions = SimpleNamespace(**self.perms)

    def is_default(self):
        return self.default


def _grant_setup(ctx, monkeypatch, role):
    monkeypatch.setattr(views, "_grant_role_id", lambda _ctx, _gid: GRANT_ROLE_ID)
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_role = MagicMock(return_value=role)
    guild.me = SimpleNamespace(top_role=_GrantRole(position=100, name="DK"))
    target = MagicMock()
    target.id = MEMBER_ID
    target.add_roles = AsyncMock()
    actor = MagicMock()
    actor.id = ACTOR_ID
    return guild, target, actor


def _grant_audit_rows(ctx) -> list[sqlite3.Row]:
    with ctx.open_db() as conn:
        return conn.execute(
            "SELECT * FROM audit_log WHERE guild_id = ? AND action = ?",
            (GUILD_ID, views.GRANT_AUDIT_ACTION),
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [
        pytest.param(_GrantRole(position=5, name="STAFF", perms={"manage_messages": True}), id="elevated"),
        pytest.param(_GrantRole(position=5, name="STAFF", perms={"administrator": True}), id="admin"),
        pytest.param(_GrantRole(position=150, name="STAFF"), id="above-bot"),
        pytest.param(_GrantRole(position=5, name="Booster", managed=True), id="managed"),
        pytest.param(_GrantRole(position=0, name="@everyone", default=True), id="everyone"),
    ],
)
async def test_grant_refuses_a_blocked_role_and_names_it(ctx, monkeypatch, role):
    guild, target, actor = _grant_setup(ctx, monkeypatch, role)

    ok, msg = await views._do_grant_role(ctx, guild, target, actor)

    assert ok is False
    assert msg.startswith("❌")
    assert role.name in msg  # the mod needs to know which role to fix
    assert "dashboard" in msg
    target.add_roles.assert_not_awaited()
    assert _grant_audit_rows(ctx) == []


# ── the Level 5 press approves into Stage 2 (Ben, 2026-10-01) ──────────
#
# NSFW unlocks only from the greeter's Stage 2 card: while Stage 2 is on the
# press must never add the role itself, whatever happened to the card. Only a
# dark Stage 2 keeps the old grant, so the button works on guilds without
# intake.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, grants, says",
    [
        pytest.param("opened", False, "Approved", id="opened-grants-nothing"),
        pytest.param("not_opted_in", False, "Nothing granted", id="skip-grants-nothing"),
        pytest.param("failed", False, "Nothing granted", id="failure-grants-nothing"),
        pytest.param("dark", True, "Access granted.", id="stage2-off-grants-as-before"),
    ],
)
async def test_level_5_press_grants_only_while_stage2_is_off(
    ctx, monkeypatch, outcome, grants, says
):
    import bot_modules.onboarding.intake.views as intake_views

    guild, target, actor = _grant_setup(ctx, monkeypatch, _GrantRole(position=5))
    guild.get_member = MagicMock(return_value=target)
    actor = MagicMock(spec=discord.Member)
    actor.id = ACTOR_ID
    ctx.member_is_mod = MagicMock(return_value=True)
    interaction = MagicMock()
    interaction.guild = guild
    interaction.user = actor
    interaction.client.ctx = ctx
    interaction.response.defer = AsyncMock()
    stage2 = AsyncMock(return_value=outcome)
    monkeypatch.setattr(intake_views, "post_stage2_card", stage2)
    replies: list[str] = []

    async def _reply(_interaction, msg):
        replies.append(msg)

    monkeypatch.setattr(views, "_safe_ephemeral", _reply)

    await views._grant_role_only(interaction, MEMBER_ID)

    assert target.add_roles.await_count == (1 if grants else 0)
    # The live grant role is what the card's NSFW access line hands out.
    assert stage2.await_args.args[3] == GRANT_ROLE_ID
    assert replies and replies[0].startswith(says)


@pytest.mark.asyncio
async def test_grant_hands_out_an_ordinary_role_and_audits_it(ctx, monkeypatch):
    role = _GrantRole(position=5)
    guild, target, actor = _grant_setup(ctx, monkeypatch, role)

    ok, _msg = await views._do_grant_role(ctx, guild, target, actor)

    assert ok is True
    target.add_roles.assert_awaited_once()
    rows = _grant_audit_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["actor_id"] == ACTOR_ID
    assert rows[0]["target_id"] == MEMBER_ID
    assert json.loads(rows[0]["extra"]) == {"role_id": GRANT_ROLE_ID, "role_name": "UNLOCK"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(discord.Forbidden(MagicMock(status=403), "no"), id="forbidden"),
        pytest.param(discord.HTTPException(MagicMock(status=500), "hiccup"), id="http"),
    ],
)
async def test_grant_that_discord_refuses_writes_no_audit_row(ctx, monkeypatch, error):
    guild, target, actor = _grant_setup(ctx, monkeypatch, _GrantRole(position=5))
    target.add_roles = AsyncMock(side_effect=error)

    ok, _msg = await views._do_grant_role(ctx, guild, target, actor)

    assert ok is False
    assert _grant_audit_rows(ctx) == []


@pytest.mark.asyncio
async def test_grant_still_reports_success_when_the_audit_write_fails(ctx, monkeypatch):
    """The role is already granted by the time the audit row is written; a DB
    hiccup there must not raise back into _resolve_card ("Never raises") and
    strand the card unresolved despite the grant having gone through."""
    guild, target, actor = _grant_setup(ctx, monkeypatch, _GrantRole(position=5))
    monkeypatch.setattr(
        views, "write_audit", MagicMock(side_effect=sqlite3.OperationalError("locked"))
    )

    ok, msg = await views._do_grant_role(ctx, guild, target, actor)

    assert ok is True
    assert msg == "Access granted."
    target.add_roles.assert_awaited_once()
