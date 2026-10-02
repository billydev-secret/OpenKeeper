# Privacy & reuse in the standards — 2026-09-13

**Scope.** Two questions about the standards set, not about features:

1. Are privacy practice and reuse actually **checked** when a branch ships
   (`/dk-ship`) and before a batch goes live (`/dk-regress`)?
2. Are the documents those checks read — CLAUDE.md, `design_guide.md`, the
   `standards-review` agent, `common_modules.md`, `plans/common-lib-round-2.md`,
   `data_register.md`, `privacy_spec.md`, `gdpr_runbook.md`,
   `no_contact_spec.md`, `dev_sessions.md` and the MCP conventions topics —
   still true?

**Method.** Each document was read against the code at `2432d9fd` by parallel
reviewers, every claim they raised was re-checked by an adversarial verifier
before it was kept, and a completeness critic asked what the pass had not
looked at. Claims about what runs were checked by running the code
(`privacy_service.SUBJECT_ID_COLUMNS` has 88 names, `LIST_VALUED_MEMBER_COLUMNS`
12; `guild_purge_service.classify_tables` on a migrated schema gives 261
guild-scoped, 33 child, 2 channel-keyed and 10 global tables). Production was
read only through `mode=ro`. A second adversarial pass checked the edits in
this branch against the code before commit.

---

## Verdict

**Question 1 — mostly no, now partly yes.** Before this branch, neither check
could catch what it was named for:

- **Reuse at ship** was reviewed against `plans/common-lib-round-2.md`, the
  consolidation backlog, and never against `common_modules.md`, the map of
  which module already owns a job. The agent was told what twins to leave
  alone, not what to reach for.
- **Privacy at ship** had no brief at all. The agent's skip-list handed "a
  table with no register row" to `test_privacy_register_coverage.py`, and that
  test — like `test_guild_purge_coverage.py` — **is never selected by the
  scoped gate for a new migration or store** in a session worktree. Both bite
  only at CI or a full run on main, which is after the merge.
- **`/dk-regress` ran no privacy check.** `scripts/privacy_coverage.py`, the only
  tool that finds member data by value rather than by column name, was
  documented in CLAUDE.md and run by nothing.

All three are fixed in this branch as documents: the agent now opens
`common_modules.md` first and carries a **Personal data** brief naming the
blind spots the tests cannot see, and `/dk-regress` gains the coverage sweep as
a reported note. The gate's test selection is a code change and is queued
below.

**Question 2 — materially stale, in the places that matter most.** The data
register described an erasure the code does not perform for nine games tables,
and a processor list that said YouTube playback stays on the box. The erasure
runbook's hand list of list-valued columns named a column that can no longer be
written and missed seven, and it told an operator the local backup window was
about a day when it is at least two. The reuse map lacked the seams added
since August and had no dashboard section at all.

**What is sound.** The production sweep is clean — see below — and the
register gate, where it runs, does its job. None of the stale claims caused a
wrong erasure that we could find: the games gap leaves data behind rather than
deleting too much, and the emoji refund bug has never had a row to act on.

---

## How privacy and reuse are checked now

| Check | At `/dk-ship` (work branch) | At `/dk-regress` (main) | CI |
|---|---|---|---|
| Register row for a conventional member column (`test_privacy_register_coverage.py`) | **Not selected** by the scoped gate for a new migration or store | Full gate ✓ | ✓ |
| Guild-removal classification (`test_guild_purge_coverage.py`) | **Not selected**, same reason | Full gate ✓ | ✓ |
| Member data by value, whatever the column is called (`privacy_coverage.py`) | — | **Step 8, added here** — a note, never a blocker | — |
| Unconventional member column, list-valued column, app-code table, new processor | `standards-review` **Personal data** brief, added here | Same agent, in the step 5 scan | — |
| Reuse: an existing seam re-implemented | `standards-review`, now pointed at `common_modules.md` first, then the LEAVE list | Same agent | — |

---

## Findings

Severity is the harm if the finding stays as it is. **FIXED** means fixed in
this branch; **QUEUED** means it needs a code change and gets its own branch.

### High

**H1. Nine games tables are named in no purge statement, and the register said
they were.** FIXED 2026-09-14 on `standards-code-queue`: whole-record purge
(`privacy_service._delete_game_records`, reaching `guild_id = 0` history rows),
`hot_potato_style` deleted per member, and migration 232 drops the dead
`pressure_nicks` and `pressure_cooldowns`. The register's `games_*` row read "DECIDED — anon family
via audit sweep; per-user rows fold into econ_purge_user". Only
`duel_cooldowns`, `duel_group_cooldowns`, `duel_nicks` and `hp_group_games` are
purged. `hot_potato_games`, `hot_potato_style`, `pressure_games`,
`pressure_nicks`, `quickdraw_games`, `mc_games`, `chicken_games`,
`games_game_history` and `games_active_games` are not. Production holds
277 + 74 + 29 + 12 + 9 + 7 + 7 + 1 rows in the first eight (read-only count,
2026-09-13). The Risky Rolls row
compounded it: resolved rounds are copied into `games_game_history`, "same
purge". The register now says **PARTIAL — open gap** in both places. The export
still sees these tables where the member column is conventional.

**H2. The emoji sponsorship expiry sweep refunds across guilds.** FIXED
2026-09-14 on `standards-code-queue` (the sweep delegates to
`expire_stale_pending`; guild-isolation test in `test_economy_loop.py`). `economy_emoji_service.py:396-412` selects expired pending
submissions with no `guild_id` predicate. Its caller, `economy_loop.py:1197-1221`,
runs once per guild and filters only the *notice* by guild (:1220), so guild
A's pass refunds guild B's submissions on guild A's expiry dial. Pin, theme and
QOTD all expire through the guild-scoped
`economy_submission_store.expire_stale_pending`. `4348caf5` added a comment at
:63-67 calling the global sweep deliberate, which will read as permission to
leave it. **Production has never held an `econ_emoji_submissions` row**, so no
money has crossed yet. Needs a guild-isolation test that fails first.

**H3. The scoped gate cannot select the two privacy coverage tests for the
change that needs them.** FIXED 2026-09-14 — `gate.PRIVACY_COVERAGE_TESTS` is
selected for any migration or store change, guarded by
`tests/test_gate_scope.py::test_a_new_table_always_selects_the_privacy_coverage_tests`.
Original finding: `gate._tokens_for` (`scripts/gate.py:165`)
maps a changed file to tests by filename token. A new migration
(`src/migrations/2xx_*.sql`) or a new `*_store.py` never produces the token
`privacy_register` or `guild_purge`, and the migrations full-run fallback
(`FULL_RUN_IF_MODIFIED_PREFIXES`, :103) is deferred in session worktrees. A
branch can therefore merge onto local main with an unregistered table and
nothing fails until CI. The fix is a gate rule that always selects both tests
when `src/migrations/` or any store changes. CLAUDE.md, `design_guide.md` and
`privacy_spec.md` said these tests make the register "a gate, not an honour
system"; all three now state the caveat — **FIXED** as documentation.

**H4. `standards-review` reviewed reuse against the wrong document and privacy
against nothing.** FIXED. See the verdict. The reuse misses it let through
since the scan started are listed under L6.

**H5. `/voice knock` and `/voice invite` never consult the no-contact list.**
FIXED 2026-09-14 — a knock reads as delivered and sends nothing, an invite
reads as done and grants nothing, a pre-pair knock accepted later opens
nothing, and both record an attempt; see `no_contact_spec.md` § Gated
surfaces. Original finding: `voice_master_commands.post_knock_request` posts a knock to the room
owner, and `_apply_invite` grants the room's permissions and DMs the invitee,
for a no-contact pair as for anyone. Room *permissions* are gated
(`effective_blocked`); these two contact moments are not. `no_contact_spec.md`
now lists them as ungated rather than implying Voice Master is covered.

### Medium

**M1. The member-facing disclosure report says two things are kept that the
purge deletes.** DEFERRED 2026-09-14 to the end-of-project docs review, with
M2 (member-facing text is batched there). FIXED there 2026-09-16, together with
nine other stale sentences in the same copy and the `/delete_me` rights line. Verified on 09-14: the anon claim is
`disclosure_copy.py:121`, and `privacy_service.purge_user_data` deletes
`anon_audit_log` by `actor_id` and `target_id`; the opt-out claim is `:126-131`,
and the purge deletes `pen_pals_optouts` (the "any block you set" half is
true, since `pen_pals_blocks` is preserved). `services/disclosure_copy.py:113-122` tells a member
"the audit record of anonymous posts is kept", and :126-131 that the Pen Pals
opt-out record is kept "so that you are not matched again". The purge removes
both. The copy must follow the code, not the reverse, and it is member-facing,
so the change carries a `Testing:` section.

**M2. YouTube playback leaves the box, and the processor inventory said it
didn't.** Register FIXED; privacy notice DEFERRED 2026-09-14 to the
end-of-project docs review, and FIXED there 2026-09-16 (`manual.html` names
YouTube, the cipher and Google's DPF certification). Facts already verified for it: Google LLC is
DPF-certified (EU, UK extension, Swiss), and a cipher request carries only the
encrypted signature, n parameter and player URL, with no video id or query. The register read "Lavalink:
local (127.0.0.1) ✓". Three flows leave: the youtube-source plugin is
OAuth-signed into a dedicated Google account (`3592c8ca`); signature
deciphering goes to the public remote cipher at `cipher.kikkia.dev` on every
load (`a7cc2402`); and Music Playlist calls YouTube's oEmbed endpoint
(`music_playlist/music_playlist_service.py:117`). Query text and video ids
leave, no Discord identity. `manual.html` § Where your data goes (:1461) still
names neither YouTube nor the cipher; its "What gets collected" table also
lacks sponsored submissions and shop purchases. `design_guide.md` now makes a
new outbound call a docs-contract obligation.

**M3. The erasure runbook's list-column grep was a stale hand copy.** FIXED.
It named `risky_active_rounds.reroll_user_ids`, which nothing can write
(`c53cb754`), and missed seven of the twelve columns. It now points at
`privacy_service.LIST_VALUED_MEMBER_COLUMNS`, the tuple the report itself scans.

**M4. The runbook understated the local backup window by half.** FIXED. It said
pre-erasure copies leave `backups/` "in roughly 24 hours — less after a run of
restarts". `db_backup.DEFAULT_MIN_AGE_HOURS = 48` means nothing younger than
48 hours is pruned, and restarts add files rather than shortening the window.
An operator quoting the old figure to a requester would have been wrong.

**M5. `/dk-regress` had no privacy step.** FIXED — step 8 runs
`privacy_coverage.py` against production read-only (about 3 s) and reports gaps
as notes. `dev_sessions.md`'s summary of the command also gained the
`config_key_audit.py` step it was missing.

**M6. `privacy_spec.md` and the runbook still promised to purge gender.** FIXED.
`member_gender` went in migration 214 (2026-09-05). Also corrected in
`privacy_spec.md`: `SUBJECT_ID_COLUMNS` is 88, not 86; list-valued columns are
the named one plus eleven, not five; dial `0` purges in the removal event
itself (`guild_purge_cog._on_guild_remove` runs `_run_due` immediately), not at
the next daily sweep; and the confessions clause now says the pending queue,
the alias map and the audit rows are purged.

**M7. `foolsday_names` is an unregistered member-data table in a dead
service.** FIXED 2026-09-14: the service and its test are deleted, migration
231 drops both tables, and the `foolsday_exclusions` purge statement and the
purge test's one expected warning are gone. Correction to the count below: on
2026-09-14 prod held `foolsday_names` 0 and **`foolsday_exclusions` 10** (prank
opt-outs), dropped by Billy's decision. Original finding:
`services/foolsday_service.py:66` creates it with
`CREATE TABLE IF NOT EXISTS` (user id plus original nickname). Nothing has
imported the service since the April Fools cog went (`c4f8758a`, 2026-05-29);
production holds 0 rows (read-only count, 2026-09-13). Delete the service and drop both of its tables. Noted
on the register's small-stores row.

**M8. Raw `<@id>` mentions in Survivor summaries.** FIXED 2026-09-14 — the four
mod-log summaries resolve the member through `build_name_fn`, pinned by a
route test per action. The same defect, found while fixing it, was in Voice
Control's three web force-action mirrors (`routes/voice_master.py` force-delete,
force-transfer, clear-profile); those now take names too. Original finding:
`web_server/routes/survivor.py:942,987,1040,1070` build
`f"<@{user_id}> marked dead…"`-style summaries, the defect CLAUDE.md names
("a member named inside an embed is resolved"). The eliminate and revive lines
(942, 987) landed in `024afc6d` (2026-08-17), before the standards scan existed;
the host added and removed lines (1040, 1070) in `5d1439d0` (2026-09-12), and
the standards scan on that branch (`7de1ba70`) did not flag the new pair.

### Low

**L1. `common_modules.md` was out of date and dashboard-blind.** FIXED. Four rows
misdescribed their module: `AppContext.member_is_mod` does pass Manage Server
and Administrator (the roles-only check is `GuildConfig.member_is_mod`);
`question_source` has an FFA fallback; `on_game_start` is an optional no-op hook,
not a required override; and the advisor sees server context when
`advisor_server_context` is on. Added the seams a new feature most often
re-implements: `refuse_launch`, `round_pacing`, `LiveBoards`,
`message_search_service`, `StickyPanel`, `chunk_text`, `safe_ephemeral`,
`is_host_or_mod`, `role_provision`, `game_store`, `anon_audit_service`. Added a
**Dashboard** section with the nine shared modules and how many of the 147
panels import each.

**L2. `plans/common-lib-round-2.md` read as untouched.** FIXED. Bugs 2 and 3, A1,
the payout-footer rider and most of B1 have shipped; each is struck with its
commit. Bug 1 (H2) and bug 4 were still open at review time; both are FIXED
2026-09-14 on `standards-code-queue` — `cogs/hot_potato/views.py` `PassView`
and `cogs/quickdraw/views.py` `FireView` now have an `on_error`.

**L3. `data_register.md` bookkeeping.** FIXED. The undecided tally was 20, now
17; `xp_daily` was listed as undecided in the XP row against its own row's
"permanent"; seven line citations into `privacy_service.py` and
`economy_service.py` had drifted by up to 600 lines and now name the function
or list instead, since the anonymise-departed-member branch moves every one of
them again; whisper A1 (cross-guild
forget-me) was fixed in `2b3094aa` but still listed; gap-list items 6, 7 and 8
had shipped (`xp_events` rollup, the access export `2a3fee2e`, the backup
window `1227bba1`); and the guild-removal note's table count is now 261.

**L4. `no_contact_spec.md` omitted Meadow Mahjong.** FIXED. The seat join has
been gated since `552ce9cd` (`mahjong_service._tx_join`, refusing with the
table's ordinary stale-tap line) with a test, but the spec's surface table and
test list did not say so.

**L5. Smaller drift.** FIXED unless noted:
- `dev_sessions.md` credited `scripts/hooks/install.sh` with the pre-commit
  gate; that hook comes from the pre-commit framework, and `install.sh` adds the
  QA-card hooks.
- The MCP `privacy` topic served the register and the spec but not the runbook.
- `guild_purge_service.py`'s docstring count (258 of 303) and
  `privacy_coverage.py`'s "six others" (ten).
- FIXED 2026-09-14: the `purge_user_data` docstring said the function is wired
  to no command, then that `keep_messages` is "used by `/delete_me`". Neither
  held: `/delete_me` never calls it, and the flag is the Member Data panel's
  keep-messages option (`routes/privacy.py` purge). The docstring now says so.

**L6. Reuse misses the scan let through.** FIXED 2026-09-14: the three tables
render through `renderSortableTable`, `chunk_lines` is gone in favour of
`chunk_text`, and mahjong imports `esc`. The `rules-watch.js` item went with
the Rules Watch removal. Original finding:
- `panels/rules-watch.js:557` uses `window.confirm` instead of `confirmDialog`
  (`b205e359`).
- Hand-built `<table>` markup instead of `table.js` in `panels/survivor.js:471,812`
  and `panels/shop-approvals.js:469`.
- `games_help/logic.py:160-175` `chunk_lines` re-implements
  `core/reports.chunk_text`.
- `panels/mahjong.js:19-21` defines a private `esc` beside `api.js`'s.

---

## Production coverage sweep

`scripts/privacy_coverage.py --db 'file:…/dungeonkeeper.db?mode=ro'`, 2.5 s:

- **748** member ids learned; **178** tables hold at least one.
- **0** columns holding member ids that the access export cannot see.
- **0** tables holding member ids with no register row.
- Known list-valued hits only: `risky_pending_questions.participant_user_ids`,
  `.questioners_asked` and `risky_posted_questions.allowed_replier_ids`, all
  already in `LIST_VALUED_MEMBER_COLUMNS`.

That is the same clean result the 2026-09-05 review found, over five fewer
tables (183 then). It does not contradict H1: the sweep asks whether a table is
*registered* and *exportable*, not whether the purge reaches it.

---

## Queue — separate branches, in order

1. **H2** emoji refund guild scope — money; failing guild-isolation test first.
2. **H1** games-family purge, plus the register rows back to DECIDED.
3. **H3** gate: always select the privacy and guild-purge coverage tests when a
   migration or store changes.
4. **H5** no-contact gate on `/voice knock` and `/voice invite`.
5. **M1 + M2** disclosure copy and the privacy notice — one member-facing branch
   with a `Testing:` section.
6. **M7** delete `foolsday_service` and drop its tables.
7. **M8 + L6** embed mentions and dashboard reuse misses.
8. Round-2 bug 4, `on_error` on the two single-button game views.

Status 2026-09-14: worked as one stacked branch, `standards-code-queue`, rather
than separate ones. Every item is fixed except 5 (M1 + M2), deferred to the
end-of-project docs review. The embed-mention defect M8 turned out to be
repo-wide (~57 more sites); that sweep is its own branch,
`embed-mention-sweep`.

## Open questions

- **Is the standards scan finding things?** Since the scan started
  (`7b2b2dc7`, 2026-09-03) there have been 42 merges, 49 `Review:` commits
  from `/code-review --fix`, and 6 `Standards:` commits. Either the rules are
  mostly being followed, or the agent is under-reporting; M8 and L6 suggest
  some of both. Worth sampling a few merges by hand after this branch's brief
  has been live for a while.
- **An MCP `reuse` topic.** DONE 2026-09-14: `get_conventions(topic="reuse")`
  serves the guide's new `## Reuse` section with `common_modules.md` attached.
- **Escaping in hand-built tables.** *Answered 2026-09-14.* All 35 panels
  that build table markup by string were audited site by site, along with the
  DOM-built tables (charts, Birthday Calendar, Mahjong) and every `html: true`
  column. No member-typed value reached a table unescaped. What stays raw is
  a server-issued integer interpolated at its own site — row ids, counts and
  prices in `data-*`/`value=`, about thirty of them, left alone deliberately.
  Three sites were escaped, under two rules worth stating because the code
  alone reads as inconsistent:

  1. **A value that is text, or can fall back to text, is escaped** — the
     admin-set currency name in the QOTD settings card (free text, length
     checked only), and the policy-ticket badge's fallback arm, which renders
     a bare status when one falls outside the map.
  2. **A shared builder escapes whatever a caller hands it**, the way
     `table.js` escapes every cell — CLAUDE.md, "shared dashboard widgets are
     safe by default". That is why Survivor's `numField` escapes its
     `value=` while the per-site integers around it do not: its callers pass
     server-validated ints today, and the escape is the helper's contract
     rather than a claim about those callers.

  Mahjong's local `esc` copy, which skipped `'`, was replaced with
  `api.js`'s. `tests/web/test_frontend_wiring.py` pins the three sites by
  asserting the escape is present (not that the old spelling is absent, which
  a rename would satisfy), and sweeps for a second escaper by shape as well as
  by name.
