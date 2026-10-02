"""A migration that has shipped is frozen, and changing it costs a visible line.

A migration is applied **by filename**: prod's ``schema_version`` is
``(migration TEXT PRIMARY KEY, applied_at REAL NOT NULL)``, keyed on the name of
the file, never on its contents. So once a file has run anywhere, editing it
changes what a *fresh install* builds and changes nothing about the database
that already ran it. The two diverge, silently, and no test in this suite can
see it because the suite builds its schema from the same edited files.

That is not hypothetical. ``3fa9aa86`` deleted two ``CREATE TABLE`` statements
from ``000_init.sql`` on 2026-06-11 — seven weeks after prod applied that file
on 04-24 — with no compensating migration. Production still carries
``incident_events`` (572 rows) and ``message_velocity_baseline`` (508) today
while a fresh install has never heard of them, and
``test_schema_authority_contract.py`` says so in its own docstring because no
schema built from source will ever contain them. ``9d190692`` did the same to
``019_games.sql``, removing ``games_portal_access``; that one is harmless only
because the author happened to ship ``041_drop_games_portal_access.sql``
alongside it. Nothing asked either author the question.

**What this actually enforces, which is less than it sounds.** It does not stop
an edit. It makes one impossible to land silently: the hash below must be
updated in the same commit, so the change appears as a line under a filename
rather than as six deleted lines in the middle of a 219-line file. Whether that
edit is safe is a human's answer — see "What this cannot see".

**Comment-only edits fail too, on purpose.** ``415de655`` rewrote nothing but
the ``--`` header of ``071_economy_onboarding_quests.sql``, five days after prod
ran it, and this guard flags it. Hashing the statements instead — with comments
stripped through the runner's own ``_strip_line_comments`` — would let that one
through, and was considered and rejected: a normaliser that decides which edits
are invisible is a normaliser that can be wrong in the direction of silence, and
the cost of the strict rule is one manifest line on a comment fix. A reviewer
who sees a hash move and finds only prose has learned the cheap thing quickly.

**Why the hash is over decoded text, not bytes.** ``apply_migrations_sync`` does
``path.read_text(encoding="utf-8")``; so does this. The scoped gate dispatches
pytest to a **Windows** runner, and hashing raw bytes would make the answer
depend on that machine's line-ending settings rather than on the SQL. Reading
the file the way the thing that executes it reads it removes the question.

**The runner is pinned too.** ``src/migrations/__init__.py`` splits statements
on ``;``, strips line comments, and decides which ``OperationalError`` is
tolerable — it changes what all 253 files *do* without touching one of them, and
two commits in its five-commit history did exactly that. One extra hash is a
cheap way to make that edit ask the same question.

What this cannot see
--------------------
* **Whether a flagged edit matters.** ``415de655`` (prose), ``9d190692`` (a table
  removed, with a compensating migration) and ``3fa9aa86`` (a table removed,
  with none) all produce one changed line here. Sorting them is the reviewer's
  job; this only guarantees there is something to sort.
* **Whether prod has applied a given file.** ``schema_version`` is never
  consulted — it can't be, from a test. So pre-ship iteration on an unmerged
  migration and post-ship drift look identical, and the first is meant to: you
  bump the hash in the same commit and carry on.
* **Schema written by anything but a ``.sql`` file.** A service module's
  ``ensure_*_schema()`` runs ``CREATE TABLE IF NOT EXISTS`` at boot, and 21 such
  objects had no migration author at all until migration 235.
  ``test_schema_authority_contract.py`` owns that half; green here says nothing
  about it.
* **Whether the gate will run it.** ``scripts/gate.py``'s ``select_tests`` maps
  every ``src/migrations/`` path to the two privacy-coverage tests and then
  ``continue``s past the token matching, so no filename can select this file; the
  full-run fallback that would is **deferred** in a session worktree. Editing a
  shipped migration in a worktree therefore commits clean, and this bites in the
  prod checkout, on ``gate.py`` run against main, at ``/dk-regress``, and in CI.
  One line beside ``PRIVACY_COVERAGE_TESTS`` would close that, and changing
  ``gate.py`` runs in every other session's pre-commit hook, so it is a separate
  decision and not this file's to take.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT
MIGRATIONS = ROOT / "src" / "migrations"


def _sha256_text(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _migration_files() -> list[Path]:
    """The runner's own glob — a manifest over a different set would prove nothing."""
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))


def _compare(
    on_disk: dict[str, str], manifest: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """``(changed, unlisted, orphaned)`` — the three ways the two can disagree.

    Split out from the tests so the detector can be exercised on inputs that are
    not this tree; see ``test_the_detector_actually_bites``.
    """
    changed = sorted(n for n, h in on_disk.items() if n in manifest and manifest[n] != h)
    unlisted = sorted(n for n in on_disk if n not in manifest)
    orphaned = sorted(n for n in manifest if n not in on_disk)
    return changed, unlisted, orphaned


def _on_disk() -> dict[str, str]:
    return {p.name: _sha256_text(p) for p in _migration_files()}


# --- BEGIN MANIFEST — regenerate with scripts/migration_manifest.py ---
MANIFEST: dict[str, str] = {
    "000_init.sql": "9e25342ccf606d6e1b59f6020b9cd564e6d9008b288fcea35527fe338935d7d5",
    "001_id_remap.sql": "ef73753ff5bc811b16fb7aa01988815d1d302765e908f2c655aa96922ce11cae",
    "002_todo_tables.sql": "e4cee77700400ccc115f21e091ede70a2095a4d79f22bc6b02f3bea1c9dbc2f0",
    "003_birthdays.sql": "debe08561d8b9d227890414c598c00ef946e7a99d772c78029a8a4b98e30ac14",
    "004_starboard.sql": "3697e1e58e2ddded62db18bfbbd11393d146072783870dbda80a54784d2acae9",
    "005_voice_master.sql": "942d4652f6cf510dbd4b4658f6611ac09c34b08ce8314d68f5ea7b25b9097b41",
    "006_music.sql": "c746d719be013faa61ddd452d0eec73ea1b2f2034fb085afbeda6d61e9549673",
    "007_beta_source_tags.sql": "da4904c70d17398016444a508e1c6da318a3a4f46f389d1d8e4c8a8ff46faa51",
    "008_todo_message_source.sql": "55c62d02b0909241d39d31423c8e53ad8fb39b70ba74eba2e2f02114a16970df",
    "009_veil.sql": "8f300d1a2cda184f6b299bf4c7c7fbfa39e6679a9d52a295444486765af6ed81",
    "010_confession_pools.sql": "58c9e155bd228b1f68a6b38368ba04901208bf19c5c8b68bfc0418b30dc25183",
    "011_veil_original_path.sql": "2bb5a857d3a230fd3fb86a763dfba3bcca98a10da124cf6b55aa7e6eb2e32bee",
    "012_veil_audit_log.sql": "25c2d3030b3962b68d0a5a37aa5571df805b836fba4fe02882c6f1aba8539d36",
    "012_whisper.sql": "95a6bf931fd5f6cff7e2d003b7949f8fe74dffb8d961cb8537d447b6de7f5ebe",
    "013_whisper_replies.sql": "7ab95e2d496175145d54284647ca6cf9fb94da449a2881a75ab172c8bda97cab",
    "014_whisper_reports.sql": "5c2a341c8fe0cd8aafbf4cf77ce09d0d032333e306528bf42ae0efdf888b0d93",
    "015_veil_crop_box.sql": "a568fd7fd42c5c6da67c4cde8cc720b51b60fe115daa381ab1aba0c24268165c",
    "016_veil_confession_columns.sql": "a7fa8f1c25c06e965133ed07014fd31e4786918985ec901ad3621dad604dd530",
    "017_birthday_preference.sql": "881a9f084db3dcd285c4235a055008dcdf4e4cbee067660bbd07ce37211144e5",
    "018_whisper_reply_reports.sql": "cd42fbe6bf0dfafbdd7a4f6879ccbffb59777e8179549f410d58c6739ce94490",
    "019_games.sql": "28b8ed1d76b81512cbb895c87f143e90d2751690b17189f0e6abdfa9524f9eae",
    "019_risky_roll.sql": "a956b70026cc49d19df8ea969ab7001c6cfb1c9fb9dfe4200eedc8ab641704cf",
    "020_rename_veil_to_guess.sql": "b7afeab4ab071a2f2dfe5de0c89bbfe1129dc33deb89339c9ce551afc7b2c3db",
    "021_grant_required_role.sql": "ced808c287bf8a888dc658c9505f5078ac750da155140ad72180e5a31a281a8a",
    "022_games_config.sql": "2a38f5c1ac1f1b60ef86ef37b61eb9ed3d1a19b5a3544756302e28c0a6106d20",
    "023_ticket_panels.sql": "9e13010868d799a565986a328d533afc247816d70230c4cb312b4082e33a62c7",
    "024_whisper_v2.sql": "0d9ea64b72c76c9454f3b603bb86a3dfad9b8b250201a1d4a09b0e42134143b0",
    "025_whisper_legacy_hidden_to_deleted.sql": "71da945c594158e457add5fe588e5c4456a509c67eab682155e46d5aec77f956",
    "026_quote_audit_log.sql": "4fbb2759dd738d72752749720d9038da84a294df4d06392144a701ee0a1c428f",
    "027_report_indexes.sql": "4007e20ff53fbec4d1d99305455aff376224457df76e56535dec018c7aac94dd",
    "028_pressure_cooker.sql": "a00612a199abf1ba28f15a96158802ecb9cab00aba97c793ca5b1a356278c5e0",
    "029_games_editor_role.sql": "ccd87a88e792faa6eeb3886bc90b5696b1bd450cb8c44d8ac644922ac1c989e1",
    "030_rules_watch.sql": "fe28641f3aad490d3a9ab3fe897787353ed1299b19cd732e028cb9355a8fe4d6",
    "031_bios.sql": "8872116be6dc78d45ae253a17a051127e48646fdb6fb95b317fdb4dd46229960",
    "032_duels.sql": "1af591033e574427263d2b43a5a078e675eef6ddbce61920ce259f89f930fe2f",
    "033_quickdraw.sql": "97cb4f06f6563a3648a85e23c94e40110bdbfaa546889e750c5ac9f8d885260f",
    "034_hot_potato.sql": "4bb848469fabe63f460c71535cce0968b5194828adbf1faf2bb0a5e9f69b1fa7",
    "035_duel_group.sql": "2c48b13df49f2d4b961e2d3c932a63a7237e13ecbee8c5026c1bca616178bf31",
    "036_hot_potato_group.sql": "b4ca5eb4703ae9ced9b274ff6f3b37c79c31dbbed228fc30c200866a03fcf826",
    "037_quickdraw_loser_time.sql": "57a1f864f4eca98f7b50900b9e238acb2ad4986de4bfc47b2eaf87be0f5accca",
    "038_musical_chairs.sql": "8d184972ef6f852385b845b82702078217fee97a585f50989e1494df1d46bc8f",
    "039_chicken.sql": "4fb782bdeb48f88e0126e0d898974a7c12c6c861e60377b3c5266e8c04c33748",
    "040_quote_audit_border.sql": "d66217f25c2c175a1b6c871f9796cf5388bba6de21624c2ef7a01c643827bd97",
    "041_drop_games_portal_access.sql": "901a9346c134c84d4c73ffdee2af3872b17e3f5bb025cd00df8191b80b4acf56",
    "042_needle.sql": "ff92466b85985bab83f1e677b88d4dfbc4c67b8b152f2df36569666e08dc4613",
    "043_auto_react.sql": "2a8e19525b82199b860147edf041de5d1f77efdc93b6817e4309520be0ba85ba",
    "044_bump_tracker.sql": "3f7b0cd4652df11576e3d7b064c1738850759e10be27725485f49f88a3676887",
    "045_bio_field_hint.sql": "7c7350a992af9568b794c83973d666da1815b6a1d6236bcbaedecdae9c7dafc2",
    "046_scheduled_games.sql": "84a3fcc4993acaf22875ba72d1aa2422c88fd0ca1fa52260f295232a88f7c26d",
    "047_bump_tracker_detector.sql": "98f1c6da3d6802754da87aa4e572b13ecc1281911fa2c7a105105e2cafa2004b",
    "048_pen_pals.sql": "9342fbbac4943ed0cad3c9e48ff91c26a6bbebca0f523d449d18c2a57f0fb79a",
    "049_pen_pals_panel.sql": "4a54c29aa4ac7aa0303115f627d314f2fc8e46b62fd8509c192c545abb210a18",
    "050_voice_transcriptions.sql": "ae020834945b37ad92d0b0c4d600443e92c3060a87d7d4662a62bd007072ac4f",
    "051_message_media_kind.sql": "14a2e2f8b3db396854c702c1bc8595afc7a72d06145f93e83d73f861b4bcf615",
    "052_birthday_pins.sql": "50d122bf64b4d04a8faaa3221e0a8731cfbf5dcd3aaa3ea209ce7d63fc1693ec",
    "053_games_bank_tags.sql": "de589c39c23a2dd050506eaa048ab406942e1cd3f05b5a7a33b468fd2fdada5f",
    "054_ffa_seed_bank.sql": "c09afb46bfe3d9cf608d39a8c42062575090141576e3e5388e21235f66a8d382",
    "055_voice_master_spectator.sql": "fbdd810ef8db8dacd1ec5dfbc72f81671939a722444a8f6bfaf64c4d07842f45",
    "056_external_game_tracking.sql": "de1107c21f04eecd26c124afda8b95bf86f7e56672043b9f614dffb61e61f7b7",
    "057_inactive_members.sql": "56b9e30f4922020140197f40c39e006bd4392317405e8f20995a52821141b3da",
    "058_hidden_channels.sql": "e5950059507892914881ded6268bf26a877f0a0977ef26abc7fb918332c71d62",
    "059_docs.sql": "a487f7a056069f181fa3d318dc427a7efc55ba99752e341b8203560d2801cdd4",
    "060_voice_master_age_gated.sql": "35a02402bbf69ced3dd3d2d6358be05a69ff8e70a4f8b2b526be57a3e39d8d31",
    "061_doc_placement_pinned.sql": "9e3f2c20ae84ce470da3a70602eb2e7befee9960778d366344d357f7c0b15887",
    "062_economy.sql": "697d35dfc7cf77c824e2b89f1e2cfd5fcde3c2ab5ecb72c24799ff86d82d04ef",
    "063_economy_faucets.sql": "fd4e5a977b5556a7c0767a9902a262fbd3429b9cdf7ee6f82f8641eb651ebaf1",
    "064_economy_quests.sql": "bae37cfb73a79578b35404be7d498d29fcd6882eed2c05c297a7817618b6d131",
    "065_economy_rentals.sql": "9e944a0c78170585675198e452b943613add3296004b28be7f0e1315e099bddf",
    "066_economy_metrics.sql": "a8aace7f17e396cc0ae7031480faff8a796789e6c1776ed2fbabb6d74d4c8ac3",
    "067_economy_quest_triggers.sql": "072fbe090b0939c5839aefe1dca314316a60cf2da2a7fe6bf6dec68296c19e1e",
    "068_economy_event_quests.sql": "f9ba293b26ef63c71cd295c2744915cb34610854232975e6bc112ff9611a39ff",
    "069_economy_income_sources.sql": "095a03e93ed0d70e9bf7ea2b2124758b85466d5dc11db0819c13250e40893b19",
    "070_economy_monthly_counted_quests.sql": "b99336e341a5dbd21f6e9cfd0cf89fe460c057a5a27146bfe9a6885e3556ceb8",
    "071_economy_onboarding_quests.sql": "a1cb7c0c26d902df3f2b92fda71f9ff5fedeee5b422c7096faf081c2b3df5d29",
    "072_economy_quest_target_band.sql": "886bf2571382609120d40fe49eee403010bb409d8c29992e99f367ac9ef449b9",
    "073_role_menus.sql": "14454cc011ad8ac07615e51c1ce2bdd494cc89332f1cd352cd2d081049cfb9f4",
    "074_chat_revive.sql": "05c3ed3452c8288d8aa95963d24c457b03dda584d2c6ac17bc047c0faed67630",
    "075_xp_announced_level.sql": "0f4fe9c4132b57e2962cb12f4bda2f86a9314c23f9db3d3438711153dbd46a82",
    "076_chat_revive_ping_dials.sql": "ecc235702acda9ce0cbf944060fc8597e82ff300e639a11cca8c2e9b487dbf07",
    "077_economy_icon_catalog.sql": "3c0661affca0021683ecbe0b51de47b914f0b015a1c3428ece6c3b93a2252855",
    "077_qa_tracker.sql": "a1efeda19863e21608d6167b8ff7a21e258f2ae0c2ee8f481643f628769324b5",
    "078_greeting_watch.sql": "30ea2257b0de96bd5d6a540ceda243258b69b27adce7bb882b12f20d57ccf9f3",
    "079_photo_react_trigger.sql": "0832655c7d2ff94667d92ee84b43f7a4c2885dbaf6b442bedf4b5e8d541cafeb",
    "080_chat_revive_session_model.sql": "bcd47d01d3492d5fc71a9d3f7a4966ed88079b5555e5072568912b1dd27d96cd",
    "080_econ_kind_activity.sql": "13c94d4ce98a010d90d3a49a81eb4c47f625ea0cd6b1d3ab00808fe788b97226",
    "081_bump_attribution.sql": "78eabf1cce09528714249b4f9e65d641ceed3a33ce569c52cd759e9ee3d82513",
    "082_community_weeklies.sql": "8c4f3108f0021b6c3b7c9e21eb9420f8af15464d51e685e64c84405b085095f9",
    "083_dynamic_targets.sql": "b36b89089b151c9d6b2d991d9cee1fd39c987f312fd32f22217a061a288ac499",
    "084_board_addons.sql": "9ff11d07e3d17645ae2d9843be604accc6d0b1569a0adacd587347c5e0dfdf27",
    "085_pending_promotion_posts.sql": "7e1f1d1ccc807e864d5eba31dbd60a28df4ed9859e65b9ddc4a9ffb1215afdad",
    "085_social_kinds.sql": "368f0c01cc48bf92861c319c81f65f7dff3bc1a222e296529c777f6e37dfcc67",
    "086_bank_question_last_served.sql": "36b48c7179a2af3376ba3dd03680a14397c68c612eb8ee75223de9047a6e70ad",
    "087_pen_pals_timers.sql": "2a581832e6c18fc6064c7a9a95f8947e581d5a7e9d25396bd5680df054d88e4e",
    "088_chat_revive_rhythm_max_age.sql": "2ecf7187abe06c326003d705f333cbf98ae05ee9453aa84b6b05151d45bba72f",
    "089_announcements.sql": "f156caf7cddb18bb99b2d8ebb0943196f30b456ffa4580de6db04dc88b36c431",
    "089_quest_reroll_paid.sql": "50cbaa5f45fdebefa4525b91d1f9d28e5f9241cb6eebb396141d3ebf9e934272",
    "090_qotd_sponsor.sql": "73f550fc4003e16cbf69535267c959ca85d842c3ce3fbfe9b673eaaa6d0fc503",
    "091_econ_rentals_generic_gifts.sql": "7af0577ee5dc52bd1603ddd14e249a49d1f2242378d52e0275ab7657390d6cc2",
    "092_emoji_sponsor.sql": "c5992ac6089dc4bf686c39dd7ae09abb04f84e369cb3e65055d8030fb6e2db3e",
    "093_raffle.sql": "70c634b64cb86a95c7500782af47058ff918dd80d376c9f316a8ee2738b782fb",
    "094_game_wagers.sql": "dc90c26fc8520cc8f5f5b025a159422f642ee76f6a23b6a3ddf117446487adbd",
    "095_announcement_buttons.sql": "1aea679a80b0c1bd465fc3b3b78648bbebd320bbecde14a32cef028540e2caeb",
    "095_rules_ledger.sql": "d945d0ec994457ff5f90e45ac7cb58f9e6115f42d8a8ae297b7ced66c7419c9e",
    "096_pen_pals_blocks.sql": "a62a7b683e822a43c1d271f6276d0d3a4aa8407cfa80a5d1ddf014f161043277",
    "097_external_watch_multi.sql": "272df21543d3bca7d2dd3b1726157df97470759e2f4c87ffc9d30c9a0f70bae6",
    "098_role_prune_events.sql": "c0acdc5e912ed705f1815cc66f355dbb160123a5a8110782ba728876c03a41a4",
    "099_external_game_payouts.sql": "c9c333261e66455eebb8243a06d367212ec7753f84c0273f67c1996745dfabcb",
    "099_photo_post_trigger.sql": "7a046b2364b73c65f712aa364e5e0360584abee58d17b19bb8d6dedacc31a3f6",
    "100_demurrage.sql": "7eb087ff35ddd173b099e2458c7929cdf2473c1ad95ac76d964bb5cf76bcfa65",
    "100_external_watch_catbot_kind.sql": "24e2a9b6763fc5c5abaf60027a6bda1942db52005e12d27e9979d209dd26b6f2",
    "101_econ_photo_rewards.sql": "9c89c4f78712532f1092b404c5964fd35be3dff7510119a68044642122abe598",
    "102_econ_disable_xp_conversion.sql": "a64659eb5c25777a17c0ff3bd7894efc237a01d4756bf1fabd59b4ee49e730dd",
    "103_econ_lower_monthly_quest_rewards.sql": "916301968f6af0640b73b8196f68c0bc2c2d49fba8d12b160f3300b42217ff89",
    "104_econ_sink_tuning.sql": "d3017da12991e9f8acfa5eef14d8f0bb69dcf2340885a045253a311da2797a33",
    "105_econ_drops.sql": "dae3091edc13d58e7cbb0e8c3063b57fbec4534606e6a2794a82c3816752d716",
    "106_econ_quest_pair_tag.sql": "173e65bc178d6f15a9d48d50273adec4456da38a5c0cd34a3a272fb952657b38",
    "107_econ_role_holographic.sql": "6f411b037de56701ff0e716cc5058088f6ea9b03ea7a9e48c8614061bc7fd123",
    "108_econ_pin_of_day.sql": "0f670a19972f4f8fe2624c211ebf9e081aa3b5e3dcf1bcf859ffe16159daaa7a",
    "109_econ_bounty.sql": "84a298b28a905e94a4cf32d81549e67d44f380bab445b950d1cb0b8ebe1a6651",
    "110_econ_quest_pool_snapshot.sql": "054f4a5e933814da6b60f7a4bcba9194858296139ed728821565a3b1700abe64",
    "111_econ_community_second_slot.sql": "2ecc97e9029375a01b4ddbb4e11c8ee175d08ef6828e89ff709829a8087c919b",
    "112_promotion_review_cards.sql": "5ac3495ba3e2f3f3f3d6035603685262bd253565a61df74eaf2772fcb797c511",
    "113_casino.sql": "39e2afbbd26c355445e4c4dd6fa06a84b7551199c5df090d833a36d418aba701",
    "114_casino_fancy.sql": "ac824c042346930a7e8ce20c7909804c60af2cc3377e1f0e42cb78c07b536ad0",
    "114_econ_streak_shield_price.sql": "55dc6ba1e7caae4140e7ee9df960b2c9a4df15526fdcbd637cb33af2cf590c15",
    "115_branding_product_names.sql": "9d5fd524c7f4522cc69659775a17027fbbc9254852a31d5c9b1b610b11c3f142",
    "115_intake_cards.sql": "b669644aa44a01756d9b648b44782e5bc071f97488d1bb78186181ce8f5ae609",
    "116_intake_reference.sql": "e7257d878307085bdf2a0965b22261a65cd43fe8239531e6fb327112aace2225",
    "117_voice_follow.sql": "19fa12ffb4e4a1b05a86532d0b7485e316aa0b00b98660302702d1e8d8049f6e",
    "118_econ_community_progress_snapshots.sql": "ee125601ce6de238345e5a4b215939ef5baf455f35775c3f7619cb90a7821b86",
    "119_econ_kind_activity_occ.sql": "ba458c583165358c03e9c9682e148206166798ed43b237b0ff7f3e3c963e5124",
    "120_econ_spotlight_kind.sql": "a774e0ccab8a488e8c6e9d5c8db3abd89635346b2625f12c76313a6846e29531",
    "121_schema_drift_repair.sql": "031ca8c0be90bef6dd047df62161e2b30edd131881a3605f52a6e3f1fb0ec90c",
    "122_games_guild_scope.sql": "8e931812114edd1d434b538f27cdc388bcbe599fb8c0bffee7551eea2bf07bbf",
    "123_econ_auctions.sql": "1a835bcfef0e3b0a23ce1418a198eeb1c45a19f77f050bc70feb973cd62f63a0",
    "124_legitlibs_template_guild.sql": "764a08a9e1c162053613956ed4cbdcee2f271774bea3998ca8c2c7933c15c64f",
    "125_econ_monthly_community.sql": "8126936b6986d425a1b18d7f668ee0ea2069739e8ccdcf8b2fd1072b7b4cad45",
    "126_pen_pals_room_visibility.sql": "3a1c4bc5be4d9eb6e0100f9d9924f20cbe63718380910e631e8efa7cea7587d1",
    "127_casino_derby.sql": "75cf233431a6c453d68f67f7f693e4ec4e69d827d52de87cf5cedca2a1f96a31",
    "127_pen_pals_intro_message.sql": "503606b71425810f8b03f1a354b2686a1c43a9bb997427c9d0d28446ea514422",
    "128_casino_ticker.sql": "6f6bd40514169ee879e3a41352c47aa56fdaa753220ba2ee1761b7c7ea524f7e",
    "128_pen_pals_match_mode.sql": "629794282a4ed9e381e99c44f41bf1869b0121c9175c045113f330098d5e5950",
    "129_casino_daily_standings.sql": "2816406fe068dc5b4c6f415514c534d8304ace58e67868b929e3199f80fc261f",
    "130_casino_baccarat.sql": "1b047e2fc552e4e154645f27add45ac384671b1b788a457b8ee0419df17c89f0",
    "131_casino_dice.sql": "5ae6ba9a7e2d4d486b68ac407bb0f1151fdc846632b082fb7fcc87e31c7f5a3c",
    "132_casino_war.sql": "2b3a1471814a48d53df476701e29f846b3f8f875261ac618bb50735138b227b3",
    "133_casino_keno.sql": "702d37f339b2a3261fc40141cee4c4e4ebb87832058fff4de069f2b5972e2445",
    "134_econ_setup_marks.sql": "10a52406735735f685e84dbf43af23a2ba7fe769aad787e21d4a179e946ff926",
    "134_todo_board.sql": "d1e49a6ff00c8476fb87fe8f58b1aa64d9ff2db5278c3a6350b73120bfae1eb1",
    "135_external_watch_per_channel.sql": "df4fab2fff714df6b677bff1d4b813fdde802558a9a2eb72008d057c45e40b63",
    "136_drop_guess_optins.sql": "2d1f71c1fac76d65dd77b3080614c8806b6436180a49c86cd82f8e10a81aff71",
    "136_inactive_sweep_exemptions.sql": "c9f741c52ecb9782e555f2ee677cffdd9131a0be627095c6f6f349859def903f",
    "137_bump_tracker_failure.sql": "d9208a6314b93ad06784fcd66754c58ac9911f843efc8b8574a806a0050a25ed",
    "138_econ_intake_rewards.sql": "9a1b251a14e754ac30b211cfd9165154fdc70759d9897c1f14af38744959afb7",
    "139_usage_telemetry.sql": "92b4726f7919f43ac2e68e06fd0ce0430b2ec69e234bd6cd57bf8937e529c4e8",
    "140_casino_pools.sql": "2afffd40c0e95a1ecc2776840bfc2004a3e282c6c44b460a7046b7bd1791c50a",
    "141_event_echo.sql": "972e05b0d6c78517b69c89ccd7c7b7efbe250a9dcfbbc6accff82e2b3c86e034",
    "141_nsfw_classifier.sql": "3dd033e27f1455e410e37ef105990cc600aea89c926e4df3926549fab92cb974",
    "141_xp_level_5_cards.sql": "a6c4403b6427a6b66beec99a7cfa7d3058b550e3f559175683df1f4dbd24b063",
    "142_auto_react_tips.sql": "b3e55f5396682f69f23e477f82a9b0801e3861fcc20eae1177181831648a114e",
    "143_reaction_tips.sql": "d6b70287fdb74d553f6904f3b8f05517e2b6abb3d8e24f40c90b5e81b9cadcf7",
    "144_grant_permissions_seed_mods.sql": "bb484ee7c692944ffbf2216f81419774d74d68824a6d97e821bd106a90c55c2a",
    "145_anon_audit_log.sql": "c3ea80b4b93f1a3c6c5a9277123a70970450a306d42b656e0a1f83f735c60e4c",
    "146_no_contact_pairs.sql": "1e314cef0bc1c168c19740f231c72ef5c84199e56989f6c588b693806542c3e6",
    "147_marqo_nsfw.sql": "6b82ce61eda840b19398ffe294780dafa9f1498e6ffd02c87b587368bbe0725d",
    "148_pools_metric_rotation.sql": "059b691064e2d54095dbfa6670be40580caa71de12e2a3cc72ee96c39711f43c",
    "149_bios_archived_at.sql": "1976c7199244f48edb43b737405d6f7552cae1c3f96dcf975901cd906f0e24b2",
    "150_pen_pals_reply_reminder.sql": "89986e4e3ae7c03c29b876ede0cd7543628cace83baa79f65b433069ce4bd709",
    "151_confessions_anon_audit.sql": "af619be5b7749f3976e1827c683723cefe5bd051e6b996d6243caa0b11aea42f",
    "153_xp_events_guild_created_index.sql": "9b7248bd3b0b8e51ddea38502d9266ef025e1ca806fc2af75812e94e3fa443ad",
    "154_guess_consent_record.sql": "9a3d975acf3323005242b7058298a235546dff67b7097488da7b993715e59872",
    "155_messages_deleted.sql": "0a41973d05a26adfc6194905f74e5a04d365e259845386b351d1b4b65866cd98",
    "156_mention_awards.sql": "181346ff2d5e893572654eac412a1e1a6af657b01764d1d39e52b6557b918227",
    "157_mention_award_conditions.sql": "52d1e4129a7d1d434534208ff594961b0698350280f8d6372519e554c3791529",
    "158_casino_private_rounds.sql": "5c7689ee08db66d3cd1af3d081866cbc176721de7c9112e2996686d0ce82f963",
    "159_econ_color_catalog.sql": "260b181c1c1466b023eed57a239e9520585961bc459a7890ae7755fbd7c59da5",
    "160_pen_pals_pool_events.sql": "8056a0bf409f4aa3ee5ed26db7fd18f9827289436996e8487626509ca3e9bb5a",
    "161_auto_delete_retry.sql": "a89a7a13485b44adbf3da07fb4c449a201ee73c2b4b54f55ac61bc67037bb8d7",
    "162_casino_win_history.sql": "c099d02d4a069c092f1c2e4701ff2260c126e76d1883d53254148753e44c1374",
    "163_known_channels_thread_parent.sql": "b943be37ac778562b1ee7d825235532949afe50fd00b268c9d281f65d198758a",
    "164_casino_mines.sql": "34c88445109559e8d511fb63a6a5e138eb36ba43d23b86846098c746b3160820",
    "165_music_playlist.sql": "b7db3594bcd149193f93675548244cccb17e330557a9d885ffc9e85d4825be72",
    "166_todo_chore_board.sql": "1ccdde7024d0e9ecd7e54380f50b2b951964f96516b2856d54ee86012c330ae0",
    "167_survivor.sql": "a8e67a648b8a36a6d908bc072d6afdc8b2079dac5378487794321887dac718ce",
    "168_econ_ledger_kind_index.sql": "ddea7b5db511ad12ef6ed552669766c0b9e42fd967e62a41e1ca80b6fa2943f8",
    "169_music_playlist_retry.sql": "18cb24d97dd5ba19c6430a558246e37f47affb277cc698a865e71fc0f91ff205",
    "170_drop_expand_albums_dial.sql": "334fbd307ec7cefbb8d5586739e1c25124fbc39b554d5ef28aee642b9e89f0ad",
    "171_music_playlist_best_effort.sql": "0f491df808fdc74e5ea7ac2d8c7c81bf2ef9c0bf39f442e6f382552158748964",
    "172_survivor_drop_flavor.sql": "e0b75da4ef268af2c9a2e980b3eb3ae925819535587d04b628983be16740b5f0",
    "173_risky_pending_question_age.sql": "2500879ae495c1bdb2585a675192b08bf89339609e0b0fcfa89852979693f115",
    "174_pen_pals_optouts.sql": "f7b53374d16eb12ed85491988c0b865626b921b06761e659ccf3af8747ba3d42",
    "175_mahjong.sql": "b04e9a6c915b4c3506f4d0472477fd1fa623859452bb78a6faa69087cc408b05",
    "176_mahjong_prefs.sql": "1fa374a0bf3a602d023a7d1fbafc1e6e1f7dabd352fec8d74c533b1815becbe3",
    "177_duel_nick_stake.sql": "51b1963ebd811e9edd10d5884afb2d07b278cd94e9124ba4bfbe51ad01a0eb74",
    "178_mahjong_practice.sql": "a74301f82be46db6a85b6b378d503fd738dd536b5e865013d2088c086cf9b586",
    "179_econ_shop_items.sql": "856cd6273015b708c25f43905bc778cefe0a7e191de565d93c0cb38c27841216",
    "180_todo_board_merge.sql": "0b9c292533f8b5d977fd50f986e35b1b611d3a2bf48fe6a4de7cf99e33dbecd3",
    "181_mahjong_hand_timing.sql": "786d9ebedaf119073ca12bd895af054c42fac97695953679ab411579d021540a",
    "182_shop_perk_toggles.sql": "7af096643454a60c8c9ef06aaba4779e5ded3f2a0ca51f9c08e32d44b3b5133c",
    "183_setup_suggestion_dismissals.sql": "92d5c52c96e6b9794ddc7f53f4c47b5e574f254f8ee50a2808e0311ccf37a69a",
    "184_drop_games_consent_and_guess_reuse.sql": "d7db57ab39f9dde264dbdefce51f367745fa47ceed559579310376652d360605",
    "186_xp_daily_rollup.sql": "396c46a8e77627afd157a1bd1e42d45b788d1075f8da9c90e31e4ad4722a2cff",
    "187_xp_rollup_watermark.sql": "9d387d598671ac174fa678639f9d0ea34a41e3f5fbb1b420ba39f8e7ab8c9a00",
    "188_flash_themes.sql": "c9972c905714ae5157ed4890fd8a627178e680294903bebd00840590207cc8ba",
    "189_drop_wellness_partners.sql": "8c1c20d3b74a3605cbab61ebf47814508bbaad46164fd9cf7933288ce628d11d",
    "190_login_digest_cards.sql": "b5adab1ec233b4adf1cbe36a179c8c4ea7ee3bee42778fdd927255760dbcc129",
    "191_drop_dead_econ_config_keys.sql": "bf92b60243969147323b1de2fbf74a3e3c7b7fafa6b20a9e8ab8f3e9d0c45a5b",
    "192_feature_rotation.sql": "901715d8ee810b3adf56b18c7a1eb78561fb838a1646f6379fa9607f2cd17be2",
    "193_drop_orphan_config_tables.sql": "6eda57aae8633202389921c14d78a20c6e5cc05ef6bc46bcf974171e9fe2296c",
    "194_drop_dead_game_config_columns.sql": "73fe87c16e2dd08513c011b760a0ce5aaada8fcc9c048853bc70d6e7cea18e35",
    "195_delete_dead_config_rows.sql": "5af5f6832563c8f9735c090297c451f766da2a8f232bf718f3755ed897d5eaf6",
    "196_quest_snapshot_featured.sql": "f2fc3dffe15be875ad683f83311832688a21de072137c0fd84f9beb6da21f040",
    "197_feature_rotation_games.sql": "bf3b2ce9b25d33386a74cc029814f12d5a498f036f8bc1892a4c081f64d285c3",
    "198_ping_events.sql": "b185b2bf26c19a8845cf0c46c34915e44041e88760901d5fb43dd9b78682678e",
    "199_voice_note_cleanup.sql": "f5c7e1098983826657826c076bbb622db5b0d77bc03af38e21274277c7ad6c26",
    "200_birthday_channels.sql": "b5d02a2d2548e2c71cac5db3cdba2c0273d52486be8f03a6fbcab00277eceab1",
    "200_confession_approval_queue.sql": "ba8c11d7d140d82b8a8af88278c2a4f7fdf496f1953c3217a6f20d8f2d6597c0",
    "201_newcomer_funnel_indexes.sql": "e53401ff68f47c3a9f450b646cfea283be8f7544a09b6aba09df4129500cc2b8",
    "201_todo_auto_complete.sql": "959deb2b1fddd07a3ee1367efffef0a895f529bed05140717213ef82652c4e34",
    "202_policy_ballots.sql": "4466dc13f4bf96bc5a36474e6386bf4ffa1d0c7d86b4bbc40a3fa0841435bbb2",
    "203_bot_role_provenance.sql": "9ce4922e065e6c1aebc376e98a9dc6e245f70e3b04ec469a0766846c5307fd98",
    "204_games_active_games_guild.sql": "3c768ed5f21f5aafa9604c1303540ffc7398744dd08d0f96a89a2e85c2ee5df5",
    "205_games_scheduled_last_launched.sql": "249bd2b6146d4f6da71c40caae968377c1ecbba0a6b6bc0fbe785ee36c0cf64b",
    "206_casino_daily_comp.sql": "fc8531142ef2daf485277c284ac264e89be5850888a55265511201843db237b7",
    "207_duel_naming_reason.sql": "3ad420ed5f72373f02dbe8e120c9cdda21271dab6e229aa95132da26dc687ece",
    "208_chicken_hidden_crash_hp_min_hold_mc_reruns.sql": "6b5d344972631915fc39b8adf90e6b65dcad5b7de7170f71df3eb8f0083b38e7",
    "209_mahjong_results_practice.sql": "34c98d3fbc060bed251c1bca96dd2125cd1414661c9d006854ef72bd409dab24",
    "210_risky_payoff_chase.sql": "1ba13f0661171705ac06b2d457047db484b7387fc3c274a2b7fe128ea00352c7",
    "211_confession_alias_created_at.sql": "a461ed9d3a5fbaa3204d588018bd03f4ef1f819109950acb168b959ace2cb52a",
    "212_mahjong_nudges.sql": "b09f0e0b080421b2bcae372585ef84c3f21fb55f020745fb066070a1bdc3c9a8",
    "213_risky_fallback_backoff.sql": "e4dd1b8f41cd567a104beda9cfba7aaa8dbf620a085f7bd4fb0d1cafff51c16c",
    "214_drop_member_gender.sql": "7c32f9b106370485982717241d0c766298da51feca1c3fd03fa18b697d51f605",
    "215_needle_drop_status_reactions.sql": "121cc3973298ba3c86bd6eaf80e10982747903a10ab3481443866c9ec98aef56",
    "216_delete_guess_nudge_config_rows.sql": "3b422a571cedd4398d4406e0541b84cc0ef37a3ef01743efa2ab46071b289e65",
    "217_delete_dead_guess_veil_config_rows.sql": "a5705bf12da9b09e19779400d5c3b81139d749bb162a44a53492678b1dbf319b",
    "218_departed_guilds.sql": "345e41c8d281b53dd4e24b7c29e27d47a3c4a2f9933a45cb0c150c283b1bab85",
    "219_policy_ticket_visibility.sql": "9a415a3d4a10637386183c9e337b6376073b3f641255b93d10527973899e8cad",
    "220_dead_clapback_player_limit_options.sql": "b9bde2f2a77d1b3377117d7faaf16ff7f26a2b3266f42cbed56b89c130fd8062",
    "221_survivor_hosts.sql": "415509ca341cbffb981a3e64c4e8c8d1e7cce1e78bc8c7921a8bf6441d5a6bc6",
    "222_confession_typed_reply_notify.sql": "071892f25a5e955f42fb8f3dd2e1bf1e704b0130e9a10dfaa33b9aa2c73c0883",
    "223_needle_reply_notify.sql": "d3fe4fefbe8839a9cbff90d8ab30d3f80ec6341de1db884d6df01ccf0bf11898",
    "224_todo_change_cards.sql": "6e60e31228c38e94e44e3f8c853ca23e5f35802f8e145dd47df9cc4daede2670",
    "225_bug_list.sql": "1018ed955878aa6c65eae3087290ef1404bc6d2e44e27added8addd269f2e6cb",
    "226_game_lobby_ping_rename.sql": "ff6a0b1d305354e74e98ceee943fcdf6f02f6da6cd17d7189e61165a7ca6f3c5",
    "227_remove_rules_watch.sql": "aa5714deb9deefe0e6ed828c3925c2e2ca2a34666b00bb16e0833f838766a501",
    "228_remove_message_content.sql": "7698ea6b780722c7c0b78abc863f9a813feec47f4a7c8e6307dc2eaf559512c3",
    "229_drop_social_graph.sql": "f33f268c6dcc274cac888bbcf3d3e7960c00f9d138730ce36a1fa911ce358199",
    "230_remove_image_guard.sql": "0bd08ed12ccd3d203d8360db5cced8baf7f7cfbc3b3fe2724666b8d9e570b0aa",
    "231_departure_erasure_queue.sql": "82de9806149647b343f62040c5393c6ecd4e3b9c46cc898661da5a0506a47d73",
    "232_drop_dead_pressure_member_tables.sql": "db684b53746db82b17722078d13e0f9a71a8d8816f679ffbd81ea6d9d4535c04",
    "233_drop_dead_april_fools_tables.sql": "d4b4a142bac3b83e679830262d4743d8f2b1e3d51247044445b3e7af7b236e89",
    "234_drop_quality_score_leaves.sql": "0bc9951e449e6c1235b81a5a28a00a88942954f9de5bfc4bc263cea33c89f677",
    "235_schema_parity_app_ddl.sql": "e69ac579fd375a911d7ed8d693c70ab77aa0f4456ae2a3f7e3bae6db5c78020f",
    "236_branding_panel_banner.sql": "85125c8f5a7189bcaaf9e7d846fae1ab4e67cb3e677794907e6285d2025c4d45",
    "237_break_holds.sql": "80049f464cc4d7c02940464a8b16bb747944c4c2288f24d9726ebecd2f3b4076",
    "238_branding_panel_footer.sql": "a1f59f7405b57834724141f90f1cae64d91fe64bd4621c0903b81aec03b7bb4b",
    "239_branding_panel_banner_title.sql": "59c0df188a7f1bd09bce52c7433bc9744c511b89147945b91c6fba83dd24f574",
    "240_posted_panels.sql": "3885c7319c15e13a1cf89c45bd09912b8d7931f8399efa7c21b47a8c2e5b1486",
    "241_bug_screenshots.sql": "1a760f3e952d5e4a844a84a570dbaedbc2e2076ef2527677ea2299bc3dfd476e",
    "242_branding_panel_header.sql": "51bd3a96dd2dd6b9bef635f168aa608957656f2fe49cf9263926e7be569abe8b",
    "243_dm_batch_pending.sql": "610e5b1bc34fdefb017c9629d301fabc5e44af54b67c360e4cfcca502f096ba8",
    "244_needle_reply_optins.sql": "1210ee148221b3fcf3a19d7f0f0ebe90e8198eac79eb4c2f1bd116f8d78751ad",
    "245_intake_card_stage.sql": "ebb9e0cd9304b9b3c4324b65eb1e33e2183cfdee4b2cd8cab24bdc5bcd744134",
    "246_needle_require_attachment.sql": "84cd3b38d8064c5ee4819f2c2d1d521c0ea6f6ea9d50ecaafdf6605f434a696b",
    "247_event_echo_message.sql": "fd48a6c089196bbd43930f094748df43605ab6ddb1c2a0231eb6a09ff663fa42",
    "248_greeting_watch_notify_list_retired.sql": "a039f4be37917c8cecb916690c7ef3713094be60440a116c129898f8ef8620ea",
    "249_econ_qotd_kind.sql": "4b10cc8ed3b867673943818dd25f672162f3d2d5900d359b870501d9c1e0e15e",
    "250_retire_qotd_sponsor.sql": "fd0fef086774357964f41cd690f902991cf53da83b60848912ae9030127e073a",
    "251_chat_revive_mode.sql": "3e15e8d0bee94582765e245b270bcb29a87dae6a50885ed4c16827f346e32e0e",
    "252_needle_auto_archive_minutes.sql": "29c0d78e845001b7ab211be4bde55fc75b0ef16c426a360598ca2c9cb6b6e0ac",
    "253_branding_card_strips.sql": "89abf009548158f7adcedb1fcc8fb591a91bc080f48841d62bba449b74ee6fd9",
    "254_drop_bio_question_weight.sql": "37798cd13247e335638fcf388bbaf801aa263642808b183c1a47d463df7a1a9c",
    "255_drop_wellness_default_enforcement.sql": "fd18affc7407e2bf47ff092ddc2cf0bd30fcdcec6fd6acb5fc57533da1b831ce",
    "256_guess_round_expiry.sql": "f54c649772abb0e1dc54598672c28917e265bd7bc5aa292e01f2768e3fd0fe43",
}

RUNNER_SHA = "990aba3fa426d185e68cc6d20f2a03d1a3acf9efcc4939564f722a341396808b"
# --- END MANIFEST ---


def test_no_shipped_migration_was_edited_without_saying_so() -> None:
    changed, _, _ = _compare(_on_disk(), MANIFEST)
    assert not changed, (
        "These migration files no longer hash to what the manifest records:\n  "
        + "\n  ".join(changed)
        + "\n\nA migration is applied by FILENAME — prod's schema_version keys on "
        "the name, not the contents — so every database that already ran this "
        "file keeps what the old version did, while a fresh install now gets "
        "something else. The two diverge and nothing else notices.\n\n"
        "Almost always the right fix is to put the change in a NEW migration "
        "and revert this file. If the edit really is deliberate — a comment, or "
        "a bug caught before the file ran anywhere — run "
        "`python scripts/migration_manifest.py`, and say in the commit "
        "body which of the two it is and, if the file has already shipped, what "
        "reconciles the installs that ran the old version."
    )


def test_every_migration_is_in_the_manifest() -> None:
    _, unlisted, _ = _compare(_on_disk(), MANIFEST)
    assert not unlisted, (
        "New migrations with no manifest entry — an unlisted file is one this "
        f"guard is blind to from here on: {unlisted}\n"
        "Run `python scripts/migration_manifest.py` and commit the result "
        "alongside the migration itself."
    )


def test_no_manifest_entry_outlives_its_file() -> None:
    """A rename is an edit that moves the bytes instead of changing them.

    Renumbering before a migration ships is routine here and stays green: the
    old key goes, the new one arrives, both in the commit that does the ``git
    mv``. What this refuses is a file quietly leaving both the directory and the
    manifest — which is how a shipped migration stops existing without anyone
    deciding that it should.
    """
    _, _, orphaned = _compare(_on_disk(), MANIFEST)
    assert not orphaned, (
        "The manifest names migrations that are not on disk, so they were "
        f"renamed or deleted: {orphaned}\n"
        "If this was a renumber before the file ever shipped, re-run "
        "`python scripts/migration_manifest.py`. If the file had already "
        "run somewhere, deleting it does not retract what it did — every "
        "database that applied it keeps the result, and a fresh install no "
        "longer gets it."
    )


def test_the_migration_runner_itself_is_pinned() -> None:
    """The file that decides what all 253 migrations mean.

    ``_strip_line_comments``, the split on ``;``, and the ``duplicate column
    name`` tolerance are each load-bearing for every migration at once, and two
    of this file's five commits changed one of them.
    """
    actual = _sha256_text(MIGRATIONS / "__init__.py")
    assert actual == RUNNER_SHA, (
        "src/migrations/__init__.py changed. It parses and executes every "
        "migration, so an edit here can change what already-written .sql files "
        "do without any of them being touched. If that is intended, run "
        "`python scripts/migration_manifest.py` and say in the commit "
        "body what it changes about how migrations apply."
    )


def test_the_scan_finds_the_migrations_it_is_supposed_to_find() -> None:
    """A guard on the guard: an empty glob would make every check above vacuous."""
    files = _migration_files()
    assert len(files) > 200, f"only {len(files)} migrations found — has the glob broken?"
    assert len(MANIFEST) == len(files), (
        f"manifest holds {len(MANIFEST)} entries for {len(files)} files"
    )
    assert (MIGRATIONS / "__init__.py").exists()
    assert RUNNER_SHA, "the runner hash was never filled in"


@pytest.mark.parametrize(
    ("on_disk", "manifest", "expected"),
    [
        pytest.param({"a.sql": "h1"}, {"a.sql": "h1"}, ([], [], []), id="in-step"),
        pytest.param(
            {"a.sql": "h2"}, {"a.sql": "h1"}, (["a.sql"], [], []), id="content-edited"
        ),
        pytest.param({"a.sql": "h1"}, {}, ([], ["a.sql"], []), id="new-file-unlisted"),
        pytest.param({}, {"a.sql": "h1"}, ([], [], ["a.sql"]), id="file-deleted"),
        pytest.param(
            {"b.sql": "h1"},
            {"a.sql": "h1"},
            ([], ["b.sql"], ["a.sql"]),
            id="renamed-same-content",
        ),
        pytest.param(
            {"a.sql": "h1", "b.sql": "h9"},
            {"a.sql": "h1", "b.sql": "h1"},
            (["b.sql"], [], []),
            id="one-changed-does-not-hide-behind-its-neighbour",
        ),
    ],
)
def test_the_detector_actually_bites(
    on_disk: dict[str, str], manifest: dict[str, str], expected: tuple
) -> None:
    """A guard that never demonstrates a catch proves only that it ran.

    The rows are the comparison's own behaviour, not this tree's: if an edit
    ever makes ``_compare`` return empty lists whatever it is given, the sweeps
    above would still pass — green, and blind. The rename row is the one worth
    reading twice; it is the only way a shipped migration changes with no hash
    anywhere disagreeing.
    """
    assert _compare(on_disk, manifest) == expected
