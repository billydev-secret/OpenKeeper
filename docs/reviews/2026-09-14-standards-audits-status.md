# Standards audits: status recheck, 2026-09-14

Condensed 2026-10-01; the full original is at `git show 66a4d886:docs/reviews/2026-09-14-standards-audits-status.md`.

On 2026-09-13 the `standards-and-practices-review` session ran four bodies of
work: the privacy and reuse review
([2026-09-13-privacy-reuse-standards-review.md](2026-09-13-privacy-reuse-standards-review.md)),
an unforced conventions audit (30 survivors), an external standards audit
(Discord policy, GDPR, Anthropic usage policy, OWASP, WCAG, GitHub, systemd)
and an internal design audit. The three chat-only audits were turned into a
120-row register here and re-verified several times. The original register
held the per-row evidence, commits and recheck narrative; this version keeps
only what is still open.

## Resolved tally

112 of 120 rows are done or closed (counts re-derived 2026-09-27, then
re-checked 2026-10-01 against `scripts/gate.py`, `SECURITY.md` and the
migrations).

| Source | Items | Resolved | Still open |
|---|---|---|---|
| Fix queue, live defects | 9 | 8 | 1 (item 7) |
| Unforced conventions, force it | 23 | 23 | 0 |
| Unforced conventions, brief lines and doc fixes | 16 | 16 (3 are refutations that stand) | 0 |
| External standards | 36 | 29 | 7 (3.2, 3.8, 3.9, 3.20, 3.24, 3.35, plus 3.19 which is fix-queue 7; the 3.13 toggle is a residue of a done row) |
| Internal design | 36 | 36 (4.24 refuted, 4.33 closed) | 0 |

Resolved IDs: fix-queue 1-6, 8, 9; every row of sections 1, 2 and 4; section 3
rows 1, 3-7, 10-12, 14-18, 21-23, 25-34, 36.

Fix-queue items 8 and 9 (a doubled DM footer and an unescaped member name in
welcome and leave cards) came out of building guards, not from the original
audit. Both are fixed. The `docs/embed_style_guide.md` paragraph that cites
"live instances" under fix-queue item 8 is out of date: the live instances are
gone, though the per-function contract test still cannot tell one embed object
from two.

Two migration-immutability facts survive from the fifth and sixth passes. The
schema has two authors (migrations and service modules that re-declare tables
at boot), closed by migration 235 and a second contract test. Two orphan
tables, `incident_events` and `message_velocity_baseline` (572 and 508 rows in
prod), exist in no migration and are left for a decision. The migration hash
manifest test is mapped by `gate.select_tests`, so the earlier "does not bite
in a worktree" gap is closed.

## Still open

All of these are Ben's presses or decisions outside the repo, except 3.35.

1. **Fix-queue 7 / external 19: Truth or Dare bank holds member ids.**
   `/opt/tod/tod.db` `questions.author` carries a Discord id on 697 rows,
   copied by one-off extractors on 2026-08-04. Fix: Ben removes the copied
   data on the ToD side. DK keeps no code that writes the bank (the scrub
   script was deleted unrun on 2026-09-27).
2. **External 2: real member content in the public repo (partial).** History
   is rewritten and the tree is clean. Pending: final gc, delete the
   pre-rewrite bundle, a GitHub Support purge of PR refs, and the Windows
   runner's copies. Ben: run it once `scope-server-copy`'s staged paths are
   merged or discarded.
3. **External 8: branch protection requiring pinned Actions (partial).**
   Pinning and its test exist (`c206f9ad`). Branch protection returns 403 on
   the current GitHub plan. Fix: a plan change.
4. **External 9: Dependabot alerts, security updates and CodeQL are all off**
   (confirmed with `gh api`). Fix: Ben turns them on in repo settings.
5. **External 13 (remainder): the Private vulnerability reporting toggle is
   still off.** `SECURITY.md` exists and points to it. Fix: Ben flips it.
6. **External 20: the ToD MCP exposes 30 tools, including delete, behind one
   URL secret.** Fix: operator change outside the repo (split credentials or
   drop the delete tool).
7. **External 24: no read-deny on the prod `.env` in Ben's user Claude Code
   settings.** Fix: add the deny rule.
8. **External 35: the question bank is global across guilds and undocumented.**
   `games_question_bank` has no `guild_id`. Ben decided on 2026-09-19 to scope
   it per guild (migration, backfill, every route and query filtered) and on
   2026-09-27 left it out of scope. Fix: build that migration when wanted.

Rows 3.4 (LegitLibs AI Prep tiers) and 4.24 (unguarded purge loops) were
closed or refuted by decision and are not open.

## Lessons kept from the passes

- A status column nobody re-runs goes wrong in both directions: sessions close
  rows without editing this file, so re-verify a row against HEAD (and
  `git worktree list`) before building it.
- A count nobody re-measures is a guess. Several counts here were wrong the
  day they were written. `tests/test_ruff_lint_baselines.py` re-measures the
  lint ones on every run.
- A row that names a code instance is not closed until that instance is, even
  when the brief line or doc half has landed.
- A guard's documented blind spot is a place to go looking. Fix-queue item 9
  was a live default-on defect inside such a blind spot.
