# Todo — Feature Spec

Shared per-guild todo list for the mod team. Tasks arrive from a slash command,
from a sticky Discord board, from the web dashboard, or from a recurring
schedule; they all land in the same list, curated from the dashboard.

The list has **one** Discord board, carrying five headed sections: **quest
sign-offs** waiting on a mod, **paid requests** waiting on a mod, **confessions
to approve** waiting on a mod, **today's chores** as a "did we do it today?"
scoreboard, then **everything else outstanding**. It is one board, not two
(see "Why the split was undone" below), and it carries the quest sign-off queue
and the economy's paid-approval queues.

## Surfaces

| Surface | Type | Permission | Purpose |
|---|---|---|---|
| `/todo task:<text>` | Slash | Moderator (server only) | Add a free-form task |
| Board **➕ Add Task** | Button + modal | Moderator | Add a task with optional notes |
| Board **✅ Complete** | Button + select | Moderator | Tick one or several off: chores first, then tasks |
| Board **✍️ Sign-Offs** | Button + select | **Economy manager** | Review a pending quest claim and approve or deny it |
| Board **🧾 Approvals** | Button + select | **Economy manager** | Review a paid request (themed day or pin) and approve or decline it |
| Board **🕵️ Confessions** | Button + select | Moderator | Review a confession that mod-approve mode is holding |
| `/bug bug:<text> [screenshot]` | Slash | **Filing dial**: moderators, or every member (server only) | File a bug on the bug list, optionally with one image |
| Bug board **🐛 File a Bug** | Button + modal | **Filing dial** | File a bug with optional details, and up to three images where screenshots are on |
| Bug board **✅ Complete** | Button + select | Moderator | Close one or several bugs |
| `GET /api/todos` | Web | Mod | List todos (newest first, capped at 200) + board placement |
| `POST /api/todos` | Web | Mod | Create a free-form todo as the authenticated user |
| `POST /api/todos/{id}/complete` | Web | Mod | Mark a todo complete |
| `PUT /api/todos/board` | Web | **Admin** | Post / move / remove the board |
| `PUT /api/todos/board/change-cards` | Web | **Admin** | Turn the change cards on or off |
| `GET/POST /api/todos/recurring` | Web | Mod | List / create recurring definitions |
| `PUT/DELETE /api/todos/recurring/{id}` | Web | Mod | Edit / delete a definition |
| `POST /api/todos/recurring/{id}/{pause,resume,run-now}` | Web | Mod | Row actions |
| `GET /api/bugs` | Web | Mod | The bug list, same shape as `GET /api/todos` plus `filing` |
| `POST /api/bugs` | Web | Mod | File a bug as the authenticated user (no filing dial, no cooldown) |
| `POST /api/bugs/{id}/complete` | Web | Mod | Close a bug (404 for a todo id) |
| `PUT /api/bugs/board` | Web | **Admin** | Post / move / remove the bug board |
| `PUT /api/bugs/board/change-cards` | Web | **Admin** | The bug board's own change-card switch |
| `PUT /api/bugs/filing` | Web | **Admin** | The filing dial: `mods` or `everyone` |

The list is a moderator worklist end to end, and both surfaces resolve
"moderator" the same way: **`AppContext.is_mod`**: administrator or
manage_guild, otherwise a role in the guild's configured
`mod_role_ids`/`admin_role_ids`. The dashboard applies that rule through
`web_server.auth.resolve_guild_perms`, which the `moderator` tier resolves
through for **every** panel, not only this one.

Both gate on the same rule so a mod is never passed on the dashboard and
refused by the board's buttons (or the reverse). The configured mod role, the
thing a server actually means by "moderator", decides, with the two elevated
bits kept as a short-circuit and a bits-only fallback for a guild that has
configured no staff roles at all. `admin` and
`manage_server` stay bit-only: a config row must not be a path to the ceiling.

Board *placement* and the *change-card dial* are the admin-gated actions:
either one makes the bot post into a channel, which is server configuration
rather than worklist curation.

## Behavior

### `/todo`

Strips whitespace, rejects empty input, rejects task text longer than 500
characters, otherwise adds the todo and replies ephemerally with the new id.
Rejects DMs. Refreshes the board if one is posted.

### The board

A single message the bot keeps at the **bottom of a configured channel**.

- **Contents.** Five headed sections: **✍️ Quest sign-offs**, **🧾 Paid
  requests**, **🕵️ Confessions to approve**, **🔁 Today's chores**, then **📋 Tasks**. Each is omitted entirely
  when empty, rather than stacking "nothing here" sentences; when all are, the
  board says so once.
- **Quest sign-offs.** Pending `econ_quest_claims` rows, oldest first, one line
  each: `**Alex** — Post a selfie · 🪙 500` (the guild's own currency emoji).
  They lead the board because they are the only section where somebody else is
  waiting on the mods rather than the other way round, and they carry **no id**:
  a claim `#14` printed beside a task list whose `#14` is a different row
  invites the wrong one being ticked. See "Sign-offs on the board" below.
- **Paid requests.** Pending rows from the economy's paid-submission
  queues (a themed day, a pin) merged oldest first, one
  line each: `**Alex** — 🎨 Theme: Cursed Cooking · 🪙 300`. They sit directly
  under the sign-offs for the same reason those lead: a member has already
  spent the coins and is waiting. The queue label is shown because one section
  covers two products; no id, same rule as above. See "Paid requests on the
  board" below.
- **Confessions to approve.** Pending `confession_pending` rows (held only while
  a guild's confession mod-approve mode is on), oldest first, one line each: the
  body clipped to `TASK_CLIP`, then its age. Up to 5 rows, with no id and, on
  purpose, **no author**: approving must not become a wider way to unmask an
  anonymous post than the admin-only audit log is. The overflow line reads
  "…and **N** more waiting" because there is no dashboard queue to point at. The
  **🕵️ Confessions** button opens the picker (moderator tier, re-checked inside
  the picker).
- **Tasks.** Outstanding tasks only (neither completed nor written off),
  **oldest first**, so the longest-waiting task sits at the top where it nags.
  Each row is `` `#12` ``, the id as a monospace chip, followed by the task
  as ordinary flowing text, clipped at `TASK_CLIP` (44).
  **Chore-spawned rows are excluded**: the chores section above already shows
  them, with more state than a task line can carry, and listing them twice
  would look wrong.
- **No age on a task row, and why.** A live `<t:…:R>` in a padded cell was
  measured against the 13 real tasks on the production board at a phone's
  ~34-character width: a padded cell with the age cost 27 wrapped lines,
  dropping the padding but keeping the age cost **43**, and no layout that
  keeps the age beats the age-less one ("2 months ago" pushes almost every
  short row over the width by itself). The age-less flowing row costs **22**
  and shows 44 characters of each task. The list is oldest-first, so *position* already
  carries what the age was for here, and the exact date is one tap away on the
  dashboard. The chore rows keep their timestamp: there is one per chore, a
  handful per guild, and "who did it and when" is the question that board
  answers.
- **Row budget.** 15 rows across the sections. Sign-offs take up to 5 off the
  top, paid requests up to 5, confessions up to 5, and chores up to 8; tasks get
  the rest, floored at 3. No backlog of claims, requests or chores may push the task list off the
  board. Overflow reads "…and **N** more on the dashboard".
- **The card.** A Components V2 card; see "The cards" below.
- **Footer.** `J sign-offs waiting • P paid requests waiting • C confessions to
  approve • N of M chores done • K tasks • updates automatically`. Each part is dropped when its
  section is, rather than reading "0 sign-offs waiting" or "0 of 0 chores
  done".
- **Staying at the bottom.** A member message in the board's channel arms a 6s
  debounce, then the board is deleted and re-posted (Discord has no reorder
  API). Reuses `core.sticky.StickyPanel` (the shared sticky machinery), whose `on_message` filters
  out bot messages unless the panel opts into `restick_on_bot` (the todo board
  doesn't), so a repost can't self-loop. Placement is serialized per guild
  by an `asyncio.Lock`, and the stored ids are re-read *inside* the lock so a
  racing post can't orphan the live board.
- **Staying current.** Task mutations from Discord refresh the board
  immediately; dashboard mutations refresh best-effort and are otherwise picked
  up by the 60s loop. An edit is skipped when the rendered signature is
  unchanged: ages tick client-side, so "2h → 3h" costs no API call.
- **Self-healing.** If the board message is deleted by hand, the next refresh
  re-posts it rather than going quietly dead.
- **One repaint on boot.** The 60s loop's first pass repaints *every* posted
  board, not only the guilds where something spawned. A board posted by a
  previous release can be carrying a view the running one no longer registers,
  and its button would answer "This interaction failed" until something
  repaints it. An edit replaces the view along with the content, so one pass on
  boot closes that window instead of waiting for the next message in the
  channel. It also heals any board that drifted while the bot was down.
- **Post before delete.** The replacement is sent *first*, then the old board
  removed. Deleting first would destroy a working board whenever the new
  channel turns out to be unpostable, and if the target is the old channel
  there'd be nothing left to heal from. Any `HTTPException` (not just
  `Forbidden`) leaves the existing board untouched and surfaces a 400.
- **Text channels only.** The picker filters `/api/meta/channels` down to text
  channels: `guild.get_channel()` can't resolve a thread, so offering one would
  400 on a channel the UI had just listed, and a thread can archive out from
  under a board that lives by delete-and-repost.

The view is a static-`custom_id` persistent view (`todo_board_add`,
`todo_board_complete`, `todo_board_signoffs`, `todo_board_approvals`,
`todo_board_confessions`)
re-registered in `cog_load`, so buttons survive restarts.

**One Complete button over both sections.** `board_logic.completable_options`
puts open chores first, then tasks, and carries a chore forward by its
`todo_id`: the thing being completed is a todo row either way, and the select
only ever needed the id. Two boards meant two buttons and a mod having to know
which list a row was on before they could tick it.

### The cards (Components V2)

Every card this feature posts is a `LayoutView` built by the pure builders in
`todo/layouts.py` (`docs/components_v2_style_guide.md`); `board_logic` and
`change_logic` still decide every word, and the builders only lay it out.

- **The two boards are live boards (paradigm 8).** A `## 📋 Server Todo` /
  `## 🐛 Bug List` title, one Text Display per non-empty section (`### heading`
  over its rows, a line between sections), the counts as a `-#` line, and the
  buttons in one row at the foot. Rows stay text: a button beside each of
  fifteen rows would cost 45 of the 40 components a message may carry.
- **Both boards are bare, deliberately.** The style guide's
  general rule gives a newly converted channel hub the small lobby strip and
  footer; these are staff worklists, the todo board is the tallest card the bot
  posts, and the strip would cost a screen of scroll on the card mods read
  most. No Help button either: neither list has a FAQ doc.
  `tests/todo/test_todo_layouts.py::test_the_boards_are_bare` fails if art is added.
- **Every button is grey**, since Add and Complete are not ranked presses. The
  `custom_id`s are stable, so a board posted by an older release converts in
  place on the boot repaint.
- **The text budget.** A V2 card's text is capped at 4,000 characters across
  the whole message. Rows are clipped raw and then escaped, so a board that is
  solid markdown in all five sections measures about 4,140 and would be refused
  whole. `layouts._fit_sections` trims rows off the longest section until the
  board fits and closes it with "…cut short to fit the board."; the same full
  board in plain words is about 3,300 and is never touched.
- **The Complete picker** is an ephemeral card (`## ✅ Complete Tasks` /
  `## ✅ Complete Bugs` over a multi-pick dropdown) that redraws in place into
  its outcome: `## ✅ Tasks Completed`, or `## ❌ Already Completed` when
  someone else got there first. It listens for three minutes, inside the
  fifteen an ephemeral message stays editable. One-line refusals ("❌ Only
  moderators can…") stay plain text.
- **Change cards** are the plan's title as a `##` heading over its lines, with
  no controls. A bug card's screenshots sit in a `MediaGallery` under the lines
  (a V2 message shows no upload a component doesn't reference), each with the
  alt text "Screenshot for bug #N", and **spoilered when the bug channel is
  Discord age-gated** (`is_age_gated_channel`), as member-submitted media.

### Sign-offs on the board

Quest claims that need a human to say yes appear here, not as a per-claim card
in the economy's bank channel. Nothing about them is stored by this feature:

- **No mirrored rows.** The section reads `econ_quest_claims` live, through
  `economy/quests_service.py` `pending_signoff_rows` (claim joined to quest, oldest
  first) and `pending_signoff_count` for the footer. There is nothing to keep
  in sync, resolving a claim anywhere removes it from the board, and the
  Complete button structurally cannot offer one, because a claim is approved, not
  ticked off.
- **The button, not the row, acts.** The board is one sticky message and
  Discord caps components per message, so Approve/Deny cannot hang off it once
  per claim. **✍️ Sign-Offs** opens an ephemeral pick-one select (Discord's
  25-option cap), and picking a claim edits that ephemeral into the claim's
  full detail (member, quest, reward, criteria, prior denials) with
  Approve/Deny under it. The identical shape the Complete button uses. A claim
  someone else resolved while the picker was open renders as resolved, with no
  buttons.
- **Whose permission.** `can_manage_economy` (admin or the configured economy
  manager role), *not* the board's `AppContext.is_mod`, because approving pays real
  currency. In the main guild the two roles are the same one, so the move shut
  nobody out. The board's own Add/Complete buttons keep the mod gate.
- **Repaints.** Every edge repaints the board: a claim filed (`/bank quests`,
  a trigger phrase, a game; one repaint per batch there, not per claimant),
  resolved from the board or the dashboard, or expired by the sweep. All
  best-effort via `economy/quest_views.py` `refresh_signoff_board`, which reaches the cog
  through `get_cog("TodoCog")`, because the claim is already committed and, on the
  resolving side, the member already paid.
- **Where the outcome is announced.** Not here, and never by DM; the register
  channel carries it. See `docs/specs/economy_spec.md` §4 (sign-off quests) for the
  full rule, including the two cases that announce nowhere at all.

### Paid requests on the board

The economy sells two things a moderator has to approve before they happen: a
**themed day** and a **pin**. They appear here on the sign-off pattern above,
not as their own Approve/Decline card in the economy's `bank_channel_id`,
for the same reason, plus a sharper one: a request names the member
and quotes what they wrote, so a card in a member-facing channel would publish
an unreviewed submission to the whole server.

- **No mirrored rows.** The section reads the two submission tables live,
  through `economy/approvals_service.py` `pending_approvals` (a `UNION ALL` over the
  products in `QUEUES`, oldest first, each arm served by its own
  `(guild_id, state, created_at)` index) and `pending_approval_count` for the
  footer. Nothing to keep in sync; resolving anywhere removes it; the Complete
  button structurally cannot offer one.
- **One section, one button, two queues.** A heading per product would be
  two headings over nothing most days. The row carries its queue label, the
  select option carries it again, and **🧾 Approvals** opens one ephemeral
  pick-one select across both. Picking a request edits that ephemeral into
  **that product's own review card**, with that product's own Approve/Decline buttons under it. A
  request someone else resolved while the picker was open renders as resolved,
  with no buttons. The select's value is `kind:id`, because the ids are
  per-table and theme #3 is not pin #3.
- **Whose permission.** `can_manage_economy`, not `AppContext.is_mod`: every
  decision moves currency, and a denial refunds it.
- **Repaints.** Every edge repaints the board: a request submitted, resolved
  from the board or the dashboard, or expired by the economy's hourly sweep.
  All best-effort through `economy/view_helpers.py` `refresh_todo_board`.
- **The money is already taken.** These are charged-at-submit queues. Deny and
  expiry are refund paths, and the exactly-once guarantee lives in
  `economy/submission_store.py` `refund_once`'s `refunded_at IS NULL` predicate, not
  in anything this board does.
- **What did not move.** The **emoji sponsorship** is a third paid submission
  but is not on the board: its approval is an upload, not a yes/no, and it has
  the dashboard queue Pin of the Day never had. `notify_member`'s public
  bank-channel fallback for a member with DMs closed is a different mechanism
  and is unchanged.

### Today's chores

The board's top section, same question it always asked: **"did we do it
today?"**, where the Tasks section below asks "what is outstanding?"

- **Contents.** One row per *active* recurring definition (its **latest**
  instance, done or not) in `time_of_day` order, so it reads like a shift
  checklist rather than by an id nobody thinks in. Each row is a state box
  (✅ / ⬜) leading the line, the chore in bold, then who ticked it and a live
  `<t:…:R>`, then a 🔥 streak. No padded cell: the boxes align just as well by
  starting the line, whereas a fixed-width cell wraps on a phone and strands a
  bare backtick on its own line. Paused definitions are left out: a
  chore parked for the holidays is not one the team is failing, and showing it
  with a dead streak reads as a reproach.
- **A scoreboard, not a pending list.** A ticked chore *stays* on the board
  until the next reset replaces it. A board that removes the answer the moment
  it is yes cannot answer the question it exists for.
- **Streaks.** Consecutive completed instances, ending at the first missed one.
  Shown from two up: a 🔥 1 on everything that happened once is noise that
  hides a real run. **Today does not count against you:** the newest instance is
  skipped while it is still outstanding, or a chore due at 09:00 would read as a
  broken streak every morning until someone ticked it.
- **How a miss reaches the board: `missed_previous`, not `chore_state`.** The
  reset writes the old instance off and spawns its replacement *in the same
  call*, so the latest instance (the only one this board renders) is never
  the missed one. Rendering `chore_state == "missed"` would therefore show three
  consecutive undone days as a plain ⬜ with the footer's missed count
  pinned at zero. So each row also carries whether the instance *before* it was
  written off, rendered as `❌ missed last run` on a still-open row and cleared
  the moment the chore is ticked again. `chore_state`'s `missed` branch stays
  as defense for a row `mark_missed` closed with nothing spawned behind it,
  which the service permits.
- **Footer.** `N of M done`, plus `· K missed last run` when any are. Counts
  every active chore including those past the visible window, so it can't
  disagree with the dashboard about how the day went.
- **What the button can offer.** `board_logic.tickable_chores`: a row with a
  todo behind it, still open. A done chore has nothing to tick and a missed one
  is closed business `complete_todo` refuses anyway. It lives beside
  `chore_state` rather than in the cog so the button and the board read the
  same rows through the same rule. When the button has
  nothing at all to offer, `nothing_to_tick_message` distinguishes three cases:
  no chores configured, none due yet (naming the
  soonest and when it lands), and genuinely all ticked off. It is only reached
  when chores exist and no tasks are pending either; with tasks outstanding
  the picker is never empty.
- **No separate Add.** A chore is a *recurring definition* with a cadence,
  created on the dashboard; **➕ Add Task** makes a one-off.

### Why the split was undone (migration 180)

Migration 166 split the board in two so a daily "post the QOTD" wasn't buried
among "fix the quote bot", putting the two lists in two channels. Migration 180
merged them again because that cost more than it saved:

- The boards could never share a channel. A Discord channel has one bottom
  slot; both wanting it meant every member message woke both, they raced, and
  whichever landed second owned the bottom while the other sat buried: quiet,
  permanent, and unrepairable by any amount of activity in the channel. So a
  server with one mod channel had to choose.
- **Choosing is what happened.** In prod the chore board was posted and the
  all-todos board never was, leaving 25 open tasks with no Discord surface at
  all.
- Every path forked: two placements, two refreshes, two views, two sticky
  listeners, a collision check and its 409.

One board with headed sections answers both questions in one place, which is
what having two boards was trying to do. `todo_board.kind` went with the split
(a single-valued discriminator is a worse lie than no column) and the
`conflicting_board` / `board_conflict_detail` pair went with it. The general
`panel_posting.sticky_conflict` guard still covers collisions with *other*
sticky panels, which is the case that remains real. `panels/sticky_registry.py`
lists the todo board under the `todo-board` key alongside the other resident
sticky panels, so `/bank auction start` warns when the target channel already
holds it (`economy/auction_views._sticky_check`).

**Merge rule.** Keep the posted row. If a guild somehow has both posted, keep
the **chores** channel: chores are mod-facing and the merged board carries
them, so landing it in a public channel would disclose more than landing tasks
in a mod channel. The losing board's Discord message is left where it is (a
migration cannot call Discord) and goes stale until someone deletes it. In
prod this never arises. Pinned in
`tests/migrations/test_migration_180_todo_board_merge.py` against production's real rows.

### Recurring tasks

Definitions live on the dashboard and materialise a normal todo row when due.

- **Reminders, not automation.** The bot adds "Post QOTD" to the list; a mod
  posts the QOTD. The bot never performs the chore. (Photo Challenge has real
  automation of its own, described in `photo_challenge_spec.md`; this is the checklist,
  not a second copy of it.) The bot may, however (migration 201), *notice*
  that the chore was done and tick it off (see **Automatic sign-off** below).
  The doing is still a human's; only the confirming is automated.
- **Cadence.** `daily` or `weekly` only, at a `time_of_day` in minutes since
  guild-local midnight; weekly carries a JSON weekday set (Mon=0). A one-shot
  task is just a task. Column names mirror `games_scheduled` and the time math
  is `games/scheduled_service.py` `compute_next_run`, so the two can't drift.
  Guild-local time comes from the fixed `tz_offset_hours` (no DST).
- **Daily reset.** When an occurrence comes round and the previous instance is
  still outstanding, that instance is **written off** (`todos.missed_at`) and a
  fresh row spawns in its place. Exactly one row per definition is ever
  outstanding, so nothing stacks, and the day that did not happen leaves a
  durable record.

  A skip-if-pending rule would keep one ageing row instead: Monday's untouched
  QOTD would still be Monday's row on Wednesday, you could not tell which day it
  stood for, one tick would credit both, and no streak would be computable
  because a skipped day left no row at all. The trade is more rows for an
  answerable question.

  A written-off row is **closed**: `pending_todos`/`pending_count` exclude it (so
  it also leaves the all-todos board), and `complete_todo` refuses it: you
  cannot tick yesterday's box today, which would invent a completion and put a
  hole in the streak either side of it.
- **Catch-up.** `compute_next_run(after=…)` advances past every missed slot to
  the next future one, so three days of downtime spawns one row on boot and
  writes off *one* instance, not three. Downtime is not evidence that a chore
  was skipped; the record deliberately covers only the days the bot was
  watching.
- **Created after its own time of day.** A chore whose slot has
  already gone by on the guild's current local day materialises **one instance
  immediately**, so it is tickable from the moment it exists. It borrows *Run
  now*'s semantics, not the reset's: `next_run_at` is untouched (still the real
  next occurrence) and nothing is written off as missed, because creating a chore is
  not a day boundary. A weekly chore created on a day it does not run gets
  nothing, because it has no occurrence today to have missed.

  Without this, a chore added at 12:16 for an 09:00 daily would have no
  instance until the next morning, while the chore board drew it ⬜ open and
  **Mark Done** answered "Every chore is already ticked off" over a board
  showing open work.
- **Resume** recomputes `next_run_at` from now, so a long pause doesn't come
  back and immediately fire a stale slot.
- **Run now** adds one instance immediately and changes nothing else: not the
  entry's `status`, not its `next_run_at`. **Skip-if-pending still applies
  here**, deliberately: a manual add is not a day boundary, so pressing the
  button twice can neither stack duplicates nor write the first press off as
  missed, which would fabricate a failure out of a double click and the streak
  would wear it. It deliberately does *not* go
  through `spawn_due`, which scans every guild, so driving it from one guild's
  request would spawn other guilds' due chores and rewrite their `next_run_at`
  with the requesting guild's UTC offset. Leaving `status` alone likewise means
  "add one now" can't quietly un-pause an entry paused for the holidays.
- **Delete** stops the repeat but leaves any already-spawned row on the list;
  that's real outstanding work, and silently removing a task a mod is part-way
  through would be worse than orphaning it.

#### Automatic sign-off (migration 201)

A definition may carry an `auto_complete` trigger naming an event the bot
already watches for. When that event happens in that guild, the definition's
open instance is ticked off by `auto_complete_chores`.

The reason: when the chores were reminders only, of the 28 instances prod's two
chores spawned in two weeks, exactly **two** were ever ticked and 24 aged into
`missed_at`. The bot already knew the chores had been done and made a human
confirm it anyway. A scoreboard that records a miss on a day the thing
demonstrably happened is worse than no scoreboard.

- **Triggers are an explicit column, never a text match.** `'qotd'`,
  `'spicy_qotd'` and `'game'`; `NULL` (the default, and every pre-201 row) means a mod ticks it by
  hand. Matching a chore by its task text ("Do a QOTD") would key automation
  off free text a mod typed into a dashboard field, where a rename, a typo or a
  second guild's phrasing silently stops it with no error anywhere. An
  unrecognized trigger is **rejected at write time** rather than stored, since a
  chore that claims to sign itself off and never does is the worst outcome.
- **Where it fires.** `qotd` rides QOTD registration: the marker path in
  `events/cog.py` (already gated to once per message), where a mod's message tags
  the QOTD role. A test holds every `create_qotd` call site to
  signing off, so a new registration path can't leave the chore marked missed.

  `spicy_qotd` rides the same marker branch for a question registered as the
  spicy kind ([economy_spec.md](economy_spec.md) § Spicy QOTD). Each kind signs
  off only its own trigger (`economy.logic.QOTD_CHORE_TRIGGER`), so a spicy
  question never ticks "Do a QOTD" and a regular one never ticks the spicy
  chore.

  `game` rides `sign_off_game_chore`, called from every path where a human
  starts a multiplayer game by hand: `finish_launch_response` (the 16 party
  games from `/games play`), `BaseDuel._handle_accept` (a challenge accepted),
  `BaseGame._handle_lobby_start` (an N-player lobby reaching its start),
  `games/risky_roll/cog.py` `_start_game`, and the recap relaunch buttons on Name Your
  Price, Mt. Rushmore and Clapback, which go straight to a launcher and so miss
  the shared seam.
- **The game trigger is moderator-only** (`AppContext.member_is_mod`, the same
  definition as every other mod gate). "Run a game" is a chore a mod owes the
  server, so two ordinary members accepting a duel is a multiplayer game being
  run but is not that chore being done. Ungated, it would go green on any active
  day with nobody on staff involved, making it a report on how busy the
  server was rather than a checklist. An **uncached member is treated as not a
  mod**: a chore left open is recoverable with the board's Complete button,
  where a tick that should not have happened is not. The check itself runs in
  the worker thread, not on the loop, because it reads the guild config, which hits the
  DB on a cold cache. The QOTD trigger needs no equivalent gate, since registration
  already requires the economy manager role.
- **A scheduled game does not count**, and this is enforced by *where* the call
  sits rather than by a flag. Party games reach the board through two doors,
  `/games play` and the scheduler calling the same `launch()`, and only the
  interactive door passes through `finish_launch_response`, so there is nothing
  for the scheduler to opt out of and nothing it can forget. A tripwire in
  `tests/todo/test_todo_auto_signoff_wiring.py` fails if the helper ever appears in
  the scheduler. Without this the chore would tick itself green every morning
  off the two daily schedules already running, and stop meaning anything.
- **The game trigger fires at the start, not the finish.** The mod's part in
  "run a game" is done once the room has a game in it; waiting for the archive
  would leave the board claiming a skip for a game that was played but never
  formally ended, or one that flopped with nobody joining.
- **Credit goes to the human**, not the bot: the QOTD's poster, the game's host
  (a mod, per the gate above),
  a duel's challenger (not the acceptor, who answered an invitation rather
  than issuing one). `completed_by` is a real member id everywhere else on the
  board and a mod reading it wants to know who did it.
- **Safety is inherited, not rewritten.** The tick goes through the existing
  `open_instance_id` + `complete_todo` pair, so it cannot resurrect a written-off
  row, cannot double-credit a chore a mod already ticked, cannot fire twice for
  a second QOTD the same day, and cannot race the board's own Complete button;
  the loser's guarded `UPDATE` returns rowcount 0. Only `active` definitions
  fire: a chore paused for the holidays is one a mod said to stop tracking, and
  ticking it while paused would make the pause invisible and the streak wrong on
  resume. A failure never propagates: a chore that can't sign itself off must
  not take down the QOTD registration or the game launch it hangs off.
- **The trigger can only tick what already exists**, and that is a real sharp
  edge: a chore due at 09:00 whose event happens at 08:00 has no open instance
  to credit, so 09:00 spawns one that nobody ticks and it ages into `missed_at`,
  the exact failure this feature exists to fix, now silent. Fixing it in code
  would mean either pre-crediting an instance that does not exist or teaching
  `spawn_due` that today's slot is already accounted for, which changes the
  reset semantics the streak is computed from. It is instead a **configuration
  rule**, stated in the panel hint and the manual: set the chore's time at or
  before the earliest the thing would ever be done.
- **The board is repainted by the caller** (`todo/cog.py` `repaint_board`), not by
  the board loop. That loop deliberately repaints only what it spawned plus
  failed retries ("every user-facing mutation repaints the board itself"), so
  an unrepainted sign-off would leave the chore looking outstanding on the
  surface a mod reads until the next daily spawn, which is indistinguishable
  from the feature not working. The repaint is skipped when nothing was ticked,
  and the DB work on the game paths goes through `asyncio.to_thread` like every
  other `open_db` there, under `open_db_immediate`, since reading the wired
  definitions and then writing the completion is exactly the read-then-write a
  deferred transaction can fail with `SQLITE_BUSY_SNAPSHOT`, which
  `busy_timeout` does not retry and the per-definition guard would swallow. The
  write lock is only taken once a plain read confirms the guild *has* a
  game-triggered chore, so a launch in a guild with none (every guild until
  someone picks the trigger) never queues behind another writer.

  Every game seam signs off **after** its interaction has been answered, never
  before: a repaint is a REST edit that discord.py sleeps through under
  per-channel rate limiting, which is long enough to burn the three-second
  window and fail an accept whose game is already ACTIVE (the hazard
  `todo/cog.py` `add_todo` documents). The QOTD repaint likewise sits *before*
  `events/cog.py`'s `result is None` return, which fires whenever the
  poster has already had their daily login, which is most QOTD posts.
- **Not every hand-started game reaches the seam** automatically: a cog with
  its own command or its own restart button has to carry the call. Risky Rolls
  does (after its response, outside its channel lock, and outside the `try`
  whose handler tears a live round down), as do the recap relaunch buttons on
  Name Your Price, Mt. Rushmore and Clapback, each signing off only when the
  relaunch actually produced a game, so a failed rematch never ticks a chore
  green. Missing one of those is the mistake that has been made three times, so
  a test now scans the cogs for a handler that calls a launcher directly without
  signing off, rather than trusting a list. Mahjong is deliberately out: it is
  closer to solo play.


### Web list

The dashboard shows pending and completed lists for the active guild, plus both
board cards and the recurring-task table. Names are resolved against the active
guild. Completion records the moderator who clicked complete.

A written-off recurring row shows a **Missed** chip and no Mark Complete button,
and is excluded from the Pending filter, the same rule the boards and
`pending_count` use, so the three can't disagree. The stat tiles count all
three states independently rather than deriving one by subtraction, and a
**Missed** tile appears only when there is something in it.

**The list is sectioned by kind** (bug-list row 249): **🎁 Shop
orders** first (paid work with a member's coins in escrow behind it), then
**🔁 Chores**, then **📋 Tasks**, each headed with its count, empty sections
dropped. A row is exactly one kind, read off `purchase_id` then
`recurring_id` (the API exposes both); the glyphs are the Discord board's own
markers so the two surfaces name a row alike. Sections sit *inside* the
Pending / Completed / All filter rather than replacing it, and selection and
the URL state are unchanged. Quest sign-offs and paid requests are **not**
todo rows and stay on their own economy panels; the dashboard list is split
by what the todos table holds, not a mirror of the board. The bug list mounts
the same code with `sections: false` (it spreads the todo config, so it has
to say so): a bug is never an order or a chore. Pinned by
`tests/web/test_mobile_layout.py::test_todo_list_is_split_into_orders_chores_and_tasks`.

**Board placement is visible.** The board card states whether it is posted, and
carries a warning while it is not: it is the only Discord surface that can tick
anything off. The same cost is spelled out in the remove confirmation: an unposted board
silently removes every Discord completion path.

Creating, editing, deleting, pausing or resuming a **definition** repaints the
board directly. The 60s loop is not a backstop for it: that only repaints
guilds where a spawn or a write-off happened, and the chores section is one row
per definition, so without the explicit repaint an added chore stays invisible
and a deleted one leaves a ghost row until the next scheduled fire, a day away
for a daily and a week for a weekly.

### Change cards (migration 224, todo #210)

The board is **edited in place** when it changes, and a Discord edit does not
mark a channel unread. So a change to the board is invisible until a mod
happens to look at it; a card posted above the panel on each change makes the
channel show as modified and leaves a history.

A card is a plain message, not a second sticky panel. It is posted into the
board's channel and the board is then **re-stuck beneath it**, so the cards
accumulate above the panel as a log and the live view stays at the bottom. The
reposition is explicit (`board.schedule_restick`) because the board is a
`restick_on_bot=False` panel: the bot's own card never reaches `on_message`, so
nothing would move the board otherwise, and the card would end up *below* it.

**No card/restick loop is possible.** A card is emitted only when the poll finds
new database rows; reposting the board creates no rows, so the board moving can
never cause another card. The card is also deliberately **not** recorded via
`sticky._note_placed`: `_delayed_restick` yields to any message `was_placed`
recognizes, so registering it would pin the board permanently above the cards.

**Two cadences:**

| Cadence | Covers |
| --- | --- |
| Every arrival, batched into one card per 60s tick | new tasks, shop orders, quest sign-offs, paid approvals, confessions |
| One card a day at guild-local noon | recurring chores spawned, and chores written off as missed |

**Completions are never announced at all**, in either card. That is a
decision, not an omission: the busiest day on the production board carried 41
ticks and the row disappearing is already the whole notification.
Held by `test_completions_are_never_announced_anywhere`.

**Confessions are a count and nothing else**: no author, no content. Two
reasons stack: `pending_confessions` returns no author id at all by design, so
approving a confession cannot become a second way to de-anonymize one; and a
board *section* clears when a mod resolves the confession whereas a change card
is **permanent**, so quoting one would leave a denied-and-deleted confession
printed in the channel forever. This is stricter than the board section directly
above it, on purpose. Pinned by
`test_confession_card_never_quotes_the_confession_or_names_anyone`.

**Detection is a poll, not twenty hooks.** The board has ~20 repaint call sites;
instrumenting each would be twenty chances to miss one. Instead the
existing 60s `todo_board_loop` tick (already exactly the coalescing
window) reads each section's own `pending_*` query with a `since_ts` filter.
Stacking the age filter on the *pending* predicate rather than querying the
tables raw means an item a mod handled inside the window is never announced: a
card saying Ada needs a sign-off, posted after Ada was signed off, is worse than
silence. The loop makes no per-minute reads *while the feature is dark*, which is the normal state: one
indexed `config` lookup per tick returns no guilds and the poll stops there.

**Every decision is in `change_logic`, not the cog.** `plan_cards` owns the whole
tick: which rows become which lines, that an order is credited to its **buyer**
rather than whoever added it (crediting the adder would name a mod, or the
purchase machinery, for something a member paid for), and the boot pass's
bank-and-stay-quiet case. `cursor_after` owns the one rule about the watermark: a
card that was meant to post and hit a *transient* failure leaves it alone, so the
card is rebuilt next tick rather than silently dropped from the history. `local_noon_before`
borrows `games/scheduled_service.py`'s `_epoch_to_local`/`_local_to_epoch` for the
day boundary, the same pair `todo/recurring_service.py` borrows and for the same
reason: this feature and the chore scheduler must not hold two opinions about
where a guild's day starts. What is left in `todo/cog.py` is a channel, a batched
name lookup and a write, which is why those rules are provable without a gateway.

**Three separate silences, all deliberate:**

- **A NULL cursor announces nothing.** `change_cursor_at`/`digest_at` ship NULL
  and mean "nothing announced yet", which the planner reads as "bank the clock
  and stay quiet". Without it, ticking the dial on in a guild sitting on a queue
  would make the feature's first act a wall of cards about days-old items.
- **Switching the dial on re-arms that guard**, every time, by clearing both
  cursors, not just the first time ever. A guild that ran the feature in March
  and re-enabled it in September would otherwise get six months in one card.
- **The loop's boot pass banks the *arrivals* clock and posts nothing.**
  Downtime is a window nobody was watching, so a deploy must not post a backlog
  card naming items that sat on the board the whole time. It deliberately leaves
  `digest_at` alone: the daily card is a pair of *counts* over the window since
  the last digest, so covering the downtime costs one line and is simply
  accurate, whereas re-banking it would mean any restart before noon shrank
  that day's window to "since the deploy", and prod's chores spawn in the
  morning, so a routine deploy would leave the digest nothing to report and it
  would not post at all. Held by
  `test_a_restart_does_not_cancel_the_days_chore_digest`.

**A permanent failure is given up on, not retried forever.** Holding the
watermark is right for a 503 and wrong for a 403: the next minute's attempt on a
channel the bot has lost access to (or one that has been deleted) fails
identically, and each failure widens the arrival window, so a weekend of a locked
channel would come back as a wall of everything since Friday, built by a query
reaching further back every tick. `change_cards.send_change_card` (shared with
the bug board) therefore reports three
outcomes rather than a boolean: `discord.Forbidden`/`discord.NotFound` and a
channel that no longer resolves to a text channel all give up and bank the cursor,
dropping that one card; only `discord.HTTPException` holds it for a retry. Held by
`test_a_locked_channel_is_given_up_on_rather_than_retried_forever` and
`test_a_deleted_channel_is_given_up_on_too`.

**The daily counts are read once a day, not once a minute.** The two chore
numbers are the only figures in the pass with no cursor to make them cheap:
`chore_changes_since` scans the window since the last digest, and the planner
throws them away on the other 1,439 minutes. So the cog runs that query only
behind the same condition `plan_digest_card` fires on (`digest_at` is set and
older than the local noon just past), which keeps the dark-guild cost at one
`config` lookup and the enabled-guild cost at the section queries alone. Held by
`test_the_daily_chore_query_is_skipped_off_noon` and its twin asserting the query
still runs when the digest is actually due.

**The tick's clock comes from inside the read transaction.** Both cursors are
stamped with a `time.time()` taken against the snapshot the arrival queries run
on, not one read afterwards in the cog. Between the read and the send sits an
awaited name lookup, and a row landing in that gap would be invisible to the
queries yet already older than the cursor, so nothing would ever announce it.
Pinned to the snapshot, the worst case flips to the harmless side: a row racing
the first statement is either seen now or still newer than the cursor and seen
next tick.

**The dial says so when it can do nothing.** Cards go *above* the board, so a
guild with the dial on and no board posted publishes nothing at all, which
would look, from the panel, exactly like a working feature. Ticking the box
re-renders the board card: with no board posted it carries a hint saying nothing
will be posted until the board itself is, and the toast says the same at the
moment of ticking.

Ships dark: the `todo_change_cards` config key is absent unless an admin ticks
the box on the Todo List
page (Moderation → Queues & Workflows). Its truthiness is `core.db_utils.parse_bool`'s allow-list (`1`, `true`,
`yes`, `on`) in *both* readers (the panel's and the loop's work-list query)
so a value typed by hand into the prod `config` table cannot mean "on" to one
and "off" to the other.

### The bug list (migration 225)

A second list beside the todo list, for bugs. It is the same table: a bug is a
`todos` row with `list = 'bug'`, so the open/closed predicate, the completion
race guard and the erasure scrub are the todo list's, not copies of them. Every
list-sensitive function in `todo/service.py` takes `list_key`, defaulting to the
todo list, so nothing that predates the bug list can see a bug.

**What it does not carry:** no chores, no shop orders (`create_todo` refuses a
`recurring_id` or `purchase_id` on a bug), no sign-off, approval or confession
sections. The bug board is only the pending bugs, oldest first, rendered by the
same `render_rows` as the todo board's Tasks section, with a "N pending bugs"
footer.

**Its own board, in its own channel.** `todo_board` is keyed by
`(guild_id, list)`, the bug board is a second `StickyPanel` in `BugCog`
(`todo/cog_bug.py`) with persistent buttons `bug_board_add` and
`bug_board_complete`, and it is registered as `bug-board` in the sticky
registry, so posting either board into the other's channel is warned about
like any other collision. `bug_board_loop` publishes the board set, drains
failed edits, and repaints every bug board once on boot; nothing spawns bugs on
a schedule, so a quiet tick does nothing else.

**Who files.** `/bug` and the File button read the per-guild `bug_filing`
config key (`todo/service.py` `bug_filing_mode`): **absent or anything other than
`everyone` means moderators only**, which is exactly `/todo`'s gate, so the
command ships dark. Read strictly per-guild, with no legacy `guild_id = 0`
fallback, because the open state is a write surface for every member. With
filing open, a non-moderator waits `BUG_COOLDOWN_SECONDS` (300) between
filings, measured from their newest bug row (`last_bug_filed_at`), with no separate
timestamp store, so an erasure takes the cooldown history with it. Moderators
are never refused and never cooled down. The modal re-checks the gate at
submit, because it can sit open across an admin closing filing.

**Who closes.** Moderators only, whatever the dial says. `complete_todo` is
scoped by list, so the bug picker cannot close a todo (ids share one sequence)
and the todo board's picker cannot close a bug. The bug picker re-checks the
moderator gate on submit, because the ephemeral picker outlives the click.

**Change cards, on their own switch.** The bug board posts the todo board's
change cards when the per-guild `bug_change_cards` config key is on. It is a
separate key, so turning cards on above one board never starts them above the
other, and each board keeps its own cursor on its own `todo_board` row. Absent
means off. The bug card is **arrivals only**: "🐛 1 new bug" / "🐛 N new bugs",
one line per bug. There is no noon digest (it counts recurring chores, which a
bug list has none of), so a bug board's `digest_at` is never read or written.
Every guard is the todo board's, reused rather than copied: `plan_bug_cards`
runs `plan_arrival_card` under the bug titles, and sending, the
retry/give-up split and cursor storage are `change_cards.deliver_cards`, which
both cogs call. `bug_board_loop` runs the pass after its repaints and banks
the clock silently on boot.

**A bug card quotes the bug and never names its filer, deliberately.** A
task card names its adder because the adder is a moderator. A bug can come
from any member, and a card is a Discord message the erasure scrub cannot
reach, so a name on it would outlive the anonymization the erasure promises.
The text is kept on erasure anyway, so quoting it widens nothing. For the same
reason the bug pass does no name lookup at all. Pinned by
`test_a_bug_card_quotes_the_bug_and_never_names_the_filer`.

**Screenshots ride the card, and come off it when the bug closes** (migration
241, bug-list row 244). A member can attach images to a bug:
one through `/bug`'s `screenshot` option, up to `SCREENSHOT_MAX` (3) through a
`ui.FileUpload` in the File a Bug modal, images only, each under 8 MB
(`todo/service.py` `screenshot_refusal`). The bot **keeps no copy**:

* **Offered only where there is a card to carry them**:
  `bug_screenshots_open`: the bug board is posted and its change-card switch is
  on. Elsewhere the modal has no upload field. A slash option can't be hidden
  per guild, so `/bug` refuses a screenshot there *before filing* (the text is
  still in the member's command box). The modal instead files the bug and drops
  the images with a note, because the member has typed the report out.
* **Held only until the card posts.** The row's `attachment_urls` holds the
  upload links as `{"url", "ext"}` pairs; the member's filename is not kept, and
  the card renames every image `bug-<id>-<n>.<ext>`. The next tick's card
  downloads them (`bot.http.get_from_cdn`), skipping any that fails or would
  push the message past the guild's upload limit, and attaches at most 10
  (Discord's per-message cap); a 413 resends the card without them. Once the
  cursor moves, `settle_bug_screenshots` clears the links for **the whole
  window**: a boot pass, a switch-just-on tick or a give-up drops its bugs'
  images rather than holding them for a later card, and records the card's
  channel/message id on each bug whose images posted. `expire_bug_screenshots`
  drops any link older than an hour on every tick, the backstop for a switch
  turned off between a filing and the card.
* **Closing a bug edits its images off the card**; the text stays. This is the
  one exception to "a card is never edited". `bug_board_loop` runs
  `strip_closed_screenshots` every tick across all guilds (whatever the switch
  says now): `bug_cards_to_strip` finds bugs with a recorded card that are
  closed **or whose filer was erased** (`added_by` = 0; the purge cannot reach
  Discord, so the loop takes the images down within a minute of it), grouped so
  one card costs one edit, and keeps every image that isn't that bug's
  (`is_bug_image`; the trailing dash keeps bug 12 off bug 123's images). A
  card that is gone or can no longer be edited is given up on; a transient
  failure is retried next tick.
* **Two shapes of card are stripped.** A fetched Components V2 message reports
  no `attachments`, so a V2 card's gallery is rebuilt instead
  (`change_cards._strip_layout_card`): each image that stays is read back by
  the `bug-<id>-<n>` name in its link (`media_filename`), downloaded from the
  card and uploaded again under that name, and the edit replaces the message's
  files with exactly those, which removes the closed bug's picture from
  Discord rather than only from view. A surviving image that won't download is
  lost with the removed ones: taking the closed bug's screenshot down is the
  promise, and it is never held up for someone else's picture. A card posted as an
  embed with plain attachments (older cards) takes the original
  edit (`message.edit(attachments=kept)`); both paths are tested in
  `tests/todo/test_bug_cog.py`.

**On the dashboard.** The Bug List page (route id `bugs`, under Moderation) is
the todo page mounted over `/api/bugs`: `panels/bugs.js` hands
`panels/todo.js`'s `mountList` its own routes, copy and two switches: the filing
dial on, recurring tasks off. The list, detail pane, board card and change-card
row are the same code, so a fix to one page reaches the other. The routes are
the todo handlers in `routes/todo.py` called with `list_key='bug'`; the only
per-list data is the owning cog (`BugCog`), the board's sticky-registry key and
the noun in an error. Completion is list-scoped, so `/api/bugs/{id}/complete`
404s on a todo id and the reverse. Filing from the dashboard is moderator-gated
and bypasses the dial and cooldown, which are about members in Discord. An
unknown filing mode is refused rather than stored, because the reader treats
anything unrecognized as closed, so a stored typo would read as saved while
enforcing nothing new.

## Permissions

- Discord: moderator-gated through `AppContext.is_mod` (administrator or
  manage_guild, otherwise a configured mod/admin role). `/todo` rejects DMs.
  **Filing a bug is the exception:** `/bug` and the bug board's File button
  follow the `bug_filing` dial (moderators by default, every member when an
  admin opens it). Closing a bug is moderator-only either way.
- Web: every endpoint requires `moderator`, resolved by `resolve_guild_perms`
  from the same rule; `PUT /api/todos/board`,
  `PUT /api/todos/board/change-cards` and the three bug-list PUTs
  (`/api/bugs/board`, `/api/bugs/board/change-cards`, `/api/bugs/filing`)
  additionally require `admin`. Reading the bug list stays moderator-only
  even when filing is open to everyone. The panel
  disables the board card off `can_manage_board`. The change-card dial is admin
  for the same reason placement is: curating the worklist is mod work, but
  deciding whether the bot posts messages of its own is server configuration.
- The parity between the two is pinned by `tests/todo/test_todo_mod_tier_parity.py`,
  which asserts both surfaces agree case by case.

## User-visible errors

| When | The user sees |
|---|---|
| `/todo` used in DMs | "❌ Server only." |
| Used by a non-moderator | "❌ Only moderators can add to the todo list." |
| Board button clicked by a non-moderator | "❌ Only moderators can manage the todo list." |
| Task is empty after stripping | "❌ Task cannot be empty." |
| Task is longer than 500 characters | "❌ Task must be 500 characters or fewer." |
| Web completion targets a missing or already-completed row | HTTP 404: "Todo not found or already completed." |
| Board channel doesn't exist in the guild | HTTP 400: "That channel doesn't exist here." |
| A `kind` field in the board body | HTTP 422: the body forbids extras, so a client still sending it is told rather than having it silently ignored |
| **Complete** with chores configured, all ticked, nothing else pending | "Every chore is already ticked off. ✨" |
| **Complete** with chores configured but none yet due, nothing else pending | "Nothing due yet — **&lt;chore&gt;** first lands &lt;relative timestamp&gt;. ⏳" |
| **Complete** with no chores configured and nothing pending | "Nothing pending — the list is already clear. ✨" |
| Bot can't post in the chosen channel | HTTP 400: "I can't post in that channel — check my Send Messages and Embed Links permissions." |
| Board action while the bot is disconnected | HTTP 503: "The bot isn't connected right now — try again in a moment." |
| Weekly recurrence with no days | HTTP 400: "Pick at least one day of the week." |
| Recurrence other than daily/weekly | HTTP 400: "Repeat must be daily or weekly." |
| Time of day out of range | HTTP 400: "Time of day must be between 00:00 and 23:59." |
| Run now while a copy is still pending | 200 with "That task is already on the list — nothing new added." |
| `/bug` or File a Bug by a non-moderator while filing is closed | "❌ Only moderators can file bugs." |
| A member files again inside the cooldown | "❌ You filed a bug a moment ago — you can file another in N minutes." |
| Bug is empty after stripping / longer than 500 characters | "❌ Bug cannot be empty." / "❌ A bug must be 500 characters or fewer." |
| Bug board **Complete** by a non-moderator | "❌ Only moderators can close bugs." |
| Bug board **Complete** with nothing open | "No open bugs — the list is already clear. ✨" |

## Non-goals

- **No automation of the chore itself.** A recurring entry never posts the
  QOTD, the photo prompt, or anything else; it only adds the reminder.
- **No assignees, priorities, due dates, or labels** on individual tasks. The
  list is intentionally flat; recurrence lives on the definition, not the row.
- **No editing a task.** A todo can be created and completed, never amended.
  (Recurring *definitions* are editable.)
- **No deleting a task.** Completion is the only way off the board, so the
  record of who did what survives.
- **No sub-daily cadence.** The loop ticks every 60s; anything finer belongs in
  a real scheduler, not a mod checklist.
- **No notifications.** Mentions in descriptions don't ping; completion doesn't
  DM the creator. A due recurring task pings only the moderators who opted into
  `@Staff · Chores` on the Mod Settings panel: the board loop hands each
  guild's freshly spawned chore names to the staff chores ping, one message
  per tick in the staff ping channel ([staff_pings_spec.md](staff_pings_spec.md)).
  A skip or a write-off alone is not an arrival and pings nobody. Otherwise the
  change cards are the one thing that deliberately *notifies*, and only as far as Discord's
  unread marker goes: a card names members but mentions nobody, so it lights the
  channel for the mod team and pings no one.
- **A change card is never edited or withdrawn.** It records what the queue
  looked like at that minute; a mod handling the item afterwards is visible on
  the board below, not by the card being rewritten. That is why a card is a
  plain message and not a second sticky panel; the history only works if the
  old cards stay put and stay wrong about the present.
- **No in-Discord configuration.** Board placement and recurring definitions are
  dashboard-only, per the project's configuration rule.
- **The sign-off and paid-request sections are guests, not features of this
  list.** They render rows the economy owns; this feature stores nothing about
  them, adds no column, and gains no migration for them. If either queue ever
  goes away, its section goes with it and the todo list is unchanged.
- **No per-chore reset rule.** The reset is uniform. A per-definition "carry
  over vs. reset" switch was considered and dropped: two rules doubles what a
  streak, a footer, and a missed row each mean, for a distinction a mod team can
  express by choosing the cadence instead.
- **No backfill of missed days.** `missed_at` starts empty; chores that ran
  before migration 166 (which added it) have no history, so their streaks begin at zero.

## Stored data

Three tables, all per-guild:

- `todos`: headline, optional description and source URL, creator id, creation
  timestamp, completion timestamp + completer id, `recurring_id` provenance, and
  `missed_at` (migration 166) for a recurring row the daily reset wrote off, and
  `purchase_id` (migration 179) for a row a custom-shop-item order spawned, and
  `list` (migration 225, `'todo'` or `'bug'`). Beyond Discord ids, a **bug
  row's text is member-written** once filing is open to everyone, and is kept
  (with the filer id cleared) on erasure (see `docs/data_register.md`).
- `todo_board`: `(guild_id, list)` PK (migration 225), `channel_id`, `message_id`, `updated_at`, plus
  `change_cursor_at` and `digest_at` (migration 224) for the change cards;
  zeroes mean "not posted" and **NULL on either cursor means "nothing announced
  yet"**, which is why they are nullable rather than defaulted, since a 0 would be an
  epoch timestamp and mean "announce everything ever". One row per (guild, list) keeps the board's channel and
  message ids atomic. The key is `(guild_id, list)` (migration 225) because the
  bug board is a genuinely separate list in a separate channel rather than a
  second view of the same rows.
- `todo_recurring`: definition, cadence, `next_run_at` cache, `status`,
  `last_run_at`/`last_status`, and `auto_complete` (migration 201) naming the
  event that signs it off, or NULL for a hand-ticked chore. No per-user data.

The change cards add **no per-user store**: the two cursors are guild-scoped
timestamps, the on/off dials are `config` rows (`todo_change_cards`, and
`bug_change_cards` for the bug board), and every name a card prints is
read from a column that already existed (`todos.added_by`, the purchase's buyer,
a claim's or submission's `user_id`). Nothing new for `docs/data_register.md`,
and `todo_board` still has no subject-id column.

The sign-off and paid-request sections add **no table and no migration**: they
read `econ_quest_claims` and the three `econ_*_submissions` tables, which the
economy owns and `docs/data_register.md` already covers.

## Notes / history

- **The bug list** is one table with a list column rather than a second set of
  tables; its own sticky board in its own channel; anyone can file (when the
  dial is open), only mods close; bug change cards sit behind their own
  switch, off by default; an erased filer's bug is anonymized with the text
  kept. The filing dial defaulting to moderators is what lets the public write
  surface ship dark.
- **Change cards (todo #210)** serve two needs at once: the channel going
  **unread**, which an in-place board edit can never do, and a readable log.
  Every arrival is announced, recurring chores get a single noon summary,
  completions are announced nowhere, and "anything new needing mods" counts.
  The 60s coalescing window is the board loop's existing tick. Ships dark.
- **Paid approval requests and quest sign-offs live on the board**, not as
  Approve/Decline cards in `bank_channel_id` (in the main guild
  `🏦│how-it-works`, a member-facing explainer channel): a card there showed a
  named member, their coins and their unreviewed idea to the whole server, and
  Pin of the Day has no dashboard queue, so a public card would be its only
  review surface. The board is one sticky message the mod team already reads.
  The older cards' persistent Approve/Decline buttons stay registered so cards
  posted before the move remain clickable, and a sign-off card's builder
  survives as the ephemeral detail view.
- There is **no `Add to Todo` message context menu**; no `ContextMenu` is
  registered. `create_todo` is called only from the slash command, the board,
  the web routes, the recurring spawner (`todo/recurring_service.py`
  `_spawn_one`), and the shop's purchase fulfillment
  (`economy/shop_items_service.py`, which is where the `purchase_id`-marked rows
  above come from). `todos.description` is populated by the board's optional
  notes field and by recurring definitions; `todos.source_message_url`
  (migration `008`) is currently written by nothing.
