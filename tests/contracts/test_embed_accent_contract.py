"""Every pure embed builder honors a passed accent and falls back to its default.

One repo-wide contract (CLAUDE.md: new embeds take their color from
``resolve_accent_color``; red/green/etc. survive only where the color is
semantic). Until 2026-07 every game re-tested it with two copy-pasted tests
per builder — ~86 near-identical functions. A new builder now adds one
``case(...)`` row here instead.

Only builders whose color IS the accent belong in this table. Semantic-color
tests ("winner stays green regardless of accent") and resolver-wiring tests
(cogs patching ``resolve_accent_color``) stay in their feature files.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.moderation.jail import embeds as jail_embeds
from bot_modules.music.playlist import embeds as music_playlist_embeds
from bot_modules.word_cloud import embeds as word_cloud_embeds
from bot_modules.core import branding_service
from bot_modules.shared import dm_branding
from bot_modules.economy import service as economy_service
from bot_modules.event_echo import logic as event_echo_logic
from bot_modules.shared import embeds as services_embeds
from bot_modules.onboarding.welcome import service as welcome_service
from bot_modules.survivor import embeds as survivor_embeds
from bot_modules.survivor import reckoning as survivor_reckoning
from bot_modules.survivor.gauntlet import GauntletFate as _Fate
from bot_modules.survivor.gauntlet import ReplayWeek as _ReplayWeek
from bot_modules.survivor.logic import OpenGame as _SurvivorGame

# Deliberately no builder defaults to this value.
ACCENT = discord.Color(0x5A32A8)


def _named(uid):
    """Stand-in ``name_fn`` for builders that name members (never ``<@id>``)."""
    return f"User{uid}"


def case(case_id, build, fallback):
    """One builder under contract: id, ``build(**kw)`` thunk, fallback color.

    ``build`` is called once as ``build(color=ACCENT)`` (accent passthrough)
    and once as ``build()`` (fallback). ``fallback`` is the exact color the
    builder must default to, or None when the feature never had a fallback
    expectation (passthrough-only builders).
    """
    return pytest.param(build, fallback, id=case_id)


def _econ_settings():
    return economy_service.EconSettings(
        currency_emoji="💎", currency_name="gem", currency_plural="gems"
    )


def _welcome_member() -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.mention = "<@1>"
    member.display_name = "Alex"
    member.id = 1
    guild = MagicMock()
    guild.name = "Test Guild"
    guild.member_count = 42
    guild.icon = None
    member.guild = guild
    member.display_avatar.url = "https://cdn.example/a.png"
    return member


CASES = [
    case(
        "event_echo.game_starting",
        lambda **kw: event_echo_logic.build_echo_embed(
            name="Truth or Dare", channel_id=1, url="https://x/1", **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    # ── spin the compliment / mfk ──
    # Spin the Compliment's pairing/wrap-recap cards and MFK's lobby/assignments
    # cards moved off this contract with the Components V2 conversion (wave 3b,
    # "games") — they're bare LayoutViews with no accent colour any more; see
    # tests/games/test_compliment_layouts.py and tests/games/test_mfk_layouts.py.
    # session recap + /games game-status converted to Components V2 cards
    # (no accent) — Components V2 wave 3b, "Games core". Their contract now
    # lives in tests/games/test_games_core_layouts.py.
    # ── two truths and a lie ─────────────────────────────────────────────
    # Two Truths and a Lie converted to Components V2 (2026-09-30): its five
    # rows are gone rather than converted.
    # Hot Takes converted to Components V2 (2026-09-30): every card is a bare
    # LayoutView with no accent, so its four rows are gone rather than
    # converted.
    # Fantasies & Dealbreakers converted to Components V2 (2026-09-30), the
    # same day as Hot Takes: its rows are gone rather than converted.
    # Traditional ToD converted to Components V2 (wave 3b, games): every card
    # is now a bare LayoutView with no container and no accent, so its
    # `traditional.recap` row is gone rather than converted.
    # Casino converted to Components V2 (docs/plans/components_v2_migration.md
    # wave 3a): every card is now a bare LayoutView with no container and no
    # accent, per the style guide's "no accent on a V2 card" rule — so the
    # casino no longer has an embed accent path to test here.
    # ── welcome / leave (member arg is constructed mock data) ───────────
    case(
        "welcome.join",
        lambda **kw: welcome_service.build_welcome_embed(
            _welcome_member(), "hi {member}", **kw
        ),
        discord.Color.blurple(),
    ),
    case(
        "welcome.leave",
        lambda **kw: welcome_service.build_leave_embed(
            _welcome_member(), "bye {member_name}", **kw
        ),
        None,
    ),
    # Pin of the Day's review card moved off this contract with its
    # Components V2 conversion (2026-09-28) — it's a bare LayoutView with no
    # accent colour any more; see tests/economy/test_pin_layouts.py. The Bounty
    # Board's card and hub joined it the same day — see
    # tests/economy/test_economy_bounty_layouts.py.
    case(
        "dm_branding.branded_dm",
        lambda **kw: dm_branding.brand_dm_embed(
            discord.Embed(title="A DM"), guild_name="Test Guild", **kw
        ),
        discord.Color(services_embeds.DM_PRIMARY),
    ),
    # ── survivor ─────────────────────────────────────────────────────────
    case(
        "survivor.status",
        lambda **kw: survivor_embeds.build_status_embed(
            _survivor_status(), season_name="S", **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.pick_confirm",
        lambda **kw: survivor_embeds.build_pick_confirm_embed(
            _SurvivorGame(
                team="SEA", opponent="NE", is_home=True, game_id="g",
                week=1, kickoff_ts=1_700_000_000.0,
            ),
            _survivor_status(), changed=False, **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.board",
        lambda **kw: survivor_embeds.build_board_embed(
            {
                "week": 1,
                "alive": [{"user_id": 1, "strikes_used": 0, "weeks_survived": 2}],
                "graveyard": [{"user_id": 2, "eliminated_week": 1}],
                "pots": {"main": 8000, "ghost": 2000},
                "most_burned": [("SEA", 2)],
            },
            lambda uid: f"soul {uid}", season_name="S", **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.reckoning",
        lambda **kw: survivor_reckoning.build_reckoning_embed(
            {
                "week": 1, "before": 3, "after": 2,
                "pots": {"main": 8000, "ghost": 2000},
                "toll_line": "the toll", "stragglers": 0,
                "arrivals": [], "eulogy_lines": ["{name} fell."],
                "deaths": [{"user_id": 2, "teams": "NE 💀", "state": "💀",
                            "died": True, "source": "picks",
                            "fatal_team": "NE"}],
                "ledger": [{"user_id": 1, "teams": "SEA ✅", "state": "",
                            "died": False, "source": None,
                            "fatal_team": None}],
                "streak_strip": [], "streak_record": 0,
            },
            lambda uid: f"soul {uid}", season_name="S", **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.history",
        lambda **kw: survivor_embeds.build_history_embed(
            [{"week": 1, "team": "SEA", "result": "win",
              "auto_assigned": False}],
            display_name="Loaf", revealed_week=1, own=False, **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.gauntlet_receipt",
        lambda **kw: survivor_embeds.build_gauntlet_receipt_embed(
            _Fate(
                weeks=(_ReplayWeek(1, "KC", "g1", "win", 0, False),),
                elapsed_count=1, strikes_used=0, dead=False,
                death_week=None, burned=("KC",), fee=50,
            ),
            buyin=0, **kw,
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.panel_enrolling",
        lambda **kw: survivor_embeds.build_panel_embed(
            season_name="S", entrants=3, buyin=0, gauntlet_mode=False, **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    case(
        "survivor.panel_active",
        lambda **kw: survivor_embeds.build_panel_embed(
            season_name="S", entrants=9, buyin=0, gauntlet_mode=True,
            week=3, games=[{"home": "SEA", "away": "NE",
                            "kickoff_ts": 1_700_000_000.0}],
            alive=7, eliminated=2, picked=5, pot=8150, ghost_pot=2000, **kw
        ),
        discord.Color(branding_service.DEFAULT_ACCENT),
    ),
    # ── music playlist (passthrough-only: `color` is a required keyword) ─
    case(
        "music_playlist.window",
        lambda **kw: music_playlist_embeds.build_window_embed(
            [], window_size=30, color=kw["color"], name_fn=str
        ),
        None,
    ),
    case(
        "music_playlist.my_posts",
        lambda **kw: music_playlist_embeds.build_my_posts_embed(
            [], window_size=30, color=kw["color"]
        ),
        None,
    ),
    case(
        "music_playlist.my_review",
        lambda **kw: music_playlist_embeds.build_my_review_embed([], color=kw["color"]),
        None,
    ),
    # Meadow Mahjong converted to Components V2 (2026-09-30): every card is
    # a bare LayoutView with no accent, so its rows are gone rather than
    # converted.
    case(
        "jail.policy_ballot",
        lambda **kw: jail_embeds.build_policy_ballot_embed(
            question="Quiet hours?", yes_ids=[1], no_ids=[2], **kw
        ),
        discord.Color(services_embeds.MOD_POLICY),
    ),
    # Passthrough-only: the cog hands it whatever safe_resolve_accent returned,
    # which is legitimately None on a guild with no accent set.
    case(
        "word_cloud.cloud",
        lambda **kw: word_cloud_embeds.build_cloud_embed(
            message_count=1_200,
            member_name=None,
            scope_label="#general",
            span=timedelta(days=7),
            source_label="stored history",
            by_sentiment=True,
            notes=["Capped at the most recent 5,000 messages."],
            color=kw.get("color"),
        ),
        None,
    ),
]





def _survivor_status() -> dict:
    return {
        "status": "alive", "strikes_used": 0, "strikes_allowed": 1,
        "eliminated_week": None, "week": 1,
        "pick": {"team": "SEA", "game_id": "g", "auto_assigned": False,
                 "result": None},
        "pick_locked": False, "pick_kickoff_ts": 1_700_000_000.0,
        "satchel_count": 31, "satchel_low": False,
    }

# Passthrough-only builders (fallback None) are excluded from the fallback test.
FALLBACK_CASES = [p for p in CASES if p.values[1] is not None]


@pytest.mark.parametrize(("build", "fallback"), CASES)
def test_builder_honors_passed_accent(build, fallback):
    assert build(color=ACCENT).color == ACCENT


@pytest.mark.parametrize(("build", "fallback"), FALLBACK_CASES)
def test_builder_falls_back_without_accent(build, fallback):
    assert build().color == fallback


# ── completeness guard ───────────────────────────────────────────────────
# Without this, the table is documentation: a new embeds module could ship
# builders with zero accent coverage while every test stays green. Discovery
# walks the embeds/rendering/formatters modules; every builder must appear in
# a case() above or in the ledger below. The ledger is the debt as of
# 2026-07-25 — move entries into CASES over time; never add to it silently
# (adding an entry is an explicit, reviewable decision to skip the contract,
# e.g. for content-string builders or semantic-color-only surfaces).
KNOWN_UNCOVERED = {
    # Bios carries its own per-guild dial (`bios_embed_color`, BiosConfig) and
    # takes it as a required `embed_color=` argument rather than the optional
    # `color=` the contract probes — there is no accent path to pass through and
    # no fallback to assert. The wizard's two prompt embeds are the same builder
    # family as the posted bio and share that dial.
    "bot_modules.bios.embeds.build_bio_embed",
    "bot_modules.bios.embeds.build_field_prompt_embed",
    "bot_modules.bios.embeds.build_question_prompt_embed",
    # Casino converted to Components V2 (docs/plans/components_v2_migration.md
    # wave 3a): every card is a bare LayoutView with no accent, so the casino
    # has no embed-accent contract left to exclude from.
    # Text renderers, not cards: the story a LegitLibs card carries.
    "bot_modules.games.legitlibs.rendering.render_filled_body",
    "bot_modules.games.legitlibs.rendering.render_filled_body_attributed",
    "bot_modules.games.legitlibs.rendering.render_redacted_body",
    "bot_modules.moderation.jail.embeds.build_adopted_policies_embed",
    "bot_modules.moderation.jail.embeds.build_jail_audit_embed",
    "bot_modules.moderation.jail.embeds.build_modinfo_embed",
    "bot_modules.moderation.jail.embeds.build_policy_close_embed",
    "bot_modules.moderation.jail.embeds.build_policy_list_embed",
    "bot_modules.moderation.jail.embeds.build_policy_proposal_embed",
    "bot_modules.moderation.jail.embeds.build_policy_vote_initial_embed",
    "bot_modules.moderation.jail.embeds.build_policy_vote_update_embed",
    "bot_modules.moderation.jail.embeds.build_ticket_open_embed",
    "bot_modules.moderation.jail.embeds.build_ticket_panel_embed",
    "bot_modules.moderation.jail.embeds.build_warning_audit_embed",
    "bot_modules.moderation.jail.embeds.build_warning_revoke_audit_embed",
    "bot_modules.moderation.jail.embeds.build_warning_threshold_embed",
    "bot_modules.music.embeds.build_queue_embed",
    "bot_modules.games.risky_roll.formatters.build_pending_prompt_content",
    "bot_modules.games.risky_roll.formatters.build_pending_question_summary",
    "bot_modules.games.risky_roll.formatters.build_question_reply_content",
    "bot_modules.games.risky_roll.formatters.build_pending_chase_content",
    "bot_modules.games.risky_roll.formatters.build_posted_chase_content",
    "bot_modules.games.risky_roll.formatters.build_fallback_question_content",
    "bot_modules.shared.embeds.build_admin_mirror_embed",
    "bot_modules.starboard.embeds.build_starboard_embed",
    "bot_modules.voice.master.embeds.build_claim_done_embed",
    "bot_modules.voice.master.embeds.build_claim_prompt_embed",
    "bot_modules.voice.master.embeds.build_howto_embed",
    "bot_modules.voice.master.embeds.build_inline_panel_embed",
    "bot_modules.voice.master.embeds.build_knock_request_embed",
    "bot_modules.voice.master.embeds.build_panel_embed",
    "bot_modules.voice.master.embeds.build_profile_show_embed",
}


def _discover_builders() -> set[str]:
    """Every build_*/render_* callable in an embeds/rendering/formatters module."""
    import bot_modules

    found: set[str] = set()
    for info in pkgutil.walk_packages(bot_modules.__path__, "bot_modules."):
        if info.name.rsplit(".", 1)[-1] not in ("embeds", "rendering", "formatters"):
            continue
        module = importlib.import_module(info.name)
        for attr, obj in vars(module).items():
            if (
                callable(obj)
                and getattr(obj, "__module__", None) == info.name
                and attr.startswith(("build_", "render_"))
            ):
                found.add(f"{info.name}.{attr}")
    return found


def test_every_builder_is_under_contract_or_explicitly_exempt():
    # A builder counts as covered if its (unqualified) name is invoked
    # somewhere in this file — i.e. it has a case() row. Ledger entries are
    # fully-qualified and never followed by "(", so they don't self-match.
    source = Path(__file__).read_text(encoding="utf-8")
    covered = set(re.findall(r"\.((?:build|render)_\w+)\(", source))
    discovered = _discover_builders()

    missing = sorted(
        b for b in discovered
        if b.rsplit(".", 1)[-1] not in covered and b not in KNOWN_UNCOVERED
    )
    assert not missing, (
        "New embed builder(s) with no accent-contract coverage. Add a case() "
        "row above (preferred) or an explicit KNOWN_UNCOVERED entry:\n  "
        + "\n  ".join(missing)
    )

    stale = sorted(KNOWN_UNCOVERED - discovered)
    assert not stale, (
        "KNOWN_UNCOVERED entries that no longer exist (or moved) — remove "
        "them so the ledger stays honest:\n  " + "\n  ".join(stale)
    )
