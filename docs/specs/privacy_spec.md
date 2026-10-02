# Privacy — Feature Spec

The **My Data** panel is the member's own data surface: an ephemeral panel in
two halves, opened from the [Help & Settings hub](server_features_spec.md)'s
📜 My Data row or `/info`'s **My data** button (`PrivacyCog.open_panel`). **Your Discord messages** erases the member's own
**Discord messages**, and only those, in every
mode: an authoritative Discord-side scan and delete that walks every readable
channel and thread looking for messages by the target. Server-side data (XP,
activity, profile, wellness, and the bot's own message records) is always
retained for moderation. The confirmation prompt says XP, activity and profile
survive, and deliberately makes no claim about the message records (see
[Confirmation](#confirmation) for why, and [Stored data](#stored-data) for what
those records hold: ingest-time metadata, never content). **Data this server holds** is the other
half: per-feature erasure of the bot's *own* records, one button per
[erasable store](#member-erasable-stores).

**A feature does not ship its own forget-me command.** It adds an
`ErasableStore` and the panel grows a button, with the deletes in a service
both the panel and the purge call. The reason is bug #222: whisper deletes
were once wired to a feature command and nothing else, so `purge_user_data`
reached no whisper at all. Enforced by
`tests/privacy/test_privacy_member_data_panel.py`.

There is no moderator-run erasure command. Per CLAUDE.md, an admin job lives on the web: the **Member Data** panel (`#/member-data`) is `adminOnly`, so moderators do not have it. It is the surface for `purge_user_data` (the hard-erasure path, via the Erase tab), alongside export (Art 15/20) and anonymization (see [Member Data panel](#member-data-panel) and `docs/plans/member-data-panel.md`, recoverable with `git show 678a16fa:docs/plans/member-data-panel.md`).

The panel's three message buttons narrow the scope to all / just images+files
/ just text (see [Modes](#modes)). Every mode is a scrub of Discord messages;
the account is untouched regardless.

The channel-walking scanner is shared with [[events-spec]]'s backfill so both features cover the same channel set.

Privacy also owns the other erasure direction: what happens to a **guild's** data when the bot is removed from it. That is not a command; it is a dashboard-set grace period that ships unset (never purge). See [Guild removal erasure](#guild-removal-erasure).

A member leaving can trigger the message deletion and the anonymization by itself, behind a per-guild dial that ships off. It is **on for the main guild** (a leaver there is anonymized automatically) and off in every other guild. See [Departure auto-anonymise](#departure-auto-anonymise). A member can run the same recipe on themselves, with no dial: My Data → **Delete everything**, see [Delete everything](#delete-everything).

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| **My Data** (Help & Settings hub row; `/info` → My data) | Button | Everyone (guild only) | The member's data panel. Three message buttons delete their own Discord messages (all / images & files / text only; server-side data always stays); one button per erasable store deletes the bot's records for that feature; **Delete everything** runs the departure recipe on the member themselves |

## Member Data panel

`#/member-data`, Moderation → Privacy, `adminOnly`. One member chosen once at the
top, five tabs, backed by `src/web_server/routes/privacy.py`:

| Tab | Reads | Writes |
|---|---|---|
| Export | `export_user_data` counts, `review_required`, notes; JSON download | nothing |
| Erase | the export's counts, labelled as the deliberate superset they are | `purge_user_data_counted` (`purge_user_data` plus the total rows changed); the toast reports that total and its message-log share |
| Anonymise | `plan_anonymization`; plus the missed-text sweep | `anonymize_user_data`, `anonymize_missed_text` |
| Discord Messages | scan progress | Discord only, no DB write at all |
| When Members Leave | the `departure_auto_anonymise` dial and this guild's `departure_erasure_queue` (not about the chosen member) | the dial; switching off empties the queue; Retry a parked job, Leave alone (drop) any job |

Three constraints are load-bearing and are restated in the route module's own
docstring:

- **Receipts never name the subject.** `audit_log` is not in
  `ANONYMIZE_KEEP_TABLES`, so a receipt written after an anonymization puts the
  real id back into the database the run just cleaned, and one written before is
  rewritten to the surrogate and records nothing. `purge_user_data` does not
  clear plain `audit_log`, so a purge receipt naming the member outlives the
  erasure. Every receipt here carries `target_id=NULL` and counts only; actor and
  timestamp are real.
- **The Anonymise tab is primary-guild only.** It runs across every guild at
  once (a surrogate must be consistent or the aggregates it protects stop adding
  up), and the bot serves a guild whose economy someone else runs. Export and
  Erase are guild-scoped and work anywhere. The When Members Leave dial works
  on any guild, because the automatic path only goes bot-wide for a member
  leaving the primary guild; everywhere else it uses the guild-scoped
  anonymize (see below).
- **Deleting Discord messages writes nothing server-side.** It is the one
  operation on the page that never opens the database (see
  `bot_modules/privacy/deletion_service.py`, whose guarantee is asserted by a
  test). It therefore gets no receipt either.

Export renders counts only, never row content: the payload behind them is the
whole of a member's data and a browser is the wrong place for it. The file
arrives as a `Content-Disposition` download. Anonymise Apply holds a SQLite write
lock for the length of the run (**12.4 s for 186,991 rows across 108 tables**,
measured 2026-09-13 against a copy of the production pre-anonymize snapshot),
and the confirm dialog says so. Message content and the social graph are no longer stored (migrations 228/229), so read the figure as an upper bound.

## Modes

The three message buttons on the My Data panel ("All my messages", "Images & files", "Text only") map to the same three `mode` values the engine takes. The Member Data panel's Discord Messages tab offers the same three against another member.

| Mode | Value | Deletes from Discord | Account data (XP, activity, profile) | Local archive |
|---|---|---|---|---|
| All messages | `all` (default) | every message | untouched | untouched |
| Images & files only | `media` | messages carrying media | untouched | untouched |
| Text messages only | `text` | messages carrying no media | untouched | untouched |

The modes differ **solely in which Discord messages go**: every mode leaves the account and the server's own records intact, so clearing your photos (or everything) doesn't cost you your level. No mode reaches the DB purge on either surface.

The two partial modes partition a member's messages exactly: every message is in `media` or `text`, never both.

**What counts as media** (`message_has_media`): attachments, stickers, and embeds of type `image`/`video`/`gifv`. Discord auto-generates an embed for any posted link, so `link`/`article`/`rich` previews deliberately do **not** count, since otherwise ordinary chatter with a URL would be swept into "clear my images". A posted image *URL* has no attachment but does produce an `image` embed, so it counts.

Selection happens **during the scan** (`find_user_messages(predicate=...)`): the scan returns only `(message_id, channel_id)`, so a mode that selects on message content has to decide while the `discord.Message` is still in hand. A message the predicate rejects is never collected and therefore never deleted.

## Member-erasable stores

The panel's lower half. `privacy.logic.MEMBER_ERASABLE_STORES` is the list, one
`ErasableStore` per feature, and the panel builds a danger button per entry,
so adding a feature's member-facing erasure is a data change, not a new
command.

| Store | Button | Clears | Survives |
|---|---|---|---|
| `whispers` | Whisper history | every whisper the member sent or received in this guild, with its replies, guesses and reports cascading off it (`whisper.repo.forget_member`) | reports the member *filed* on someone else's whisper, and guess rows recording that someone guessed them on a third party's whisper (Art 17(3)(e) rights of others) |
| `guess` | Guess Who data | every round the member submitted or is the answer to, whole: the cached photo, the bot's crop post in the Guess channel, a confession round's text, and every guess and Guess-log row on it, plus their own guesses and log rows on other rounds, and guesses naming them (`games.guess.repo.forget_member`; the photo and post first, via `ErasableStore.clear_media` → `games.guess.cleanup_service.clear_member_media`) | somebody else's round they solved stays, with `solver_id` scrubbed to NULL; `guess_consents`, because the opt-in is the consent evidence and `/guess optout` owns it |

Each entry carries its own confirm copy, because the honest scope differs per
store: the whisper confirm says the reports the member filed stay, since a
member who reads "delete everything" and then still sees their reports was
misled. The Guess confirm is assembled from `disclosure_copy.GUESS_ROUNDS_ERASED`
and `GUESS_SOLVER_SURVIVES`, the same sentences the privacy notice's games
entry uses, so the click and the notice cannot describe two deletions.

**Things outside the database go first.** A store whose data reaches past its
rows sets `clear_media`, an async step the panel awaits before the eraser and
outside its transaction: a Discord delete or a file unlink cannot be rolled
back, and once the rows are gone nothing knows which post or photo was the
member's. If it raises, the eraser does not run and the member reads
"Something went wrong and nothing was deleted", never the store's done line.
Guess uses it to take down each round's channel post (NotFound/Forbidden
tolerated, since a post a mod already removed is not a failure) and then unlink the
photo; a photo that won't unlink raises `MediaNotCleared`. The dashboard purge
route and the departure job run the same `clear_member_media` before their own
transactions, so all three erasure paths take the post down. Billy's
rule: *"Deleting the message should clear everything from guess."*

Three rules hold this together, and each has a test:

1. **The deletes live in a service**, not in the button. `forget_member` takes
   a connection, so `purge_user_data` can call it inside the erasure
   transaction it owns. Tested in `tests/whisper/test_whisper_forget_member.py`.
2. **No cog ships an erase-my-data command.** An AST walk over every cog in
   `src/` fails on one. The standing exceptions are `/birthday remove` and
   `/voice profile reset`: both delete a stored row, but both are also the
   member's off switch for that feature, so folding them in would take the off
   switch with them.
3. **The register and the panel agree.** A row marked `**Member-erasable**` in
   `docs/data_register.md` must have a button, and a button's tables must have
   the marker. Both directions fail loudly: a register row promising the
   member can clear something, with nothing that does, is a promise nothing
   keeps.

Halves 2 and 3 are `tests/privacy/test_privacy_member_data_panel.py`, which no filename
token reaches (one watches every cog, the other watches a doc), so
`scripts/gate.py` selects it by path.

## Behavior

### Confirmation
Every My Data button opens an ephemeral confirm view with a danger button and **Cancel**, carrying that button's own copy (what goes, what survives). Pressing it does not start anything: a **second, final step** replaces it, the same for every button: "🛑 **Last check.** <subject> will be gone for good…" with the danger button **Delete it, I'm sure** (`privacy.logic.render_final_confirm` / `FINAL_CONFIRM_LABEL`). Billy: *"All of the deletion buttons need to have a confirm too, just in case"*. The final label differs from every first-step label on purpose, so a double-click on a button that stays under the cursor cannot carry straight through (`tests/privacy/test_privacy_modes.py` pins both the difference and the two-press wiring in `_MyDataPanelView._confirm`). Each view checks that the clicker is the original invoker, times out silently after **60 seconds**, and the final one disables itself on click. Until the member has pressed both, nothing is touched.

The prompt and button name the *real* scope: a media scrub's button reads "Yes, delete my images & files" (or "their" when a mod acts on someone else). The prompt says what survives (XP, activity and profile stay exactly as they are) **before** the irreversible click, rather than letting the member discover it in the summary afterwards.

**The prompt deliberately says nothing about the `messages` archive.** What survives in `messages` is a metadata row, but any phrasing such as "the server keeps its own copy of your messages" reads like content retention, because a member forms the wrong belief before a parenthetical arrives. Two rewordings failed, so the owner's call was to drop it from the consent flow: a six-word aside in front of a danger button is the wrong venue for a retention disclosure, and the venue that carries it in full today is `manual.html` §Your Data & Privacy ("It keeps a record that you posted, where and when, and that record stays when you delete a message in Discord"). The member-facing `privacy-tools` doc is the second venue but **is not carrying it yet**: the rewritten body lives in `docs/proposals/privacy-tools-body.md` and is staged in the prod `docs` row; it needs a Docs-panel save and a re-post before members read it. `tests/privacy/test_privacy_modes.py::test_prompt_makes_no_claim_about_a_server_side_message_copy` pins the omission across all three modes, because a stranger meeting this flow cold sees a missing disclosure and restores it. The My Data panel body (`render_panel`) that precedes the confirm step follows the same rule for the same reason, one step earlier in the flow; `tests/privacy/test_privacy_modes.py::test_panel_makes_no_claim_about_a_server_side_message_copy` pins that omission too.

A per-target lock prevents racing: while a deletion for user X is running, neither the My Data panel (by X) nor an admin's Member Data → Discord Messages run against X will start a second one. The panel sees a "deletion already running" ephemeral and bails; the route returns 409. The lock lives in memory on the cog (`_active_deletions`) rather than in a table, because a deletion run must not write server-side data.

### Phase 1 — Discord-side scan
The scanner walks every channel the bot can read: text channels, voice and stage chat channels, active forum threads, and archived threads (including private archived threads when the bot has Manage Threads). Anything the bot can't see is silently skipped: it won't be deleted, but the rest of the run continues. Progress updates throttle to roughly one edit every 2 seconds and read: "Scanning the server for your messages — channel **D/T** (**F** found so far)…". The final update is always sent.

If the scan finds zero matching messages, the delete phase is skipped entirely and the user gets a single completion message confirming that nothing was found and nothing else was touched.

### Phase 2 — Discord-side delete
For each channel with hits:
- **Channels the bot cannot reach** count their messages as "already gone", since Discord agrees they're not there.
- **Archived threads** are unarchived before processing and re-archived after, because Discord refuses sends and deletes against archived threads.
- **Forum thread OPs** (the post that anchors the thread) get deleted, then the bot posts a `[deleted]` tombstone so the thread itself (and other members' replies) survive under the bot's name. These count as "replaced", not "deleted".
- **Recent messages (≤14 days)** use bulk delete in batches of 100, with a 1-second pause between batches.
- **Older messages (>14 days)** are deleted one at a time, with a half-second pause between calls, because Discord's API has no bulk-delete for older messages.

Per-message failures (permissions denied, transient HTTP errors) are counted as "failed" and logged; the run continues. Progress is rendered as a 20-character bar like `[████████░░░░░░░░░░░░] 42/100`, throttled to one edit every 1.5 seconds.

### Phase 3 — DB purge (retired; out-of-band erasure)
**Does not run from the My Data message buttons or the Discord Messages tab, in any mode.** Both stop after the Discord-side delete; no DB row is touched.

The purge implementation, `purge_user_data` (`src/bot_modules/privacy/service.py`), is deliberately kept off those buttons. It is the implementation behind a genuine legal erasure request (e.g. GDPR). The **Member Data panel's Erase tab** runs it (`web_server/routes/privacy.py`); [gdpr_runbook.md](../gdpr_runbook.md) §2 keeps the by-hand equivalent for a snapshot, a bulk run or a dashboard outage. It removes the user's rows from XP (including reaction awards), voice sessions, member activity, member events, birthdays, bios (row + snapshotted answers/fields), voice-master profiles and trust lists (both sides), watch lists and invite edges (both sides), wellness state and counters, usage telemetry (`usage_events`, see [[usage-telemetry-spec]]; nothing prunes that table on a schedule, so this run is the only thing that ever clears it), audit-event tables, and, via `economy.service.econ_purge_user`, all per-member economy and casino state (`econ_ledger` is deliberately preserved: it is the pseudonymous double-entry record, and deleting one side of transfers would break audit sums). With `keep_messages=False` it also drops the `messages` archive and its children (`_MESSAGE_CHILD_TABLES`: mentions, reactions, sentiment; the attachment and embed tables went with migration 228) and the per-user dedup table, chunked in batches of 500 ids so a heavy poster can never blow SQLite's bound-variable cap mid-erasure. Every per-table delete tolerates schema drift (guild deployments vary by age): a failed table logs a warning and the rest of the purge proceeds; the caller owns the transaction, so a hard failure rolls the whole erasure back rather than leaving partial state.

A full legal erasure is therefore two presses: **Discord Messages** for the Discord side, then **Erase** for the DB side (or the script equivalents of each).

### Phase 4 — Final report
If the interaction token is still alive, the ephemeral message is edited with the summary. Long scans can outlive Discord's 15-minute interaction lifetime; in that case the bot DMs the actor instead. A closed-DM actor gets nothing user-facing, only log lines.

A typical summary (the noun follows the mode, so "Images & files deleted from Discord" for a media scrub; the tombstone and failure lines appear only when non-zero):

```
All done. Here's what was removed:
Messages deleted from Discord: **N**
XP, activity, and profile: **kept for moderation**.
Forum posts replaced with tombstone: **R**
Messages that couldn't be deleted (no access): **M**
```

The copy is deliberately neutral (no "your") because the Member Data panel shows this same summary to the acting admin, not to the subject: `deletion_status` renders it with the same two renderers the Discord card uses, so both surfaces report a finished run in identical words.

## Anonymisation — the third path

`anonymize_user_data` (`src/bot_modules/privacy/service.py`) is neither the purge nor the export, and it answers a question
neither of them does: **a member has left, their data should stop being theirs,
and the server still needs to be comparable against its own past.** Like the
other two it is unwired from any command and run by an operator:
`scripts/anonymize_user_data.py`, procedure in
[gdpr_runbook.md](../gdpr_runbook.md) §2b.

The purge is the wrong instrument for that case: measured on production for one
heavy member it destroys 163,701 of 184,009 rows, the whole XP timeline
included (measured 2026-09-13; the absolute numbers are smaller since migrations 228/229, the conclusion is the same). Instead the member's Discord id is rewritten to a **small surrogate
integer** (1, 2, 3 …, far below the snowflake range so it cannot collide with a
real account), the names are destroyed, and the member's own free text is
cleared. Every DK metric is an aggregate over rows keyed by a member id, and
aggregation needs that key to be *stable and distinct*, not *meaningful*, so
**every count, sum, rate and distribution is mathematically unchanged**.
`tests/privacy/test_privacy_anonymize.py` computes a battery of aggregates before and
after and asserts equality, because that is the whole claim of the feature and
a docstring is not enforcement; a production rehearsal confirmed 20/20 probes
bit-identical across 186,775 rewritten rows.

Three mechanisms, in this order; the order is load-bearing, since the free-text
clear finds its rows by `subject_col = user_id` and the id sweep is about to
overwrite that:

1. **Free text cleared** (`ANONYMIZE_FREE_TEXT_COLUMNS`): the member's own
   expression, row kept and text gone. Each entry names the column identifying
   the *author*, never merely a column naming the member, so the whispers they
   sent go and the ones they received stay.
2. **Id and username swept from text**: inside JSON blobs, CSV id lists and
   other members' message content. `instr()` rather than `LIKE '%…%'`, because
   `LIKE` is case-insensitive for ASCII while `REPLACE` is not, and a dry run
   whose counts do not match the apply's rowcounts is worse than no dry run.
   The **display name is deliberately not swept**: a username is a globally
   unique handle with no spaces, a display name is free-form and routinely a
   common word. Handles are unique but **not prefix-free**, though, so the
   username needle is *shielded*: every other `known_users.username` that
   contains the subject's is parked under a sentinel for the length of the
   statement and restored by the same statement (`_username_shields`). Without
   it a departed `lark_hollow` rewrote a live `lark_hollow92` to
   `cleared_user_192`, in that member's own roster row. The shield is in the
   `instr()` count as well as the `REPLACE`, so a row naming only the live
   member is neither counted nor touched.
3. **The id itself**, in every column the live schema says names a member:
   `SUBJECT_ID_COLUMNS` intersected with each table, as the export does and
   unlike the purge's ~130 hand-written statements.

`ANONYMIZE_KEEP_TABLES` holds the nine tables that keep the real id, decided
table by table; `data_register.md` § The third path carries the per-table
reasoning, including the two directions the list deliberately disagrees with the
purge's preserve decisions.

**Differences from the purge worth knowing.** It runs across **all guilds at
once** (a partial run leaves the real name beside timestamps that match the
surrogate exactly), and it tolerates **only** genuine schema drift: a
constraint failure propagates and rolls the whole rewrite back, because a
swallowed statement here would leave the real id in place while the report
claimed the sweep succeeded.

**A table it cannot safely name is skipped, and the report says which.** A
table name is reflected, never bound, so every schema-walking sweep formats it
into its own statements; `guild_purge_service.table_names` puts each through
`sql_identifier` and drops one that is not a plain identifier rather than
letting it abort every erasure. That name then lands on the report's `skipped`
list beside the ordinary drift entries; the operator answers the subject from
the report, not from the log.

**No mapping table is written.** A retained surrogate ↔ real-id key would make
the result pseudonymous under Recital 26 and it would remain personal data, so
the operation is one-way and cannot be undone. `known_users` keeps one row per
surrogate (`1 → "Cleared user 1"`, `is_bot = 0` so `core/bot_exclusion` still
counts it), a label, not a link, and what stops every embed rendering a bare
`<@1>` when `name_resolver` falls back to a mention.

**The clear lists are the part that goes stale, so sweep after every run.**
The id rewrite is complete the moment it runs; "which text is this member's own
words" is a judgment made table by table. The first production run
(2026-09-11) reported success while judgments were missing for
`audit_log.extra.$.name` (38 voice-room titles they typed),
`qa_verdicts.note` and a since-dropped transcript table, each in a table the id sweep had already reached, so the
report said "anonymized" with their words beside the surrogate.
`anonymize_missed_text` runs the text half against an already-anonymized
surrogate (`--remediate --surrogate N`), with the inverse guard: the subject
must be a surrogate, never a live account. The runbook §2b makes the sweep a
required step rather than a good habit.

**The honest limit, stated rather than overclaimed:** this is strong
pseudonymization, **not** mathematical anonymity. The subject's messages keep
their timestamps and channel ids, so someone holding Discord scrollback for
those channels can still correlate the surrogate back to a real account. What
the rewrite removes is the ability to do that *from this database alone*.

The second half of that limit is the display name. Production holds a *current*
member called `Lark Hollow` (`lark_hollow92`), so a needle for a departed
member's display name would rewrite a different living person, and narrowing
it to a transcript speaker prefix does not separate them. Text on *other* members' rows
can therefore still quote the departed member's lines under their display name, because clearing it
would destroy other members' evidence. State the result as **"no longer identifiable by id or
username from this database"**; never as "anonymous".

## Delete everything

The design call was *"hard erase but anonymize usage data"*, which is exactly what the departure
job does, so the button runs that recipe on a member who is still here:
`privacy/self_erasure_service.erase_everything`, driven by
`PrivacyCog.erase_everything`. **No dial.** *"It's always on, it's an important
privacy guard"* is a deliberate exception to ship-it-dark.

**Order.** Claim `_active_deletions` (held through both confirms and the whole
run) → first confirm (`render_everything_confirm`) → last check → refuse a
jailed or ticketed member (`departure_erasure_service.standing_hold`, the
departure holds without its dial) → `run_deletion(MODE_ALL)` on the same
progress card as the message buttons, stopping with nothing else touched if
any message failed → decide scope (`choose_scope`: all guilds only when pressed
in the primary guild by a member in no other shared guild) → recheck the hold
→ end an open break through `breaks.service.end_break`, roles restored, and stop
if it won't → `clear_member_media` → `anonymise_member` under
`open_db_immediate`, which rechecks the hold under the lock → summary card
(`render_everything_summary`, one per outcome).

**Why the break is ended, not deleted.** Departure deletes the row because
nobody is there to give roles to. A present member with a deleted row would
keep the break role and lose the list that restores everything else.

**Live state is not protected, on purpose.** A leaver's wager escrow, game
seat, owned voice room and Pen Pals pairing are refunded and closed by the leave
listeners before departure runs. A member pressing this button has had none of
that, and nothing checks. Billy: *"just erase"*. They move to the surrogate
with everything else. `privacy.logic.EVERYTHING_IN_PROGRESS` says so in the
confirm, and `tests/privacy/test_privacy_modes.py::test_everything_confirm_says_anything_in_progress_is_lost`
pins it, because that sentence is the only guard.

**Not an `ErasableStore`.** The store contract is "the same function
`purge_user_data` runs". This button's eraser is the anonymization, so it would
break the register↔panel test. It sits on its own row below the stores.

**The receipt** is `privacy_self_anonymise`, with the bot as actor and
`target_id` NULL. The member's id as actor would be the one row the run left
naming them. The log line carries the guild and the outcome, never the id.

**What it does not do.** It does not purge: XP, coins, game history and the
rest stay in the server's totals under a surrogate. Warnings, jails, tickets,
DM consent and no-contact rows keep the real id (`ANONYMIZE_KEEP_TABLES`). A
guild-scoped run is linkable in the way the departure section describes, and
the summary tells the member to run it in their other servers.

## Departure auto-anonymise

When a member leaves, the bot runs the message deletion and the anonymization for them.
Built to `docs/plans/departure-auto-anonymise.md` (recoverable with
`git show 678a16fa:docs/plans/departure-auto-anonymise.md`);
`privacy/departure_erasure_service.py`, with the listener and worker in
`privacy/cog.py`.

**The dial.** `departure_auto_anonymise`, per guild, `"0"` by default. It is set from Member Data → When Members Leave and is
deliberately **not in `settings_registry`**: that registry feeds the setup
advisor, which ranks a wired-but-off feature as its best suggestion, and an
irreversible erasure is not something to nudge an admin toward. The other two
privacy dials are absent from it for the same reason. Switching it off empties that
guild's queue, so a later switch-on never reaches someone who left while it
was off.

**Queue, not event.** `on_member_remove` checks `hold_reason` and writes a
`departure_erasure_queue` row (migration 231). It does no work in the event
itself, because nine other leave listeners look the member up by real id, a
wager refund among them, and listener order is undefined. `on_member_join`
deletes the row. A `tasks.loop` running every 60 s takes the oldest row past
its `next_at` (`SETTLE_SECONDS` = 300), one job per tick. It skips a member
already in `_active_deletions`, the set the My Data panel and the Discord Messages
tab use.

**Held back, for good.** A bot; an id below the snowflake floor, i.e. already a
surrogate; the dial off; a member with an active jail; a member who opened a
ticket, or is a participant not removed from one, whose status is `open` or
`closed`. An archived (`deleted`) ticket does not hold. A release or an archive
does not re-queue anyone. `process_job` asks again at pickup, after the message
scan and the presence sweep (the last thing before anything irreversible), and
again under the write lock, together with "are they back in the guild?". Any hit
deletes the row with nothing done.

**One job, in order.**
1. `run_deletion(MODE_ALL)`. A raise, or `failed > 0`, stops the job before
   anything irreversible.
2. `games.guess.cleanup_service.clear_member_media`: take down the bot's post for
   each of the member's Guess rounds, then delete their cached originals with
   `games.guess.repo.list_cached_originals_for_user`, which ignores age and solve
   state. A post that raises anything but NotFound/Forbidden, or a file that
   exists but cannot be unlinked, stops the job. Each
   path is cleared in **its own transaction, right after its own unlink**, and
   never in step 3's, which can roll back: an unlink cannot, so a shared
   transaction would let a second file's refusal restore the first file's path
   and leave the row naming something already deleted.
3. In one `open_db_immediate` transaction:
   - delete the queue row, **before** the rewrite, since the id sweep would
     otherwise turn it into a row naming the surrogate;
   - then `anonymise_member`, the core My Data's Delete everything shares:
     delete an open break, delete their Guess records with
     `games.guess.repo.forget_member` in every guild the scope reaches (with the photo
     and post gone a kept round would be a hole), allocate the surrogate, anonymize,
     and write a `privacy_auto_anonymise` receipt holding surrogate, scope and
     counts, with `target_id` NULL and the bot as actor.

**Scope** (`choose_scope`):
- All guilds (`anonymize_user_data`) only when the member left the **primary**
  guild and is in no other guild the bot is in. Presence is checked with
  `get_member`, then `fetch_member`; any HTTP error other than NotFound raises,
  so an unknown answer is never read as "gone".
- Otherwise `anonymize_user_guild_data` on the guild they left. It narrows
  every statement to rows reached through that guild: its `guild_id` column, a
  child table's parent, or a channel-keyed table's live channel ids. Rows in
  bot-wide tables, and the member's rows in other guilds, keep the real id. The
  result is **linkable**: the surrogate's activity in the left guild can be
  lined up against the same member's untouched rows elsewhere, and its report
  says so (`out_of_scope`).
- A second guild's dial therefore can never do what the Anonymise tab refuses
  to do from there.

**Failure.** The row keeps `attempts` and `last_error` (truncated to 300 chars).
Retries back off from `RETRY_BASE_SECONDS` = 1800, doubling each time. After
`MAX_ATTEMPTS` = 5 the row parks as `failed` and waits for an admin's Retry or
Leave alone. The worker never retries a parked row by itself. A restart
mid-scan leaves the row in place, and deletion is idempotent.

**What it does not reach.**
- Messages in channels the bot cannot read.
- A leave while the bot was offline: no event fires and there is no
  reconciliation.
- Members who left before the dial was turned on.
- Pictures the bot itself posted, such as the Guess Who crop.
- A sponsored emoji or role icon the member bought.

## Subject access export

`export_user_data` (`src/bot_modules/privacy/service.py`) is the read
half of the erasure path, the answer to an access request (GDPR Art 15) or a
portability request (Art 20). Like the purge it is **unwired from any command**
and run by an operator: `scripts/export_user_data.py --guild <gid> --user <uid>`,
with `--summary` for a table/row-count view that discloses no content. The
procedure is in [gdpr_runbook.md](../gdpr_runbook.md) §1.

**It finds tables by column discovery, not from a curated list.** Every table in
`sqlite_master` whose columns intersect `SUBJECT_ID_COLUMNS` (88 conventional
member-reference names: `user_id`, `author_id`, `reactor_id`, `winner_id`,
`user_a`/`user_b`, …) is queried for the subject, guild-scoped where the table
has a `guild_id`. A curated list is the thing that goes stale, and a stale
access export is an incomplete answer to a statutory request; discovery means a
new feature's table is covered the day it lands, provided it names its member
column conventionally. The one table discovery skips is one whose **name** no
query can safely carry (`sql_identifier` refuses it); the export leaves it
unsearched and says so in `notes`, because an access answer that quietly omits
a table is the wrong answer, and the name has to be checked by hand.

**The export is deliberately a superset of the purge.** Categories the server
keeps under Art 17(3) (`econ_ledger`, sanction history, consent audit,
no-contact orders) are exported even though they are never deleted. Retention
is not a disclosure exemption. `test_export_covers_every_table_the_purge_deletes`
enforces the direction: any table the purge learns to delete must be reachable
by the export, and the test reads both table lists off the source rather than
restating them.

Two limits the export reports rather than hides:

- **Message children** (`message_mentions`, `_reactions`, `_sentiment`;
  the attachment and embed tables were dropped by migration 228) carry no member column, so they are the subject's
  data only by hanging off their message. Discovery cannot see them, so they
  are reached by joining through the author's message ids, chunked at 500 like
  the purge.
- **List-valued columns** (`risky_pending_questions.participant_user_ids`, the
  games family's rosters and payloads, and the rest) store member ids as
  JSON/CSV, which an equality match cannot find. `LIST_VALUED_MEMBER_COLUMNS`
  names them, and the export emits a note telling the
  operator to grep them by hand; the member-facing report scans them by value.

Tables in `THIRD_PARTY_TABLES` (whispers, guesses, no-contact orders,
moderation records) come back flagged in `review_required`.
Art 15(4) says an access request must not adversely affect others' rights, so
the counterparty decision is surfaced for a human rather than resolved by
redacting (which corrupts the record) or dropping (which hides the tension).

## Guild removal erasure

The member-erasure paths above erase a **member**. This section is the other direction:
what happens to a guild's data when the bot is removed from that guild.

Without `guild_purge_service` a departed guild's rows would stay in the database forever, in every table carrying a
`guild_id` (253 of 293 tables on 2026-09-20). `guild_purge_service` closes that.

**Classification.** The service reads the live schema rather than a hand-written
list, so a table added next year is covered without anyone remembering to add
it. Every table falls in exactly one of four buckets: **guild-scoped** (has a
`guild_id` column, deleted by it), **child** (reached through its foreign key
to a guild-scoped parent, deleted deepest-first so a grandchild goes before the
row it points at), **channel-keyed** (`games_session_tracker`,
`legitlibs_channel_config`; no `guild_id`, reached through the channel-id
snapshot taken at removal), and **global** (holds no guild data; never touched).
`tests/privacy/test_guild_purge_coverage.py` **hard-fails on a table in none of them**
in CI and in any full gate run, the same standing the data register has for
member data. The scoped pre-commit gate selects both tests whenever a migration
or a store file (`store.py`, `*_store.py`) changes (`gate.PRIVACY_COVERAGE_TESTS`),
so a work branch meets them before it merges, not at push.

**The dial.** One instance-wide grace period, on the dashboard's Config →
Moderation & Privacy page, with states rather than several toggles:

| Value | Meaning |
| --- | --- |
| unset (default) | **Never purge.** A removal is queued and logged; nothing is deleted. This is what every deployment starts as |
| `0` | Purge immediately: the removal handler runs the purge in the same event, without waiting for the daily sweep |
| `N` (1–365) | Purge `N` days after removal; a re-invite inside the window cancels it entirely |

It is stored at `guild_id = 0` and writable only from the primary guild, because
the departing guild's own config rows are among the things being deleted, so a
per-guild setting would erase itself. Reads are unrestricted so every guild's
panel can show the operator's policy.

**Timing.** `on_guild_remove` writes one `departed_guilds` row: the guild id,
the timestamp, the deadline **baked in** (never recomputed later, since the config it
came from will be gone), a snapshot of the guild's channel ids, and whether the
row came from the event or from reconciliation. A daily loop purges what is due.
`on_guild_join` deletes the row, so a re-invite before the deadline loses
nothing.

**Downtime.** A removal that happens while the bot is offline fires no event, so
the daily loop also reconciles: any guild the database knows but the bot is not
in gets **queued**, never purged on the spot, and any queued guild the bot
plainly *is* in is un-queued (the latter even with the dial off, since
cancelling a purge is always safe). Reconciliation refuses to run against an
empty guild list, so a failed Discord connection cannot queue the whole
instance.

**Guild 0 is refused.** It is the instance-wide config slot, the shared
LegitLibs template pool and the bot-global AI prompt store. The service raises
rather than trusting a caller to filter it.

**What goes that a member erasure keeps.** `no_contact_pairs`,
`no_contact_events` and `econ_ledger` are all preserved against `purge_user_data`
under Art 17(3): the first two because the order protects the *other* party,
the third for double-entry integrity. A guild removal deletes them anyway. Both
grounds are about a surface that still exists: once the bot is gone there is
nothing that could consult a protective order, and the books whose integrity is
being kept are that guild's. See the note in `docs/data_register.md`.

## Permissions

- The bot needs **Manage Messages** to delete messages, **Read Message History** + **View Channel** on every channel it scans, **Manage Threads** to surface unjoined private archived threads, and **Send Messages** in forum threads where it has to post the `[deleted]` tombstone.
- The My Data panel is open to everyone; rejects DMs. Every button on it is actor-locked: a press by anyone else is refused, the same check the confirm step makes.
- The Member Data panel and every route behind it require **admin** (`require_perms({"admin"})`); moderators do not have member erasure.
- The confirm buttons hard-check that the clicker is the actor. On the dashboard the equivalent is the route's own permission dependency; there is no separate confirm token.

## User-visible errors

| When | The user sees |
|---|---|
| My Data opened in DMs | "❌ This only works in a server." |
| Member Data opened by a moderator | The nav entry renders locked, and every route returns 403. |
| A message deletion already running for the actor | "❌ A deletion is already running for your account — please wait for it to finish." |
| A deletion already running for the target | Member Data: 409, "A deletion is already running for that member." |
| Wrong user clicks the confirm button | "This isn't your confirmation." |
| Confirm view times out / cancelled | "Cancelled." (or no message on timeout) |
| Scan-of-empty | "All done. No messages found in any channel I can read. Nothing else was touched — XP, activity, and profile stay as they are." |
| Discord-side delete partially blocked | Summary still posts; counters show how many couldn't be deleted |
| Interaction token expired mid-run | Final summary is DM'd to the actor instead of edited in place |

## Non-goals

- **No undo.** The two confirm steps are the only safety net; deletion is permanent.
- **No partial selectors.** Users can't say "delete only my XP" or "delete only my messages in #channel". The only switches are which surface (the My Data panel for yourself, the Member Data tab for another member) and the `mode` (all / media / text).
- **No cross-guild delete.** The My Data panel's message and store buttons clear one guild only; a user in three servers must run them in each. Delete everything reaches every guild's *records* only under `choose_scope`'s all-guild case, and still deletes messages in the guild it was pressed in.
- **No DB deletion from the message deleters.** Neither the My Data panel's message buttons nor the Member Data panel's Discord Messages tab touches any server-side row, [[dm-perms-spec]] audit / consent rows included. DB erasure is the manual, out-of-band `purge_user_data` run (Phase 3 above), and even that deliberately preserves the consent/audit forensic record.
- **No member-facing export command.** The [Subject access export](#subject-access-export) is an operator script, with the same reasoning as the purge.
- **No retry queue** for the message buttons or the Discord Messages tab. Failed deletions surface in the summary but don't reschedule; re-running after fixing perms is the path. The departure auto-anonymize is the exception: it has its own queue with backoff (above).
- **No notification to the target.** An admin deleting Alice's messages from Member Data does not DM Alice; the action is silent except for the actor's own progress display.

## Configuration

Privacy has one **per-guild** setting, `departure_auto_anonymise` (default `"0"`, see [Departure auto-anonymise](#departure-auto-anonymise)), and one **instance-wide** setting: the guild-removal grace period described above, stored at `guild_id = 0` and editable only from the primary guild. It ships unset, which means never purge. The behavioral constants (60-second confirm timeout, the 14-day cutoff between bulk and one-at-a-time delete, the throttle cadences) mirror Discord's own constraints and are not exposed.

The only per-call switches are the `mode` (which slice of Discord messages is targeted) and whether the channel walker tries to surface unjoined private archived threads (on for privacy, off for the [[events-spec]] backfill caller).

## Stored data

The My Data message buttons and the Discord Messages tab are **pure deleters of Discord messages**: they purge no table. Privacy owns two tables of its own, neither used by them. `departure_erasure_queue` holds a departed member's real id only until their auto-anonymize runs, and is deleted in that same transaction. It is also in `purge_user_data`'s simple-table list. The other is `departed_guilds`, which belongs to the guild-removal path: one row per departed guild holding the id, the timestamps, the channel snapshot and how the row was created, deleted in the same transaction as that guild's data. It names no member, so `purge_user_data` has nothing to clear in it. Deletion enumerates the target's messages by walking Discord itself (a live `channel.history` scan across every readable channel, as Phase 1 describes), **not** by reading the local `messages` archive, so gaps in the archive don't limit what gets deleted.

The `messages` archive itself holds less than the name suggests: the bot stores no message text, attachment URLs or embeds, only metadata derived at ingest time (author, channel, timestamps, media kind, sentiment). Migration 228 dropped the `content` column and the attachment and embed tables. That metadata is disclosed in the privacy notice (and in the `privacy-tools` doc once its staged body is saved and re-posted), not in the My Data panel or confirm prompt (see Confirmation above for why).

**Preserved by the message deleters**: everything server-side, since they write no DB deletions at all. For the out-of-band `purge_user_data` run, the preserve list is implicit: a table is purged iff it's named in the purge (XP incl. reaction awards and the retired `xp_daily` rollup, activity, birthdays, bios, wellness, watch/trust/invite pair tables, member events, audit events, per-member economy/casino state, and optionally the archive), and deliberately preserved otherwise: the `econ_ledger` double-entry record, [[dm-perms-spec]] consent and audit data, [[no_contact_spec]] protective records, [[jail-spec]] tables, [[confessions-spec]] reply routing (`confession_threads`, left to its 14-day TTL); the pending queue, the alias map, the rate-limit row (bug #222: it timestamps when the member posted anonymously, and it now ages out on the same 14-day sweep as well) and every `anon_audit_log` row naming the member are purged. The [[guess-spec]] tables and the welcome card are purged, not preserved: a round the member submitted or is the answer to is deleted whole with the guesses and log rows on it, and their own `intake_cards` row goes with its steps. What survives there is column-level: their id as another round's `solver_id` (scrubbed to NULL) and as the greeter on another member's card.

[[pressure-cooker-spec]] rows are **not** preserved: `_delete_game_records` takes them with the other group games, and `pressure_nicks`/`pressure_cooldowns` were dropped outright by migration 232.

**The five rows bug #222 re-decided.** An engineering reason in the register's ground cell is not a lawful basis, so each is now erased as follows. `grant_role_permissions`: purged (`user` rows only), a standing authority, not a record anyone relies on. `confession_rate_limits`: purged, and given the 14-day TTL it never had. Whispers: purged through `whisper.repo.forget_member`, the same engine the My Data panel button runs. `pen_pals_pool`: purged (a live queue). `pen_pals_sessions`: closed, then the member's seat scrubbed, because the row is also the other member's no-repeat memory. `pen_pals_blocks`: the member's own blocks purged, blocks against them and admin separations preserved on Art 17(3)(e). `mahjong_tables`: a closed row's `state` snapshot blanked, since a preserved `mahjong_results` foreign key forbids deleting the row itself. The grounds are in `docs/data_register.md`; each feature spec carries the consequence.

New per-user tables landing in other features must make an explicit decision: join the purge or document why they're kept, and add a row to `docs/data_register.md` either way.

**GDPR review additions.** The purge also clears the starboard (both who was starred and who starred), duel and group-game records, role-menu picks, inactivity exemptions, the LegitLibs writing tables, the two paid-submission products (`econ_emoji_submissions`, `econ_qotd_submissions`), and eight small per-member stores. One category is on the **preserved** side with a named ground: `created_by`/`updated_by`/`set_by`/`hidden_by` on admin-authored configuration (a scheduled announcement, a dashboard doc, a role menu, a hidden channel), kept under Art 17(3)(e) as the record of who configured a server surface, extending the existing `mention_award_rules.created_by` precedent rather than leaving it a one-off. The five tables the review raised as open decisions were settled by the owner, and three of them are **not** a plain delete. `quote_audit_log` goes whole from either side. `inactive_members`, `promotion_review_cards` and `qa_tests`/`qa_verdicts` split by role: the row is deleted where the member is its subject, and kept with the id blanked where they were only the mod who acted on somebody else's row, since deleting there would take an unrelated member's record away because a mod asked to be erased (the `todos` precedent). `voice_master_blocked` is **asymmetric**: the member's own block list goes, entries naming them as the blocked party are preserved under Art 17(3)'s rights-of-others carve-out. That is deliberately unlike `voice_master_trusted`, which is purged from both sides: a trust list confers something and can be dropped freely, a block list withholds something and cannot.

The purge's schema-drift tolerance can hide real failures (a purge statement naming a missing column fails silently). `tests/privacy/test_privacy_service.py::test_purge_runs_clean_against_the_migrated_schema` fails on any swallowed schema error, and `scripts/privacy_coverage.py` reports, from a live database, every column whose values are member ids but whose name `SUBJECT_ID_COLUMNS` does not know.

In-memory only: the set of currently-running deletions (keyed by target user id), cleared in a finally-block whether the run succeeded, failed, or was cancelled.
