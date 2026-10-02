# Design guide — building a feature in Dungeon Keeper (Reference)

The entry point. Everything a feature has to get right is written down
somewhere in this repo; nothing tells you *the order you hit those decisions*
or *what must be true before you commit*. That is what this document is.

## How to read it

**This guide owns the spine, not the detail.** Almost every line here is a
rule plus a pointer to the document that actually owns it:

- `→ doc § Section`: the rule lives there. That document is authoritative.
  If it and this guide disagree, **it wins** and the pointer here is a bug
  worth fixing. Pointers are written as `doc § Section` because that is
  exactly `get_doc(path, section=…)` over the MCP.
- `⌂`: no other document owns this, so this guide does. There are only a
  handful; keep it that way. A new cross-cutting rule belongs in an owner
  doc with a one-line pointer here, not in this file's body.

**This guide and CLAUDE.md are not summaries of each other.** CLAUDE.md is
loaded into every session automatically, so it pays a token cost on every
turn and states each rule as tersely as it can. This is a document you open
on purpose, so it can afford the *why* and the *order*. When a rule belongs
in both, CLAUDE.md states it and this explains it.

**When in doubt, the code wins.** `docs/INDEX.md` classifies every spec as
Reference, Design or Aspirational; a Design spec may lag the code and an
Aspirational one describes things nobody built. → `INDEX.md`

---

# Part 1 — The decision sequence

Five decisions, in the order they actually come up. Getting one wrong early
is expensive; getting it wrong at step 4 ships a safety hole.

## 1. Where does the surface live?

**Admin and server configuration lives on the web dashboard.** Every
feature's settings get an admin-gated panel in `src/web_server/`, filed under
the right nav heading. Not a slash command, not a modal, not a button flow.
→ CLAUDE.md § Design philosophy

**Discord is for member self-service and mod actions**: playing a game,
opting in, customizing your own perks, a mod running QOTD.

Why the line is drawn there: Discord command surfaces sprawl and can't be
discovered, versioned or gated the way a page can, and a knob that only one
admin can find is a knob nobody tunes. A feature that shipped
command-managed gets its knobs moved to the web and **the commands deleted**.
That migration is the expected follow-up, not optional cleanup.

If a feature genuinely seems to need in-Discord admin config, **raise it and
ask** rather than building it.

## 2. If it's a dashboard panel — which page, and which id?

**Settings live with the data they produce.** A feature that already has a
report or a queue keeps its dials at the bottom of that page, one pane, with
the settings half read-only for non-admins (`lockUnlessAdmin`). A feature with
no such page keeps them under **Config**, unless it belongs to a moderator
*job* that already has a heading of its own (Moderation → Role Management),
in which case its dials live under that heading with the pages they serve.
→ `dashboard_ia.md` § Where settings live

**The route id is frozen the moment it ships.** Deep links, bookmarks, the
nav `help:` mappings and usage telemetry all key off it. New ids are the bare
feature name (`pen-pals`, `role-menus`), with no `config-`/`games-`/`mod-`
prefix, because a prefix encodes the section, which is the thing that moves.
Regroup and relabel freely; never rename an id.
→ `dashboard_ia.md` § Naming and route ids

Panel-local state goes in the URL as `#/<page-id>?key=val`, so a filtered view
is a link someone can send.
→ `dashboard_ia.md` § Panel-local URL state

## 3. If it's a Discord surface — what shape?

**One ephemeral panel with buttons and modals beats a sprawl of subcommands.**
A member should reach everything a feature offers them from one message.

**Collapse controls.** One dial with a few states beats several overlapping
toggles; Voice Control's access dial is the reference
implementation. If a
config page feels jumbled, reorganize it rather than appending to it.
→ CLAUDE.md § Design philosophy

Command naming, description style, button and modal copy, select placeholders:
→ `embed_style_guide.md` § Slash commands, § Buttons, modals & selects

A view that must survive a bot restart needs a stable static `custom_id`,
`timeout=None`, and re-registration at cog load, and its callback must
degrade to an ephemeral note rather than becoming a dead button.
→ `embed_style_guide.md` § Persistent views

A view with a finite timeout instead tracks `self.message` and inherits
`core/utils.ExpiringView`, so it visibly disables itself on the member's
screen rather than failing silently on the next press.
→ `embed_style_guide.md` § Expiring views

A view only one member may press inherits `core/utils.MemberScoped` and
keeps just its refusal copy.
→ `embed_style_guide.md` § One-member views

## 4. Who does it put in contact, and who can see it?

The safety step. Three gates, and one rule that governs all of them.

**Never ship a preference or toggle that isn't enforced.** A setting the code
doesn't read is worse than no setting: it is a promise to a member that the
bot silently breaks. A passing test *is* that enforcement (see Part 3).
→ CLAUDE.md § Design philosophy

**NSFW gates on `is_age_gated_channel()`**: Discord's own age flag, on the
channel or its category, never a bot-side toggle an admin can flip on a channel
Discord doesn't consider adult. A bare `channel.is_nsfw()` misses a category's
flag.
→ CLAUDE.md § Design philosophy, `shared/age_gate.py`

**Sensitive access is opt-in, and reach is the narrowest that works.** Member
self-service replies are ephemeral by default; go public only for genuinely
shared state. Recurring economy DMs gate on the opt-in game role
(`notify_member(require_game_role=True)`).
→ `embed_style_guide.md` § Reach & privacy

**Any surface that puts two members in contact must consult the no-contact
list.** Whispers, directed questions, anonymous replies, matching passes, voice
room permissions, anything where one member's action reaches a specific other
one. The surfaces gated today are tabulated in the spec; a new member-to-member
surface joins that table or it is a hole. Read the list through
`is_no_contact_conn` / `no_contact_partners_conn` rather than querying
`no_contact_pairs`, so the table's shape stays owned by one module.

Two things about this gate are easy to get wrong and both matter:

- **The refusal must be indistinguishable** from an ordinary success or an
  ordinary failure. The blocked party must never be able to infer that a
  no-contact entry exists. A gate that leaks is worse than no gate, because
  it tells someone they were named.
- **Enforcement and logging are separate questions.** Surfaces gated inside a
  matching loop or a permission sync record no attempt event, because there is
  no single moment there that means "he tried". They are enforced just as
  strictly.

And one that follows from the first, because the reflex is to reach for a
blanket refusal:

- **Suppress the reaching-out, not the member's own outcome.** Run the
  ordinary path and drop only the part that arrives at the other person.
  `/bank pay` and `/bank gift` between a no-contact pair *move the money* and
  suppress the recipient's notification; refusing the transfer would leave the
  sender's balance intact and let `/bank` tell him he's blocked. Risky Rolls
  gates **the die**, not the pairing, because a number that was never generated
  cannot be audited against the public roster. The same instinct applies past
  no-contact: Event Echo **echoes out of an age-gated room and reports the
  crossing** rather than skipping it, because a blanket "never echo out of an
  NSFW channel" rule would have silently killed both of that feature's live
  sources. Reach for the narrowest suppression that still ends in an ordinary
  outcome, and where you can't, say which sources a blanket rule would kill.

→ `no_contact_spec.md` § Gated surfaces, § The disclosure rules

## 5. What data does it store?

**Store the minimum and derive at ingest.** Message text is not stored at all (migration 228), so compute the metadata you need when the message arrives rather
than keeping the text to compute it later. Several features exist only
because their metadata-plus-pointer shape made the content unnecessary.
→ CLAUDE.md § Design philosophy

**A new table holding per-user data needs a `data_register.md` row in the same
commit**, and the row must carry an explicit decision: does `purge_user_data`
clear it, or is it preserved, and if preserved, on what Art 17(3) ground, not
just the engineering reason. The register is the record of processing
activities; an unregistered table is invisible to an access or erasure
request, which is a compliance failure, not a docs failure.
→ `data_register.md`, `gdpr_runbook.md`

**Member-facing erasure is one panel, not a per-feature command.** If a
member can clear a feature's records themselves, that is a button on the **My Data** panel.
Add an `ErasableStore` to `privacy.logic.MEMBER_ERASABLE_STORES` and mark the
register row `**Member-erasable**`. Put the deletes in a service that takes a
connection, so `purge_user_data` calls the same function inside its own
transaction. A feature that keeps its own forget-me command keeps the knowledge
of what to delete to itself: `/whisper forget-me` did, and an operator erasure
reached no whisper at all for months while the member could clear the lot
(bug #222). `tests/privacy/test_privacy_member_data_panel.py` fails on a new erasure
command, and on a register row whose promise has no button.
→ `privacy_spec.md` § Member-erasable stores

**If the column naming the member isn't one of the conventional names in
`privacy.service.SUBJECT_ID_COLUMNS`, add it there too**, otherwise the
subject-access export cannot see the table.
→ `privacy_spec.md` § Subject access export

**Member-facing collection also needs a line in the privacy notice**:
`manual.html` § Your Data & Privacy.

**A table with no `guild_id` must be classified for guild removal**: child
of a guild-scoped table, channel-keyed, or global, in `guild_purge_service`.
`tests/privacy/test_guild_purge_coverage.py` fails on a table in none of them.
→ `privacy_spec.md` § Guild removal erasure

**Anything that leaves the box is named**: a new cloud API, Lavalink plugin
or remote service gets a line in the register's § Processors and in
`manual.html` § Where your data goes, even when no member id travels with it.
→ `data_register.md` § Processors

⌂ **Migrations are numbered `NNN_name.sql` in `src/migrations/`.** Take the
next free number, and re-check it against `main` before you merge; parallel
worktrees pick the same number constantly, and two files sharing one is a
merge conflict that only shows up when the chain runs.
Stamp time as epoch seconds: a `REAL` column written from `time.time()` or
defaulted with `(unixepoch())`, never `CURRENT_TIMESTAMP`/`datetime('now')`,
whose UTC text compares wrongly, and silently, against every float in the
schema. `tests/migrations/test_migration_epoch_timestamps.py` holds the ten shipped
exceptions and fails on an eleventh.

---

# Part 2 — Coding standards

## Reuse

**Before you write a second one, check whether something already owns it.**
The shared seams (the no-contact read, the NSFW verdict, the name resolver,
branded DMs, the paid-submission ledger, the panel-posting guards, the question
bank draw, the settings registry, the grounded-Claude stack) are catalogued
with a line each saying which module docstring to open.
→ `common_modules.md`

And before proposing a de-duplication, read the LEAVE list: a repo-wide review
already examined the obvious candidates and kept many of them apart on purpose.
→ `git show 678a16fa:docs/plans/common-lib-round-2.md` (plans are no longer
committed; that commit is the last one holding it)

Every feature is a package: `bot_modules/survivor/` with `logic.py`,
`embeds.py`, `views.py`, and its cog beside them. Code several features lean
on lives in four shared homes: `core/` for infrastructure, `shared/` for
helpers several features use, `panels/` for the posted-panel machinery,
`no_contact/` for the no-contact list. (There are no flat `services/`, `cogs/`
or `commands/` packages.) A **new** feature is a new package.
**The directory is not the point; the layer boundary
is**, so match the naming the gate looks for below.

## Layering

**Behavior goes in the logic/service layer. Cogs, views and routes are glue.**
A cog resolves Discord objects, calls one function, and renders the result.
This is a design decision made at build time, not a refactor you do when the
tests get awkward. It is what makes the feature testable offline at all, and
it is why the test rule in Part 3 can say "don't test through Discord mocks".
→ CLAUDE.md § Regression tests

The gate enforces the floor: a **new** `logic.py` / `store.py` / `service.py` /
`*_logic.py` / `*_service.py` with no mapped test is a hard commit failure.

Imports point downward. Neither the dashboard nor a logic-layer module
imports a cog or a command module at run time; a helper both sides need moves
into a store or service the cog imports too. The layer is decided by file,
not directory, so it survives a move: a cog is any file `cog_files()` finds
(it defines `setup()` or sits in a cog folder), a command module is under
`commands/`, presentation is a views/embeds/layouts/modals/panel(s) module
and may import glue, and every other `bot_modules` file is logic.
`tests/contracts/test_import_layering.py` holds today's eight crossings, each with its
reason, and fails on a ninth. The eighth (`games/utils/game_manager.py` →
`todo.cog`) is not new: the old directory-based sweep couldn't see it.

## Bot-side code

- **Embeds are built by pure `build_*` functions in a per-feature `embeds.py`**:
  plain dicts and primitives in, `discord.Embed` out, no Discord or network
  calls. Name lookups arrive as a resolver callable. A cog building eight
  embeds inline is the anti-pattern.
  → `embed_style_guide.md` § Builder conventions
- **Color comes in as a parameter**, resolved by the caller via
  `safe_resolve_accent(source, guild)` (`core/branding`), never
  `resolve_accent_color` directly, which raises and is a hard test failure.
  `source` is whatever the caller holds: a bot, an AppContext, or a db_path.
  A builder never resolves the accent itself. Keep a hard-coded red/green only
  where the color is *semantic*.
  → `embed_style_guide.md` § Color
- **DMs go through `shared/dm_branding.py`**: `send_branded_dm` for the
  common case, `brand_dm_embed` for callers that own their own delivery
  policy. Four near-identical `_try_dm` helpers existed before it did; there
  should not be a fifth.
  → `embed_style_guide.md` § DM branding
- **Escape member-supplied text and pin your mentions.**
  `discord.utils.escape_markdown` before it goes in an embed,
  `escape_mentions` in any `content=` that isn't allow-listed, and always set
  `allowed_mentions` explicitly: default `AllowedMentions.none()`, and when
  you do want a ping, allow-list exactly the role or user intended.
  → `embed_style_guide.md` § Mentions, pings & user-supplied text

- **Naming a member in an embed? Resolve it; `<@id>` renders as a number.**
  An embed mention is resolved by the *reading* client from its own cache, so
  anyone who hasn't seen that user gets bare digits. Use
  `shared/name_resolver.build_name_fn` (member cache → `known_users` →
  `<@id>`, markdown-escaped), have builders take a `name_fn`, and guard the
  wiring with a test. Content mentions are fine; a no-contact pair degrades to
  a plain `User <id>` on purpose.
  → `embed_style_guide.md` § Naming members in embeds

- **Hold a message id? Link the message, not the channel.** A channel-only
  link lands the reader at the bottom of the channel; build permalinks with
  `core/utils.jump_url`, never a hand-rolled URL. Link the channel only when
  the room itself is the subject.
  → `embed_style_guide.md` § Pointing at things

- **Credit or debit once: claim the row atomically first.** Two presses, a
  restart and a sweep can all reach the same payout. Make the state change that
  proves "this call owns it" a single statement and check its rowcount before
  moving money. `game_manager.end_game` deletes the live-game row and returns
  when `rowcount` is 0; `claim_next_approved` uses `UPDATE … RETURNING`. A
  read-then-write pair is not a claim.
  → `economy_spec.md`

- **A task you won't await goes through `core/background.spawn`.** A bare
  `asyncio.create_task(...)` whose result is dropped can be collected mid-run
  (the loop holds only a weak reference) and fails with no traceback until it
  is. `spawn(coro, label=..., logger=log)` holds it and logs the crash as it
  happens; a loop that ticks forever is `run_forever` in the same module.
  `test_core_background.py` fails on a new discarded task.

- **A loop that posts on a schedule gives up on a dead channel.** Count
  consecutive failures per destination with `core/background.FailureStreak`
  and skip a paused one, instead of logging the same 403 every tick;
  `log.txt` is wiped every boot, so repeated warnings never add up to
  evidence. Sticky panels, coin drops and chat revive are the three users.
  Every streak registers itself, so a paused destination reaches the
  dashboard's Configuration Problems card with no extra wiring. Give the
  streak a label an admin can read, and pass `channel_id=` when the key
  doesn't carry the channel.
  → `common_modules.md` § Embeds, DMs and panels

## Dashboard-side code

The shared widgets are **safe by default**, and the safety only holds if you
use them:

- **`table.js` escapes every cell.** A column opts into markup with
  `html: true` and then owns its own escaping, and that opt-in is the whole
  security boundary.
- **A picker never silently drops the value it was given.** Assigning a
  `<select>` a value it has no `<option>` for selects *nothing*: the control
  renders blank, or falls to whatever sits first (usually "(none)"), and is
  then indistinguishable from one nobody ever set. Config panels save the whole
  form at once, so the next save of an unrelated field writes that blank over a
  real setting. Two sources put a value outside the option list: an id whose
  role/channel was **deleted in Discord**, and a stored number outside the
  presets a panel happens to offer. Build role/channel/category options with
  the `config-helpers.js` builders (`roleSelect`, `channelSelect`, the `Multi`
  variants) or the `mount*Picker` wrappers, which keep an unmatched id selected
  and label it `⚠ Missing role (id …)`; where a value is assigned *after* the
  options are built (a mode/resolution change, or an edit form populating
  itself), use `selectValueOrAdd`. `tests/web/test_shared_js_safety.py` holds
  the invariant, and `tests/web/test_activity_compare_picker.py` the
  rebuild-on-mode-change case.
- **Config panels mount through `mountAsync`**, so a failed first fetch
  renders an error with a retry instead of a permanent spinner. **Let the
  rejection reach it.** Five panels wrapped their loader's own fetch in a
  try/catch that rendered a bare error and returned *normally*, so
  `renderFailure` (the error *plus a working Try again button*) could never
  run, and the `errorMsg` they declared was dead code. `wellness-caps.js` is
  the model for a panel that also refreshes: rethrow on first load, render in
  place afterwards.
- **A report panel that refreshes uses `mountReloadable`** (`report-helpers.js`),
  which puts the catch on *every* pass. Seven health panels guarded only the
  first load, so a failed refetch left the previous figures up under a Show
  Bots toggle that had already flipped, giving numbers that look like an answer and
  are not.
- **A bounded number is refused, not clamped.** Out of range is a 422 naming
  the field, declared as `Query(ge=, le=)` or `Field(ge=, le=)` where the
  value enters, so FastAPI raises it. A clamp reports success for a value it
  did not store, and the panel goes on showing what the admin typed. Derived
  numbers (arithmetic nobody typed) still clamp.
  → `web_testing.md` § Bounded inputs
- **Unsaved-edit tracking is per guarded form**, not per page. `guardForm`
  registers the container and `showStatus` clears only the form its status
  element sits in. `isFormDirty(form)` asks the same registry, which is how a
  panel that rebuilds itself after an unrelated action decides whether it may
  (mahjong remounts after a card upload and would otherwise throw away
  half-typed House Rules). **Do not report a held rebuild through
  `showStatus(el, true, …)`**: if that element sits inside the guarded form (mahjong's `[data-status]` does), it clears the very dirt that held the
  rebuild, so the next action goes through and takes the edits with it. Use a
  toast.
- **`web_server/helpers.resolve_names` fills a name field only while it is
  falsy**, so a route seeds the field with `""`, never a placeholder such as
  `str(user_id)`. A truthy seed defeats both the `known_users` lookup and the
  `User <id>` fallback, and members outside the live cache render as raw
  snowflakes.
- **A form submit goes through `guardedSubmit`**, never a bare
  `addEventListener("submit", …)`. A handler that awaits an `apiPost` leaves
  its button live for the whole round trip, and a double click posts twice.
  → `embed_style_guide.md` § Dashboard (JS) specifics
- **An optimistic write owns its rollback.** A list mutated and re-rendered
  before the PUT has to be put back when the PUT fails, or the screen shows
  state the server never accepted. Pen Pals' separations did not, and that is
  the keep-them-apart list. Never write a failure message into an empty-state
  element either: shop-approvals did, and one failed fetch replaced the real
  empty copy for the rest of the session. The dirty flag is per form, not one module-global boolean: with a shared boolean, on a panel guarding two to four forms, saving one,
  or any unrelated action reporting success, silently disarmed the warning
  protecting half-typed values in the rest.
- **A new guild-scoped cache in `config-helpers.js` must be cleared in
  `resetMetaCaches()`**; a test hard-fails if it isn't, because a stale cache
  survives a guild switch and shows one server's members inside another.
- **Snowflake ids cross the boundary as JSON strings both ways.** Never
  `parseInt` an id: a bare number above 2^53 is silently rounded, and the
  sweep in `web_testing.md` will catch it after you've already shipped the bug.
- **Destructive confirms go through `confirmDialog`**: question plus one
  consequence sentence, `danger` styling, never native `confirm()`, and the
  same for `prompt()` and `alert()`. eslint enforces it; there is one
  documented exception.
  → `embed_style_guide.md` § Dashboard (JS) specifics, `web_testing.md`
- **Network goes through `api.js`** (`api`, `apiPost`, `apiPut`,
  `apiDelete`), which owns the credentials, the error envelope and the 401
  redirect. eslint bans a bare `fetch()` or `XMLHttpRequest` elsewhere; three
  files opt out, each with its reason written above the opt-out.
  → `embed_style_guide.md` § Dashboard (JS) specifics

**Styling picks a step off the scale; it does not invent a value.** Sizes come
from `--t-1..--t-6`, spacing from `--s-0..--s-7`, and the rule for a config
page is compact rows, airy sections. The palette is Discord's on purpose, two
of its contrast decisions are load-bearing, and Archivo's width axis is what
the nav rail uses to mark the section you're in, so it is not swappable for a
static face. → `dashboard_visual_language.md`

**Run the JS gates before pushing.** Node 20 is installed user-local purely so
the blocking CI lint job can be reproduced: `npm install --no-save` once per
worktree, then `npx eslint src/web_server/static` and
`npx stylelint "src/web_server/static/**/*.css"`. Static-asset cache-busting
is automatic per boot, so JS edits appear after the next restart, not before.
→ CLAUDE.md § Conventions

## Copy

Structure rules are global; **voice** splits by surface: games may be playful
("Start a Would You Rather game!"), while moderation, economy and admin stay
measured. Titles and labels are Title Case; prose is prose; denials carry `❌`;
it is **"server", never "guild"** in member-facing copy; currency and perk
vocabulary route through the guild's own settings rather than a hard-coded
"coins".
→ `embed_style_guide.md` § Voice & terminology, § Titles, labels & casing;
`copy_style_guide.md` for prose style (no em dashes in sentences), the names
to use, and the 120-character field-hint rule

---

# Part 3 — What tests ship with it

**Same commit as the feature. Every time.** This is the standard, not a
coverage percentage:

- The **happy path**.
- **Every guard and branch**, especially the safety gates from Part 1 §4:
  NSFW, opt-in, role gates, no-contact. A passing test *is* the enforcement
  CLAUDE.md's safety rule demands; without one you have a comment, not a gate.
- For a bug fix, **a test that fails before the fix**. Write it first and
  watch it fail; a test written after a green fix proves nothing about the
  bug.
- **A bug seen in Discord still gets its test at the logic layer.** Reproduce
  the state that broke, not the surface it showed up on. At most one wiring
  assertion in the cog test, and only when the glue itself was wrong.

**Chose the surprising branch? Pin it with a test that fails if someone
reverts it**, and say why in the owning spec. This is the rule that keeps a
deliberate decision from being "fixed" by the next session, which is the single
most repeated way work here gets undone. The Guess solved-reveal prints a bare
`User <id>` when the submitter and the answer are a no-contact pair (naming
them together in the bot's own voice manufactures the very association the list
exists to prevent), so resolving it is a regression, and two tests say so. The
party games' turn pings stay **public** because an ephemeral message notifies
nobody and would silently stall the game. `ama` stays in `VALID_GAME_TYPES`
even though no new `ama` rows are accepted, because `import_bank` 400s a whole
payload on one bad line. None of those reads as intentional from the code
alone. A comment is not enough and a note somewhere else is not enough: the
test is what a stranger's `/simplify` pass will actually run.

**Prefer a `pytest.param` row over a new test function** when you're covering
another value variant, and check whether a shared contract table already covers it: embed accents live in `tests/contracts/test_embed_accent_contract.py` and
take one `case()` row per new builder, never a per-file copy.

**Tests live in the folder that mirrors the code**: `tests/<feature>/` for
`src/bot_modules/<feature>/`, `tests/core/`, `tests/shared/` and so on for
the shared homes. Three folders are by kind rather than feature:
`tests/contracts/` for the cross-cutting sweeps and shared contract tables,
`tests/tooling/` for `scripts/` and the gate, and `tests/web/` for the
dashboard (with `tests/beta/`). **Basenames stay unique across the whole
tree**: there are no `__init__.py` files, so pytest imports each test by
its filename and a second `test_foo_logic.py` in another folder fails
collection. Reach the repo root through `from tests.paths import REPO_ROOT`,
never `Path(__file__).parents[N]`, so a test never depends on its depth.

Dashboard work gets some coverage for free: a new route joins the
authorization sweep automatically (add it to `PUBLIC_PATHS` only if it is
genuinely public), and the browser suite checks every panel for layout
breakage and console errors on mount. Add an interaction scenario when the
layout only appears behind a click.
→ `web_testing.md` § Adding a route? Two freebies, CLAUDE.md § Regression tests

---

# Part 4 — Before you commit

⌂ The assembled obligations. Nothing here is new; the point is that it is in
one place, in the order the pre-commit hook and the reviewer will hit it.

## The docs contract

**No real people or server identifiers anywhere in the tree.** The repo is
public. A member's display name, handle, quote or message, and a real guild,
channel, role, message or user id, never go into a doc, a test, a fixture, a
comment or a commit message — use synthetic ids (a run of six or more
repeated digits, `1284999999999999995`) and labelled placeholders
(`GUILD_B`, `<#TICKET_CHANNEL_ID>`), and invented names of the same shape
when a parser case needs a dot, an underscore or an emoji.
`tests/tooling/test_no_real_identifiers.py` enforces the id half against an
allowlist that records why each exception is allowed (a guild id a
migration pins, a public bot's id); the name half is this rule and the
reviewer. Found the hard way before the first public push (2026-10-01):
an anonymised member named in the privacy docs, real usernames in game
fixtures, a maintainer's handle in a test.

Same commit, not a follow-up.

- [ ] Behavior changed ⇒ the matching spec in `docs/` is updated, and its
      `INDEX.md` classification too if the flavor changed.
- [ ] UI or UX changed (new/changed slash command, dashboard panel, embed
      copy, button or modal flow) ⇒ `src/web_server/static/manual.html` is
      updated. It is a different surface from `docs/` and drifts on its own.
      A field hint longer than the rule allows moves its detail into the
      panel's manual section (`copy_style_guide.md` § Field hints).
- [ ] New per-user table ⇒ a `data_register.md` row with the purge-or-preserve
      decision and, if preserved, the Art 17(3) ground.
- [ ] **Changed** per-user data the register already covers (a retention
      sweep, a dropped table or column, `purge_user_data`, the `ANONYMIZE_*`
      lists) ⇒ the register cell, `manual.html` § Your Data & Privacy and
      `privacy/disclosure_copy.py` move with it, and the published Discord
      `privacy-tools` doc is named as an outstanding press.
      `data_register.md` § Cross-cutting questions, item 9, owns this.
- [ ] Subject column not in `privacy.service.SUBJECT_ID_COLUMNS` ⇒ added.
- [ ] Member can erase it themselves ⇒ an `ErasableStore` on the **My Data** panel and a
      `**Member-erasable**` register row, never a per-feature forget-me
      command.
- [ ] New member-facing collection ⇒ a line in `manual.html` § Your Data &
      Privacy.
- [ ] New table without a `guild_id` ⇒ classified in `guild_purge_service`.
- [ ] New outbound call ⇒ `data_register.md` § Processors and `manual.html`
      § Where your data goes.
- [ ] New doc ⇒ a row in `docs/INDEX.md` with its classification.
- [ ] README only if the change alters what the bot *is*, meaning a feature area
      appearing or disappearing. Not for a command being added or renamed.

## Enforcement

- [ ] Every gate from Part 1 §4 has a test that proves it denies.
- [ ] Member-to-member surface ⇒ no-contact consulted, and the refusal is
      indistinguishable from an ordinary outcome.
- [ ] No preference or toggle that nothing reads, including a **config key
      staged into prod ahead of the branch that reads it**. Staging one is a
      normal move here; leaving it unnamed is how `econ_cat_catch_daily_cap`
      sat in prod for four days as a cap nothing enforced. Name the branch that
      must land, in the commit that stages the value.
- [ ] Deliberate anomaly ⇒ a test that fails if it is reverted (Part 3).
- [ ] New logic-layer file ⇒ a mapped test file (the hook hard-fails otherwise).

## Gates

- [ ] `python scripts/gate.py --scoped` runs automatically in the pre-commit
      hook (ruff, then the tests mapped to the staged diff). Neither heavy
      check runs there: **pyright and the browser panel sweep are CI's**, on
      every push/PR, because neither can be scoped to a diff and parallel
      sessions ran one copy each. `--quick` runs both locally before a push;
      `--pyright` / `--browser` force one into any run. In a
      session worktree a shared-file edit no longer fans out to the whole suite;
      the gate names the paths whose full run it **deferred** to main.
- [ ] `python scripts/gate.py` on **main**, once a batch of merges is complete;
      this is where the deferred runs are paid, and where a clean merge between
      two parallel sessions is caught leaving main red. One run covers every
      branch merged since the last one; it is not a per-ship step. A green run
      moves the local `last-full-gate` tag; teardown prints how many commits
      main is past it, so a skipped run is visible rather than remembered.
- [ ] A new pure `*_logic.py` ⇒ try adding it to `[tool.pyright] strict` in
      pyproject.toml and raise `FLOOR` in `tests/tooling/test_pyright_strict_list.py`.
      The list only grows: a listed file that stops passing gets its types
      fixed, not its entry removed.
- [ ] Dashboard assets touched ⇒ `npx eslint` and `npx stylelint`, the exact
      commands the blocking CI job runs.
- [ ] Full suite green before a **push to origin**; CI on that push satisfies
      it. Run it locally only solo; a parallel full run can exhaust the tmpfs
      quota and spray bogus sqlite errors.
- [ ] Coverage floor in `pyproject.toml` not lowered.

## The commit itself

- [ ] Subject is `Scope: descriptive summary`, ~60 characters.
- [ ] Body is prose: why, edge cases handled, what the tests cover.
- [ ] No `Co-Authored-By` / `Claude-Session` trailers.
- [ ] **User-facing** ⇒ the body ends with a `Testing:` section of `- [ ]`
      lines. Not every behaviour change earns one: it has to be something a
      member or admin can see in Discord or on the dashboard, so an internal
      refactor, a dep bump, a docs or test-only commit gets none.
- [ ] Those lines are written for a **volunteer tester, not a developer**;
      they *are* the QA card. One action and one observable result per box,
      naming what the tester clicks and sees rather than what the code calls
      it, each verifiable in one sitting. The card itself is assembled from
      every `Testing:` section the branch merged, once, at `/dk-ship` teardown.

→ CLAUDE.md § Docs, § Gates, § Commits

---

# Part 5 — After it merges

⌂ A merge is not a launch, and this repo is unusual in how far apart those two
are. The checkout **is** production, so merging puts the code where prod reads
it, but the service is still running the old one, and the user restarts it.
Between those moments a feature is on main and doing nothing at all.

**Ship it dark.** A feature with a live blast radius lands inert and is turned
on afterwards, deliberately, by someone looking at the server. Feature Rotation
shipped with its rooms unhidden; GIF Guard shipped with an empty channel list; Flash Themes sits
behind four prod steps; chore auto-sign-off ships `auto_complete` NULL for every
existing row, so a restart alone changes no behaviour whatsoever. Prefer a dial
that defaults to off, a NULL that means "not configured", or a table with no
rows, over a flag day. State in the commit body what the feature does the
instant it is live, and if the honest answer is "nothing until someone picks a
channel", that is the good answer.

The corollary is that **the switch is not yours to flip.** The restart, a
Sync or Post press that publishes to members, a value written into the prod
`config` or `todos` tables, a backfill against live data, each of those is an
action the user takes, or explicitly asks for, even when you are certain it is
right and even when you have already written the script. Prepare it, say
exactly what pressing it will do, and stop. A feature that ships dark makes
that easy; one that ships hot makes it impossible.

Two consequences for how you *describe* the work:

- The `Testing:` lines are written for someone testing the **live** server, so
  they must not assume a state that only exists after a prod step nobody has
  taken. If the feature needs a dial set first, that is a step in the card, not
  an assumption behind it.
- A note saying a branch is unmerged, a dial unset or a table empty records
  what was true **when it was written**. Re-check anything that names a file,
  flag, migration number or branch before you act on it.

---

# Part 6 — Where the rules live

The eleven documents this guide points at, and what each owns.

| Document | Owns |
|---|---|
| `CLAUDE.md` | The working agreement: where config lives, the docs contract, test discipline, the gates, commit shape. Loaded into every session automatically |
| `docs/INDEX.md` | Every spec's classification (Reference / Design / Aspirational) and the rule that the code wins |
| `docs/embed_style_guide.md` | Embeds, panels and the shape of user-facing copy: color, card anatomy, casing, errors, voice, commands, buttons, DM branding, escaping, dashboard JS copy |
| `docs/copy_style_guide.md` | Prose style for everything a person reads (no em dashes in sentences, names to use), the field-hint rule and its test, and the Help guide's own rules |
| `docs/components_v2_style_guide.md` | A card built as a Components V2 `LayoutView`: what changes from the embed guide (mentions ping, no container and no accent bar, Section rows, subtext, the 40/4000 budget, WebP media) and what carries over by pointer |
| `docs/dashboard_ia.md` | Dashboard nav taxonomy, where a feature's settings live, the frozen route ids, panel URL state |
| `docs/data_register.md` | The Art 30 record of processing: every table holding personal data, its retention, purge coverage and Art 17(3) grounds |
| `docs/specs/privacy_spec.md` | Deletion and subject-access export, and `SUBJECT_ID_COLUMNS` |
| `docs/web_testing.md` | The dashboard sweeps and the Playwright browser suite |
| `docs/specs/no_contact_spec.md` | The no-contact list: gated surfaces, disclosure rules, storage |
| `docs/common_modules.md` | The shared seams: which module already owns a job, and which docstring to open. A map, not a rulebook |

Plus the two surfaces that are not dev docs at all:
`src/web_server/static/manual.html` is the user-facing guide rendered in the
dashboard's Help panel, and `README.md` is a landing page for someone
evaluating the bot.

And the newcomer's path, which owns no rule and restates none:
`CONTRIBUTING.md` (how a change lands) → `docs/dev_setup.md` (a development
copy on your own machine) → `docs/codebase_tour.md` (where things are) →
this guide.

---

# Part 7 — When this guide is wrong

- **Code beats specs.** If a spec and `src/` disagree, the code is what runs.
- **The owner doc beats this guide.** Every `→` line is a summary; the target
  is authoritative. A disagreement is a pointer to repair, not a choice.
- **A cross-cutting rule with no home goes in an owner doc**, and gets one
  line here. Writing the rule itself into this file makes it one more place
  rules live, which is the problem this document exists to solve.
- The machine view of this guide is `get_conventions()` on the DK MCP server,
  which serves these sections to claude.ai. It reads this file live, so there
  is nothing to keep in sync, but its topics address this guide's headings
  by name, so **renaming a heading here is a code change**; a test will tell you.
  → `dk_mcp_server.md`
