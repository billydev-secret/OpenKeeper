# Data register — record of processing activities

Every table holding personal data: what it is, how long it is kept, whether the
erasure path clears it, and what leaves the box. Built by the 2026-08-05 staged
review's per-bundle GDPR passes; **promoted out of `docs/reviews/` on 2026-08-06
to a maintained document**, because a record of processing that is only a dated
audit snapshot goes stale; this one had drifted within a day of being written.

## Controller

| | |
|---|---|
| Controller | **The Golden Meadow** (TGM), the Discord server the bot runs for. It decides what the bot collects and why. |
| Contact for a data request | Open a TGM ticket (`/ticket open`). Self-service covers part of it: `/mydata` for what a member can see and clear, and the message-deletion buttons. `manual.html` § Your Data & Privacy tells members the same. |
| EU/UK representative (Art 27) | None appointed. |
| Data protection officer | None appointed. |

## Processors and transfers (Art 28; Art 30(1)(d)–(e))

What each one receives is in the [running inventory](#processors-running-inventory)
below. This table records the terms each works under and the safeguard for data
leaving the UK/EU. The safeguard wording matches `manual.html` § Where in the
world, which is what members read. Every contract is the provider's standard
terms, accepted on sign-up; there is no separately negotiated agreement.

| Processor | Role | Located | Transfer safeguard | Terms |
|---|---|---|---|---|
| Discord Inc. | Hosts everything the bot reads and writes | US | EU–US Data Privacy Framework (+ UK extension); SCCs | Discord Developer Terms of Service and Developer Policy |
| Anthropic PBC | AI assistant, quest ideas, game prep; the operator's Truth or Dare bank | US | SCCs | Anthropic Commercial Terms of Service, which incorporate its Data Processing Addendum |
| Cloudflare, Inc. | Dashboard tunnel. It terminates TLS, so dashboard traffic (session cookie, ids in requests, each panel's data) passes through it decrypted | US | EU–US Data Privacy Framework; SCCs | Cloudflare Self-Serve Subscription Agreement, which incorporates its DPA |
| Google LLC (YouTube) | Music search text and video loads, under the bot's own Google account; oEmbed title lookups | US | EU–US Data Privacy Framework (+ UK, Swiss extensions) | YouTube Terms of Service. Not a processor contract: Google acts as its own controller for these requests |
| Spotify AB | Track lookups; playlist add/remove by track id | Sweden (EU) | None needed | Spotify Developer Terms. Spotify acts as its own controller |
| `cipher.kikkia.dev` | YouTube player signature code only: no track, no search, nothing about a member | Unknown | Not needed: no personal data | None |

**Not processors.** GitHub holds the code and runs CI on a fixture database
(`dk_dev.db`); no workflow reads the production database, and the 2026-09-13
history rewrite removed the personal data once committed to the repo. The NAS
holding off-device backups is the operator's own hardware in the same home.

> **On the per-commit docs contract.** A new table holding per-user data needs a
> row here in the same commit, with an explicit purge/preserve decision. See
> `CLAUDE.md`. A preserved table must name the ground it is preserved on, not
> just the engineering reason.

**Related:** [gdpr_runbook.md](gdpr_runbook.md) is the operator procedure for
access, erasure, anonymisation and breach. `scripts/export_user_data.py` answers
"what do we hold on this person" from the live schema. Run it rather than
reading this table when the question is about one member.

**The Purge? column answers one of three questions, not one of two.** Since
2026-09-11 there is a third path, `anonymize_user_data`, for a member who has
left, and a table's purge decision does **not** determine its anonymise
decision. See [The third path](#the-third-path-anonymisation-2026-09-11) below;
the two lists differ deliberately in both directions.

Columns: **Purge?** = is the table covered by `purge_user_data` (verify in code,
cite `file:line`); where a table is deliberately preserved, name the **Art 17(3)
ground**. **Processor** = data leaving the box.

**`**Member-erasable**` in the Purge? cell** means the member can clear this
store themselves, from the **My Data** panel. That is a different question from whether
an operator erasure reaches it, and recorded here because a store the member
can delete is one they have already been promised control of.
`tests/privacy/test_privacy_member_data_panel.py` fails if a row carries the marker
with no button on the panel, or a button clears a table whose row lacks it. The
marker is the only member-facing erasure surface there is: a feature does not
ship its own forget-me command (2026-09-17, bug #222 item 3: `/whisper
forget-me` was retired into the panel, and `/delete_me` with it).

**Retention vocabulary (set 2026-09-05).** The word *indefinite* used to do two
incompatible jobs: "we decided to keep this forever, on this ground" and
"nobody ever looked". It appeared in 44 of the 78 data rows, which made the
column unreadable as a record of decisions. The four values now mean distinct things:

| Value | Meaning |
|---|---|
| **permanent — <ground>** | A decision, with the ground named. Never deleted. |
| a period (e.g. **180 days**) | Enforced by a sweep. Cite the code. |
| **until &lt;event&gt;** | Bounded by a condition, not a clock (until removed, until purged). |
| **undecided** | No retention decision has been taken. Kept forever by default, not by choice. |

A cell beginning **`Mixed.`** covers several tables whose periods differ; take
no single duration from it. Two rows are mixed: the XP/activity family and
`guess_rounds`.

Everything else in this column obeys one rule, added 2026-09-05 after two cells
were found breaking it: **a duration appears here only if something is actually
deleted on it.** `whispers` had "the 30d age-lock is a visibility rule, not a
deletion", but nothing ages out, yet a reader taking a number got 30 days. `xp_daily`
said "permanent by design: what survives `xp_events`' 90-day retention", so a reader
got 90 days for a table kept forever, the exact opposite of the truth. Both now
carry their explanation in Notes. Measurements, justifications and cross-references
belong in Notes; the Retention cell states the period and its mechanism.

**17 rows are `undecided`** (recounted 2026-09-13; against 20 `permanent`, 14 a period, and the rest
event-bounded or mixed). That number is the honest backlog, and it is the
one to watch: it should only ever go down, and a new row should never be added
as `undecided` without a reason it could not be decided at the time.

## Preserved categories and their grounds

Erasure that quietly retains categories is worse than a documented partial
erasure. These are the five the purge deliberately keeps:

| Preserved | Art 17(3) ground | Strength |
|---|---|---|
| `econ_ledger` | (e) legal claims, and integrity of a double-entry record where deleting one side corrupts the counterparty's balance | Solid |
| Sanctions: jails, warnings, tickets, policy tickets | (e) legal claims: the canonical record if a moderation decision is challenged | Solid |
| `dm_audit_log`, `dm_consent_pairs` | (e) legal claims: the record exists precisely to answer "did they agree" | Solid |
| `no_contact_pairs`, `no_contact_events` | (e) legal claims + the **other** party's rights: erasing a protective order at the request of the restrained party defeats it | Solid |
| `voice_master_blocked` — the **target's** side only | (e) rights of others: a block entry is the room owner's protection, and erasing it at the request of the member it excludes defeats it. The `no_contact_pairs` reasoning, applied to a smaller surface. The erased member's *own* list is purged | Solid |
| Admin-authored configuration: `created_by` / `updated_by` / `set_by` / `hidden_by` on announcements, dashboard docs, role menus, hidden channels, chat-revive questions, shop items, mention-award rules | (e) legal claims: the record of who configured a server surface, and the counterpart to whatever that surface then did. The row is about the **server**, not about the member | Moderate: clean for a surface that spends coins or grants roles, thinner for a doc edit. Revisit if a request is contested |

Everything preserved is still **exported** on an access request: Art 17(3)
exempts deletion, not disclosure.

## The third path: anonymisation (2026-09-11)

`privacy_service.anonymize_user_data` answers a question neither of the other
two does: *a member has left, and their data must stop being theirs, but the
server still needs to be comparable against its own past.* The purge is the
wrong instrument for it: measured on production for one heavy member it
destroys **163,701 of 184,009 rows**, the entire XP timeline included, so every
"how busy were we in July" question loses its baseline.

Instead the member's Discord id is rewritten to a **small surrogate integer**
(1, 2, 3 …, far below the snowflake range so it cannot collide with a real
account), their names are destroyed, and their own free text is cleared. Every
metric here is an aggregate over rows keyed by a member id, and aggregation
needs that key to be *stable and distinct*, not *meaningful*, so **every count,
sum, rate and distribution is mathematically unchanged**. That claim is enforced
by `tests/privacy/test_privacy_anonymize.py`, not asserted here, and was verified on a
production snapshot: 20 of 20 aggregate probes bit-identical across 186,775
rewritten rows.

**No mapping table is written**, and that is the point. A retained
surrogate ↔ real-id key would make the result pseudonymous under Recital 26 and
it would remain personal data. The operation is one-way and cannot be undone.

**Honest limit, stated here rather than overclaimed.** This is strong
pseudonymisation, **not** mathematical anonymity. The subject's messages keep
their timestamps and channel ids, so someone holding Discord scrollback for
those channels can still line the surrogate's activity up against a real
account. What the rewrite removes is the ability to do that *from this database
alone*. Say that to anyone who asks; do not call the result anonymous.

### What keeps the real id, and why it is not the purge's list

`ANONYMIZE_KEEP_TABLES`: decided table by table by the owner on 2026-09-11.
The common thread is that **the row exists in order to name a person**, so a
surrogate empties it of its purpose:

| Kept as-is | Why a surrogate breaks it |
|---|---|
| Sanctions: `jails`, `warnings`, `tickets`, `ticket_participants`, `policy_tickets` | A warning's whole payload is "this member did X"; anonymised it says "someone did X". Warning counts also drive escalation, so a rewritten history hands a returning account a clean sheet |
| `dm_audit_log`, `dm_consent_pairs` | Consent forensics, and the party it mostly protects is the **other** one. It is what a member shows when accused of unsolicited DMs |
| `no_contact_pairs`, `no_contact_events` | The only rows that must keep matching a **live id in future**. A rewritten pair stops matching, and the failure is silent by design: the gate refuses indistinguishably from an ordinary outcome, so nothing anywhere would report that the protection had stopped working |

**Rewritten here although the purge preserves them**: the asymmetry is
deliberate in this direction too:

| Rewritten | Why the preserve ground does not reach |
|---|---|
| `econ_ledger` | A balance is a `SUM` over `user_id`, so the whole balance moves to the surrogate intact and no counterparty moves by a coin. The Art 17(3)(e) integrity worry is about *deletion*, which this is not |
| `audit_log` | Pure statistics about server actions, and statistics is exactly what survives a rewrite. (`reaction_log` and `voice_follow_log` were rewritten on the same reasoning until migration 229 dropped them.) |

**One accepted inconsistency:** `audit_log` is rewritten while `warnings` is
kept, so a `warning_delete` entry and the warning it describes now disagree
about who the member was. A display oddity on an admin-only surface, not a loss;
the warning row itself still names them.

### The receipts these operations write (decided 2026-09-13)

`audit_log` being rewritten has a consequence that only shows up once these
operations get a surface that writes receipts, which the Member Data panel
(`web_server/routes/privacy.py`) does. A receipt recording an anonymisation is:

* written **after** the run → it puts the real id back into the database the
  run just finished cleaning. The anonymisation is undone by its own receipt.
* written **before** the run → the sweep rewrites it to the surrogate along
  with everything else, so the record no longer says who was anonymised.

Erasure has the mirror problem: `purge_user_data` does not clear plain
`audit_log`, so a receipt naming the erased member would be the one row that
outlives the erasure, the register's own worst case.

**Decision (Billy, 2026-09-13): receipts name the operation, never the
subject.** Every `write_audit` from the privacy routes carries
`target_id = NULL` and an `extra` holding counts only: surrogate number, rows,
tables. Actor and timestamp are real, which is what accountability actually
requires: who ran it, when, and how much moved. Nothing in the receipt says who
it was about, and nothing needs to.

Deleting a member's Discord messages writes **no receipt at all**. That path
never opens the database, a guarantee `bot_modules/privacy/deletion_service.py`
states and a test asserts, and a receipt would be the thing that broke it.

### Automatic on departure (2026-09-15)

Behind the per-guild `departure_auto_anonymise` dial, which ships off, the
anonymisation runs by itself a few minutes after a member leaves. It runs after
their Discord messages are deleted and their cached Guess Who originals
removed. Everything above still holds, with three additions:

- **Two scopes.** All-guild only for a member leaving the primary guild who is
  in no other guild the bot shares. Otherwise the guild-scoped variant runs,
  which rewrites only rows reached through the left guild. That result is
  weaker: bot-wide tables and the member's rows in other guilds keep the real
  id, so the surrogate is linkable back to them from this database. Say so
  when asked; the report counts what it left (`out_of_scope`).
- **Not everyone.** Jailed members, and members who opened or remain on an
  open or closed ticket, are left untouched for good. The same goes for
  members who come back before it runs.
- **The queue is personal data** (`departure_erasure_queue`, row above). It is
  deleted in the anonymise transaction itself, never after, because a queue
  row outliving the rewrite would be exactly the surrogate↔member key this
  section forbids.

The receipt is `privacy_auto_anonymise`, following the 2026-09-13 decision
below: surrogate, scope and counts, with `target_id` NULL.

**Guess records are deleted, not rewritten (2026-09-27).** Both automatic paths
(this one and the member's own, below) run `guess_repo.forget_member` inside
the anonymise transaction, in every guild the scope reaches
(`guess_repo.member_guild_ids` for all-guild). Their Guess posts and photos
are taken down first (`guess_cleanup_service.clear_member_media`), and a
round kept under a surrogate with no picture would be a hole rather than a
play record. Billy: *"Deleting the message should clear everything from
guess."* The operator's own Anonymise tab on the Member Data panel still
rewrites Guess rows to the surrogate; it has not been changed.

### The member's own press (2026-09-27)

My Data → **Delete everything** runs the departure recipe on a member who is
still here, from the Help & Settings panel, after two confirms
(`privacy/self_erasure_service.py`, sharing `anonymise_member` with the
departure job). Billy: *"Hard erase but anonymize usage data"*. There is **no
dial**: *"It's always on, it's an important privacy guard."* The differences
from departure:

- **No settle delay and no queue row.** The member pressed it, so nothing is
  held back for an accidental leave, and there is no row holding their id.
- **Holds refuse rather than keep.** A member who is jailed, or who opened or
  is still on a live ticket, is told a mod must be involved, and nothing is
  deleted (`departure_erasure_service.standing_hold`, the same checks without
  the dial). The check runs before the scan, after it, and again under the
  write lock; a hold caught by either later check stops the run with the
  messages already deleted gone and every record untouched. The summary says
  so rather than claiming nothing moved.
- **An open break is ended**, roles restored, rather than deleted: the member
  is still here to be given them back. A break that will not end stops the run.
- **Live state is not protected.** Nothing checks for an escrowed bet, a game
  seat, an owned voice room or a Pen Pals pairing, because no leave listener
  has refunded or closed them. They move to the surrogate with everything
  else. Billy: *"just erase"*. The confirm says so, and a test pins the sentence.
- **Scope** is `choose_scope`, as for departure: all guilds only when pressed
  in the primary guild by a member in no other guild the bot shares.

The receipt is `privacy_self_anonymise`, with the bot as actor and `target_id`
NULL. The member's own id as actor would be the one row the run left behind.

### What text is destroyed

`ANONYMIZE_FREE_TEXT_COLUMNS` clears the member's **own** expression, keeping
the row: `whispers.message`, `whisper_replies.content`,
`bio_answers.answer`, `bio_field_values.value`, `econ_bounties.title`/
`.description`, `voice_master_profiles.saved_name`, `duel_nicks.original_nick`,
`member_xp.last_message_norm`. `messages.content` was on this list until
migration 228 dropped the column (2026-09-13); the first production run nulled
17,226 message contents at **exactly zero** metric loss, because a message row
keeps its `ts`, `channel_id`, `sentiment`, `emotion`, `media_kind` and
`reply_to_id`.

Each entry names the column identifying the **author**, never merely a column
naming the member: `whispers` is keyed on `sender_id`, so the whispers the
member *sent* go and the ones they *received* (somebody else's words) stay.

Two things a naive sweep would have taken and this one does not:
`tickets.description` (a kept row whose text is the evidence) and
`music_playlist_tracks.title`/`.artist` (third-party song metadata on a row the
member happened to add). A third, `rules_ledger.excerpt`/`.matched_phrase`, was
kept on the same reasoning (measured before deciding, the member was the
`target_id` on those rows and the `author_id` on none, so the excerpts were
*other members'* messages about them) until the table was dropped with Rules
Watch (2026-09-13, migration 227).

### Three surfaces the first production run missed (found 2026-09-11)

The post-condition sweep after the live apply (a whole-schema walk for text
still sitting on the surrogate's rows) found three columns the clear lists did
not name. In each the id rewrite had already landed, which is the dangerous
shape: **the report said the member was anonymised while their words sat beside
surrogate 1.** An id sweep is complete the moment it runs because the schema
tells it every column; "which text is the member's own words" is a judgement
made table by table, and judgments are what go missing.

| Column | Rows | What it held | How it is cleared |
|---|---|---|---|
| `rules_events.window_json` (dropped 2026-09-13, migration 227) | 207 | A verbatim 5–6 line channel transcript captured around a flagged message, quoting their lines under their display name | Cleared on **any** subject column (`ANONYMIZE_TRANSCRIPT_COLUMNS`), not just where they are the author: they are `author_id` on 93 and `target_id` on 114, and both windows quote them equally |
| `audit_log.extra` `$.name` | 38 | The voice-room titles they typed: `"lark's Room"`, but also `"Film Night w Rook"` and `"Fern Appreciation Night"`, which name other people | `json_remove` of the one key (`ANONYMIZE_JSON_TEXT_KEYS`). Clearing the column would throw away 108 rows of `channel_id`/`reason` telemetry to remove text from 38 |
| `qa_verdicts.note` | 1 | A QA tester's note in their own words | Ordinary free-text clear; `verdict` and `paid_amount` stay, so the QA record and the payout are untouched |

None of it moves a metric: every column that makes those rows countable is a
number or an enum (`guard_verdict`, `priority_score`, `vader_compound`,
`mutual_interaction_count`, `paid_amount`), and `rules_ledger` (the table a
rules event was promoted to when it mattered) kept its `excerpt`, so clearing
the raw event stream destroyed nobody's evidence. Both Rules Watch tables, and
the transcript clear, were later dropped (2026-09-13, migration 227).

**Remediating a surrogate anonymised by older code.**
`anonymize_user_data` refuses a subject below the snowflake floor, because
rewriting one surrogate into another would silently merge two anonymised
members' metrics, so it cannot be re-run to pick up a grown clear list.
`anonymize_missed_text(conn, surrogate_id)` runs the text half only, with the
**inverse** guard: the subject must be a surrogate, never a real account.
Pointed at a live member it would destroy their words while leaving them fully
identified, which is worse than doing nothing.
`scripts/anonymize_user_data.py --remediate --surrogate N` is the operator
form, `--dry-run` by default.

**Keep the activity labels.** Owner's call, 2026-09-11: `xp_events.source`,
`xp_daily.source`/`.day`, `econ_ledger.kind` and their kin are declared `TEXT`
and sit on the subject's rows, so a sweep written as "every text column on a
row naming this member" takes them. They are not words anybody wrote. They are
the enum and the date that make the row countable, and clearing
`xp_events.source` would delete the difference between a message and a voice
minute across 48,646 rows. Pinned by
`test_activity_labels_are_never_cleared`.

### The id and username elsewhere

The numeric id and the **username** are replaced wherever they appear inside
text: JSON blobs, CSV id lists, other members' message content (13,038 rows in
production, mostly inside other people's rows). The **display name is
deliberately not swept**: a username is a globally unique handle with no spaces,
while a display name is free-form and routinely a common word. Sweeping one
production display name would have rewritten 246 question-bank routing tags, an
attachment URL, other members' channel and role names, and an ordinary phrase that happened to contain the
same word.

**The username needle is shielded.** Handles are globally unique but not
prefix-free, and the sweep is a plain substring `REPLACE`, so a departed
`lark_hollow` would have rewritten the live `lark_hollow92` to
`cleared_user_192`, in that member's own `known_users` row and in every
message that ever mentioned them. `_username_shields` parks every colliding
handle under a sentinel for the length of the statement and restores it in the
same statement, in both the `instr()` count and the `REPLACE`, so anonymising
one member cannot alter another member's record or overstate the dry run.

**The decisive case, measured 2026-09-11:** production holds a *second, current*
member whose display name is `Lark Hollow` and whose username is
`lark_hollow92`. A `lark` needle does not merely damage prose; it rewrites a
different living person's name, and narrowing it to the transcript speaker
prefix (`"] lark:"`) does not make it safe, because no needle can tell the two
apart.

**So this is the residue, stated plainly rather than rounded away.** After the
transcript clear above, roughly 500 `rules_events.window_json` blobs belonging
to *other* members' rows still quoted the subject's lines under their display
name, since they were simply talking in the channel when somebody else's message was
flagged. Erasing those would have meant erasing other members' evidence
windows. (`rules_events` dropped 2026-09-13, migration 227, which ended that
particular residue; the limit stands for any other text quoting a display
name.) The honest claim is that this database no longer identifies them **by id
or username**, and that a reader who already knows the display name can still
find them in other people's text. That is the same limit the section above
states, reached from a different direction.

`known_users` keeps one row per surrogate: `1 → "Cleared user 1"`, `is_bot = 0`
so `core/bot_exclusion` still counts it, `current_member = 0` because it is
true. That row is a **label, not a link**: it records which surrogates are
taken without recording who any of them was, and it is what stops every embed
and leaderboard rendering a bare `<@1>`.

Operator procedure: `docs/gdpr_runbook.md` § Anonymising a departed member.

| Table / store | Feature | Data class | Retention | Purge? | Processor | Notes |
|---|---|---|---|---|---|---|
| econ_wallets, econ_ledger, econ_* (49 tables) | Economy | user_id + amounts + timestamps; meta sampled clean (no PII) | **permanent — Art 17(3)(e)** (legal claims; and double-entry integrity, where deleting one side corrupts the counterparty's balance) | YES — **`econ_purge_user` shipped `6ac71558`** (`economy_service.econ_purge_user`, called from `privacy_service.purge_user_data`); `econ_ledger` deliberately preserved | — | G1 closed |
| econ_login_digest_cards | Economy — live login digest | `user_id` + the **Discord DM channel id and message id** of that member's morning digest DM, the guild-local day it belongs to, a hash of what the card currently renders, and the seven `LoginOutcome` scalars (paid, streak, milestone, grace, reset, shield, prior streak) frozen at send time. Migration 190. **No rendered prose is stored** — the quest board, the community movers and the wellness blurb are all re-read from source on each refresh, so the wellness section's text never lands here | one row per member per guild, overwritten by the next day's digest; the hourly sweep also deletes any row whose `local_day` isn't today, so it self-prunes within a day | **YES — purge** (in `econ_purge_user`'s `_PURGE_USER_ID_TABLES`). No Art 17(3) ground and none needed: it is a pointer to a message in the member's own DMs, not a record of anything. Erasing the row stops the card being edited; the DM itself is the member's own copy in their own inbox, which they can delete, and which the bot does not reach into — consistent with every other DM the bot has sent them | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; guild_id on the row ✓. The stored ids are Discord snowflakes, so any dashboard surface would need the string treatment — there is none today |
| messages, message_mentions, message_reactions, message_sentiment | Message archive | **metadata only**: author, channel, timestamps, reply target, `media_kind`, `sentiment`/`emotion`, @-mention edges, reaction counts, plus `deleted_at`/`deleted_source` — *when* a message was deleted from Discord and whether it was a plain deletion or an auto-delete sweep. **No text.** Since 2026-09-13 nothing writes message text, attachment URLs or embeds, and migration 228 dropped `messages.content` (600,799 rows held text, 61.8 MB) and the `message_attachments` (45,433) and `message_embeds` (84,276) tables under `secure_delete` (docs/plans/remove-message-storage.md) | **permanent** — the row and every derivation. The 365-day content sweep and its `message_retention_enabled` dial went with the text (the dial never ran in any guild) | YES (keep_messages flag) — the deletion columns are part of the row and go with it | — | Backups taken before migration 228 still hold the text until they are deleted, and until a manual `VACUUM` the database file's older free space still holds fragments (2 of 200 sampled texts in the dry run). Deletion marking added `155_messages_deleted.sql`: this records that a deletion happened so mod surfaces stop offering a dead deep link. **A member's privacy-panel purge is deliberately not distinguished** — it lands as `discord` like any other deletion, so the archive does not record that a member exercised a privacy control. Exported via the existing `author_id` in `SUBJECT_ID_COLUMNS`; no new table |
| processed_messages, known_users | Message archive | user_id dedup/roster | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | YES | — | |
| member_xp, xp_events, xp_daily, xp_reaction_awards, voice_sessions, member_activity, member_events, invite_edges | XP/activity | activity timestamps, voice presence | Mixed. `member_xp`, `voice_sessions`, `member_activity`, `invite_edges`: **undecided** — kept indefinitely by default. `xp_daily`: **permanent — by design** (its own row below). **`xp_events`: permanent — decided 2026-09-16** (Billy: *"Xp and related metrics are anonymized but retained"*); a departure anonymises the rows rather than deleting them. The 90-day rollup-and-prune dial (XP settings → Event Retention) that once sat beside it was never switched on and was **removed 2026-09-30**, so nothing ages these rows out. **`member_events`: permanent — it is the join/leave record.** Its reader (`reports_data.get_greeter_log_sessions`) takes whatever range the caller asks for with **no retention floor**, so any period here would silently cut a report whose range reaches past it. (The original reason was Rules Watch's tenure signal, which went with that feature on 2026-09-13.) Removed from the sweep 2026-09-05 after the ship review; 1,497 rows, so keeping it costs nothing — | YES | — | ~~xp_reaction_awards MISSING from purge~~ **added `6ac71558`** (`privacy/service.py`, simple-table list). `xp_events` retention was added 2026-08-26 as an opt-in dial and removed 2026-09-30 without ever having run: it contradicted the 2026-09-16 permanent decision above. Erasure clears `xp_events` and the stale `xp_daily` alike |
| usage_events | Telemetry | command/panel usage per user | **permanent — decided**: no pruning, by design; the spec discloses it | YES | — | spec discloses |
| ping_events | Ping Response report | **the pinger, not the pinged**: `author_id` of whoever sent a role ping, plus channel, role ids, `@everyone` flag and timestamp. No content, and **no responder rows at all** — turnout is counted at read time from `messages` (posters only; reactions stopped counting when `reaction_log` was dropped, 2026-09-13) and only ever surfaces as a number | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice. A provisional 180-day sweep exists but ships OFF (`retention_service`, 2026-09-05; dial `behavioural_retention_enabled`), is not enabled in any guild, and is now the only table the sweep covers. The 180 was once shown to truncate the Connection Graph replay; that reader went with the social graph (migration 229), but nobody has re-decided the number against this table. Re-decide before enabling | **YES — purge** (migration 198; `privacy/service.py`, delete by `guild_id`+`author_id`). No Art 17(3) ground worth claiming: it is analytics about how a ping landed, and "we wanted the numbers" is not a ground | — | Was undecided until 2026-09-05. No report reads ping history beyond 90 days. `author_id` in `SUBJECT_ID_COLUMNS` ✓ so the export sees it. `role_ids` is a JSON list of **role** ids, not member ids, so it is not a `LIST_VALUED_MEMBER_COLUMNS` blind spot. Deliberately stores no per-responder rows — "who personally answered which ping" was scoped out as a heavier footprint than the question needed. Disclosed in manual.html §Your Data & Privacy |
| anon_audit_log | Anon features | deanonymizing actor↔target map | 90d sweep | YES — **DECIDED** | — | ✓ |
| wellness_* (12 tables) | Wellness | usage caps, streaks, blackouts | 30d post-optout GC (wired ✓) | YES | local Ollama only | model citizen; §8 reconciled; partners table dropped by migration 189 (never used) |
| whispers, whisper_replies, whisper_guesses, whisper_reports, whisper_reply_reports | Whisper | anon message content + **sender identity**, report reasons | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purged**, and **Member-erasable**: since 2026-09-17 an operator erasure runs the same engine the member's own button does, two callers of (`whisper_repo.forget_member`): the member's own **My Data** → **Whisper history** button, and `purge_user_data` (`privacy/service.py`, whispers block). Until 2026-09-17 only `/whisper forget-me` could reach it and an operator erasure took no whisper at all — bug #222 item 3. **Preserved, Art 17(3)(e) rights of others:** a report *this* member filed on somebody else's whisper, and a guess row recording that someone guessed them on a third party's whisper. Both are rows about another member's message and the mod decision behind it, so they are not this member's to erase — **DECIDED** | — | The 30-day age-lock is a **visibility** rule, not a deletion — nothing ages out, so no duration belongs in the Retention cell; a reader extracting one would tell a member their whispers are deleted after 30 days. forget-me's cross-guild over-delete (whisper A1) was fixed 2026-08-05 (`2b3094aa`); reporter/guessed rows survive by decision, not omission (was whisper D1); dashboard audit exposes ids not content ✓; `whispers.exposed` column retained but unread/unwritten since 2026-09 (Expose was dead code — see whisper_spec) |
| confession_threads, confession_emoji_assignments, confession_rate_limits | Confessions | deanonymizing author ids; `confession_emoji_assignments` is the user → pseudonym map per root message (a confession thread **or an Anonymous Truth or Dare prompt**, which reuses the machinery) | **14-day TTL** for threads (7 days until 2026-09-12, raised so reply DMs keep working for a fortnight; the TTL is the end of service — a reply to an older confession notifies nobody) **and, since migration 211 (2026-09-04), for alias assignments** — `created_at` stamped on assign, swept by the same `purge_old_thread_posts`; 211 backfills every pre-existing row from its thread's own `created_at` (or the migration moment where no thread row remains), and the sweep never takes a row still at 0, so no alias on a live thread or prompt is lost across the restart; FFA also clears its prompts' rows the moment the host closes the game. Before that the alias map had no clock and 445 thread-less rows had outlived their threads in prod (anon-tail-69). `confession_rate_limits` **joined the same 14-day sweep 2026-09-17** (bug #222 item 4): keyed per member rather than per thread, it was the one confessions row with no clock at all, and `last_confess_at` is a timestamp saying this member posted an anonymous confession at that moment. Nothing is lost — the cooldown is seconds and the daily cap resets daily, so a fortnight-old row can only ever permit the next post | **Partial** — `confession_emoji_assignments` **purged** (`privacy/service.py`, confessions block: it is the one row tying a still-visible pseudonym to the member); `confession_threads` left to its TTL (routing metadata, and in `THIRD_PARTY_TABLES`); `confession_rate_limits` **purged** 2026-09-17 (bug #222 item 4): "operational, no content" was an engineering reason, not an Art 17(3) ground, and the row's timestamps are precisely what correlates an erased member against a confession still visible in the channel — **DECIDED** | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓ so export sees the alias rows |
| confession_pending | Confessions | **confession body + author_id** — the only place the bot stores a confession's text itself (`anon_audit_log` stores none and joins `messages`); written only while mod-approve mode holds a submission | deleted on approve, on reject, and by a **7-day sweep** (migration 200). No rejected-confession archive of any kind | **YES — purge** (`privacy/service.py`, confessions block). Deleted outright rather than left to the TTL its sibling `confession_threads` relies on: the row is queued *to be published*, so erasing a member and then posting what they wrote — because a mod reached the queue before the sweep — is not defensible | — | `author_id` in `SUBJECT_ID_COLUMNS` ✓ so export sees it; guild_id present ✓. Not in `THIRD_PARTY_TABLES`: the row names one member and no second party. Seven days was inherited from the manual.html promise that the confession↔author link self-destructs in a week. When the posted-confession TTL rose to 14 days (2026-09-12) this one stayed at 7 on purpose: a pending row holds the confession *text* beside the author, which the routing rows never do |
| needle_reply_optins | Needle (Auto-Thread) | members who switched **Replies to Your Threads** on in Roles & Pings (`user_id`, `created_at`) — a preference, no content (migration 244, 2026-09-28; replaced `needle_reply_mutes`, the per-thread opt-out of migration 223, dropped with no rows in prod) | until the member turns it off (Roles & Pings, or the button on a reply DM) — Off deletes the row | **YES — purge** (`privacy/service.py`, next to the confessions block). Nothing else relies on it; a returning member gets no reply DMs until they opt in again. Not a My Data button: turning it off *is* the delete, and it stays with its feature (CLAUDE.md: a feature off switch) | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; guild_id present ✓. Not in `THIRD_PARTY_TABLES`: one member per row |
| dm_batch_pending | Batched DMs | a notification waiting to go out as one DM (kinds in `dm_batching_spec.md`: Needle replies, confession replies, AMA answers, Whisper guesses, economy notes): the recipient (`user_id`), the member whose action it reports (`actor_id` — for a Whisper guess, the member the guess **named**; for an anonymous confession reply, 0, so no row links an anonymous replier to the reply), the message id it is about (`ref_id`), a small JSON payload, and `created_at`. The payload holds channel ids, plus a Whisper guess's outcome (right/wrong, guesses left). An `ama_answer` row ties the anonymous asker (`user_id`) to their answered card (`ref_id`) and the hot seat (`actor_id`) — the same link the games tables already hold for an AMA (row `games_*`), kept here only until the batch is sent. **No message content**, with one exception: an economy note's payload holds its rendered line, which for `/bank pay` includes the member's memo — already stored on both `econ_ledger` rows, so the copy adds nothing that isn't kept anyway (migration 243, 2026-09-28) | **until its batch is sent** — ten minutes after the first row of the batch, longer only across a restart; deleting the `ref_id` message, or the thread, deletes the row first | **YES — purge** (`privacy/service.py`, after `needle_reply_optins`), on either side: an erased member is neither told of replies nor announced as a replier | — | `user_id` and `actor_id` both in `SUBJECT_ID_COLUMNS` ✓; guild_id present ✓. In `THIRD_PARTY_TABLES`: a recipient's row names the replier |
| bios, bio_answers, bio_field_values | Bios | self-disclosed profile text | self-service delete ✓; archived-on-leave **12-month TTL** (`9374c306`, migration 149) | YES — **added `6ac71558`** (`privacy/service.py`, simple-table list) | — | both G2 and G3 closed |
| mahjong_seats | Meadow Mahjong | who sits at which live/past table (`table_id`, `seat_index`, `live`) — normalized out of the state JSON precisely so purge/export can see seating. **Negative `user_id`s are house bots** (plans/mahjong-bots.md B3/B9), not members — no data subject exists for them | until purged | **YES — purge** (migration 175; `privacy/service.py` mahjong block). A live seat first dissolves its whole table — status `purged`, every seat freed, all held escrow refunded through `econ_game_wagers` — because a hand can't continue around a hole; then the member's rows delete. No Art 17(3) ground: the coins that moved live on in the preserved `econ_ledger` | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; guild_id denormalized ✓ |
| mahjong_tables | Meadow Mahjong | serialized engine state of each table (racks, exposures, discards) + `host_id`, sticky message id, `nudges` (ids of the turn pings still awaiting a sweep, cleared when swept or when the table closes) | closed rows kept as game history | **Partial — dissolve + close on purge, then scrub the closed row** (see `mahjong_seats`): the purged member's live table closes and refunds. The closed row itself **cannot** be deleted — preserved `mahjong_results` game history points at it by foreign key, so deleting it would take the other players' hand records with it — so since 2026-09-17 (bug #222 item 6) its `state` snapshot is blanked to `{}` and a `host_id` naming the member is zeroed. That empties the documented list-column blind spot rather than declaring it out of reach: the snapshot named seat member ids and embedded other members' concealed racks. Nothing reads a closed table's state back (the service refuses a member action on a non-live row; boot restore and the dashboard both select `status = 'live'`), so the shell of the row is all the foreign key needs. Still flagged in `THIRD_PARTY_TABLES` so an access export marks a **live** table review-required — **DECIDED** | — | `host_id` in `SUBJECT_ID_COLUMNS` ✓; in `THIRD_PARTY_TABLES` ✓ |
| mahjong_results | Meadow Mahjong | one row per settled hand: winner, line, how won, stake, and (since migration 181) when the hand was dealt and how many turns it ran — hand properties, not per-member ones. Since migration 209 a **practice** hand (member vs house bots) also writes this one row, flagged `practice = 1`, for hand-timing telemetry only — it writes no `mahjong_result_seats`, no `mahjong_stats`, no coins and no `games_game_history` row; its `winner_id` (the member or a negative bot id) is anonymised on purge like any other | **permanent — group game history**: a hand's record belongs to every player at the table, so one seat's erasure cannot delete it | **Anonymised, not deleted**: `winner_id` → NULL on purge. The row also carries the other seats' hand history through `mahjong_result_seats`, so deleting it would erase their records with the requester's | — | `winner_id` in `SUBJECT_ID_COLUMNS` ✓ |
| mahjong_result_seats | Meadow Mahjong | per-seat coin/point deltas + fallow flag of each settled hand (negative `user_id` = house bot, no data subject) | **permanent — group game history** (see `mahjong_results`) | **YES — purge** (member's own rows delete; other seats' rows survive on the anonymised result). No Art 17(3) ground — money history is `econ_ledger`'s job | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; guild_id denormalized ✓ |
| mahjong_stats | Meadow Mahjong | per-member aggregates (hands, wins, coins won/lost, biggest win) per mode | until purged | **YES — purge**. Pure derived aggregates; nothing reads them but the dashboard report | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓ |
| mahjong_prefs | Meadow Mahjong | the member's chosen assistance level (`off`/`target`/`gap`/`coach`) from the /mahjong My Settings menu (migration 176; plans/mahjong-assist.md) | until purged | **YES — purge** (`privacy/service.py` mahjong block). A preference tied to a member id has no Art 17(3) ground to survive an erasure | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; guild_id denormalized ✓ |
| pen_pals_pool | Pen Pals | who is waiting to be matched right now; no content | live queue — a row lasts until the member is matched or leaves the pool | **YES — purge** 2026-09-17 (bug #222 item 5). A live queue, not a record: there is no Art 17(3) ground to hold a place in a matching queue against an erasure request, and leaving it was a defect as much as a register gap — the matcher would pair an erased member and open a private channel for an account that is not supposed to exist — **DECIDED** | — | ✓ |
| pen_pals_sessions | Pen Pals | pairing metadata ONLY (no letter content ✓) — the two member ids, the channel, the clock | kept as the all-time no-repeat memory (`_past_partners`); a pairing is one-per-lifetime | **Partial — close, then scrub the seat** 2026-09-17 (bug #222 item 5). A live session is first closed with `close_reason = 'purged'`, because the row cannot simply be deleted: the orphan-channel sweep on boot keys on a *closed* session, so a deleted row leaves the private channel standing with nothing left that knows to remove it. Then the member's id is zeroed in whichever seat they held and the row stands, because it is also the **other** member's no-repeat memory — **Art 17(3)(e) rights of others**, applied to the column rather than the row (the `mahjong_results.winner_id` / `survivor_hosts.added_by` precedent). Consequence, accepted: a returning member is matchable with their old pen pal again, which is correct — an erased member has no history to repeat; and the surviving member is not re-pooled, since an erasure runs out-of-band with no bot in hand (one Join press restores them) — **DECIDED** | — | ✓ |
| pen_pals_blocks | Pen Pals | protective blocks, member- or admin-placed; no content | until the member clears their own, or an admin clears a separation | **Partial — split on who the record protects** 2026-09-17 (bug #222 item 5). The member's own `source = 'member'` blocks are **purged**: their preference, with no ground to outlive them (the `pen_pals_optouts` precedent). Everything else is **preserved on Art 17(3)(e)** — a block somebody else placed *against* them, and an admin separation in either direction, are protective records about another member or a moderator decision, and an erasure must not be a way to clear one. The `voice_master_blocked` ground; see `docs/specs/no_contact_spec.md` for the same rule stated for contact — **DECIDED** | — | ✓ |
| pen_pals_pool_events | Pen Pals | who joined/left the matching pool and why (`join`/`leave`/`skip` + reason); no content | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge** (migration 160). Operational history only: nothing downstream reads it, the no-repeat matchmaking memory lives in `pen_pals_sessions`, and the protective blocks live in `pen_pals_blocks` — so there is no Art 17(3) ground to hold it against an erasure request | — | `user_id` already in `SUBJECT_ID_COLUMNS`, so export sees it ✓ |
| pen_pals_optouts | Pen Pals | members who asked not to be matched (`guild_id`, `user_id`, `at`); no content | until the member rejoins (they clear it themselves by joining) | **YES — purge** (migration 174, decision confirmed by Billy 2026-08-20). A bare preference — the state behind a Leave Pool press — with no Art 17(3) ground to outlive the member's own erasure request; only the two requeue paths and `_eligible_pool` read it. Known consequence, accepted: `pen_pals_pool` and `pen_pals_sessions` are preserved (row above), so an erased member can still be re-pooled when a preserved session of theirs expires — one Leave press restores the flag, and erasure is an out-of-band operator act, not a member command | — | `user_id` already in `SUBJECT_ID_COLUMNS`, so export sees it ✓; note the **preserved** `pen_pals_sessions` (row above) means an erasure mid-session can still see that session expire and re-pool the member — pre-existing, tracked separately |
| survivor_players | Survivor | season entry: alive/ghost status, strikes, join time, week of elimination | **permanent — group game history**: archived seasons stay queryable, and a season with one player removed no longer reconciles | **YES — purge** (migration 167; `privacy/service.py` scopes through `survivor_seasons` because the tables key on `season_id`, not `guild_id`). A game record with no Art 17(3) ground: the money already moved lives in the preserved `econ_ledger`, so erasing the entry corrupts nothing | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; **guild_id denormalized onto the row** (stage-4 review) so the access export's standard guild scoping covers it — no cross-guild disclosure; boards simply lose the name |
| survivor_picks | Survivor | weekly NFL team picks + result per week/slot; **secret until the weekly Reckoning reveal** (bot never shows a live pick to anyone but admins) | **permanent — group game history** (see `survivor_players`) | **YES — purge** (same statement pair as `survivor_players`). Erasure mid-season removes the member from the game entirely — acceptable: the gauntlet replay reads only `nfl_games`, never other players' picks, so determinism for everyone else is untouched | — | guild_id denormalized like `survivor_players` — export guild-scopes ✓; secrecy is a *display* rule, not storage: admins can see live picks via mod-log by design (spec §1.2) |
| survivor_hosts | Survivor | `user_id` of a member an admin named as Survivor host, `added_by` admin id, `added_at` (migration 221) — an access grant: the Survivor dashboard panel plus the host DMs | **until removed** — an admin removes the host on the panel | **YES — purge** the member's own host entry: an access grant has no Art 17(3) ground, and erasing it only revokes the panel and the DMs (a Reckoning week with no host left falls back to the bot's own post). A host entry the erased member merely *added* survives with `added_by` zeroed — the grant belongs to the host, and nothing reads `added_by` back | — | `user_id` and `added_by` both in `SUBJECT_ID_COLUMNS` ✓; guild-scoped. Host DMs name players (pick status, Reckoning data) — the same view the admin panel gives, so it is not a no-contact surface: nothing is delivered to the named member |
| dm_audit_log, dm_consent_pairs, dm_requests | DM perms | consent records + transition audit | `dm_requests` live map: pruned on revoke. `dm_audit_log`, `dm_consent_pairs`: **permanent — Art 17(3)(e)** (legal claims — the record exists precisely to answer "did they agree", so erasing it would destroy the evidence it was created to hold) | **DECIDED** — forensic preserve (privacy_spec:111) | — | ✓ |
| member_birthdays | Birthday | **month/day only, no year** ✓ + visibility pref | until removed | YES — **added `6ac71558`** (`privacy/service.py`, simple-table list) | — | minimized by design |
| jails, warnings, tickets, ticket_participants, policy_tickets, role_grant audit | Mod actions | sanction history, mod-authored reasons (transcripts are their own row below) | **permanent — Art 17(3)(e)** (legal claims — the canonical record if a moderation decision is challenged) | **DECIDED — preserve** (canonical mod record). One deliberate hole: an **admin** can erase a single `warnings` row from the dashboard (`DELETE /api/moderation/warnings/{id}`) — a mod-correction tool for a warning issued against the wrong member, not an erasure path a member can invoke. The `warning_delete` audit entry retains the deleted row's member, reason, issuer and timestamp, so the deletion is itself part of the preserved record | — | ✓ |
| transcripts | Mod actions | the full message history of a closed ticket, released jail, closed policy ticket, or a jail channel deleted because its member left (2026-09-16): every message's text, `author_id`, `author_name`, timestamp, embeds and attachment URLs, as one JSON blob per save, keyed by `record_type` + `record_id`. One record can hold several saves (a jail archived on departure and again on release). Quotes **everyone** in the channel, mods included | **permanent — Art 17(3)(e)** (legal claims — the appeal or ticket conversation is the substance of the moderation record it belongs to) | **DECIDED — preserve** (Billy, 2026-09-16), alongside the mod record above. Not reached by `purge_user_data` or departure auto-anonymise; a jailed or ticketed leaver is held there anyway | — | **Export blind spot:** the row has no member column — members are named only inside the JSON (`messages[].author_id`) and through the parent `jails`/`tickets` row — so neither `SUBJECT_ID_COLUMNS` nor `LIST_VALUED_MEMBER_COLUMNS` finds it and an access export does **not** include transcripts. Answer an access request for them by hand: find the member's jail/ticket ids, read the transcripts on the dashboard. Also a third-party disclosure risk if exported whole — it quotes other members |
| policy_ballots | Policy Tickets | one community ballot on a policy proposal (migration 202): the question an admin typed, which channel it was launched in, its thread and card ids, the window it ran for, and the yes/no/abstain counts + outcome **frozen at close**. It names no voter — `opened_by` and `closed_by` (NULL when the deadline sweep closed it) are the only member ids on the row | **permanent — record of a decision the server made**; the closed result names nobody | **DECIDED — preserve**, Art 17(3)(e) legal claims. The same ground `policy_tickets` is preserved on (row above), for the same reason: the row is the record of a governance decision and who called it, not a record about a member. The frozen counts are aggregates naming nobody | — | `opened_by` added to `SUBJECT_ID_COLUMNS` 2026-09-03 (`closed_by` was already there), so an access export sees an admin's own ballots; `guild_id` on the row ✓ |
| policy_ballot_votes | Policy Tickets | how one member voted in one community ballot (`yes`/`no`/`abstain`, one row per member per ballot, changeable until close). **Attributed and fully public** — the tally card in the thread names every voter, which is Billy's 2026-09-03 decision and is why the member-facing copy must never call a ballot anonymous | **permanent — record of a decision the server made** (votes are non-anonymous by design; the ballot card names the voter while it runs) | **YES — purge**. An activity record with no Art 17(3) ground; the same call `policy_votes` gets (row below). Safe because the decision it fed is preserved in aggregate on `policy_ballots`: erasing a vote from a **closed** ballot cannot move a result that was frozen at close. Erasing from an **open** one does remove it from the live tally, which is correct — an erasure is an out-of-band operator act and the member is no longer a participant | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; `guild_id` denormalized onto the row so no parent join is needed (unlike `policy_votes`, which `privacy_service` has to scope through `policy_tickets`). Not in `THIRD_PARTY_TABLES`: a ballot vote discloses nothing about anyone else |
| grant_role_permissions | Role Grants | the *Who Can Hand This Out* allow-list per grant: `entity_type` 'user' rows name a **member** in `entity_id` (role rows name a role and are not personal data) | until the admin removes the entry on the Role Grants panel | **YES — purge** 2026-09-17 (bug #222 item 1), `entity_type = 'user'` rows only. An entry is an **authority the member holds**, not a record about them that anyone relies on afterwards, so there is no Art 17(3) ground to keep the name: erasing it revokes a keeper's ability to hand the role out, which is the identical effect to an admin removing the row on the Role Grants panel. It does not sit with the mod record kept above — that is the *history* of a moderation act, which (e) reaches; this is a standing permission, which it does not. Matched on the type as well as the value, because `entity_id` holds a **role** id for a role row and the two numeric spaces overlap; role rows are not personal data and are untouched — **DECIDED** | — | `entity_id` added to `SUBJECT_ID_COLUMNS` 2026-08-29, so an access export now sees it. **Legacy `give_role_permissions` — DROPPED (migration 193, 2026-08-30).** The pre-`grant_role_permissions` shape (`guild_id`, `target_role_id`, `entity_type`, `entity_id`), created by application code that no longer exists. It held 2 prod rows naming 2 members, had no reader or writer anywhere in `src/`, and was created by no migration; the rows were a silent no-op invisible to the Role Grants panel, so an erasure or access request could not meaningfully act on them. Dropped rather than wired up — the live allow-list is `grant_role_permissions` |
| watched_users | Mod watch | watcher↔watched pairs (2 rows) | until unwatched | YES — **added `6ac71558`, both sides** (`privacy/service.py`, pair tables) | LAN LLM screening | covert moderation, mods-only ✓ |
| no_contact_pairs, no_contact_events | Mod safety | protective orders + attempt log (no text) | **permanent — third-party protection**: the order exists to protect the *other* member, so the subject's erasure cannot lift it | **DECIDED** — deliberate preserve; add spec line (dmperms-nocontact G1) | — | ✓ |
| guess_consents | Guess | consent evidence: when, and which disclosure version (version 0 = a legacy role holder who never saw the disclosure: a recorded gap, not consent; written only by `scripts/backfill_guess_legacy_consents.py`, owner-run) | kept through optout (`withdrawn_at` stamped); cleared by full erasure | YES — `privacy/service.py` simple-table list | — | Art 7(1) evidence, migration 154 |
| guess_rounds, guess_guesses, guess_audit_log | Guess | intimate images on disk until solve (the `guess_cache/` originals the rounds point at — `var/guess_cache/` once the runtime data moves, `disaster_recovery_runbook.md` § Runtime data layout) and the crop posted in the Guess channel; anon confession text (`guess_rounds.confession_text`) + submitter id | Mixed. Originals of unsolved rounds: **7d age-out** (`775d903d` shipped 90d; cut to 7 on 2026-09-15), and deleted — solved or not — when the departure auto-anonymise runs for the submitter, deleted when a round **expires** unsolved (`guess_round_lifetime_days`, off by default; the card is deleted too, nothing is revealed, and a failed card delete is retried by the next daily sweep via `expiry_cleared_at`), and at once when the member leaves the server (`games/guess/cleanup_service.withdraw_departed_member`, which also flags their open rounds unsolvable and stamps `guess_consents` withdrawn). Since 2026-09-17 an **orphan sweep** (`guess_cleanup_service`) also deletes any cached original no round points at, which is the only thing that reaches a file whose row was removed out of band; a round whose channel post fails is rolled back, file first. The rows themselves: **until an erasure**, and otherwise indefinitely | **YES — purge, round whole** (decided 2026-09-20), and **Member-erasable** since 2026-09-27: one engine, `guess_repo.forget_member`, and every caller runs it: the member's own **My Data** → **Guess Who data** button, `purge_user_data`, and, since the same date, both self-running anonymisations (the departure job and My Data → **Delete everything**, through `departure_erasure_service.anonymise_member`), which delete Guess records rather than rewrite them to the surrogate. All of them take the round's **channel post** down and unlink its original first (`guess_cleanup_service.clear_member_media`; Billy: *"Deleting the message should clear everything from guess"*). The operator Anonymise tab still rewrites Guess rows to the surrogate. The button leaves `guess_consents` alone — that row is the consent evidence, and only a full erasure clears it. A round the member submitted **or is the answer to** is deleted entire, taking the `guess_guesses` and `guess_audit_log` rows hanging off it — other players' guesses included. The round *is* the member's submission (in prod every round's submitter is also its answer), so nothing coherent is left once the image and `confession_text` go; the Risky Rolls and Hot Potato ground, and the accepted cost is the same — other members lose a play record. Also deleted: guesses they made on rounds that survive, guesses anyone made **naming** them, and their `guess_audit_log` actor rows. **Preserved:** their id as `solver_id` on somebody else's round, which is scrubbed to NULL rather than deleting that member's round (Art 17(3)(e), the `pen_pals_sessions` seat-scrub ground); the solved-round leaderboard already filters `solver_id IS NOT NULL`. The on-disk original and the channel post are removed by the **caller** before the rows go (the route, `web_server/routes/privacy.py`, or the My Data button) — neither can be rolled back inside the purge's transaction, and once the round is deleted nothing knows which post was theirs and only the orphan sweep could ever find the file. Originals are read by submitter **or answer** since 2026-09-27, matching the round set; before that a round naming the member as its answer had its row erased while its file waited for the sweep. Until this decision **no `guess_*` table but `guess_consents` was purged at all**, which the cell recorded as "STILL NO" from 2026-08-06 to 2026-09-20 | — (local) | consent view + `/guess optout` shipped `775d903d` (U1/G1/G2 closed). **Purge coverage closed 2026-09-20.** Still open: the confession-text TTL that was only ever a "consider". Four columns fewer since migration 184: `allow_reuse`, `is_reuse`, `original_round_id`, `reuse_blocked` were the cut round-reuse feature's ballast (all 408 prod rounds held 0/0/NULL/0), so a round now records nothing about being replayed |
| casino_* (bets, daily, weekly, member_stats, hands incl. **mines**, **rounds**) | Casino | wagering history (behavioral) | **permanent — game history and money record**, tied to the preserved `econ_ledger` that settles it | YES — folded into `econ_purge_user` (`6ac71558`); the five `*_rounds` tables added there with migration 158, when a round stopped belonging to the channel and started naming the player who opened it, and `casino_mines_hands` with migration 164 (2026-08-16) — the hand-maintained list is why a new per-member casino table is invisible to erasure until someone adds it. `casino_pools_rounds` stays out: the daily market has no owner, and its per-member data is all in `casino_pools_bets` | — | money code = repo's best |
| risky_active_rounds, risky_round_rolls | Risky Rolls | who opened a round, who rolled and what they rolled, and the resolved seats (`highest_user`, `lowest_user`, `second_*`) | deleted on round close; an abandoned round lives until `/risky reset_state` or the 120-min auto-close. Since 2026-09-04 a resolved round's summary (opener, who rolled what, the resolved seats) is first copied into `games_game_history` — the `games_*` row below, purged whole with the rest of the games record since 2026-09-14 | **YES — purge** (2026-08-20, `privacy/service.py` Risky Rolls step). Dice in a party game: nothing reads a closed round back and no Art 17(3) ground holds one. Rows go whole, and `risky_round_rolls` is deleted by resolved `game_id` first because the purge's connection makes no `PRAGMA foreign_keys` promise — the FK cascade the feature's own store relies on cannot be assumed here | — | Seat columns are all in `SUBJECT_ID_COLUMNS`, so export sees them ✓. `reroll_user_ids` survives in the schema but nothing can write it (`c53cb754`) |
| risky_pending_questions | Risky Rolls | the round's winner, the second questioner on a 1, and CSV lists of participants / tied players / who has already asked | 7 days, swept on bot startup (migration 173) | **YES — purge** (2026-08-20). Same grounds as the rounds above; the CSV columns are matched by exact membership (`',' \|\| col \|\| ','` against `',id,'`), not a bare `LIKE '%id%'`, which would also match 1234 for 123 | — | All four CSV columns are in `LIST_VALUED_MEMBER_COLUMNS`, so the export discloses the blind spot ✓. Was **indefinite** until migration 173 (2026-08-20) gave the table a `created_at` and the 7-day sweep the posted questions already had — 58 rows had accumulated in prod against 2 live rounds. Pre-migration rows carry a NULL age and are swept on the first startup after deploy: a NULL predates the column, so it predates the release. `chased_at` (migration 210, 2026-09-04) is a timestamp, not a member column: when the one "your question is still waiting" re-ping went out, kept so a restart cannot send it twice; `fallback_attempts` / `fallback_attempted_at` (migration 213, 2026-09-05) are a counter and a timestamp on the same footing — how often the deck has failed to draw a question for the prompt, so it can back off and give up |
| risky_posted_questions | Risky Rolls | `asker_id`, CSV of allowed repliers, and **`question_text` — the member's own words**, posted publicly in-channel | 7 days, swept on bot startup (`risky_roll_cog`) | **YES — purge** (2026-08-20). A question the member wrote goes with them; one they were only a recipient of names them in `allowed_replier_ids` and goes too | — | `asker_id` in `SUBJECT_ID_COLUMNS` ✓; `allowed_replier_ids` newly in `LIST_VALUED_MEMBER_COLUMNS`. Note this is **stored member-authored content**, unlike the message pipeline where content is off by default — the reply text is not stored, only the question. Migration 210 (2026-09-04) adds `chased_at` (timestamp of the one answerer re-ping) and `from_bank` (the fallback dial drew this question from the Truth or Dare bank for a winner who never asked — so `question_text` on such a row is **bank** text, not the member's own words; `asker_id` still names the winner it was posted for) |
| games_*, duels, hot_potato_*, pressure_*, quickdraw_*, mc_*, chicken_* | Games | participation, winners, nicks; anon games via anon_audit_log 90d | **permanent — group game history** | **YES — purge, whole record** (decided 2026-09-14, review H1). The anonymous games are covered by the `anon_audit_log` 90-day sweep; `duel_cooldowns`, `duel_group_cooldowns`, `duel_nicks` and `hp_group_games` have their own row below. `hot_potato_games`, `pressure_games`, `quickdraw_games`, `mc_games`, `chicken_games`, `games_active_games` and `games_game_history` are deleted **whole** when the member sits in any seat column or appears anywhere in the row's JSON (`privacy_service._delete_game_records`: a digit-bounded match, so roster entries, a nested `holder_id`/`player_id`, an AMA `asker_id`/`hot_seat_id` and a `scores`/`rolls` key all count). No Art 17(3) ground holds a party game's record, and one with a hole in it is incoherent — the group Hot Potato call. Accepted cost: a deleted `games_game_history` row lowers that guild's games-played and unique-player figures. **`guild_id = 0` rows are reached too** for the two shared-record tables — 27 history rows archived before migration 204 carry no guild, and the match is on the member's own id, so they are that member's data whichever guild they came from. `hot_potato_style` (a member's style score) is deleted per member. `pressure_nicks` and `pressure_cooldowns`, dead since migration 032 and read by nothing, are **dropped** by migration 232 | — | Before 2026-09-14 this cell said per-user rows fold into `econ_purge_user`; they never did, and none of these tables was named in any purge statement. Production rows on 2026-09-14: `games_game_history` 286, `pressure_games` 74, `hot_potato_games` 29, `hot_potato_style` 12, `quickdraw_games` 9, `mc_games` 7, `chicken_games` 7, `games_active_games` 0; dropped by 232: `pressure_nicks` 1, `pressure_cooldowns` 4. The JSON columns are in `LIST_VALUED_MEMBER_COLUMNS`, so the export discloses them and the member report scans them by value. Seat columns are all in `SUBJECT_ID_COLUMNS` ✓ |
| voice_master_profiles, voice_master_trusted | Voice Master | member room prefs + trust lists | until changed | YES — **added `6ac71558`** (`privacy/service.py`, simple-table list for profiles and pair tables for trusted — both sides) | — | |
| games_external_messages | External games | **content + embeds_json** parse buffer — another bot's game-result posts, banked raw so a format change can be re-parsed; the only member data is the mentions/usernames those bots print | **30 days, enforced daily**: `GamesExternalCog._buffer_sweep_loop` (24h `tasks.loop`) calls `games/external/logic.py` `sweep_old_buffer_rows`, which deletes rows whose `collected_at` is older than `PARSE_BUFFER_RETENTION_DAYS` (30) | — (buffer, not a purge target; payouts are booked into `econ_ledger` at parse time and the row has no read-back use) | — | Steady-state churn, not growth: ~18,300 rows on 2026-09-04 with the oldest `collected_at` 2026-08-05 — the sweep has been biting since the 08-06 retention ship (`49a02867`). The 2026-09-02 review brief's "still growing" was this churn (photo-external-111) |
| intake_cards, intake_card_steps | Intake | newcomer progress, done_by greeter ids | **until an erasure**, and otherwise indefinitely (112 cards / 462 steps on 2026-09-20) | **SPLIT — decided 2026-09-20.** The member's **own card** and its steps: **YES — purge**. The card is a record about them — when they arrived, who greeted them, how far the checklist got — and no Art 17(3) ground holds a welcome checklist. `intake_card_steps` has no `guild_id` and reaches its card by id, so the card ids are resolved first: the purge's connection makes no `PRAGMA foreign_keys` promise, the same reason `risky_round_rolls` is deleted by resolved id. Their id as a **greeter on another member's card** (`intake_cards.resolved_by`, `intake_cards.opened_by` — the Level 5 approver who opened a Stage 2 card, migration 245 — and `intake_card_steps.done_by`): **preserved**, Art 17(3)(e) — that is the other member's welcome record and the audit of who admitted them, the `mahjong_results.winner_id` precedent. `intake_cards.stage` (migration 245, 2026-09-28) marks a card as the join pass or the Level 5 pass; it adds no per-user data, and a member's Stage 2 card is their own card, purged alike | — | intake-confessions G2 |
| greeting_watch | Greeting watch | ids+timestamp ONLY, no text (by design ✓) | **30d GC on verdicted rows** (`49a02867`, `onboarding/greeting_watch/service.py`, `gc_resolved_watches`) | — | — | G1 closed. 352 resolved rows still present — the GC runs off the 60s loop, so it clears on the next bot restart |
| audit_log, incident_events, role_events, role_prune_events | Mod/audit misc | actor/target ids | **permanent — Art 17(3)(e)** (legal claims). Retention was literally `?` here until 2026-09-05: nobody had ever filled it in, and the Notes cell was blank, so three of the four tables were preserved with no ground named — the one thing this register's own contract forbids | role_events YES, others **NO** | — | 18,835 live rows (audit_log 1,751; incident_events 572; role_events 16,467; role_prune_events 45). These are the record of *moderation and role actions taken*, the same category as jails and warnings, and are preserved on the same ground: if a decision is challenged, this is what explains it. `role_events` is purged because it records a member's own role history rather than a decision about them |
| voice_transcription_config | Voice transcription | config only — transcripts never stored | n/a | n/a | local faster-whisper | ✓ no personal data |
| mention_award_rules | Mention Awards | guild config (channel, amount, conditions JSON) + `created_by` admin id; **a `from_user` chip names a member** inside the JSON (migration 157) | until deleted by an admin | **SPLIT.** `from_user` chips: **YES** — purge strips the erased member's chips, deleting a rule left empty (`privacy/service.py`, mention_award_rules step); also in `LIST_VALUED_MEMBER_COLUMNS` so the export discloses the JSON blind spot. `created_by`: preserved, Art 17(3)(e) — the record of who opened a currency faucet, counterpart to the `econ_ledger` rows it produces | — | Awards land in `econ_ledger` (preserved, registered) and dedupe via `games_external_payouts`. **Message content is never stored**: chips are matched live off the gateway and discarded |
| music_playlist_tracks | Music Playlist | `added_by` poster id + track/title/artist + source link + message pointer (migration 165) | **permanent — by design**: rolled-off rows are the "what did we listen to in July" history | **YES — purge** (decided in the spec: no Art 17(3) ground — "who posted a song" carries no legal-claims or integrity weight, and the Spotify playlist is keyed by track id, not poster). Deleted by `music_playlist_store.purge_member_rows`, called from `privacy_service.purge_user_data` | Spotify Web API (track ids only, no member identity) | Member column is `added_by`, already in `privacy_service.SUBJECT_ID_COLUMNS`, so the access export sees the table with no code change |
| music_playlist_unmatched | Music Playlist | `added_by` poster id + posted link, extracted title, best-candidate track + score; `reviewed_by` mod id | **permanent — by design** (reviewed rows kept as queue history) | **YES — purge**, same decision and same function (called from `privacy_service.purge_user_data`): the member's rows are deleted; rows they merely *reviewed* survive with `reviewed_by` nulled (a review is a mod action, not the reviewer's personal record) | — (queue rows never leave the box; only an approval's playlist add reaches Spotify) | `added_by` deliberately chosen over the spec's `posted_by` so `SUBJECT_ID_COLUMNS` covers it for free |
| todos | Todo / Mod Chores / Bug list | `added_by` and `completed_by` Discord ids against a task the mod team shares. On the todo list (`list = 'todo'`) the task/description text is server work product, written by moderators. **On the bug list (`list = 'bug'`, migration 225) the text is member-written** once an admin opens filing to everyone: a member's own description of what went wrong, which can say anything they chose to type. `missed_at` (migration 166) names nobody — it records that a recurring chore's day passed undone. **`purchase_id` (migration 179)** points at the custom-shop-item order a row was spawned for; the task text of such a row deliberately names the ITEM ONLY ("Deliver Custom Emoji"), never the buyer, precisely so the anonymise-don't-delete decision below stays true — a name baked into the task would survive the erasure inside the half of the row that is kept. The buyer is rendered by joining `econ_shop_purchases`, which IS purged, so an erased buyer's order line reads as the same unknown member every other surface shows | **undecided** for the row — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice. **`attachment_urls` is transient:** a bug report's screenshot links are cleared by the next bug-card tick that covers the report, whether or not the images posted (`todo_service.settle_bug_screenshots`), and at most one hour after filing by the backstop (`expire_bug_screenshots`, `SCREENSHOT_HOLD_SECONDS`) | **YES — anonymised, not deleted** (`privacy/service.py:_scrub`, `added_by`→0, `completed_by`→NULL). The row is two things at once: the team's outstanding work and two ids naming a person. Deleting it to reach the ids would take real work off other people's list — a task someone else is part-way through vanishing because an unrelated member left — so the ids are cleared and the work stands. Nothing identifying survives, which is what the erasure right asks for; the blanked values are the same "unknown" every surface already renders for an unresolvable member. **Bug reports get the same treatment, and the text is kept** (decided by the owner 2026-09-12): a filed bug is a report about the bot, not about the member, and it stays on the dev queue until someone fixes it — deleting it because the reporter left would silently drop a known defect. No Art 17(3) exception is claimed for that text; the position is that once the filer id is cleared, the report no longer identifies them. **The residual gap, stated plainly:** free text the member typed can name themselves ("my wallet shows…" plus a username), and the scrub cannot see inside it. There is no delete path for a todos row today, so honouring an erasure that reaches into a bug's wording is a manual edit of the row. **Bug change cards** (off unless an admin turns on the bug board's own switch) copy a new bug's text into a message in the bug board's channel; that message lives in Discord, outside the database, and no erasure reaches it — which is why the card prints the text only and never the filer's name, so the copy carries nothing the kept row doesn't. The manual's privacy notice tells members their bug reports are kept. **Bug screenshots** (migration 241): `attachment_urls` holds the Discord upload links a filer attached, **transiently** — cleared by the next bug-card tick (posted or not) and by an hourly backstop — and the purge clears a member's held links outright (`privacy/service.py:_scrub`, `todos.attachment_urls`). The image itself is never stored by the bot; it lives only on the bug card, and `card_channel_id`/`card_message_id` (no member data) record which card, so the bug loop edits the images off it when the bug closes **or its filer is erased** (the blanked `added_by` is the signal) — the one part of a card an erasure does reach | — | **Was missing from this register entirely until 2026-08-17** — the table predates the register and no row was ever written, so it was invisible to an access or erasure request. `completed_by` was likewise absent from `SUBJECT_ID_COLUMNS`, so an export showed the tasks a member *added* and silently omitted the ones they *did*; added in the same commit. Found while building the mod chore board, which surfaces "who ticked it" into a Discord channel |
| todo_board, todo_recurring | Todo / Mod Chores | `todo_board` holds **no member data** (channel/message ids). `todo_recurring` is cadence config plus `created_by`, the member who defined the chore | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — `created_by` anonymised to 0** (`privacy/service.py:_scrub`). Not optional housekeeping: the spawner stamps `created_by` onto every `todos` row it materialises, so leaving it would write an erased member's id straight back into `todos.added_by` at the next fire, and every day after — the erasure would silently undo itself | — | listed so the feature's three tables are all accounted for rather than two of them reading as an omission |
| econ_shop_purchases | Economy — Custom shop items | one member's order of an admin-defined item: `user_id`, the item, the price snapshot at purchase, an optional free-text `note` the member typed ("engrave BILLY"), `resolver_id` naming the mod who delivered or refused it, and a `deny_reason` a mod wrote | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge** (migration 179; added to `economy_service._PURGE_USER_ID_TABLES`, so it rides the existing `econ_purge_user` sweep). No Art 17(3) ground is claimed: the money that moved lives on in the preserved `econ_ledger`, so deleting the order corrupts no balance. **Open orders are settled first** (`release_open_orders`, called at the top of `econ_purge_user`): a pending order holds a unit of the item's stock and a todo on the mods' board pointing back at it, so deleting the row alone would strand the todo — a mod ticks it off and silently delivers nothing — and burn the stock unit forever. Erasing one member is not a reason to keep everyone else's shelf short. No refund is written: the wallet and ledger rows go in the same sweep, so the coins would have nowhere to land; the todo closes as *missed*, which is the truth. Purging is also what keeps the `todos` promise above honest — the buyer's identity must disappear from the mods' board, and the join is the only place it lives | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; `guild_id` on the row ✓. `resolver_id` is a mod acting in role, cleared with the row it sits on |
| econ_theme_submissions | Economy — Flash Themes | one member's purchased themed day: `user_id`, the `title` and `blurb` they wrote, the price snapshot, `resolver_id` naming the mod who approved or declined it, and a `deny_reason` a mod wrote. The `title`/`blurb` are member-authored free text and are **published** — the live announcement is a pinned message in the theme channel crediting the buyer by name | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge, with one anonymised exception** (migration 188; added to `economy_service._PURGE_USER_ID_TABLES`, so it rides the existing `econ_purge_user` sweep). No Art 17(3) ground is claimed: the money that moved lives on in the preserved `econ_ledger`, so deleting the submission corrupts no balance. **A theme that is RUNNING is detached, not deleted** (`anonymise_live_theme`, called at the top of `econ_purge_user` beside `release_open_orders`): a live row holds a pinned announcement in Discord that only the hourly expiry sweep knows how to take down, and that sweep finds its work by reading `state = 'live'` rows. Deleting the row would strip the member's name and simultaneously strand the pin at the top of the channel permanently, with nothing left to say it should ever come down — a worse outcome for the erased member than the row itself. So `user_id` goes to 0 (the same unknown-member every surface renders, and which also takes the row out of the purge's `WHERE user_id = ?` sweep) while the window and pin ids stand, and the announcement comes down on schedule. The `todos` anonymise-don't-delete precedent above | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; `guild_id` on the row ✓. `resolver_id` is a mod acting in role, cleared with the row it sits on. **The published announcement itself is not erased by the purge** — it is an ordinary Discord message that still carries the buyer's `<@id>` mention, and it comes down with the pin when the window ends (up to `MAX_THEME_HOURS` = 168h later). The purge is a synchronous DB path with no gateway access, so it cannot edit the card (a Components V2 `LayoutView`, not an `Embed`); a mod ends the theme early on request, which deletes the post immediately. The manual's privacy notice states this explicitly rather than implying the live post is anonymised |
| econ_shop_items | Economy — Custom shop items | guild catalogue config (name, price, stock, window). The only personal datum is **`created_by`**, the admin who defined the item | until deleted by an admin | **NO — `created_by` preserved**, Art 17(3)(e): the record of who opened a spend surface, counterpart to the `econ_ledger` rows the item's sales produce. The mention_award_rules `created_by` precedent | — | Deliberately excluded from the purge sweep; an item cannot be hard-deleted while orders are open (`delete_item` raises) so erasure can never strand escrowed coins |
| xp_daily | XP rollup (migration 186) | per-member daily XP sums by source+channel — derived from `xp_events`, no new data class | **permanent — until dropped**: no writer or reader since 2026-09-30, it keeps its last contents until a migration drops it | **YES — decided at creation** (`privacy/service.py`, purged alongside `xp_events`) | — | **Retired 2026-09-30.** It backed the opt-in Event Retention dial, which was never switched on and was removed together with its prune, the nightly rollup and every reader's union with this table. Left in place rather than dropped by migration (owner's call, 2026-09-30); its single-row watermark `xp_rollup_state` holds no member data and is cleared by the guild purge. Still purged rather than preserved: it is the same activity as `xp_events`, only summed, so keeping it would leave an erased member's XP on disk. New table 2026-08-26 |
| starboard_posts, starboard_reactors | Starboard | who was starred (`author_id`) and everyone who starred them (`user_id`), per message | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge** (2026-09-02 GDPR review). No Art 17(3) ground: a starboard entry is a popularity record, and "we liked the leaderboard" is not a ground | — | Neither column was in `SUBJECT_ID_COLUMNS`' reach before — `author_id` was, `user_id` was, but no purge statement named either table. The starboard **message itself** is an ordinary Discord message the purge cannot reach, the same documented limit as a published Flash Theme announcement |
| duel_cooldowns, duel_group_cooldowns, duel_nicks, hp_group_games | Games — duels, group Hot Potato | who duelled whom and when; the nickname imposed on a loser and their original nick; a group game's host, holder, winner, loser and its full roster | **permanent — group game history** (see the `games_*` row) | **YES — purge** (2026-09-02 GDPR review). Party-game rows: nothing downstream reads a closed game back, so no Art 17(3) ground holds one. A duel row names exactly two members and is meaningless with one removed, so it goes **whole** from either side — the Risky Rolls precedent | — | The register's existing "games_*, duels, hot_potato_*…" row claimed these folded into `econ_purge_user`; they did not — none of these four tables was named in any purge statement. **`hp_group_games` carries a new list-column blind spot**: `roster`, `alive`, `elimination_order` and `pass_log` are JSON/CSV lists of member ids. The purge matches `roster` and `alive` by exact membership; the export cannot, so they belong in `LIST_VALUED_MEMBER_COLUMNS` |
| inactive_sweep_exemptions, inactivity_prune_exceptions | Inactivity sweep | members held back from an inactivity sweep, and who added the exemption | until removed | **YES — purge** (2026-09-02 GDPR review). Bare preferences, no Art 17(3) ground | — | Known consequence, accepted on the `pen_pals_optouts` precedent: erasing the exemption re-exposes the member to the next sweep. One mod action restores it, and an erasure is an out-of-band operator act |
| role_menu_bindings, role_menu_grants | Role menus | which roles the member picked from a menu, and the grant/remove log | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge** (2026-09-02 GDPR review). A record of which self-service roles someone chose; the roles themselves live on Discord, and no Art 17(3) ground holds the log | — | `role_menu_bindings` has **no `guild_id`** and is scoped through `role_menus.id`; a bare `user_id` match would erase the member in every guild the bot serves. `role_menus.updated_by` is admin authorship — preserved, see the admin-configuration row below |
| voice_master_channels | Voice Master | the owner of a live temporary voice room | live state only — the row dies with the room | **YES — purge** (2026-09-02 GDPR review). Live state, not history | — | Completes the Voice Master family alongside `voice_master_profiles` and `voice_master_trusted` (both already purged). `voice_master_blocked` is **open** — see below |
| xp_level_5_cards, pending_promotion_posts, birthday_announcements, bump_tracker_log, policy_votes, reaction_tip_awards, auto_react_placements | Small per-member stores | the level-5 congratulation card; a queued "ready for promotion" post; that a birthday was announced on a date; which member bumped the server on a listing site; how a member voted on a policy ticket; who tipped whom by reaction; whose message got auto-reacted | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge** (2026-09-02 GDPR review). None claims an Art 17(3) ground: they are activity records, dedup markers and bare preferences. `reaction_tip_awards` goes from both sides — the coins that moved live on in the preserved `econ_ledger` | — | `policy_votes` has no `guild_id` and is scoped through `policy_tickets`. `foolsday_exclusions` (April Fools prank opt-outs) was on this row until 2026-09-14, when its dead service was deleted and migration 233 dropped it and `foolsday_names` (10 and 0 prod rows) |
| legitlibs_templates, legitlibs_revisions, legitlibs_reports | LegitLibs | a template the member wrote (`author_id`), a revision they edited (`editor_id`), and a report they filed (`reporter_id`) | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge** (2026-09-02 GDPR review). Templates and revisions are the member's own writing, which has no ground to outlive their erasure request | — | Empty in production — the feature is live but unused, so nothing was retained in error. `legitlibs_revisions` scopes through `legitlibs_templates`; **`legitlibs_reports` keys on an in-memory `game_id` with no table behind it**, so it has no parent to scope through and is deleted across guilds. Accepted: the only personal datum on the row is the id being erased. `legitlibs_channel_config.set_by` is admin authorship — preserved, below |
| announcements, docs, revive_questions, hidden_channels, role_menus, voice_master_name_blocklist, legitlibs_channel_config | Admin-authored configuration | the **admin who wrote or changed a server surface**: `created_by` on a scheduled announcement and a chat-revive question, `updated_by` on a dashboard doc and a role menu, `hidden_by` on a hidden channel, `added_by` on the voice-room name blocklist, `set_by` on the LegitLibs channel. No member-subject data — the row is about the server, and the id is the signature on it | until an admin deletes the entry | **NO — preserved**, Art 17(3)(e): the record of who configured a server surface, and the counterpart to whatever that surface then did. The `mention_award_rules.created_by` and `econ_shop_items.created_by` precedent, applied consistently to the rest of the dashboard rather than left as two one-offs | — | All seven columns are now in `SUBJECT_ID_COLUMNS` (`hidden_by`, `updated_by`, `set_by`, `added_by`, `created_by`), so an access request **discloses** them even though an erasure keeps them — retaining data does not exempt it from disclosure. A member erased mid-tenure leaves a bare id against a config row; that is the same tension the `grant_role_permissions` row records, and it is resolved the same way |
| econ_quest_progress, econ_quest_progress_marks, econ_community_contrib, econ_community_tier_payouts, econ_qotd_rewards | Economy — quests & QOTD | per-member quest progress, the per-occurrence marks behind it, community-quest contributions and tier payouts, and QOTD reward credits | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge, and only actually purged since 2026-09-02.** All five sat in `economy_service._PURGE_USER_ID_TABLES`, whose statement is `WHERE guild_id = ? AND user_id = ?` — **none of them has a `guild_id`**. Every erasure failed on all five with "no such column", logged a warning and moved on, so **108,687 production rows** were reported as cleared and never touched. They key on a parent quest or QOTD and are now scoped through it | — | The finding is the *silence*, not the omission: the purge tolerates schema drift so that one table missing on an older deployment cannot abort an erasure midway, and the cost of that tolerance is that a wrong column name fails identically and forever. `tests/privacy/test_privacy_service.py::test_purge_runs_clean_against_the_migrated_schema` now runs the whole erasure against a fully migrated schema and fails on any swallowed schema error |
| wellness_cap_counters, wellness_cap_overages, wellness_blackout_overages | Wellness | the counters and overage tallies behind a member's caps and blackouts. No member column of their own — they key on `cap_id` / `blackout_id` | 30d post-optout GC with their parents | **YES — purge, and only actually purged since 2026-09-02.** The same defect as the quest tables above: all three were in the plain `guild_id`+`user_id` sweep and have neither column, so every erasure failed silently on them. They are now deleted **before** their parents, since once the parent cap is gone nothing can find them | local Ollama only | Production holds **no rows** in any of the three, so nothing was retained in error — the defect was that an erasure could not have cleared them. This is the correction to the wellness family's "model citizen" note above, which was true of the nine tables that worked and unverified for these three |
| econ_emoji_submissions | Economy — emoji sponsorship | one member's paid submission: `user_id`, the emoji name **they wrote**, the price snapshot, and `resolver_id` naming the mod who approved or denied it | **undecided** — no retention decision has been taken for this store; it is kept indefinitely by default, not by choice | **YES — purge, and only actually purged since 2026-09-02.** It shares `economy_submission_store` with `econ_pin_submissions` and `econ_theme_submissions`, which were added to `_PURGE_USER_ID_TABLES` when they shipped; this one was missed and was unreachable by erasure. No Art 17(3) ground — the money that moved lives on in the preserved `econ_ledger` | — | **Empty in production** when the gap was found, so nothing was retained in error; the first sale would have written member-authored text into a table the purge could not reach. Unlike a live Flash Theme, it strands no Discord artifact the purge must work around: `econ_emoji_submissions.close_for_rental` — the path that would take a live emoji down — **has no caller anywhere in the repo**, so nothing reads live rows back. Two residues are accepted and noted rather than fixed: an uploaded custom emoji stays in the guild (an ordinary guild asset, like a published announcement), and `image_path` points at an upload on disk that the DB purge does not delete. A new product on this store belongs in the purge list in the same commit. This row used to cover `econ_qotd_submissions` too (the sponsored question of the day, same gap, same fix); that table and `econ_qotd.sponsor_user_id` were **dropped by migration 250 (2026-09-30)** when the product was retired with `/qotd`, having never held a row |
| quote_audit_log | Quote cards | `quoter_id`, `quoted_user_id`, the quoted message id and the card's styling. No quoted text | **permanent — Art 17(3)(e)** (audit record of a mod action) | **YES — purge** (decided by the owner 2026-09-02). It is named an audit log, but it logs a decorative feature rather than a moderation decision, so the sanctions ground does not reach it and no other Art 17(3) ground was claimed. The row names two members and goes **whole from either side**: with one of them removed it records nothing coherent | — | Already in `THIRD_PARTY_TABLES`, so an access export flags it for operator review before disclosure — the row names a second member whose identity is the payload |
| inactive_members | Inactivity | `user_id`, the `moderator_id` who set them inactive, a mod-written `reason`, and `stored_roles` — the roles held back for restoration | **undecided** — kept indefinitely by default, not by choice (45 prod rows) | **YES — purge as the subject, anonymise as the actor** (decided by the owner 2026-09-02). Inactivity is not a sanction, so the preserved mod-record ground does not reach it, and holding an erased member's stored role list serves nobody. Rows where the member is the `user_id` are deleted. Rows where they are only the `moderator_id` are **kept with the id blanked to 0** — the row is a record about somebody else, and deleting it would take an unrelated member's inactivity entry away because a mod asked to be erased. The `todos` anonymise-don't-delete precedent | — | Already in `THIRD_PARTY_TABLES`. Whichever way it goes, `moderator_id` means a **mod's** erasure also touches these rows, which is the case the sanctions preserve was written for |
| break_holds | Take a Break | `user_id`, `stored_roles` — the roles held back to give back on "I'm back" — whether Spicy was swapped for Spicy Lite, the hold role ids it put on, and `started_at`. Migration 237 | **only while the break is open**: "I'm back", a mod's End break, a jail/inactive hold taking the roles over, or the departure auto-anonymise job deletes the row. No history is kept — the start and end land in `audit_log` like any mod action | **YES — purge** (decided 2026-09-26, Take a Break plan). A break is the member's own choice, not a sanction, so no Art 17(3) ground keeps it. The known consequence, accepted on the `inactive_sweep_exemptions` precedent: an erased member on a break keeps the break role and loses the list that would restore their roles; a mod gives them back by hand | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; `guild_id` on the row ✓. Deliberately **not** in `MEMBER_ERASABLE_STORES`: the row is what gives the member's roles back, and "I'm back" is the feature's own off switch. Departure auto-anonymisation **deletes** the row rather than holding the member back or rewriting it to the surrogate (decided 2026-09-26): a break survives a leave only if the member rejoins inside the settle window |
| promotion_review_cards | Promotion | `user_id` of the member under review, the review `kind`, `resolved_by` and the `resolution` a mod recorded | **undecided** — kept indefinitely by default, not by choice (4 prod rows) | **YES — purge as the subject, anonymise as the actor** (decided by the owner 2026-09-02). A promotion review is a positive personnel-style record rather than a sanction, so the mod-record ground does not reach it. Same split as `inactive_members`: the reviewed member's card is deleted, and a card the erased member merely **resolved** keeps standing with `resolved_by` set to NULL. This also brings it into line with its sibling `pending_promotion_posts`, which was already purged | — | `resolved_by` was added to `SUBJECT_ID_COLUMNS` 2026-09-02, so a mod who only ever *resolved* cards now appears in their own export; before that they matched nothing and the rows were silently absent |
| qa_tests, qa_verdicts | QA tracker | `qa_tests.verified_by` names the volunteer who signed a test off; `qa_verdicts` holds one member's pass/fail `verdict`, a free-text `note` they wrote, and `paid_amount` — coins actually paid for testing | **permanent — sign-off record**: a verdict someone relied on cannot vanish (481 tests, 308 verdicts). An erasure strips the name and keeps the test | **SPLIT — purge the verdict, unsign the test** (decided by the owner 2026-09-02). `qa_verdicts` rows are the member's own: their pass/fail call, the note they wrote, what they were paid — deleted outright. `qa_tests.verified_by` is **blanked to NULL** on the `todos` precedent: the test is the project's work product, and deleting it would take a recorded sign-off off the board because the tester left. `qa_verdicts.voided_by` names a different mod on someone else's verdict and is blanked for the same reason. No Art 17(3) ground is claimed for the verdicts — the coins already paid live on in the preserved `econ_ledger` | — | This is internal development tooling that nonetheless names real members and pays them real coins, so it is personal data on the same footing as anything else here. `verified_by` was invisible to the access export until 2026-09-02 |
| voice_master_blocked | Voice Master | `owner_id` and `target_id` — the per-room block list a member keeps for their own temporary voice channel | until removed | **ASYMMETRIC — purge the owner's side, preserve the target's** (decided by the owner 2026-09-02). The member's own list is their preference about their own room and has no ground to outlive them, so `owner_id` rows are deleted. Rows where they are the `target_id` are **kept**: that entry is somebody else's protection, and erasing it at the request of the person it excludes is precisely what Art 17(3)'s rights-of-others carve-out exists to prevent — the `no_contact_pairs` ground. Deliberately unlike `voice_master_trusted`, which is purged from both sides: a trust list confers something and can be dropped freely, a block list withholds something and cannot | — | Not the no-contact list, which is separate and already preserved. A member erased as a `target_id` currently keeps a bare id on someone else's block list |
| departed_guilds | Guild removal erasure | **no member data** — one row per guild the bot has been removed from: the guild id, when it left, the deadline its data is erased on, a snapshot of its channel ids (the two channel-keyed tables carry no `guild_id`, and the cache is gone by the time the purge runs), and whether the row came from the removal event or from a reconciliation at startup. Migration 218 | until the purge runs — the row is deleted in the same transaction as the guild's data, and a re-invite deletes it immediately with nothing erased | **n/a for `purge_user_data`** — it names no member, so a member erasure has nothing to clear here. It is itself part of the *guild* erasure path | — | Ships dark: with no grace period set on the dashboard's privacy panel, nothing is ever written to this table and no guild is ever purged. See the note below |
| departure_erasure_queue | Departure auto-anonymise | a member who left a guild whose auto-anonymise dial is on: the guild id, the **real member id**, when they left, when the worker may next try, and after a failed run the attempt count and the error. Migration 231 | until the job runs — the row is deleted **in the same transaction as the anonymisation**, because a row still holding the real id after the rewrite would be the surrogate-to-member mapping the anonymisation refuses to write. A rejoin, or a jail or live ticket found when the job runs, deletes it with nothing done. After 5 failed runs it parks as `failed` until an admin retries or drops it | YES — `purge_user_data` simple-table list | — | Ships dark: the `departure_auto_anonymise` dial defaults off, so nothing is written. See the third-path section below and `plans/departure-auto-anonymise.md` |
| _(bundles append below)_ | | | | | | |

### Note — erasure when the bot is removed from a server, 2026-09-08

Every row above answers "what happens when a **member** asks to be erased". This
note answers the other one: what happens when the **server** leaves.

**Before this, nothing happened.** A guild that removed the bot kept every row it
had ever generated, forever, in every guild-scoped table: economy balances,
message metadata, XP history, no-contact orders, moderation records. The only
cleanup anywhere in the codebase was `whisper_cog` deleting five config keys.
That is a storage-limitation failure (Art 5(1)(e)): the purpose the data was
collected for ended the moment the bot was kicked, and the controller
relationship with it ended too.

**What now happens.** `guild_purge_service` classifies the whole schema:
**261 tables carrying `guild_id`** (on 2026-09-13; it grows with every migration), **33 child tables** reached by their foreign
key (deepest first, so a grandchild goes before its parent), **2 channel-keyed
tables** reached through the channel snapshot, and **10 global tables** that hold
no guild data and are never touched. `tests/privacy/test_guild_purge_coverage.py`
**hard-fails if a new table falls in none of those four buckets**, so a feature
that lands a table without thinking about guild removal cannot ship quietly,
the same gate the member-erasure register row is.

`no_contact_pairs` and `no_contact_events` **go with the guild**, which is the
one deletion here that runs against the instinct in the preserved-categories
table above. They are preserved against a *member's* erasure because the order
protects the other party. A guild removal is different: both parties' whole
relationship with this deployment ends at once, and there is no surface left
that could ever consult the order. Keeping it would retain a protective record
about two people in a server the bot cannot see, serving nobody.
`econ_ledger` goes for the same reason: the double-entry integrity it is
preserved for is integrity of *that guild's* books, which no longer exist.

**Guild 0 is refused outright.** It is the instance-wide config slot, the shared
LegitLibs template pool and the bot-global AI prompt store; it is the one id a
purge must never be handed, so the service raises rather than trusting callers
to filter it.

**It ships dark and it is reversible until it isn't.** The grace period
(instance-wide, Moderation → Privacy) is unset out of the box, and unset means
*never purge*: a removal is logged and nothing is deleted. Set to `0` the purge
runs immediately; set to `N` the guild's data survives `N` days and a re-invite
inside the window cancels it completely. The deadline is stamped into the
`departed_guilds` row at removal time and never recomputed, because the guild's
own config rows are among the things being deleted.
## Cross-cutting questions each bundle must answer

1. Tables touched and their personal-data columns (user_id alone is personal
   data under GDPR: pseudonymous, not anonymous).
2. Lawful basis: opt-in surface? NSFW behind `is_age_gated_channel()`?
3. Retention: any TTL/cleanup loop, or grows forever?
4. Purge coverage: does the privacy purge path delete this feature's rows for
   a requesting user? (The authoritative list lives in the privacy service:
   Wave 1 bundle 1 documents it here.)
5. Minimization: content stored where metadata would do?
6. Processors: Ollama, Lavalink, Spotify, LLM APIs, anything else.
7. Mod visibility: which audit panels expose this data, gated how?
8. Exportability: could we answer a subject-access request for this feature?
9. **Changing a store already listed here is the same obligation as adding
   one.** A new or altered retention sweep, a dropped table or column, a change
   to `purge_user_data` or to the `ANONYMIZE_*` lists: each one moves this
   register's cell, the member-facing line in `manual.html` § Your Data &
   Privacy, and `privacy/disclosure_copy.py` where it changes what survives an
   erasure, in the same commit. Removing data makes those three wrong exactly as
   surely as adding it does, and nothing tests any of them: the register
   coverage test proves a row *exists* and never reads it. The published Discord
   `privacy-tools` doc is a fourth surface and a manual one: its body is in the
   repo, but it reaches members only when someone saves it in the dashboard's
   Docs panel, so a commit says that press is outstanding rather than calling it
   done.

## Deliberately not personal data

Tables that look like they belong in the register and are absent on purpose.
Recorded here so their absence reads as a decision rather than an oversight,
and so that adding a member-identifying column to one is understood as putting
it *into* the register, with a purge decision, rather than a routine schema
tweak.

| Table | What it holds | Why it stays out |
|---|---|---|
| `casino_win_history` (migration 162, 2026-08-15) | `(guild_id, payout, ts)`: a rolling `WIN_HISTORY_KEEP`-row window of the payouts this guild has publicly announced, powering the big-win broadcast's top-3% `@here` tier | No `user_id` and no other member identifier. It answers only "how big is a big win in this guild lately", which never needs to know who won. The cheaper option was raising `casino_ticker`'s retention 25 → 500, and it was rejected precisely because those rows *do* carry `user_id`: it would have retained 20× more per-member play history to power a header. Pinned by a schema assertion in `tests/casino/test_casino_service.py` |
| `music_playlist_messages` (migration 165, 2026-08-17) | `(guild_id, message_id, channel_id, status, processed_at)`: the processed-message ledger that makes a restart or a channel re-scan idempotent | No member column. Authorship joins through `messages` (already registered, purged with `keep_messages` semantics); the ledger itself records only that a message id was seen, which must survive erasure or a re-scan would re-process (and re-credit) messages the purge just cleared |

## Processors (running inventory)

> **The Processor *column* cannot answer "who else sees my data".** It is "—"
> for most rows, and it is empty for Anthropic in particular: `/ask` keeps its
> transcript inside the Discord message, so it owns no table, so it has no row
> to hang a processor on. A tool joining on the column alone would tell a member
> that nothing leaves the box, which is false. This section, not the column, is
> the inventory. (Raised 2026-09-05 by the session building the member-facing
> disclosure report, which hit exactly that.)

- **Local/LAN only**: NudeNet (Guess crops, ONNX in-process), VADER, faster-whisper,
  Ollama/llama-server (LAN, host-allowlisted): moderation, wellness, guess.
- **Anthropic API (cloud)**: advisor, quest-idea gen, game-prep.
  What the advisor sends, as of 2026-08-16 (todo #100): the asker's question;
  the cached manual; a secret-filtered, admin-gated config summary; and, only
  when `advisor_server_context` is on, names/topics of the **shared** channels
  the asker can see, staff-authored dashboard `docs`, recent sent
  announcements, and the asker's **role names and permissions**. Per-member
  rooms (jail, tickets, Pen Pals, bios wizard) are excluded for every asker
  including staff: their channel names identify their occupants. No Discord identity: no user id, and the
  asker's display name was removed with the rest.
  **No member content, by construction.** Until 2026-08-16 a background loop
  (`guild_pins_loop`) snapshotted the first five pins of every shared channel
  and fed them into each ask, taking an embed's title+description when the
  message had no content, which is the shape of a bio, a starboard entry, or
  any other embed. That loop, its snapshot, and its `is_private_room` gate are
  deleted, not merely disabled, and `tests/advisor/test_advisor_context.py` fails if any
  of them reappear. Nothing else in the AI path reads `bio_answers`,
  `bio_field_values`, `messages`, or any other per-member store. This remains
  true only as long as no new source is added to
  `advisor_context.build_asker_context`, which is why that function carries a
  pointer back to this register.
  Privacy-notice line: **shipped**. `manual.html` §Where your data goes now
  itemises what is and is not sent.
- **YouTube playback (cloud), via a local Lavalink.** Lavalink itself runs on
  127.0.0.1, but a YouTube track does not stay on the box. Corrected
  2026-09-13: this line said "local ✓" after all three of these existed:
  the youtube-source plugin is **OAuth-signed into a dedicated Google account**
  (`3592c8ca`, 2026-09-12; `lavalink/application.yml`, `oauth`), so YouTube
  sees each search and video load under that account; signature deciphering
  goes to kikkia's public **remote cipher**, `https://cipher.kikkia.dev/`, on
  every YouTube load (`a7cc2402`; `remoteCipher`); and Music Playlist's
  metadata lookup calls YouTube's keyless **oEmbed** endpoint with the posted
  link (`music/playlist/music_playlist_service.py`, `_OEMBED_ENDPOINT`). What
  leaves is query text and video ids; no Discord identity travels with any of
  them. Privacy-notice line: **written 2026-09-20**, in this docs review, and
  visible to members at the next restart (the notice is a static asset);
  `manual.html` § Where your
  data goes names YouTube (search, loads, playlist oEmbed) and the cipher, and
  § Where in the world gives Google's DPF certification.
- **The operator's Truth or Dare question bank: not a DK processor, and DK
  holds no tool for it.** `/opt/tod/tod.db` belongs to a separate MCP server
  the operator runs (`tod-mcp`, port 8321), outside this repo. On 2026-08-04
  one-off extractors in `/opt/tod` copied game questions out of DK's archive
  into it, including Risky Rolls asks with their askers' ids. **Decision
  2026-09-27: that copy is being removed on the ToD side, separately from DK**,
  and DK keeps no code that reads into or writes to the bank (the one-shot
  author scrub built on 09-26 was deleted unrun). Nothing in DK feeds it, so it
  is no row here and no line in the privacy notice; if DK ever sends it data,
  it becomes one.
- **Cloudflare Tunnel (cloud)**: the dashboard's only route in from the
  internet (`docs/deployment.md`). Cloudflare terminates TLS, so every
  dashboard request and response passes through it decrypted: the session
  cookie, the member ids in requests, and whatever a panel shows. A member who
  never opens the dashboard sends it nothing. Named in the processor table and
  in the privacy notice from 2026-09-27 (row 3.21); until then the notice
  didn't mention it.
- **Spotify Web API (cloud)**: track queries,
  and, since Music Playlist (2026-08-17), playlist add/remove calls carrying
  track ids only. No member identity in either direction; Spotify never learns
  who posted a song. OAuth token storage → security sweep. Discord CDN:
  inherent.

## Known context (seeded from loose-ends audit)

- `Discord Messages/`, Billy's personal Discord export, once sat in the repo
  root. It was not bot data, and it has since been moved out of the checkout.

## Gap list (final, 2026-08-06 — see synthesis doc for the fix queue)

Status re-verified against the code 2026-08-06 and again 2026-09-13
(`2026-09-13-privacy-reuse-standards-review.md`). Strike items **here** when
they land, not only in the synthesis header.

1. ~~Erasure-path: placeholder blowout + missed tables (synthesis #1)~~:
   **shipped `6ac71558`**.
2. ~~Disclosure vs reality: content retention ON while copy hedges (U1)~~:
   **shipped `1dfa8fea`**.
3. ~~Undisclosed sensitive-attribute profiling: mod-assigned gender~~:
   **closed by removal 2026-09-05 (migration 214).** The 2026-08-06 decision
   accepted it as internal metrics; the owner reversed that when the disclosure
   report made it plain that every access request would have to disclose a
   gender label the member never supplied and could not see. `member_gender`,
   its 423 rows, the Gender Tagging panel and the NSFW-by-Gender report are all
   gone. Nothing replaced it; in particular the detector's `FACE_FEMALE` /
   `FACE_MALE` labels were considered and rejected as a substitute: inferring a
   member's gender from their uploads is heavier processing than the mod tag it
   would replace, not lighter.
4. ~~Guess: consent/opt-out package~~: **shipped `775d903d`**. Retention
   half is partly open: original age-out shipped (90d, cut to 7d 2026-09-15), `guess_*` purge
   coverage and the confession-text TTL did not.
5. ~~Processors needing a privacy-notice line: Anthropic (advisor),
   Spotify~~: **closed 2026-08-16.** Both are named in `manual.html`
   §Your Data & Privacy → "Where your data goes"; the item's claim that
   neither appears in a user-facing surface was already stale when
   re-read. The Anthropic entry was expanded the same day (todo #100) to
   itemise what is and is not sent.
6. TTL/sweep debt: ~~rules_events (dismissed)~~ `49a02867`,
   ~~games_external_messages~~ `49a02867`, ~~bios archives~~ `9374c306`,
   ~~greeting_watch~~ `49a02867`, ~~`xp_events`~~ 90-day rollup into
   `xp_daily` shipped 2026-08-26 (migrations 186/187), was never enabled, and
   was removed 2026-09-30: `xp_events` is permanent by decision (2026-09-16). **Still open:** guess `confession_text`.
7. ~~No erasure runbook~~: `docs/gdpr_runbook.md` shipped with
   `6ac71558` and is now listed in INDEX.md. ~~No subject-access/export
   story~~: the access export shipped in `2a3fee2e` (2026-08-06).
8. ~~Backups retain erased users; document the retention window in the
   runbook~~: **documented** in `gdpr_runbook.md` step 6 (`1227bba1`): at
   least 48 hours in the local rotation, 14 days on the NAS.

Decided & healthy: whisper, wellness, confessions, dm-perms, no-contact,
pen-pals, voice transcription, anon games family, sanctions,
economy ledger (preserve), birthdays (minimized).
