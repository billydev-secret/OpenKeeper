# GDPR operator runbook

Procedures for the three things that arrive with a clock attached: a **subject
access request** (Art 15/20, one month), an **erasure request** (Art 17, one
month), and a **personal-data breach** (Art 33, seventy-two hours), plus one
that arrives with no clock at all and is easy to get wrong for that reason:
**anonymizing a member who has left** (§2b).

The bot deliberately has **no command** for any of them. The **My Data** panel
(the Help & Settings hub's 📜 row and `/info`'s My data button; `/mydata` until
2026-09-26) is the member's own panel, not an operator tool: it clears their Discord messages and,
since 2026-09-17, the bot's records for one feature at a time (whisper history
today), never another member's data, and never the whole-member erasure below.
`/delete_user`, which did the message half for a moderator against someone
else, was removed on 2026-09-13 (see [privacy_spec.md](specs/privacy_spec.md)).

**Which route to use.** Since 2026-09-13 there are two, and the dashboard is
the canonical one:

- **Moderation → Privacy → Member Data** (`#/member-data`, admin only) is where
  an ordinary request is answered. It does export, erase, anonymize (plan and
  apply), the missed-text sweep, and Discord-message deletion, against one
  member, with the report shown before every irreversible press. **Use it
  unless you have a reason not to**: running an erasure by hand from a shell
  on the production box is the thing this page exists to stop.
- **The scripts below** remain the route for anything the panel cannot do: a
  run against a snapshot rather than prod, a bulk or scripted run, a
  verification pass, or recovery when the dashboard is down. They are also what
  the panel's prose describes, so keep reading if you want to know what a
  button actually does.

Both go through the same `privacy.service` functions; neither is a
reimplementation of the other.

> **Scope caveat, read once.** Whether GDPR binds this deployment at all is a
> legal question that has not been answered; see
> [reviews/2026-08-06-gdpr-compliance-assessment.md](reviews/2026-08-06-gdpr-compliance-assessment.md).
> These procedures are written as if it does, because the cost of having them
> and not needing them is a few hours.

---

## 1. Subject access / portability request (Art 15, Art 20)

**Clock: one month** from receipt, extendable by two further months for complex
requests if you tell the requester within the first month.

```bash
# What would be disclosed, by table — no file written, nothing sensitive printed
.venv/bin/python scripts/export_user_data.py --guild <gid> --user <uid> --summary

# The actual export
.venv/bin/python scripts/export_user_data.py --guild <gid> --user <uid> --out sar.json

# The readable report to send with it (Art 12(1))
.venv/bin/python scripts/gdpr_disclosure_report.py --guild <gid> --user <uid> \
    --server-name "<server>" --with-export
```

The database is opened `mode=ro`, so this is safe to run against the live bot.

**The export is deliberately wider than the erasure path.** Data the server
keeps under Art 17(3) (the `econ_ledger` double-entry record, sanction
history, consent audit, no-contact orders) is still the subject's personal
data and still has to be disclosed. Retention is not an access exemption.

### The report is what you send; the export is what you attach

`export_user_data.py` answers Art 15 completely and unreadably: an active
member produces roughly 280,000 rows of JSON across 150-odd tables. Art 12(1)
asks for it *"in a concise, transparent, intelligible and easily accessible
form, using clear and plain language"*, and nobody learns what the bot knows
about them by reading 280,000 rows.

`scripts/gdpr_disclosure_report.py` writes that plain-language layer: a Markdown
document in ~15 categories giving, for each, what is held, why, how long, how
much, and what survives an erasure request. It gets those facts by joining the
export's row counts against `data_register.md`, and takes its *language* from
`privacy/disclosure_copy.py`: the register's own cells carry commit hashes,
table names and maintainer notes and must never be quoted to a member.

Output defaults to `private/`, which is gitignored. **A report naming a real
member is personal data in its own right. Do not commit one.**

Three things the report does that the raw export does not:

- **It cannot name a second member.** Categories built from third-party tables
  contribute a count and a note saying another member is involved, never an id.
  Art 15(4) is satisfied by construction, so the file needs no redaction pass.
- **It closes the list-column blind spot** rather than declaring it, scanning
  every known list column (`LIST_VALUED_MEMBER_COLUMNS`) by value. Check 2 below is therefore already
  done for you, and the terminal prints any hits.
- **Operator warnings go to the terminal, never to the document.** Unmapped
  tables, missing retention decisions and cross-guild scope are printed to
  stderr for you, and stay out of the member's copy.

Read the terminal output before sending. A table that is missing from the data
register appears in *no* section of the report, and the script says so loudly.
That is a register bug to fix before the document goes out, not a warning to
wave through.

### Before you send it — three checks

1. **Art 15(4) third-party review.** The script prints a `review_required`
   list: tables whose rows name a *second* member, where that person's identity
   is the payload (whispers, guesses, no-contact orders, DM consent pairs,
   moderation records). An access request must not adversely
   affect others' rights. Decide per table whether to redact the counterparty
   id, and record what you decided.
2. **The list-column blind spot.** A few columns store member ids as a JSON or
   CSV *list*, which an equality match cannot find. `export_user_data.py` names
   them in its `notes`; `gdpr_disclosure_report.py` actually scans them and
   prints any hits, so this check is only manual if you skipped the report.
   To grep them by hand, take the columns from
   `privacy.service.LIST_VALUED_MEMBER_COLUMNS`, the same tuple the report
   scans (`disclosure_service.scan_list_valued`). A copy kept here drifts: the
   last one named a dropped column and missed seven. For each:
   ```bash
   sqlite3 dungeonkeeper.db "SELECT * FROM risky_pending_questions WHERE participant_user_ids LIKE '%<uid>%';"
   ```
   A bare `LIKE` also matches a longer id that contains this one, so check
   each hit by eye.
3. **Unscoped tables.** Some tables have no `guild_id`; the script notes which
   ones actually returned rows. Their rows span every guild the bot is in, so
   for a multi-guild subject the export may include data from a guild the
   request did not cover.

### What is *not* in the export

- **Discord's own copy** of anything. The subject's messages as Discord holds
  them are Discord's to disclose, not ours.
- **Files on disk.** Guess round originals live in the cache directory, not the
  DB. Check `guess_rounds.original_path` in the export and attach the files if
  the request covers them.
- **Backups.** See the erasure section's note.

---

## 2. Erasure request (Art 17)

1. **Discord side:** Member Data → **Discord Messages**, mode "Everything they
   posted". Works for members who have left. Wait for the summary; re-run after
   fixing perms if it reports failures. (This is what `/delete_user` used to do.)
2. **DB side:** Member Data → **Erase** does this, and shows the counts first.
   The equivalent by hand, from the checkout, with the bot either running or stopped
   (WAL handles the concurrent writer; the purge is one transaction):

   ```bash
   .venv/bin/python - <<'PY'
   from pathlib import Path
   from bot_modules.core.db_utils import open_db
   from bot_modules.privacy.service import purge_user_data

   GUILD_ID = 0   # ← fill in
   USER_ID = 0    # ← fill in

   with open_db(Path("dungeonkeeper.db")) as conn:
       n = purge_user_data(conn, GUILD_ID, USER_ID)
   print(f"purged; {n} message rows existed")
   PY
   ```

   The purge covers messages+children, XP/activity/events, telemetry,
   wellness, watched/trusted/invite pair
   tables, bios, birthdays, voice-master prefs, and all per-member
   economy/casino state (`economy.service._PURGE_USER_ID_TABLES`).

   **Deliberately preserved, and the Art 17(3) ground relied on**: the full
   table is in [data_register.md](data_register.md):

   | Preserved | Ground |
   |---|---|
   | `econ_ledger` | Art 17(3)(e): establishment/exercise/defense of legal claims; also integrity of a double-entry record where deleting one side corrupts the other party's balance |
   | Sanction history (jails, warnings, tickets, policy tickets) | Art 17(3)(e): the canonical record of moderation decisions, needed if a decision is challenged |
   | `dm_audit_log`, `dm_consent_pairs` | Art 17(3)(e): consent forensics; the record exists precisely to answer "did they agree" |
   | `no_contact_pairs`, `no_contact_events` | Art 17(3)(e) + protection of the **other** party's rights and freedoms: erasing a protective order at the request of the restrained party would defeat it |

   **That table is the grounds, not the full survivor list.** It names what is
   preserved *by decision*. Two more categories were on this list until
   2026-09-20, surviving only because no code cleared them: the Guess tables
   (`guess_rounds` / `guess_guesses` / the Guess log) and the welcome card
   (`intake_cards` / `intake_card_steps`). Both are now purged: a Guess round
   goes whole, and so does the member's own card. What still survives from
   those two is column-level and deliberate: their id as the **solver** of
   another member's round (scrubbed to NULL, not deleted) and as the
   **greeter** on another member's card. Whisper rows left the list the same
   way on 2026-09-17, when bug #222 gave `purge_user_data` a call to
   `whisper.repo.forget_member`; a whisper *report* the member filed about
   someone else still survives.
   Check an export against the post-purge database before you answer, and say
   so in the reply.

   **Tell the subject what was kept and why.** Art 17 erasure that silently
   retains categories is worse than a documented partial erasure.
3. **Per-feature self-service paths**, run *as* or *for* the user where the
   request demands them: **My Data** → Whisper history (whisper data; since
   2026-09-17; `purge_user_data` now reaches it too, so this is a
   member-convenience path rather than a gap to cover), **My Data** → Guess
   Who data (deletes their rounds whole and takes the crop posts down; step 2
   and the Member Data Erase tab do the same since 2026-09-27), bios delete
   (also covered by step 2), `/guess delete` per round, `/guess optout`.
4. **Files on disk.** `purge_user_data` clears DB rows only. Guess round
   originals in the cache directory need deleting separately; take the paths
   from an export run *before* the purge.
5. **Reclaim the bytes:** deleted rows persist in the WAL and freelist:

   ```bash
   sqlite3 dungeonkeeper.db "PRAGMA wal_checkpoint(TRUNCATE);"
   # optional, rewrites the file: sqlite3 dungeonkeeper.db "VACUUM;"
   ```
6. **Backups:** automated backups run **every 6 hours** (plus one on each bot
   start). The newest 5 are kept, and nothing younger than **48 hours** is ever
   pruned (`db_backup.DEFAULT_MIN_AGE_HOURS`), so a pre-erasure copy stays in
   `backups/` for **at least 48 hours**, or longer after a run of restarts, which
   add files rather than shortening the window. Note the erasure date so an
   accidental restore can be re-purged, and **do not restore past backups over
   an erasure** without re-running step 2. If the requester asks, the honest
   answer is that backups are erased on rotation, not on request, which is an
   accepted position provided the window is stated.

   **Off-device copies on the NAS are kept for 14 days.** This is the binding
   number: an erasure has propagated to *every* copy 14 days after the purge
   runs. That is the figure to state to a requester. It is set by
   `RETENTION_DAYS` in `~/.config/dk-backup/nas.conf` and must not be changed
   without changing this sentence.

   **Also check `snapshots/` and any hand-made copy in `backups/`.** Ad-hoc
   snapshots are named outside the rotation's glob and are therefore *never*
   pruned, e.g. `dungeonkeeper-pre-tod-backfill-20260729.db`. Delete or
   re-purge them by hand; they are the copies no retention window reaches. See
   [reviews/2026-08-06-backup-disaster-recovery.md](reviews/2026-08-06-backup-disaster-recovery.md)
   (B5) and [disaster_recovery_runbook.md](disaster_recovery_runbook.md).

   **Summary of windows:**

   | Copy | Window |
   |---|---|
   | `backups/` (local rotation; `var/backups/` after the runtime-data move, `disaster_recovery_runbook.md` § Runtime data layout) | ≥ 48h (five files kept, none pruned younger than 48h) |
   | NAS `dungeonkeeper_*.db.gpg` | 14 days (AES256 at rest since 2026-09-19; pre-switch `*.db` copies age out on the same window) |
   | NAS `secrets-*.tar.gz.gpg` | 14 days (no member data: `.env` + unit files) |
   | Hand-made snapshots | **unbounded; delete by hand** |

### Multi-guild note

The purge is per-guild. A user present in both guilds needs one run per guild
id. The export has the same shape.

---

## 2b. Anonymising a departed member (Art 4(5))

> Numbered 2b rather than 3 because §1 and §3 are cited by number from
> `privacy_spec.md` and the 2026-08-06 compliance assessment.

**Use this when nobody has asked for anything.** A member has left, their data
should stop being theirs, and the server still needs to be comparable against
its own past. Running §2 instead destroys **163,701 of 184,009 rows** for a
heavy member, the whole XP timeline, so every "how busy were we in July"
question loses its baseline. (Measured 2026-09-13, before migrations 228/229
dropped message content and the social graph, so the counts a run reports today
are smaller. Step 1 prints the real ones for the member in front of you.)

**Most of the time this now happens without you.** Since 2026-09-16 the main
guild has `departure_auto_anonymise` on, so `departure_erasure_service` queues
a leaver, waits five minutes for a rejoin, deletes their messages and runs the
anonymization this section describes, with one difference since 2026-09-27:
it **deletes** their Guess Who rounds and takes the crop posts down rather than
rewriting them to the surrogate, which Step 1 still does.

**A member can also run it on themselves.** My Data → **Delete everything**
(since 2026-09-27, no dial) runs the same core (`anonymise_member`, via
`self_erasure_service`) on a member who is still here: it refuses a jailed or
ticketed member before starting, deletes every message they posted, ends an
open break, deletes their Guess records and anonymizes the rest. Its receipt is
an `audit_log` row with action `privacy_self_anonymise`, actor the bot, target
empty; the automatic departure run writes `privacy_auto_anonymise` the same
way. Neither names the member. Unlike Step 1 below, which always
anonymizes across every guild at once, the automated path (`choose_scope`)
runs all-guilds only when the leaver left the **primary** guild and is in no
other guild the bot shares; otherwise it anonymizes only the guild they left.
It holds rather than runs when the dial is off, the member is jailed, or they
have an open ticket. This section is what
you do when the dial is off, when a hold needs clearing by hand, or when you
want the plan printed before anything moves.

**Do not use it to answer an erasure request.** An Art 17 request gets §2. This
path keeps the rows and rewrites the key; the rationale, the KEEP list and the
honest limit are in [data_register.md](data_register.md) § The third path.

### Step 1 — see what it would do (read-only, safe against the live bot)

```bash
.venv/bin/python scripts/anonymize_user_data.py --user <USER_ID> --dry-run
```

`--dry-run` is the default and opens the database `mode=ro`, so this is safe
with the bot running. It prints the surrogate it would allocate, the three
sweeps column by column with row counts, and the tables that keep the real id
by decision. Read the totals. A production dry run for one heavy member
reported 108 id columns / 155,272 rows, 18 text columns / 14,113 rows, and
17,392 free-text clears, in 2.7 seconds.

If it reports anything under **"Not present in this schema"**, stop and find out
why: that is schema drift, and a skipped table is a table that keeps the real
id.

### Step 2 — rehearse on a snapshot and prove the metrics did not move

Never rehearse on a `cp` of the live database: a copy of a WAL database is
malformed. Take the snapshot through SQLite's own backup API:

```bash
.venv/bin/python -c '
import sqlite3
src = sqlite3.connect("file:dungeonkeeper.db?mode=ro", uri=True)
dst = sqlite3.connect("~/dk-gdpr-rehearsal/anon_rehearsal.db")
src.backup(dst); dst.close(); src.close()
'

.venv/bin/python scripts/anonymize_user_data.py --user <USER_ID> \
    --db ~/dk-gdpr-rehearsal/anon_rehearsal.db --apply --verify-metrics
```

Put the snapshot on `/home`, not `/tmp`: tmpfs has ~1.3G free and the database
is 1.1GB.

`--verify-metrics` snapshots sixteen aggregates before the rewrite and again
after, and fails if any moved. **That is the check this whole path stands on.**
It must print `16/16 aggregates identical`. If it reports a probe that "could
not run", treat that as a failure too: a probe that measures nothing compares
equal to itself and passes vacuously (one did, summing `xp_daily` on a column
that does not exist).

A probe that genuinely moved means the rewrite was not metric-neutral. Do not
proceed; the rehearsal did its job.

### Step 3 — apply

```bash
.venv/bin/python scripts/anonymize_user_data.py --user <USER_ID> \
    --db dungeonkeeper.db --apply
```

`--apply` must be explicit and must name the database. The whole rewrite is one
transaction, so a failure rolls all of it back rather than leaving the member
half-anonymized. Expect roughly 30 seconds for a heavy member.

**It cannot be undone.** No surrogate ↔ real-id mapping is written anywhere.
That is the point, since retaining one would make the result pseudonymous under
Recital 26. Take a backup first if you want the option of going back, and
remember that the backup is then a copy that still identifies them.

### Step 4 — afterwards

- The member appears everywhere as **`Cleared user <N>`**. Their roster row is
  rewritten, not deleted, because `name_resolver` falls back to a raw mention,
  and deleting it would leave every embed and leaderboard rendering `<@1>`.
- **The real id survives, by decision, in:** `dm_audit_log`,
  `dm_consent_pairs`, `jails`, `no_contact_events`, `no_contact_pairs`,
  `policy_tickets`, `ticket_participants`, `tickets`, `warnings`. Each is a row
  whose entire payload is naming a person; the no-contact ones must also keep
  matching a live id if the account ever returns. Those retained ids are
  isolated; every *other* table now holds the surrogate, so a kept row still
  names them in that record but can no longer be joined to their activity.
- **Reclaim nothing and check nothing else.** Unlike §2 there are no files on
  disk to chase and no rows to vacuum: nothing was deleted.
- **Backups still hold the pre-rewrite data** on the windows in §2: at least 48 hours
  locally, 14 days on the NAS. The anonymization has propagated everywhere 14
  days after it runs.

### Afterwards: sweep for text the clear list did not name

**Do this every time you run §2b by hand, and periodically regardless**, since
an automatic departure run and a member's own Delete everything (both above)
anonymize with nobody watching and no one to walk the schema afterwards (`privacy_auto_anonymise` and `privacy_self_anonymise` rows in `audit_log` are
how you find them). The id rewrite is complete the moment it runs:
the schema tells it every column. The clear lists are judgments made table by
table, and they are what goes missing. The 2026-09-11 run reported success
while a 207-row verbatim transcript, 38 voice-room names and a QA note in the
member's own words sat beside surrogate 1, because all three tables were in the
id sweep and none was in a clear list.

Walk the whole schema for text still on the surrogate's rows:

```bash
python scripts/anonymize_user_data.py --db dungeonkeeper.db \
    --remediate --surrogate <N>          # dry run, read-only
```

That reports what the *current* clear lists would take. If a column it does not
know about is holding the member's words, add it to
`ANONYMIZE_FREE_TEXT_COLUMNS` / `ANONYMIZE_JSON_TEXT_KEYS` with a test, then re-run with `--apply`. It clears
text only: no id is rewritten, no metric moves, and running it twice is a
no-op.

**Expect a residue, and do not describe it away.** The display name is never
swept (see `data_register.md` for why, production holds a second, current
member called `Lark Hollow`), so other members' transcript windows and prose
still quote the subject by name. The claim you can make is "no longer
identifiable by id or username from this database"; the claim you cannot make
is "anonymous".

### If the script refuses

| It says | What it means |
|---|---|
| `surrogate_id … out of range` | Surrogates are 1..999,999. Negative ids are mahjong house seats and anything larger risks colliding with a real account |
| `surrogate_id … is already in use` | Reusing one merges two anonymized members into a single identity, silently corrupting every per-member metric for both. Let it allocate the next free one |
| `user_id … is below the snowflake floor` | You have passed a surrogate, not a member id: you are trying to anonymize someone who already has been |
| `… is not a surrogate` | From `--remediate`, which is the inverse guard: it clears text without anonymizing anyone, so it must never be pointed at a live account |

---

## 3. Personal-data breach (Art 33 / Art 34)

**Clock: seventy-two hours** from *becoming aware*, to the supervisory
authority. "Aware" starts at reasonable certainty that a breach occurred, not
at the end of the investigation; an incomplete notification on time beats a
complete one late (Art 33(4) explicitly allows phased notification).

### What counts

A breach is any accidental or unlawful destruction, loss, alteration,
unauthorized disclosure of, or access to personal data. For this deployment the
realistic shapes are:

- Dashboard session compromise (stolen cookie, leaked `SESSION_SECRET`).
- The database file or a backup leaving the host.
- A bot token leak allowing message reads across the server.
- A bug exposing one member's data to another; the cross-guild IDOR class the
  2026-07-23 review found four of.
- Accidental publication of an export file from section 1.

### Procedure

1. **Contain first, document as you go.** Rotate the credential, revoke the
   token, take the dashboard down if it is the vector. Write timestamps down
   while you do it. The 72 hours is evidenced by your own notes.
2. **Establish scope** using the tools already here: `scripts/export_user_data.py
   --summary` tells you what a given account's exposure actually contains, and
   the [data register](data_register.md) tells you which categories are
   involved. The bot has stored no message text since 2026-09-13, and
   migration 228 erased what it had held, so a database compromise exposes
   metadata and derived data, not what members wrote.
3. **Notify Discord if API Data is involved.** This is a contract obligation,
   not a GDPR one, and it does not wait on the risk call in the next step:
   file it as soon as step 2 shows API Data is in scope. Discord Developer
   Terms of Service §5(c) ("Implement Good Security") requires: "You will
   also promptly notify us and provide us with requested information about
   any incidents of unauthorized access or use of any API Data or any
   incidents that are reasonably likely to compromise the security,
   confidentiality, or integrity of your systems (or the systems of those
   acting on your behalf) that process API Data." Neither the Developer Terms
   nor the Developer Policy name a deadline beyond "promptly" or a dedicated
   channel, so until a better route is confirmed, the notification is a
   ticket with Discord Developer Support (https://support-dev.discord.com).
   Include: what happened, which API Data categories are involved, the rough
   counts of users/guilds from step 2, containment already done, and a
   follow-up contact. This runs on its own clock; it is not the Art 33
   72-hour supervisory-authority clock and Discord being notified does not
   satisfy that one. Note the date and ticket reference in the breach
   register below.
4. **Assess risk to the people involved.** Low risk → record it and do not
   notify (Art 33(1) allows this, but the reasoning must be written down).
   Risk → notify the supervisory authority within 72h. **High** risk → also
   notify the affected members directly, without undue delay (Art 34).
   Anonymous-feature data (whispers, confessions, Guess) raises severity
   sharply: deanonymization is the harm those features exist to prevent.
5. **Record it either way.** Art 33(5) requires an internal record of *every*
   breach including ones you decided not to notify (facts, effects, remedial
   action). Append to the register below.
6. **Members notified in Discord** should get it in writing in the server, not
   only by DM, because DMs are closed by default for many members.

### Breach register

| Date aware | What happened | Categories | People affected | Discord notified? | Notified? | Ground if not |
|---|---|---|---|---|---|---|
| — | *(no breaches recorded)* | | | | | |

*Discord notified?* holds the date and Developer Support ticket reference from step 3,
or "n/a — no API Data involved" if step 2 established none was.

---

## Where the data itself is catalogued

[data_register.md](data_register.md): every table holding personal data, its
class, retention, purge coverage, and the processor it reaches. Keep it current:
a new user-data table needs a row there in the same commit.

### Checking that the catalogue is still true

Two tiers, because neither sees what the other does.

`tests/privacy/test_privacy_register_coverage.py` runs in the ordinary gate. It walks a
freshly-migrated schema and fails when a table with a column named in
`SUBJECT_ID_COLUMNS` has no register row. That catches the common case, a new
feature's table landing without its row, at commit time.

`scripts/privacy_coverage.py` is the periodic check, and the stronger one. Point
it at production, read-only:

```bash
.venv/bin/python scripts/privacy_coverage.py \
    --db 'file:~/discord-bots/dungeon-keeper/dungeonkeeper.db?mode=ro'
```

It learns the real member ids from the roster tables and then reports every
column whose **values** are member ids, whatever the column is called. That is
the only way to find a table the naming convention missed: the test above is
blind to exactly the columns `export_user_data` is blind to, which is the gap
that matters. It also sees tables created by application code rather than by a
migration, which never appear in a migrated schema (`foolsday_exclusions` was
the one it found, until migration 233 dropped it with its dead service).

Run it after a batch of features lands, and before answering a contested access
request. The 2026-09-02 review's first run found eleven columns holding member
ids that no access export could see, and 26 tables with no register row.

### The failure mode this all exists to catch

Both `purge_user_data` and `econ_purge_user` tolerate schema drift: a statement
that fails logs a warning and the sweep continues, so one table missing on an
older deployment cannot abort an erasure midway. The price is that a statement
naming a column its table does not have fails **identically**, quietly, on
every erasure, forever. Eight tables were in that state until 2026-09-02, five
of them holding 108,687 production rows that an erasure reported as cleared and
never touched.

`tests/privacy/test_privacy_service.py::test_purge_runs_clean_against_the_migrated_schema`
now runs the whole erasure against a migrated schema and fails on any swallowed
schema error. If you ever run an erasure by hand, **read the log**: a warning
line there means a table was not cleared, and the operator's confirmation to the
subject should not go out until you know which.

The same tolerance covers one more case: a table whose **name** is not a plain
SQL identifier is skipped by the schema walk entirely, because no statement can
safely carry it. That one does not hide in the log: it is on the anonymization
report's `skipped` list and in the export's `notes`. If you see it, that table
was neither searched nor swept; open it by hand before you answer the subject,
and rename it so the next request is answered properly.
