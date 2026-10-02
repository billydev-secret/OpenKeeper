"""Tests for the embed builders in ``bot_modules.moderation.jail.embeds``.

Each builder takes plain dicts / lists / strings and returns a
``discord.Embed``; tests assert on the embed's fields, color, title, and
footer rather than serializing — fast, deterministic, and gives a useful
backtrace when the cog's expected format drifts.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from bot_modules.moderation.jail.embeds import (
    DEFAULT_POLICIES_PAGE_SIZE,
    DEFAULT_POLICY_DESC_PREVIEW,
    build_adopted_policies_embed,
    build_jail_audit_embed,
    build_modinfo_embed,
    build_policy_ballot_embed,
    build_policy_close_embed,
    build_policy_list_embed,
    build_policy_proposal_embed,
    build_policy_vote_initial_embed,
    build_policy_vote_update_embed,
    build_ticket_open_embed,
    build_ticket_panel_embed,
    build_warning_audit_embed,
    build_warning_revoke_audit_embed,
    build_warning_threshold_embed,
    warning_threshold_ping,
)
from bot_modules.shared.embeds import (
    MOD_INFO,
    MOD_JAIL,
    MOD_POLICY,
    MOD_SUCCESS,
    MOD_TICKET,
    MOD_WARNING,
)
from bot_modules.core.branding import SECTION_SPACER


def _unspaced(value: str | None) -> str:
    """A field value without the trailing spacer ``apply_section_spacing`` adds.

    Stacked fields carry ``SECTION_SPACER`` for breathing room
    (docs/embed_style_guide.md § Section spacing). These tests assert content.
    """
    text = value or ""
    return text[: -len(SECTION_SPACER)] if text.endswith(SECTION_SPACER) else text


def _fv(embed, name):
    """The named field's value, spacer removed."""
    field = _field_by_name(embed, name)
    return None if field is None else _unspaced(field.value)



def _field_by_name(embed, name):
    """Return the embed field whose name equals ``name`` (or None)."""
    for f in embed.fields:
        if f.name == name:
            return f
    return None


# ── build_policy_vote_initial_embed ───────────────────────────────────


def test_initial_vote_embed_zero_voters():
    embed = build_policy_vote_initial_embed(
        channel_name="policy-test", vote_text="No bots in #general", eligible_ids=[],
    )
    assert embed.title == "🗳️ Policy Vote: policy-test"
    assert embed.color is not None and embed.color.value == MOD_POLICY
    assert _fv(embed, "Votes Cast") == "0/0"
    assert _fv(embed, "Status") == "🗳️ Voting"
    assert _fv(embed, "⏳ Awaiting") == "—"


def test_initial_vote_embed_with_small_eligible_roster():
    embed = build_policy_vote_initial_embed(
        channel_name="p1", vote_text="text", eligible_ids=[1, 2, 3], name_fn=_names,
    )
    assert _fv(embed, "Votes Cast") == "0/3"
    awaiting = _fv(embed, "⏳ Awaiting")
    assert "Ada" in awaiting and "Grace" in awaiting and "Alan" in awaiting
    assert "more" not in awaiting  # no overflow


def test_initial_vote_embed_truncates_on_characters_not_entries():
    """The roster is capped by accumulated CHARACTERS, not by a fixed count.

    The old rule showed 25 entries whatever their length, so 25 long display
    names blew past Discord's 1024-character field ceiling and the whole field
    was dropped — the failure mode a fixed cap cannot see. Long names must
    therefore truncate EARLIER than short ones.
    """
    long_names = {uid: "N" * 60 for uid in range(100)}
    embed = build_policy_vote_initial_embed(
        channel_name="p1",
        vote_text="text",
        eligible_ids=list(range(100)),
        name_fn=lambda uid: long_names[uid],
    )
    awaiting = _fv(embed, "⏳ Awaiting")
    assert len(awaiting) <= 1024
    assert "more" in awaiting

    short = build_policy_vote_initial_embed(
        channel_name="p1",
        vote_text="text",
        eligible_ids=list(range(100)),
        name_fn=lambda uid: "x",
    )
    # Same roster size, far shorter names: more of them fit.
    assert _fv(short, "⏳ Awaiting").count(",") > awaiting.count(",")


def test_vote_embeds_never_emit_a_raw_mention():
    """The rule is the rule even in a private mod channel.

    A ``<@id>`` in an embed is resolved by the READING client from its own
    cache. In the mod channel it mostly resolves, which is why this went
    unnoticed — but a mod who has never seen a colleague, or a voter who has
    since left the guild, got a bare number.
    """
    initial = build_policy_vote_initial_embed(
        channel_name="p1", vote_text="text", eligible_ids=[1, 2, 3], name_fn=_names,
    )
    update = build_policy_vote_update_embed(
        policy_title="p1",
        vote_text="text",
        yes_ids=[1],
        no_ids=[2],
        abstain_ids=[3],
        awaiting_ids=[],
        name_fn=_names,
    )
    for embed in (initial, update):
        rendered = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
        assert "<@" not in rendered


def test_initial_vote_embed_field_order():
    """The update path uses ``set_field_at`` with hard-coded indices — the
    initial embed must keep the same order so updates land in the right slot."""
    embed = build_policy_vote_initial_embed(
        channel_name="p", vote_text="t", eligible_ids=[],
    )
    names = [f.name for f in embed.fields]
    assert names == [
        "📜 Policy Text", "Votes Cast", "Status",
        "✅ Yes", "❌ No", "➖ Abstain", "⏳ Awaiting",
    ]


# ── build_policy_vote_update_embed ────────────────────────────────────


def test_update_embed_running_tally():
    embed = build_policy_vote_update_embed(
        policy_title="Title", vote_text="Text",
        yes_ids=[1], no_ids=[], abstain_ids=[2], awaiting_ids=[3],
        name_fn=_names,
    )
    assert embed.title == "🗳️ Policy Vote: Title"
    assert embed.color is not None and embed.color.value == MOD_POLICY
    assert _fv(embed, "Status") == "🗳️ Voting"
    assert _fv(embed, "Votes Cast") == "2/3"  # 1 yes + 1 abstain
    # Names alone: the roster is read, never pasted out of.
    assert _fv(embed, "✅ Yes") == "Ada"
    assert _fv(embed, "❌ No") == "—"
    assert _fv(embed, "➖ Abstain") == "Grace"


#: Four real-width snowflakes, so a leaked id is unmistakable in the render.
_SNOWFLAKES = [111111111111111111, 222222222222222222,
               333333333333333333, 444444444444444444]


def _snowflake_names(uid: int) -> str:
    """Digit-free display names, so any digits found came from an id."""
    return dict(zip(_SNOWFLAKES, ["Ada", "Grace", "Alan", "Bea"]))[uid]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: build_policy_vote_initial_embed(
                channel_name="p", vote_text="t", eligible_ids=_SNOWFLAKES,
                name_fn=_snowflake_names,
            ),
            id="initial",
        ),
        pytest.param(
            lambda: build_policy_vote_update_embed(
                policy_title="t", vote_text="t",
                yes_ids=_SNOWFLAKES[:1], no_ids=_SNOWFLAKES[1:2],
                abstain_ids=_SNOWFLAKES[2:3], awaiting_ids=_SNOWFLAKES[3:],
                name_fn=_snowflake_names,
            ),
            id="update",
        ),
    ],
)
def test_vote_cards_print_names_alone_never_a_raw_user_id(build):
    """Ruling 2026-09-09: the mod vote shows names alone, like the ballot.

    It used to append ``Name (`id`)`` under the copyable-id rule in
    docs/embed_style_guide.md. Nothing in the policy flow takes a user id
    (the vote buttons carry a *policy* id), and a policy ticket is run
    privately among mods **or** opened to the general public proposal by
    proposal — so the builder cannot know a member isn't reading. This is
    the test that fails if a later "mod-facing embeds keep the id" pass
    puts them back; `/policy open` denying @everyone by default is not
    evidence the card is mod-only.
    """
    rendered = "\n".join(f"{f.name}: {f.value or ''}" for f in build().fields)

    # "Votes Cast" is a short tally like 1/4; a snowflake is a long digit run.
    assert re.search(r"\d{5,}", rendered) is None, rendered
    for name in ("Ada", "Grace", "Alan", "Bea"):
        assert name in rendered


def test_update_embed_adopted():
    embed = build_policy_vote_update_embed(
        policy_title="t", vote_text="t",
        yes_ids=[1, 2], no_ids=[], abstain_ids=[3], awaiting_ids=[],
        outcome="adopted",
    )
    assert embed.color is not None and embed.color.value == MOD_SUCCESS
    assert _fv(embed, "Status") == "✅ Adopted"
    assert _fv(embed, "Votes Cast") == "3/3"


def test_update_embed_rejected():
    embed = build_policy_vote_update_embed(
        policy_title="t", vote_text="t",
        yes_ids=[1], no_ids=[2], abstain_ids=[], awaiting_ids=[],
        outcome="rejected",
    )
    assert _fv(embed, "Status") == "❌ Rejected"


def test_update_embed_handles_empty_vote_text():
    """When the policy has no vote_text, the field still renders something."""
    embed = build_policy_vote_update_embed(
        policy_title="t", vote_text="",
        yes_ids=[], no_ids=[], abstain_ids=[], awaiting_ids=[1],
    )
    assert _fv(embed, "📜 Policy Text") == "(no text)"


# ── build_policy_list_embed ───────────────────────────────────────────


def _policy_row(pid, title, description, passed_at=1700000000):
    return {
        "id": pid, "title": title, "description": description, "passed_at": passed_at,
    }


def test_policy_list_embed_minimal():
    policies = [_policy_row(1, "Be kind", "Short description")]
    embed = build_policy_list_embed(policies)
    assert embed.title == "📋 Passed Policies"
    field = _field_by_name(embed, "#1 — Be kind")
    assert field is not None
    assert "Short description" in field.value
    assert "<t:1700000000:d>" in field.value
    # Not truncated → no footer
    assert embed.footer.text is None or embed.footer.text == ""


def test_policy_list_embed_truncates_long_description():
    long_desc = "x" * 200
    policies = [_policy_row(1, "T", long_desc)]
    embed = build_policy_list_embed(policies)
    field = _field_by_name(embed, "#1 — T")
    assert "…" in field.value
    # The preview length is a budget the ellipsis is spent from, not a slice
    # taken before it: the rendered preview is 99 characters plus the marker,
    # never 100 plus the marker. It changed when the preview started being
    # escaped first — a description of pure markdown doubles, so the number
    # that matters is how long the *result* is, and a budget you can overrun
    # by one character is not a budget.
    preview = field.value.split("\n")[0]
    assert len(preview) == DEFAULT_POLICY_DESC_PREVIEW
    assert preview.count("x") == DEFAULT_POLICY_DESC_PREVIEW - 1


def test_policy_list_embed_paginates_with_footer():
    policies = [_policy_row(i, f"P{i}", "desc") for i in range(30)]
    embed = build_policy_list_embed(policies)
    # Only DEFAULT_POLICIES_PAGE_SIZE rows shown
    assert len(embed.fields) == DEFAULT_POLICIES_PAGE_SIZE
    assert embed.footer.text == (
        f"Showing {DEFAULT_POLICIES_PAGE_SIZE} of {len(policies)} policies."
    )


def test_policy_list_embed_no_pagination_when_zero_size():
    """``page_size=0`` shows all policies without a footer (useful for testing)."""
    policies = [_policy_row(i, f"P{i}", "x") for i in range(5)]
    embed = build_policy_list_embed(policies, page_size=0)
    assert len(embed.fields) == 5
    assert embed.footer.text is None or embed.footer.text == ""


def test_policy_list_embed_empty():
    embed = build_policy_list_embed([])
    assert len(embed.fields) == 0


# ── build_ticket_panel_embed ──────────────────────────────────────────


def test_ticket_panel_embed_static_content():
    embed = build_ticket_panel_embed()
    assert embed.title == "📩 Support Tickets"
    assert "private ticket" in embed.description
    assert embed.color is not None and embed.color.value == MOD_TICKET


# ── build_ticket_open_embed ───────────────────────────────────────────


def test_ticket_open_embed_basic():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    embed = build_ticket_open_embed(
        ticket_id=42, description="My issue", opener_name="Ada", now=now,
    )
    assert embed.title == "🎫 Ticket #42"
    assert embed.description == "My issue"
    assert embed.timestamp == now
    assert _fv(embed, "Opened by") == "Ada"
    assert _fv(embed, "Status") == "🟢 Open"
    assert "archived" in embed.footer.text


def test_ticket_open_embed_defaults_timestamp_to_now():
    embed = build_ticket_open_embed(
        ticket_id=1, description="d", opener_name="Ada",
    )
    assert embed.timestamp is not None


# ── build_modinfo_embed ───────────────────────────────────────────────


def test_modinfo_embed_minimal_no_warnings_no_tickets():
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    embed = build_modinfo_embed(
        user_label="u#1",
        user_avatar_url=None,
        account_created=created,
        account_age_days=400,
        joined_at=None,
        xp_row=None,
        watcher_count=0,
        active_jail=None,
        jail_history=[],
        warns=[],
        tickets=[],
        last_seen_ts=None,
        top_channels=[],
        msgs_30d_total=0,
        ts_formatter=lambda ts: f"t{ts}" if ts else "N/A",
    )
    assert embed.title == "ℹ️ Mod Info — u#1"
    assert embed.color is not None and embed.color.value == MOD_INFO
    # Avatar None → no thumbnail
    assert embed.thumbnail.url is None or embed.thumbnail.url == ""
    assert _fv(embed, "⭐ Level") == "No XP recorded"
    assert _fv(embed, "🔍 Watch List") == "Not watched"
    assert _fv(embed, "🔒 Jail") == "Not currently jailed"
    assert "Last seen: Never" in _field_by_name(
        embed, "💬 Activity — 0 msgs (30d)"
    ).value


def test_modinfo_embed_full_kitchen_sink():
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    joined = datetime(2025, 6, 1, tzinfo=timezone.utc)
    embed = build_modinfo_embed(
        user_label="u#1",
        user_avatar_url="https://example.com/avatar.png",
        account_created=created,
        account_age_days=500,
        joined_at=joined,
        xp_row={"level": 7, "total_xp": 12345.0},
        watcher_count=2,
        active_jail={
            "created_at": 1000, "expires_at": 2000, "reason": "spam",
        },
        jail_history=[
            {"status": "active", "created_at": 1000},
            {"status": "released", "created_at": 500, "release_reason": "served"},
        ],
        warns=[
            {"id": 1, "revoked": False, "reason": "bad", "created_at": 100},
            {"id": 2, "revoked": True, "reason": "x", "created_at": 200},
        ],
        tickets=[
            {"id": 10, "status": "open", "created_at": 1500},
            {"id": 9, "status": "closed", "created_at": 1400},
            {"id": 8, "status": "deleted", "created_at": 1300},
        ],
        last_seen_ts=1900,
        top_channels=[
            {"channel_id": 111, "cnt": 50},
            {"channel_id": 222, "cnt": 30},
        ],
        msgs_30d_total=80,
        ts_formatter=lambda ts: f"t{ts}" if ts else "N/A",
    )
    assert embed.thumbnail.url == "https://example.com/avatar.png"
    joined_ts = int(joined.timestamp())
    account = _fv(embed, "👤 Account")
    # Time since joining, not the date — the client shows the date on hover.
    assert f"Joined: <t:{joined_ts}:R>" in account
    assert f"<t:{joined_ts}:D>" not in account
    assert "Level **7**" in _fv(embed, "⭐ Level")
    assert "12,345 XP" in _fv(embed, "⭐ Level")
    assert "**2 mods watching**" in _fv(embed, "🔍 Watch List")

    jail_field = _fv(embed, "🔒 Jail")
    assert "Currently jailed" in jail_field
    assert "Expires:" in jail_field
    assert "spam" in jail_field
    assert "**Past jails:** 1" in jail_field

    warn_field = _fv(embed, "⚠️ Warnings")
    assert "**Active:** 1 / **Total:** 2" in warn_field

    ticket_field = _fv(embed, "📩 Tickets")
    assert "**Open:** 1 / **Closed:** 2" in ticket_field
    assert "#10" in ticket_field  # most recent

    assert embed.image.url == "attachment://modinfo_activity.png"


def _modinfo_with(*, active_jail=None, jail_history=(), warns=()):
    """A ``/modinfo`` embed with only the reason-bearing inputs varied."""
    return build_modinfo_embed(
        user_label="u#1", user_avatar_url=None,
        account_created=datetime(2024, 1, 1, tzinfo=timezone.utc),
        account_age_days=1, joined_at=None, xp_row=None, watcher_count=0,
        active_jail=active_jail, jail_history=list(jail_history),
        warns=list(warns), tickets=[], last_seen_ts=None, top_channels=[],
        msgs_30d_total=0, ts_formatter=lambda ts: f"t{ts}" if ts else "N/A",
    )


def test_modinfo_escapes_the_same_reason_columns_the_audit_cards_do():
    """``/modinfo`` reads back reasons the audit embeds already escape.

    The file disagreed with itself: ``build_jail_audit_embed`` escaped the
    jail reason and this summary printed the same column raw, so a `**` typed
    into ``/jail`` bolded part of a card that reads as the bot's own summary
    of a member. Same column, same surface, same answer.
    """
    embed = _modinfo_with(
        active_jail={"created_at": 1, "reason": "**spam**"},
        jail_history=[
            {"status": "active", "created_at": 1},
            {"status": "released", "created_at": 0, "release_reason": "||served||"},
        ],
        warns=[{"id": 1, "revoked": False, "reason": "__rude__", "created_at": 2}],
    )
    jail_field = _fv(embed, "🔒 Jail")
    warn_field = _fv(embed, "⚠️ Warnings")
    assert "**spam**" not in jail_field and "spam" in jail_field
    assert "||served||" not in jail_field and "served" in jail_field
    assert "__rude__" not in warn_field and "rude" in warn_field


def test_modinfo_reason_fields_stay_under_the_field_cap():
    """Nothing caps a reason where it is typed, and escaping doubles it.

    A slash-command string parameter takes far more than a field holds, and
    the summary stacks three warning reasons into one value — so the cap has
    to be enforced here or the whole card comes back a 400.
    """
    embed = _modinfo_with(
        active_jail={"created_at": 1, "expires_at": 2, "reason": "*" * 4000},
        jail_history=[
            {"status": "active", "created_at": 1},
            {"status": "released", "created_at": 0, "release_reason": "*" * 4000},
        ],
        warns=[
            {"id": i, "revoked": False, "reason": "*" * 4000, "created_at": i}
            for i in range(5)
        ],
    )
    for name in ("🔒 Jail", "⚠️ Warnings"):
        value = _field_by_name(embed, name).value
        assert len(value) <= 1024, f"{name} is {len(value)} chars"
        assert not value.rstrip("…").endswith("\\")


def test_modinfo_embed_xp_source_breakdown():
    """Per-source XP renders under the level line, compacted, non-zero only."""
    embed = build_modinfo_embed(
        user_label="u", user_avatar_url=None,
        account_created=datetime(2024, 1, 1, tzinfo=timezone.utc), account_age_days=1,
        joined_at=None,
        xp_row={"level": 12, "total_xp": 45300.0},
        xp_by_source={
            "text": 31200.0, "voice": 9800.0, "reply": 3100.0,
            "image_react": 1200.0, "grant": 0.0,
        },
        watcher_count=0, active_jail=None, jail_history=[],
        warns=[], tickets=[], last_seen_ts=None,
        top_channels=[], msgs_30d_total=0,
        ts_formatter=lambda ts: "t",
    )
    level = _fv(embed, "⭐ Level")
    assert "Level **12**" in level
    assert "💬 Text 31.2k" in level
    assert "🔊 Voice 9.8k" in level
    assert "↩️ Reply 3.1k" in level
    assert "🖼 React 1.2k" in level
    # Zero-value sources are omitted.
    assert "Grant" not in level


def test_modinfo_embed_no_xp_source_breakdown_when_absent():
    """No xp_by_source → Level field is just the single summary line."""
    embed = build_modinfo_embed(
        user_label="u", user_avatar_url=None,
        account_created=datetime(2024, 1, 1, tzinfo=timezone.utc), account_age_days=1,
        joined_at=None, xp_row={"level": 3, "total_xp": 200.0},
        watcher_count=0, active_jail=None, jail_history=[],
        warns=[], tickets=[], last_seen_ts=None,
        top_channels=[], msgs_30d_total=0,
        ts_formatter=lambda ts: "t",
    )
    assert _fv(embed, "⭐ Level") == "Level **3** · 200 XP"


def test_modinfo_embed_singular_watcher_label():
    """One watcher → "1 mod watching", not "1 mods watching"."""
    embed = build_modinfo_embed(
        user_label="u", user_avatar_url=None,
        account_created=datetime(2024, 1, 1, tzinfo=timezone.utc), account_age_days=1,
        joined_at=None, xp_row=None, watcher_count=1,
        active_jail=None, jail_history=[],
        warns=[], tickets=[], last_seen_ts=None,
        top_channels=[], msgs_30d_total=0,
        ts_formatter=lambda ts: "t",
    )
    assert "1 mod watching" in _fv(embed, "🔍 Watch List")


# ── Audit embeds ──────────────────────────────────────────────────────


def test_jail_audit_embed_with_reason():
    embed = build_jail_audit_embed(
        target_name="Ada", moderator_name="Bea",
        duration_text="2h", reason="spam",
    )
    assert embed.title == "🔒 Member Jailed"
    assert embed.color is not None and embed.color.value == MOD_JAIL
    assert "Ada jailed by Bea" in embed.description
    assert "**Duration:** 2h" in embed.description
    assert "**Reason:** spam" in embed.description


def test_jail_audit_embed_no_reason():
    embed = build_jail_audit_embed(
        target_name="Ada", moderator_name="Bea", duration_text="Indefinite",
    )
    assert "Reason:" not in embed.description


def test_warning_audit_embed_slash_command_path():
    """The /warn command supplies just reason; no notes or source link."""
    embed = build_warning_audit_embed(
        target_name="Ada", moderator_name="Bea",
        active_count=3, reason="rule 1",
    )
    assert embed.title == "⚠️ Warning Issued"
    assert "**Reason:** rule 1" in embed.description
    assert "**Active warnings:** 3" in embed.description
    assert "Jump to source" not in embed.description
    assert embed.color is not None and embed.color.value == MOD_WARNING


def test_warning_audit_embed_context_menu_path_includes_notes_and_link():
    embed = build_warning_audit_embed(
        target_name="Ada", moderator_name="Bea",
        active_count=2, reason="bad msg", notes="repeat offender",
        source_jump_url="https://discord.com/.../msg",
    )
    assert "**Notes:** repeat offender" in embed.description
    assert "Jump to source message" in embed.description


def test_warning_audit_embed_escapes_notes_like_its_reason_sibling():
    """``notes`` sat unescaped two lines below an escaped ``reason``.

    No caller passes ``notes=`` from ``src/`` today, so this is a trap rather
    than a live bug — and that is exactly why it needs pinning. An unescaped
    parameter beside an escaped one reads to the next reader as a considered
    exception, so the first caller to wire it up inherits the gap silently.
    """
    embed = build_warning_audit_embed(
        target_name="Ada", moderator_name="Bea", active_count=1,
        reason="**r**", notes="**n** and ||hidden||",
    )
    assert "**n**" not in embed.description and "||hidden||" not in embed.description
    assert "n" in embed.description and "hidden" in embed.description


def test_warning_threshold_embed_carries_no_ping():
    """The ping must NOT be in the embed.

    These two tests used to assert the opposite, which is how the alert came to
    notify nobody for as long as it did: a mention inside an embed is resolved
    by the reading client and fires no notification. The roles ride in the
    message ``content=`` now. → embed_style_guide.md § Mentions, pings
    """
    embed = build_warning_threshold_embed(target_name="Ada", active_count=5)
    assert embed.title == "🚨 Warning Threshold Reached"
    assert embed.description is not None
    assert "<@&" not in embed.description
    assert "**5**" in embed.description


@pytest.mark.parametrize(
    ("role_ids", "expected"),
    [
        ([100, 200], "<@&100> <@&200>"),
        ([100], "<@&100>"),
        ([], ""),
    ],
)
def test_warning_threshold_ping_builds_the_content_line(role_ids, expected):
    assert warning_threshold_ping(role_ids) == expected


def test_warning_revoke_audit_embed():
    embed = build_warning_revoke_audit_embed(
        warning_id=42, target_name="Ada", moderator_name="Bea",
        active_count=1, reason="appealed",
    )
    assert embed.title == "✅ Warning Revoked"
    assert "#42" in embed.description
    assert "**Reason:** appealed" in embed.description
    assert embed.color is not None and embed.color.value == MOD_SUCCESS


def test_warning_revoke_audit_embed_without_reason():
    embed = build_warning_revoke_audit_embed(
        warning_id=42, target_name="Ada", moderator_name="Bea",
        active_count=1,
    )
    assert "**Reason:**" not in embed.description


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: build_ticket_open_embed(
            ticket_id=1, description="d", opener_name="Ada"), id="ticket-open"),
        pytest.param(lambda: build_jail_audit_embed(
            target_name="Ada", moderator_name="Bea", duration_text="2h"), id="jail-audit"),
        pytest.param(lambda: build_warning_audit_embed(
            target_name="Ada", moderator_name="Bea", active_count=1), id="warning-audit"),
        pytest.param(lambda: build_warning_threshold_embed(
            target_name="Ada", active_count=5), id="warning-threshold"),
        pytest.param(lambda: build_warning_revoke_audit_embed(
            warning_id=1, target_name="Ada", moderator_name="Bea", active_count=0),
            id="warning-revoke"),
        pytest.param(lambda: build_policy_close_embed(
            title="Foo", moderator_name="Bea"), id="policy-close"),
    ],
)
def test_audit_and_ticket_builders_name_members_never_mention_them(build):
    """These took ``*_mention`` strings until 2026-09-14, so every caller
    passed ``user.mention`` — a bare number on a mod-log card for any mod who
    hadn't cached the member, and always for one who had left. They take
    resolved names now (``build_name_fn``)."""
    embed = build()
    text = "\n".join(
        [embed.description or ""] + [f"{f.name} {f.value}" for f in embed.fields]
    )
    assert "Ada" in text or "Bea" in text
    assert "<@" not in text


# ── Policy proposal embeds ────────────────────────────────────────────


def test_policy_proposal_embed_basic():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    embed = build_policy_proposal_embed(
        policy_id=1, title="Foo", description="Bar",
        proposer="Ada", now=now,
    )
    assert embed.title == "📋 Policy Proposal #1: Foo"
    assert embed.description == "Bar"
    assert embed.timestamp == now
    assert _fv(embed, "Status") == "💬 Open for Discussion"
    assert "Use /policy vote" in embed.footer.text


def test_policy_proposal_embed_defaults_timestamp():
    embed = build_policy_proposal_embed(
        policy_id=1, title="Foo", description="Bar", proposer="Ada",
    )
    assert embed.timestamp is not None


def test_policy_proposal_embed_names_the_proposer_never_mentions_them():
    """The card can be opened to every member from its own button.

    It used to render the proposer as ``user.mention``, which was survivable
    while the channel was mod-only and stopped being survivable the moment
    the same card could go public — an embed mention is resolved by the
    reading client's own cache.
    """
    embed = build_policy_proposal_embed(
        policy_id=1, title="Foo", description="Bar", proposer="Ada",
    )
    assert _fv(embed, "Proposed by") == "Ada"
    assert "<@" not in "\n".join(
        f"{f.name} {f.value}" for f in embed.fields
    )


@pytest.mark.parametrize("visibility,shown", [
    (None, "🔒 Mods only"),          # a row written before migration 219
    ("mods", "🔒 Mods only"),
    ("public", "🌐 Open to members"),
    ("nonsense", "🔒 Mods only"),    # unknown reads as private, never public
])
def test_policy_proposal_embed_states_who_can_see_the_channel(visibility, shown):
    embed = build_policy_proposal_embed(
        policy_id=1, title="Foo", description="Bar", proposer="Ada",
        visibility=visibility,
    )
    assert _fv(embed, "Visibility") == shown


def test_policy_close_embed_with_reason():
    embed = build_policy_close_embed(
        title="Foo", moderator_name="Bea", reason="rescinded",
    )
    assert embed.title == "📋 Policy Proposal Closed"
    assert "**Foo**" in embed.description
    assert _fv(embed, "Reason") == "rescinded"


def test_policy_close_embed_no_reason_no_field():
    embed = build_policy_close_embed(title="Foo", moderator_name="Bea")
    assert _field_by_name(embed, "Reason") is None


def test_adopted_policies_embed_listing():
    embed = build_adopted_policies_embed(
        [
            {"title": "P1", "description": "d1"},
            {"title": "P2", "description": "d2"},
        ]
    )
    assert embed.title == "📜 Adopted Policies from This Proposal"
    assert _fv(embed, "P1") == "d1"
    assert _fv(embed, "P2") == "d2"
    assert embed.color is not None and embed.color.value == MOD_SUCCESS


def test_adopted_policies_embed_truncates_long_descriptions():
    """Discord field values cap at 1024 chars — the builder must truncate."""
    long_desc = "x" * 2000
    embed = build_adopted_policies_embed([{"title": "P", "description": long_desc}])
    assert len(_fv(embed, "P")) == 1024


def test_adopted_policies_embed_escapes_member_markdown():
    """This card goes to the policy channel, not to one moderator."""
    embed = build_adopted_policies_embed(
        [{"title": "**Rule**", "description": "||hidden|| and **bold**"}]
    )
    field = embed.fields[0]
    assert "**Rule**" not in field.name
    assert "Rule" in field.name
    assert "**bold**" not in field.value and "||hidden||" not in field.value
    # Escaped, not dropped — the policy still reads as written.
    assert "bold" in field.value and "hidden" in field.value


@pytest.mark.parametrize(
    "builder",
    [
        lambda title: build_adopted_policies_embed([{"title": title, "description": "d"}]),
        lambda title: build_policy_list_embed([_policy_row(1, title, "d")]),
    ],
    ids=["adopted", "list"],
)
def test_policy_field_name_survives_an_all_markdown_title(builder):
    """Escaping doubles a title of markdown characters; 256 is a hard cap.

    A title is capped at 200 characters when it is written, which sits safely
    under Discord's 256-char field-name ceiling — until every one of those 200
    characters is a ``*``, which ``escape_markdown`` turns into 400. Discord
    rejects the whole embed with a 400, so escaping without re-capping trades
    a formatting bug for an outage on exactly the titles the escape defends
    against.
    """
    embed = builder("*" * 200)
    name = embed.fields[0].name
    assert len(name) <= 256, f"field name is {len(name)} chars, Discord caps at 256"
    # It was genuinely escaped, not simply thrown away.
    assert "\\*" in name
    # And a truncated name never ends on a dangling backslash, which would
    # escape the ellipsis instead of the character it was meant for.
    assert not name.rstrip("…").endswith("\\")


# ── The rest of the policy surfaces, on the same contract ─────────────
#
# The field-name case above was found first and fixed alone, which left four
# more surfaces carrying the identical trap: a title capped at 200 raw
# characters reaching a 256-character ceiling once escaping has doubled it.
# An embed *title* is the same 256 as a field name, and the vote and proposal
# cards both prefix the bot's own text to it, so the title's real allowance is
# smaller still. These pin every one of them rather than the one that bit.


@pytest.mark.parametrize(
    ("builder", "prefix"),
    [
        (
            lambda title: build_policy_proposal_embed(
                policy_id=12, title=title, description="d", proposer="Ada",
            ),
            "📋 Policy Proposal #12: ",
        ),
        (
            lambda title: build_policy_vote_initial_embed(
                channel_name=title, vote_text="t", eligible_ids=[],
            ),
            "🗳️ Policy Vote: ",
        ),
        (
            lambda title: build_policy_vote_update_embed(
                policy_title=title, vote_text="t",
                yes_ids=[], no_ids=[], abstain_ids=[], awaiting_ids=[],
            ),
            "🗳️ Policy Vote: ",
        ),
    ],
    ids=["proposal", "vote-initial", "vote-update"],
)
def test_policy_embed_title_survives_an_all_markdown_title(builder, prefix):
    embed = builder("*" * 200)
    assert embed.title is not None
    assert len(embed.title) <= 256, f"title is {len(embed.title)} chars"
    # The bot's own prefix is never what gets trimmed away.
    assert embed.title.startswith(prefix)
    assert "\\*" in embed.title
    assert not embed.title.rstrip("…").endswith("\\")


@pytest.mark.parametrize(
    "builder",
    [
        lambda text: build_policy_vote_initial_embed(
            channel_name="p", vote_text=text, eligible_ids=[],
        ),
        lambda text: build_policy_vote_update_embed(
            policy_title="p", vote_text=text,
            yes_ids=[], no_ids=[], abstain_ids=[], awaiting_ids=[],
        ),
        lambda text: build_policy_ballot_embed(question=text),
    ],
    ids=["vote-initial", "vote-update", "ballot"],
)
def test_policy_text_is_escaped_and_fits_the_field(builder):
    """The mod's typed text is escaped at the builder, and then made to fit.

    Both halves matter. The vote card is edited in place on every press, and
    the ballot card re-renders from the DB in a public thread — so a `||spoiler||`
    in the policy text would hide the rest of a card the bot appears to have
    written. And the modal that produces it allows 1000 characters, which is
    under the 1024-character field ceiling only until escaping doubles it.
    """
    field = _field_by_name(builder("*" * 1000), "📜 Policy Text") or _field_by_name(
        builder("*" * 1000), "📜 The Question"
    )
    assert field is not None
    assert len(field.value) <= 1024, f"field value is {len(field.value)} chars"
    assert "\\*" in field.value
    assert not field.value.rstrip("…").endswith("\\")

    plain = builder("||hidden|| **bold**")
    rendered = _fv(plain, "📜 Policy Text") or _fv(plain, "📜 The Question")
    assert "||hidden||" not in rendered and "**bold**" not in rendered
    assert "hidden" in rendered and "bold" in rendered


# ── Community ballot card ─────────────────────────────────────────────


def _names(uid: int) -> str:
    return {1: "Ada", 2: "Grace", 3: "Alan"}.get(uid, f"Member {uid}")


def test_ballot_embed_names_every_voter_and_counts_them():
    embed = build_policy_ballot_embed(
        question="Quiet hours?",
        yes_ids=[1, 2],
        no_ids=[3],
        name_fn=_names,
    )

    assert _fv(embed, "✅ Yes (2)") == "Ada, Grace"
    assert _fv(embed, "❌ No (1)") == "Alan"
    assert _fv(embed, "➖ Abstain (0)") == "—"
    assert _fv(embed, "Votes Cast") == "3"


def test_ballot_embed_never_emits_a_raw_mention():
    """An ``<@id>`` inside an embed is resolved by the *reading* client from
    its own cache, so in a public roster it degrades to a bare number for most
    readers. Every id goes through the resolver."""
    embed = build_policy_ballot_embed(
        question="Quiet hours? <@999>",
        yes_ids=[1],
        no_ids=[2],
        abstain_ids=[3],
        name_fn=_names,
        closes_at=1_700_000_000,
    )

    rendered = "\n".join(
        [embed.title or "", *(f"{f.name}\n{f.value}" for f in embed.fields)]
    )
    # The question is a moderator's own text and is reproduced verbatim; every
    # *voter* is a name.
    assert "<@1>" not in rendered
    assert "<@2>" not in rendered
    assert "<@3>" not in rendered


def test_ballot_embed_says_what_the_rules_are():
    """The footer is the member-facing statement of the three things people
    get wrong: who may vote, what abstaining does, and that a vote is public."""
    footer = (build_policy_ballot_embed(question="q").footer.text or "").lower()

    assert "anyone who can see this thread" in footer
    assert "abstain" in footer
    assert "ties fail" in footer
    assert "public" in footer
    # "Anonymous" must never appear: `policy_ballot_votes` stores the voter,
    # and the card names them in the channel.
    assert "anonym" not in footer


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        pytest.param(None, "🗳️ Open", id="open"),
        pytest.param("passed", "✅ Passed", id="passed"),
        pytest.param("failed", "❌ Failed", id="failed"),
        pytest.param("cancelled", "🚫 Cancelled", id="cancelled"),
    ],
)
def test_ballot_embed_status_tracks_the_outcome(outcome, status):
    embed = build_policy_ballot_embed(question="q", outcome=outcome)

    assert _fv(embed, "Status") == status


def test_ballot_embed_shows_a_deadline_only_while_open():
    open_embed = build_policy_ballot_embed(question="q", closes_at=1_700_000_000)
    closed_embed = build_policy_ballot_embed(
        question="q", closes_at=1_700_000_000, outcome="passed"
    )

    assert _fv(open_embed, "Closes") == "<t:1700000000:R>"
    assert "Closes" not in [f.name for f in closed_embed.fields]


def test_ballot_embed_has_no_deadline_field_when_there_is_no_deadline():
    """`closes_at = 0` is the guild's voting-deadline dial turned off."""
    embed = build_policy_ballot_embed(question="q", closes_at=0)

    assert "Closes" not in [f.name for f in embed.fields]


def test_ballot_voter_lists_are_capped_by_characters():
    """Discord fields stop at 1024 chars and display names vary wildly in
    length, so the mod vote's fixed 25-mention cap does not transfer."""
    long_names = {uid: "N" * 60 for uid in range(200)}
    embed = build_policy_ballot_embed(
        question="q",
        yes_ids=list(range(200)),
        name_fn=lambda uid: long_names[uid],
    )

    value = _fv(embed, "✅ Yes (200)")
    assert len(value) <= 1024
    assert "more" in value
