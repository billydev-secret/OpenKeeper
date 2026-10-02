"""The repo-layout map: where every file under ``services/``, ``cogs/`` and
``commands/`` goes when ``src/bot_modules/`` becomes one package per feature.

Read by ``scripts/relayout.py``. Paths are relative to ``src/bot_modules/``.

A rule is ``(stage, old, new)``, and the first rule that matches a file wins,
so explicit rows sit above the families they carve out of. Three shapes:

* an exact file: ``"services/moderation.py" -> "moderation/service.py"``;
* a directory, ending ``/``: every file below it moves, keeping its
  sub-path (``"cogs/casino/" -> "casino/cog/"``);
* a family, with one ``*`` matching a run of ``[a-z0-9_]`` in both
  (``"services/economy_*.py" -> "economy/*.py"``).

Families are what make the map replayable on a branch: a module a branch adds
as ``services/economy_newthing.py`` is placed by the family rule with no map
edit. A file under the three old directories that no rule matches is an
error, and gets a row here rather than a guess.

Stages (see the plan): 1 cross-cutting packages, 2 small features, 3
moderation and privacy, 4 economy and casino, 5 games and duels. Stage 6 moves
tests (``TEST_ROWS`` and the rules above it, at the end of this file) and
deletes the emptied directories. Stages 7-9 (2026-10-01) fold sibling
features into one package each — music, dm, voice, roles (7), onboarding
(8), moderation (9) — and re-place their tests the same way.
"""

from __future__ import annotations

Rule = tuple[int, str, str]

RULES: list[Rule] = [
    # ── Stage 1: cross-cutting packages (no cog of their own, except panels
    # and no_contact) ──────────────────────────────────────────────────────
    (1, "services/branding_service.py", "core/branding_service.py"),
    (1, "services/command_sync.py", "core/command_sync.py"),
    (1, "services/gif_guard_logic.py", "core/gif_guard_logic.py"),
    (1, "services/role_provenance.py", "core/role_provenance.py"),
    (1, "services/channel_rollup.py", "core/channel_rollup.py"),
    (1, "services/settings_registry.py", "core/settings_registry.py"),
    (1, "services/name_resolver.py", "shared/name_resolver.py"),
    (1, "services/embeds.py", "shared/embeds.py"),
    (1, "services/dm_branding.py", "shared/dm_branding.py"),
    (1, "services/anon_audit_service.py", "shared/anon_audit_service.py"),
    (1, "services/feature_roles.py", "shared/feature_roles.py"),
    (1, "services/replies.py", "shared/replies.py"),
    (1, "services/quote_renderer.py", "shared/quote_renderer.py"),
    (1, "services/quote_box.py", "shared/quote_box.py"),
    (1, "services/message_store.py", "shared/message_store.py"),
    (1, "services/age_gate.py", "shared/age_gate.py"),
    (1, "services/ollama_client.py", "shared/ollama_client.py"),
    (1, "services/ai_config.py", "shared/ai_config.py"),
    (1, "services/pyplot_lock.py", "shared/pyplot_lock.py"),
    (1, "services/discord_scan.py", "shared/discord_scan.py"),
    (1, "services/sticky_registry.py", "panels/sticky_registry.py"),
    (1, "services/panel_*.py", "panels/*.py"),
    (1, "cogs/panel_refresh_cog.py", "panels/cog.py"),
    (1, "services/no_contact_*.py", "no_contact/*.py"),
    (1, "cogs/no_contact_cog.py", "no_contact/cog.py"),
    # ── Stage 3: moderation and privacy ──────────────────────────────────
    # (listed before stage 2 only where a stage-2 family would otherwise
    # swallow the file; order decides the match, the number decides when)
    (3, "services/moderation.py", "moderation/service.py"),
    (3, "services/ai_moderation_service.py", "moderation/ai_service.py"),
    (3, "services/bulk_cleanup_service.py", "moderation/bulk_cleanup_service.py"),
    (3, "services/mod_*.py", "moderation/*.py"),
    (3, "cogs/mod_cog.py", "moderation/cog.py"),
    (3, "cogs/mod_stats_cog.py", "moderation/cog_stats.py"),
    (3, "cogs/rename_cog.py", "moderation/cog_rename.py"),
    (3, "services/staff_ping_service.py", "mod_settings/staff_ping_service.py"),
    (3, "services/policy_ballot_service.py", "jail/policy_ballot_service.py"),
    (3, "cogs/jail_cog.py", "jail/cog.py"),
    (3, "commands/jail_commands.py", "jail/jail_commands.py"),
    (3, "services/privacy_service.py", "privacy/service.py"),
    (3, "services/disclosure_*.py", "privacy/disclosure_*.py"),
    (3, "services/departure_erasure_service.py", "privacy/departure_erasure_service.py"),
    (3, "services/self_erasure_service.py", "privacy/self_erasure_service.py"),
    (3, "services/retention_service.py", "privacy/retention_service.py"),
    (3, "services/guild_purge_service.py", "privacy/guild_purge_service.py"),
    (3, "cogs/privacy_cog.py", "privacy/cog.py"),
    (3, "cogs/guild_purge_cog.py", "privacy/cog_guild_purge.py"),
    (3, "cogs/retention_cog.py", "privacy/cog_retention.py"),
    # ── Stage 4: economy and casino ──────────────────────────────────────
    (4, "services/color_palette.py", "economy/color_palette.py"),
    (4, "services/reaction_tip_service.py", "economy/reaction_tip_service.py"),
    (4, "services/economy_service.py", "economy/service.py"),
    (4, "services/economy_*.py", "economy/*.py"),
    (4, "cogs/economy/", "economy/cog/"),
    (4, "cogs/economy_cog.py", "economy/economy_cog.py"),
    (4, "services/casino_*.py", "casino/*.py"),
    (4, "services/pools_*.py", "casino/pools_*.py"),
    (4, "cogs/casino/", "casino/cog/"),
    # ── Stage 5: games and duels ─────────────────────────────────────────
    (5, "services/risky_roll/", "games/risky_roll/"),
    (5, "cogs/risky_roll_cog.py", "games/risky_roll/cog.py"),
    (5, "services/guess_*.py", "games/guess/*.py"),
    (5, "cogs/guess_cog.py", "games/guess/cog.py"),
    (5, "cogs/mahjong_cog.py", "games/mahjong/cog.py"),
    (5, "services/games_db.py", "games/db.py"),
    (5, "services/scheduled_games_service.py", "games/scheduled_service.py"),
    (5, "services/game_*.py", "games/*.py"),
    (5, "cogs/games_dev_cog.py", "games/cog_dev.py"),
    (5, "cogs/games_legitlibs/", "games/legitlibs/"),
    (5, "cogs/games_*_cog.py", "games/*/cog.py"),
    (5, "games_*/", "games/*/"),
    (5, "cogs/chicken/", "duels/chicken/"),
    (5, "cogs/hot_potato/", "duels/hot_potato/"),
    (5, "cogs/hot_potato_group/", "duels/hot_potato_group/"),
    (5, "cogs/musical_chairs/", "duels/musical_chairs/"),
    (5, "cogs/pressure_cooker/", "duels/pressure_cooker/"),
    (5, "cogs/quickdraw/", "duels/quickdraw/"),
    # ── Stage 2: small features ──────────────────────────────────────────
    (2, "services/advisor_service.py", "advisor/service.py"),
    (2, "services/advisor_*.py", "advisor/*.py"),
    (2, "services/announcements_service.py", "announcements/service.py"),
    (2, "services/auto_delete_service.py", "auto_delete/service.py"),
    (2, "services/auto_react_service.py", "auto_react/service.py"),
    (2, "services/birthday_service.py", "birthday/service.py"),
    (2, "services/chat_revive_*.py", "chat_revive/*.py"),
    (2, "services/confessions_service.py", "confessions/service.py"),
    (2, "services/dm_perms_service.py", "dm_perms/service.py"),
    (2, "services/event_echo_*.py", "event_echo/*.py"),
    (2, "services/feature_rotation_service.py", "feature_rotation/service.py"),
    (2, "services/greeting_watch_*.py", "greeting_watch/*.py"),
    (2, "services/intake_*.py", "intake/*.py"),
    (2, "services/welcome_service.py", "welcome/service.py"),
    (2, "services/onboarding_service.py", "welcome/onboarding_service.py"),
    (2, "services/login_card_service.py", "welcome/login_card_service.py"),
    (2, "services/invite_tracker.py", "invites/tracker.py"),
    (2, "cogs/invite_cog.py", "invites/cog.py"),
    (2, "services/inactive_report_service.py", "inactive/report_service.py"),
    (2, "services/inactivity_prune_service.py", "inactive/prune_service.py"),
    (2, "services/lavalink_manager.py", "music/lavalink_manager.py"),
    (2, "services/music_*.py", "music/*.py"),
    (2, "services/spotify_resolver.py", "music_playlist/spotify_resolver.py"),
    (2, "services/ping_tracker_service.py", "ping_tracker/service.py"),
    (2, "services/promotion_review_*.py", "xp/promotion_review_*.py"),
    (2, "services/xp_service.py", "xp/service.py"),
    (2, "services/xp_*.py", "xp/*.py"),
    (2, "services/message_xp_service.py", "xp/message_xp_service.py"),
    (2, "services/voice_xp_service.py", "xp/voice_xp_service.py"),
    (2, "services/qa_service.py", "qa/service.py"),
    (2, "services/role_grant_audit_service.py", "role_grant/audit_service.py"),
    (2, "services/role_grant_logic.py", "role_grant/logic.py"),
    (2, "commands/role_grant_commands.py", "role_grant/role_grant_commands.py"),
    (2, "services/role_roster_service.py", "bot_roles/roster_service.py"),
    (2, "services/managed_roles_store.py", "bot_roles/managed_roles_store.py"),
    (2, "services/starboard_service.py", "starboard/service.py"),
    (2, "services/survivor_*.py", "survivor/*.py"),
    (2, "services/todo_service.py", "todo/service.py"),
    (2, "services/todo_*.py", "todo/*.py"),
    (2, "cogs/bug_cog.py", "todo/cog_bug.py"),
    (2, "services/voice_master_service.py", "voice_master/service.py"),
    (2, "commands/voice_master_commands.py", "voice_master/voice_master_commands.py"),
    (2, "services/voice_transcription_service.py", "voice_transcription/service.py"),
    (2, "services/watch_service.py", "watch/service.py"),
    (2, "services/wellness_service.py", "wellness/service.py"),
    (2, "services/wellness_*.py", "wellness/*.py"),
    (2, "services/whisper_service.py", "whisper/service.py"),
    (2, "services/whisper_*.py", "whisper/*.py"),
    (2, "services/activity_graphs.py", "analytics/activity_graphs.py"),
    (2, "services/channel_health.py", "analytics/channel_health.py"),
    (2, "services/channel_health_logic.py", "analytics/channel_health_logic.py"),
    (2, "services/delivery_health_logic.py", "analytics/delivery_health_logic.py"),
    (2, "services/health_*.py", "analytics/health_*.py"),
    (2, "services/reports_data.py", "analytics/reports_data.py"),
    (2, "services/sentiment_service.py", "analytics/sentiment_service.py"),
    (2, "services/usage_telemetry_service.py", "analytics/usage_telemetry_service.py"),
    (2, "cogs/usage_telemetry_cog.py", "analytics/cog_usage_telemetry.py"),
    (2, "services/db_backup.py", "admin/db_backup.py"),
    (2, "cogs/dev_cog.py", "admin/cog_dev.py"),
    (2, "cogs/support_cog.py", "admin/cog_support.py"),
    (2, "cogs/events_cog.py", "events/cog.py"),
    (2, "cogs/quote_cog.py", "quotes/cog.py"),
    (2, "cogs/pen_pals_cog.py", "pen_pals/cog.py"),
    # A feature's single cog: cogs/<name>_cog.py -> <name>/cog.py. Last, so
    # every carve-out above wins.
    (2, "cogs/*_cog.py", "*/cog.py"),
    # ── Stages 7-9: sibling features become one package, as games/<game>/
    # (docs/plans/feature-package-merges.md). A child that repeats its
    # parent's name drops the prefix; the rest keep theirs. ─────────────
    (7, "music_playlist/", "music/playlist/"),
    (7, "dm_perms/", "dm/perms/"),
    (7, "dm_batch/", "dm/batch/"),
    (7, "voice_master/", "voice/master/"),
    (7, "voice_transcription/", "voice/transcription/"),
    (7, "role_menus/", "roles/menus/"),
    (7, "role_grant/", "roles/grant/"),
    (7, "bot_roles/", "roles/bot_roles/"),
    (8, "welcome/", "onboarding/welcome/"),
    (8, "intake/", "onboarding/intake/"),
    (8, "greeting_watch/", "onboarding/greeting_watch/"),
    (9, "jail/", "moderation/jail/"),
    (9, "watch/", "moderation/watch/"),
    (9, "tickets/", "moderation/tickets/"),
    (9, "mod_settings/", "moderation/settings/"),
]

#: Files that are not moved but deleted once their directory is empty (stage 6).
LEFT_BEHIND = frozenset({"services/__init__.py", "cogs/__init__.py", "commands/__init__.py"})

#: Top-level directories whose every file must be placed by a rule.
MUST_EMPTY = ("services/", "cogs/", "commands/")


# ── Stage 6: tests ───────────────────────────────────────────────────────
#
# A test moves to ``tests/<feature>/`` beside nothing but its own feature's
# tests, keeping its basename (the gate selects by basename, so selection is
# unchanged). Placement is computed, not listed, so a test a branch adds is
# placed too. In order, the first that answers wins:
#
# 1. ``tests/web/`` and ``tests/beta/`` already mirror their package: stay.
# 2. ``TEST_ROWS``, by basename (``""`` keeps it in ``tests/``).
# 3. A whole-tree sweep (``*_contract``, ``embed_*``) -> ``tests/contracts/``;
#    ``migration*`` -> ``tests/migrations/``; ``dk_mcp_*`` -> ``tests/dk_mcp/``.
# 4. The name: ``test_<feature>...`` for the longest feature package that
#    prefixes it, ``games_<game>`` and the packages games/ and duels/ absorbed
#    included.
# 5. What it imports: the ``bot_modules`` package it names most, if it wins
#    outright (``core.db_utils``, which nearly every test opens a DB through,
#    doesn't count).
# 6. Otherwise it stays in ``tests/``; a tie gets a row in ``TEST_ROWS``.
#
# ``tests/tooling/`` holds tests of scripts/ and of the repo itself; it is not
# ``tests/scripts/`` because ``scripts`` is a namespace package that a second
# portion would silently merge with.

TEST_KEEP = ("tests/web/", "tests/beta/")

#: Packages folded into games/ or duels/ whose tests still carry the bare name.
TEST_NAME_ALIASES = {
    "games_": "games", "game_": "games", "guess": "games", "legitlibs": "games",
    "mahjong": "games", "risky_roll": "games", "mfk": "games", "story": "games",
    "clapback": "games", "compliment": "games", "scheduled_games": "games",
    "duel": "duels", "duels": "duels", "chicken": "duels", "hot_potato": "duels",
    "musical_chairs": "duels", "pressure": "duels", "quickdraw": "duels",
    "pools": "casino", "quest": "economy", "shop": "economy", "perk": "economy",
    "wager": "economy", "faucet": "economy", "reaction_tip": "economy",
    "quote": "quotes", "invite": "invites",
    # Stages 7-9: a name-placed test follows its package into the parent,
    # and a stem a new parent key would capture by accident stays put.
    "dm_perms": "dm", "dm_batch": "dm", "dm_branding": "core",
    "voice_master": "voice", "voice_transcription": "voice", "voice_note": "economy",
    "role_menus": "roles", "role_grant": "roles", "role_roster": "roles", "bot_roles": "roles",
    "welcome": "onboarding", "intake": "onboarding", "greeting_watch": "onboarding",
    "jail": "moderation", "watch": "moderation", "ticket": "moderation",
    "tickets": "moderation", "mod_settings": "moderation",
    "voice_xp": "xp", "message_xp": "xp",
    "health": "analytics", "activity_graphs": "analytics", "channel_health": "analytics",
    "delivery_health": "analytics", "reports_": "analytics", "usage_telemetry": "analytics",
    "db_backup": "admin",
}

TEST_ROWS: dict[str, str] = {
    **dict.fromkeys((
        "test_backup_to_nas.py", "test_check_commit_msg.py", "test_ci_watch.py",
        "test_dk_session.py", "test_discord_rest.py", "test_encoding_portability.py",
        "test_enroll_survivor_sim_players.py", "test_env_hermeticity.py",
        "test_gate_mobile_scope.py", "test_gate_scope.py", "test_gate_tiers.py",
        "test_lint.py", "test_make_panel_art.py", "test_post_testing_docs.py",
        "test_preflight_scripts.py", "test_pyright_strict_list.py", "test_relayout.py",
        "test_remote_test.py", "test_requirements_lock_sync.py",
        "test_ruff_lint_baselines.py", "test_ship_flow_wiring.py",
        "test_workflow_action_pins.py", "test_economy_tuning_report.py",
        "test_db_template.py",
    ), "tooling"),
    **dict.fromkeys((
        "test_import_layering.py", "test_cog_size_cap.py", "test_command_tree_size.py",
        "test_mention_default_audit.py", "test_layout_no_container.py",
        "test_layout_hub_banner.py", "test_booster_multiplier_wiring.py",
        "test_view_timeout_contract.py", "test_extension_discovery.py",
        "test_boot_smoke.py", "test_question_source_bank_only.py",
    ), "contracts"),
    "test_watchdog_retry.py": "tooling",
    # The import count ties, or picks a feature the test only borrows from.
    "test_engagement_faucet_triggers.py": "economy",
    "test_voice_note_quest_scope.py": "economy",
    "test_ai_config.py": "shared",
    "test_anon_audit_service.py": "shared",
    "test_feature_roles.py": "shared",
    "test_message_deletion_marks.py": "shared",
    "test_ai_moderation_service.py": "moderation",
    "test_branding_service.py": "core",
    "test_channel_rollup.py": "core",
    "test_settings_registry_table_signals.py": "core",
    "test_departure_erasure_service.py": "privacy",
    "test_retention_service.py": "privacy",
    "test_managed_roles_store.py": "roles",
    "test_panel_registry.py": "panels",
    "test_panel_title_service.py": "panels",
    "test_sticky_registry.py": "panels",
    "test_staff_ping_service.py": "moderation",
    "test_login_card_service.py": "onboarding",
    "test_game_rewards.py": "economy",
    "test_db_utils.py": "core",
    "test_mod_stats_cog.py": "moderation",
    "test_mod_stats_service.py": "moderation",
    "test_policy_ballot_service.py": "moderation",
    "test_policy_vote_repo.py": "moderation",
    "test_promotion_review_service.py": "xp",
    "test_role_roster_service.py": "roles",
    # Dashboard tests outside tests/web, whose conftest they don't want: stay.
    "test_web_routes.py": "",
}
