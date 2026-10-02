# Proposed wording — the `privacy-tools` doc, split in two

**Status: PROPOSAL. Not applied.** The live `privacy-tools` doc is production
data, authored on the dashboard Docs panel and posted to two channels
(`PRIVACY_TOOLS_CHANNEL_ID`, `RULES_FAQ_CHANNEL_ID`). Changing it changes what members
are reading right now, so this session did not touch it. Apply it from the Docs
panel and re-post if you want it live.

## Why

The 2026-09-05 retention review's first finding: the live doc is candid about
*what* is kept and says **nothing at all** about *how long* — no durations, no
mention of the 7-, 30-, 90-day or 12-month sweeps. `manual.html` carries a
"How long" value on every row. So the surface most members actually read is the
one that answers the question least.

This adds one section. It changes nothing else — the existing text is good and
the carve-outs are well put.

## Where it goes

After **The carve-outs**, before **The tools**. It needs the carve-outs' honesty
about the message copy to have landed first, and it reads as the natural next
question.

## The text

The complete new body is in **`privacy-tools-body.md`** beside this file, ready
to paste into the Docs panel whole. It is the live doc as it stood on
2026-09-05 21:19 plus one new `## How long it's kept` section and one clause on
the message carve-out. Nothing else is altered.

## Two things that changed after this proposal was first written

**The doc was retitled and trimmed on 2026-09-05.** It is now
*"Privacy & Data Retention"* — a heading that promises retention information the
body did not contain. The hedged "on servers that archive message content"
wording is also no longer needed: this doc is guild-scoped and its guild is at
`message_storage_level=all`, so the 12-month rule can be stated plainly.

**Superseded on 2026-09-13: the bot no longer keeps message text at all.** The
storage dial and the 12-month text sweep were removed, and migration 228 erases
the stored text on the restart that ships it
(docs/plans/remove-message-storage.md). `privacy-tools-body.md` now says the
bot keeps a record of each message but never the text, and the 12-month line is
gone from its retention list. The **live** doc still carries the old carve-out
("keeps its own copy of your messages, text included") and must be re-pasted
from the Docs panel after that restart, or it tells members something false.

**The 7-day anonymity claim was false and is not reproduced.** See F9 in the
review. Confessions write to `anon_audit_log`, so the author link lives 90 days,
not 7. The draft says 90.

## Sequencing

`update_doc` re-renders every placement live, so saving from the Docs panel
updates both posted messages by itself — but a direct database write would not,
and would leave the channels showing the old text. **Save from the panel; do not
write the row directly.**

The 12-month and 180-day lines describe sweeps that ship in `ab775b0e` and are
**inert until the next restart**. Everything else in the list is already
enforced and verified against live data. Restarting before saving keeps the
document true on the day it is posted.


---

# The split (decided 2026-09-05)

The live doc was retitled *"Privacy & Data Retention"* and trimmed earlier that
day, which cut the safety tools out. Rather than restore them into a doc whose
own title no longer covers them, they become a second doc. Two subjects, two
docs:

| Doc | `doc_key` | Covers |
|---|---|---|
| **Privacy & Data Retention** | `privacy-tools` *(existing — key stays)* | The promise, the carve-outs, how long things are kept, and the tools that change what is kept |
| **Safety Tools** | `safety-tools` *(new)* | Blocking, `/nocontact`, DM permissions, feature opt-outs, and the ticket route |

Bodies: `privacy-tools-body.md` and `safety-tools-body.md`, both ready to paste.

**The access route is restored deliberately.** The trim had removed the only
text telling a member how to request a copy or an erasure — an Art 15/17 route,
not a nicety. It now appears in both docs, framed for each: "a copy of
everything held about you, or a full erasure" under retention, "someone who is
bothering you, or a copy / erasure / correction" under safety. A member landing
on either finds the way through.

**Every reference verified against production**, because the trimmed text was
old enough to have rotted:

- `<#DM_PERMS_CHANNEL_ID>` — the DM-permissions panel channel, confirmed live in
  `dm_panel_settings` for this guild, with a panel message posted.
- `<#TICKET_CHANNEL_ID>` — the ticket channel, confirmed live: the intake
  reference blocks still direct newcomers to it.
- `/nocontact`, `/delete_me`, `/whisper forget-me`, `/whisper optout`,
  `/guess optout`, `/bank shop` — all still exist in source. `/bank shop`
  survived the shop reorganisation.

**Placement.** `privacy-tools` currently sits in `PRIVACY_TOOLS_CHANNEL_ID` and
`RULES_FAQ_CHANNEL_ID` (the rules/FAQ channel). `safety-tools` most naturally
goes to the same two, so a member scrolling either channel meets both halves —
but that is a placement decision, not a wording one.


## Status after the ship review (2026-09-05)

**The sweeps ship OFF.** The behavioural period rested on a premise the review
disproved — the Connection Graph replay reads `user_interactions_log` over 210
days, not the 90 the first draft claimed — so 180 would blank its earliest
weeks. The number is the owner's to re-decide, and the dial stays off until
then.

**Update 2026-09-13.** That premise is gone: the social graph was removed
(`docs/plans/remove-social-graph.md`) and migration 229 drops
`user_interactions_log`, `reaction_log` and `voice_follow_log` outright. The
behavioural sweep now covers `ping_events` alone, and the body's "who you
interacted with" line became a role-pings line. The 180 still awaits a
re-decision; nothing now reads further back than it.

Both lines in the body are therefore written as *"once the server switches that
on"*. **Do not post this doc while the dial is off and the sentence reads as an
unconditional promise** — as written it is accurate either way, but re-read it
after the period is settled, because the honest wording changes once it is.

`member_events` was removed from the sweep entirely: tenure is
`MIN(ts) FROM member_events` with no window, so any period there mis-scores a
long-standing member as a newcomer. Joins and leaves are now disclosed as kept.

## Rewritten 2026-09-14, after the restart

The restart that applied migrations 227/228/229 landed on 2026-09-14 08:30. The
live doc is now **actively false on two sentences** — it still says "Two things
leave the box" and "the bot keeps its own copy of your messages, text included",
and the text it promises was erased (600,799 rows) six hours ago.
`privacy-tools-body.md` was rewritten to match what the system now does:

- **Three** outbound destinations, not two. YouTube joins Anthropic and Spotify:
  search text and video links go out under the bot's own account, and every load
  also hits the public remote cipher (`cipher.kikkia.dev`). No Discord identity
  travels with any of them (`docs/data_register.md` § Processors).
- **The reductions are stated, not just implied.** Message text erased and no
  longer recorded; the reply/reaction/voice-follow logs dropped by migration 229.
  `message_reactions` survived but holds counts only, so the record line now says
  "how many reactions it drew" rather than "the reactions".
- **A bug-report bullet** in the erasure carve-outs. `/bug` went live with
  migration 225 and `bug_filing = everyone`, so member-written reports are in
  `todos` now. The register's decision — id anonymised, text kept, free text
  naming yourself edited by hand on request — is stated in the member's words.
- **A new "If you leave" section**, mirroring `manual.html` § Your Data &
  Privacy: anonymise-not-delete, with its three caveats (irreversible; mod /
  DM-consent / no-contact records keep the real id; other people's words keep
  your name).

The 180-day role-ping line keeps its *"once the server switches that on"* hedge —
`behavioural_retention_enabled` is still absent from the prod `config` table, so
the sweep is off.

**Still true: save from the Docs panel, do not write the row directly.** Both
posted placements re-render from the panel save; a direct write leaves the
channels showing the false text.

## Rewritten again 2026-09-15 — "we don't retain your data, or your pictures"

The brief was to bring the doc up to the standard the last fortnight's removals
set. Two of the three are done and the doc now says so out loud rather than
leaving it to be inferred from a carve-out:

- **The promise gained a "used to keep, no longer does" paragraph.** Message
  text (nothing records it since 2026-09-13; migration 228 erased 600,799 rows
  on the 09-14 restart) and the social graph (migration 229 dropped
  `user_interactions_log`, `reaction_log`, `voice_follow_log`). Stating a
  deletion is worth more to a reader than the absence of a sentence about it.
- **A fourth carve-out: pictures.** *"Your pictures are looked at, but not
  kept."* No copy of a posted image is stored — what survives is the verdict:
  `nsfw_classifications` (3,329 rows), `nsfw_detections` (7,115) and
  `nsfw_blocks` (106, author_id attached). Framed as the audit trail behind a
  removal, which is the only honest reason it exists.
- **Guess Who is named as the one exception**, because it is one: originals sit
  in `guess_cache/orig` (11 files, 32 MB; 12 open rounds hold a path) until the
  round is solved, with `ORIGINAL_MAX_AGE_DAYS = 90` clearing unsolved ones. It
  is opt-in and the consent text already says this, so the doc matches what a
  joiner was told. It also gained a line in the retention list.
- Retention list: added the Guess line; the "kept for as long as the server
  runs" sentence now names the image verdicts explicitly rather than letting
  them hide in "most of it".
- `/delete_me` is described correctly — all / pictures / text, which is what
  the command actually offers (`privacy_cog.py:342`).

### What was deliberately *not* claimed

A blanket "the bot retains nothing about you" would be false and the doc must
not say it. Still permanent, and still listed: coin balance and the full ledger,
moderation records, XP and levels, game and casino history, self-entered bios
and birthdays, joins and leaves (no window at all — tenure is
`MIN(ts) FROM member_events`), and the metadata row for every message.

### The image-inspection removal — MERGED 2026-09-15 (a524a052)

Image Guard is gone: commit `133e455a`, merged as `a524a052` and pushed. The
classifier and the tagger are removed, migration 230 drops
`nsfw_classifications` / `nsfw_detections` / `nsfw_blocks`, and their
`data_register.md` rows went with them. So the "no pictures" beat above becomes
descriptive at the **next restart**, not before — the promise "the scores and
labels it used to keep were deleted" is only true once migration 230 has run.

**The scope changed, on Billy's instruction, from what this document assumed.**
The plan was to keep the admin-picked Spoiler-Required Channels list and simply
drop the classifier from it. The standards scan pointed out that the 08-15 audit
had found most listed channels were *not* age-gated, so keeping the list would
have gone on rejecting images in SFW channels — the exact complaint that started
this. Asked, Billy said: *"It should only be based on discord setting."* The
spoiler rule now follows Discord's NSFW flag alone (the channel, or its
category). The list, `PUT /config/spoiler` and the config-spoiler panel are
deleted; `config-spoiler`, `nsfw-blocks` and `nsfw-tags` redirect to
`config-global` via `MOVED_PAGES`; migration 230 also clears the
`spoiler_required_channels` bucket, 17 rows.

**This one does not ship dark, and that is deliberate.** Every NSFW channel
starts enforcing the spoiler rule at the restart, and the 17 listed channels
stop being covered unless Discord marks them NSFW. It is a behaviour change a
member sees the moment the bot comes back, which is the house exception being
taken knowingly rather than missed — Billy asked for exactly this.

**Also removed with the tagger: the bare-chest rule** (#99, shipped 08-15). It
was a body-part judgement, so it could not survive the detector it ran on. Named
in the commit body rather than left to be discovered.

**Still true: save from the Docs panel, do not write the row directly.** Both
posted placements re-render from the panel save; a direct write leaves the
channels showing the old text. The row's `title` column also still reads
*"Privacy & Safety Tools"* while the body's H1 says *"Privacy & Data
Retention"* — fix it in the same save.

## Cut to four beats, 2026-09-15 — and now **aspirational**

The brief: *"80% shorter. The bot keeps xp metrics but no text or pictures. When
you leave the server the activity metrics are anonymized and everything else is
automatically deleted. Then the privacy tools, and GDPR request. We should make
the changes to make this true."*

The body went from 1,398 words to **366 (74% shorter)** — short of 80%, and
deliberately. What is left is load-bearing: the three processors (Art 13(1)(e)),
the 90-day anonymity log, the four erasure carve-outs (Art 17(3)), and the
Guess Who photo exception. Cutting to 280 means dropping one of those, and each
one is a thing a member would be surprised to learn later.

Dropped: the "used to keep, no longer does" paragraph, the separate carve-outs
section, the whole `## How long it's kept` retention clock, the `## If you leave`
caveats, and the mod/admin erasure note.

### The important part: **do not paste this yet**

Two of its four beats are **not true today**. This is the first version of this
document that is aspirational rather than descriptive, and it must not reach the
Docs panel until both land:

1. ~~**"It does not keep your pictures."**~~ **CLEARED in code 2026-09-15**,
   merged as `a524a052`. Still blocked on the **restart**: migration 230 is what
   actually drops the three `nsfw_*` tables, so until the bot is restarted the
   database still holds the scores this sentence says are gone.
2. ~~**The whole `## When you leave` section.**~~ **BUILT 2026-09-15**, merged
   as `1f514ec7`. Blocked now on two presses rather than on code: the **restart**
   (migration **231** creates the job queue — it took 231 because main claimed 230
   for the Image Guard removal while this was in review), and then **turning
   `departure_auto_anonymise` on for the guild**. It ships dark, default `"0"`, so
   *"It happens by itself"* is false on every server until that dial is set. The
   doc as drafted describes the dial-on state; pasting it before the dial is on
   makes a promise the bot is not keeping.

### What the departure work does not cover, 2026-09-15

Audited by the branch against the drafted text; the draft now says all of this
out loud in a short *"a few limits"* paragraph rather than leaving it to be
discovered:

- **Held members.** Jailed, or with a ticket opened or joined, open or closed —
  nothing runs until that is settled, and tickets are kept for good by decision.
- **The real account survives in `ANONYMIZE_KEEP_TABLES`** — moderation records,
  DM-consent records, no-contact orders. Each of those is someone else's
  protection, which is why they keep a real id.
- **Other guilds.** Only the departed guild's rows are rewritten; bot-wide tables
  and other-guild rows keep the real id, so "a meaningless number" holds only for
  someone who left the primary guild and is in no other. The draft now says "a
  number", not "a meaningless number".
- **A departure the bot never sees.** The listener enqueues on `on_member_remove`
  and there is no reconciliation pass, so leaving while the bot is offline is
  never queued at all. The draft points those members at the erasure ticket.
- **Bot-posted Guess Who crops stay.** The leaver's *original* is deleted; the
  cropped clue the bot posted into the round is a bot message, not theirs, and
  `MODE_ALL` does not touch it. The draft carves this out in one clause.
- Also unqueued: a rejoin inside the 5-minute settle delay, and a job that
  exhausts its 5 attempts and parks as failed awaiting an admin.

### Two decisions taken 2026-09-15

**Departure deletes text as well as images.** This *reverses* the 2026-09-14
scoping, which was `MODE_MEDIA` with "text message deletion" listed under Out of
Scope. Asked directly — with the consequence spelled out, that every reply anyone
made to a departed member now quotes a deleted message and old threads lose one
side of the conversation — the answer was everything. So the departure worker
runs `MODE_ALL`, and the doc's *"everything else of yours is deleted: what you
wrote in your own words, and the pictures you posted"* is the literal spec.

**Guess Who originals: 7 days, not 90, and cleared on departure.** Rather than
carry a three-month exception inside a sentence promising no pictures, the
retention drops to a week (`ORIGINAL_MAX_AGE_DAYS`, and the `GUESS_CONSENT_TEXT`
that quotes it), and the departure worker deletes the leaver's cached originals.
The doc keeps a one-clause carve-out, because the file genuinely exists while a
round is open and a member should not have to discover that.

**Still true: save from the Docs panel, do not write the row directly**, and fix
the row's `title` column (*"Privacy & Safety Tools"*) in the same save.

### A second picture store, found 2026-09-15

The image-removal branch turned up one the register's framing had let hide:
`/bank shop` writes sponsored-emoji uploads to `econ_emoji/<guild>/` and the
role-icon perk keeps uploaded icon bytes. Both are member-supplied pictures the
bot keeps indefinitely — legitimately, because the guild is *using* them as
server assets, and deleting one on departure would break an emoji every member
can see in old messages.

So the promise is narrowed from "your pictures" to **"the pictures you post"**,
with the two deliberate hand-overs named: the Guess Who submission (7 days, goes
on departure) and the bought-in emoji/role icon (kept while in use). Both are
opt-in purchases or submissions, not a by-product of chatting, which is the line
the sentence is really drawing.

**Open, for Billy:** the departure worker does *not* clear a leaver's sponsored
emoji or role icon. That is the right default — they are the guild's assets now
— but it means a departed member's upload outlives them, and the doc says so
rather than hiding it.

### The long reference doc lags too, 2026-09-15

`manual.html` § Your Data & Privacy is the full reference behind this short
statement, and four of its claims are falsified by the same two branches. Each
edit was assigned to the branch that causes it, so the reference lands true in
the same commit as the behaviour rather than drifting:

- *image-inspection removal* — delete the **Image checks** table row (it
  promises a stored explicitness score plus detected body parts and positions,
  which is exactly the three dropped tables), and drop "Image classification"
  from the local-processing list under *Where your data goes*.
- *departure automation* — rewrite the opening of **If you leave the server**
  ("Nothing happens automatically" is the sentence being reversed), keeping its
  four earned caveats; and change the **Guess Who photo** row's retention from
  90 days to 7 plus deletion on departure.

Both branches were told to leave the **YouTube omission** in that same
processors list alone — it is parked on Ben's end-of-project docs review, and
two sessions picking it up would collide. It stays a known gap until then.

## Both gates clear — ready to paste, 2026-09-16

- **Gate 1 (pictures):** the 2026-09-16 14:27 restart applied migration 230;
  the `nsfw_*` tables are gone.
- **Gate 2 (departure):** the same restart created the queue (231), and
  `departure_auto_anonymise` was set to `1` for the main guild the same day, at
  Billy's request. *"It happens by itself"* is now true.

A verification pass against code and prod corrected both bodies before paste:

- **Processors:** YouTube playback also calls a remote signature cipher
  (`cipher.kikkia.dev`), which receives only the encrypted signature, the `n`
  parameter and the player URL — no video, no query, no identity. Named in one
  clause.
- **Departure:** says outright that activity is *kept but anonymised* (Billy,
  2026-09-16: *"Xp and related metrics are anonymized but retained"*), and that
  text typed into a bot feature (QOTD/themed-day submissions, Guess
  confessions, bug reports) is outside `ANONYMIZE_FREE_TEXT_COLUMNS` and stays
  under the surrogate.
- **Anonymity log:** whispers and Guess rounds are not in `anon_audit_log`, so
  the 90 days never applied to them.
- **Opt-outs:** `/bank shop` has no opt-out; the economy's is `/bank mute`.
  `/whisper forget-me` deletes whispers *sent or received*, not "held about you".
- **Erasure keeps:** `purge_user_data` also leaves DM-consent records, whispers,
  every `guess_*` table and `intake_cards`. Listed, without promising a fix.

> **Superseded in part, 2026-09-18.** Bug #222 retired `/delete_me` and
> `/whisper forget-me` into the `/mydata` panel and gave `purge_user_data` a
> call to `whisper_repo.forget_member`. The two bullets above were verified
> against the code as it stood on 2026-09-16 and are now wrong on both counts:
> whispers **are** removed by an erasure, and neither command exists. The bodies
> in `privacy-tools-body.md` / `safety-tools-body.md` were rewritten against
> current `main`; this file stays as the record of the 09-16 check, not as
> instructions to paste from.
>
> The **YouTube omission** parked above is also closed: the 2026-09-20 docs
> review named YouTube and the signature cipher in `manual.html` § Where your
> data goes and § Where in the world, and in the register's § Processors. Do not
> re-park it.

**Paste both docs in the same sitting.** `privacy-tools` ends by pointing at
**Safety Tools**, and prod has no `safety-tools` row yet — pasting one alone
leaves a pointer to nothing.
