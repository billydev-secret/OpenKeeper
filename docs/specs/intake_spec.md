# Intake cards (welcome tracker) + procedure reference

**Status:** Reference (built). The build plans were local scaffolding
(`docs/plans/intake-cards.md` is recoverable with
`git show 678a16fa:docs/plans/intake-cards.md`; the two-stage plan is gone).
Two stages + grant steps ship dark.

Two halves, both dashboard-configured (Config → New Members → Intake Cards), no
slash commands:

1. **Intake cards**: on join, a card posts to greeter chat tracking the
   welcome procedure as a checklist; the open cards are the intake queue.
   An optional **Stage 2** card follows when a member's Level 5 promotion is
   granted (§ Two stages).
2. **Procedure reference**: the `#welcome-procedure` content is
   dashboard-edited blocks the bot syncs into a channel, question lists
   rendered **one message per question** for one-tap copy-paste.

## Cards

- **Post:** `on_member_join` (humans only; jailed rejoiners skipped) when
  `intake_enabled` + a channel resolves (`intake_channel_id`, falling back
  to `greeter_chat_channel_id`). The card is a bare Components V2 layout
  (`build_intake_layout`), a paradigm-5 shape
  (facts, a list, the buttons in one row at the foot) rather than
  paradigm 6's row-per-action list, see § Anatomy of
  `docs/components_v2_style_guide.md`. **The frame is the card**: **the
  member's name is the title**, drawn into the games' small lobby header
  strip when the guild has one, and the guild's **footer strip closes the
  card under the buttons**, the way a lobby closes. In a channel of a
  dozen open cards each one ends with art instead of a row of grey buttons
  abutting the next card's strip. Both come from `core.layout.lobby_art`,
  so the footer never draws without the header (a guild with only a hub
  footer sees a bare card, like its lobbies); without art the name is a
  `##` heading and the card is bare. The footer is there because, in a queue
  of a dozen cards, cards without it run into each other.
  The header strip is drawn **in memory and attached**, never cached in the public
  art directory the games' titles use: that directory outlives an erasure,
  and would become a store of member names no purge reaches
  (`bot_modules/panels/title_service.titled_strip_bytes`, a small in-process cache; every
  edit re-attaches it, since `attachments=` replaces what the message
  holds). The footer has no member in it and stays the hosted URL.
  Characters the face can't draw are dropped first
  (`drawable_title`); a name with nothing left keeps the heading and no art.
  Under the title, one text block beside the member's avatar, in the order
  a greeter acts on it: **the event as a `###` line** ("🚪 Just joined" /
  "🌙 Reached Level 5"; the header avoids the internal word "stage"; once
  the card closes **the outcome takes its place**, "🎉 Intake complete —
  welcomed by ‹name›", "🚫 Dismissed by ‹name›", "👋 They left the server",
  "🔨 They were banned", "➡️ Reached Level 5, approved by ‹name›; a new
  card follows", so a scan of the channel reads outcomes and the event line
  goes); `<@&greeter_role_id>` **on its own line** (allow-listed at the
  send, dropped once the card closes; the line a PvP challenge gives its
  target); then a `-#` facts line: username, **how long they have waited**
  ("joined ‹pill›" from the card row's `created_at`; on a Level 5 card
  "approved by ‹approver› ‹pill›", or "promoted ‹pill›" when no approver
  is recorded; the approver sits here, not in the heading, which stays short
  beside the avatar on a phone), account age, and invited-by
  (`invite_edges`), so the thumbnail fills its block. Nothing in the text
  is bold: the drawn name is the card's one loud thing. The facts are plain text with the
  timestamp pills; the username is markdown-escaped. An inviter whose
  account is a bot (a listing bot's link, DISBOARD and its kind) reads
  "via ‹bot›" instead of "invited by ‹name›"; an inviter who has left
  can't be told apart and stays "invited by". There is no
  progress bar: the checklist right under it shows how far along. Then, under a divider and with no heading of its
  own, the ✅/☐/⏭️ checklist lines in **one text block** (done shows
  who/when; `done_by 0` renders "automatically"; a pending grant step names
  its role in brackets). **A line drops to `-#` subtext the moment it is
  history** (done or skipped), and a pending line keeps the body size, so
  what is still owed is the only body text, a card quiets down as the work
  gets done, and a completed card is visibly a receipt; the order is the
  procedure's order, untouched (a dismissed card's never-done lines stay at
  body size: they were never done). The pending glyph is U+2610 BALLOT BOX, because the ⬜
  emoji draws as a flat grey block on the dark theme. Under the block, the buttons in
  rows of five, each naming its step: **Done: ‹label›** / **Undo:
  ‹label›** for a manual step, **Give role: ‹label›** for a grant step,
  with **Dismiss** (no emoji) last in the same row, all grey. A manual
  step that carries a **code** draws its line but **no button in either
  state**: the greeter's canned message ticks it, so the card treats it
  like an auto step, with no Done and no Undo since nothing was pressed. Which steps count as coded is read from the **live** step
  config at each render (`coded_step_keys`, per stage), the same way the
  codes themselves are matched, so giving a step a code takes its button
  off every open card at its next redraw and clearing the code brings it
  back; a card drawn before the edit keeps its old button until then, and
  a press on it changes nothing: the handler asks the live config as well
  (`step_takes_a_press`), redraws the card without the button and tells
  the presser the step ticks from the canned message, so a stale button
  can neither tick nor pay for a step the code owns. (A failed read of the
  coded set at render time is logged and draws the buttons as before; the
  press-time check still holds.) One text block and
  one foot row read as one list (a `Section` row per pressable step would
  give those steps taller, padded rows and a lone button floating at the
  right). A closed card has no buttons. The fullest
  card (20 steps, all pressable, both strips and avatar) is 34 components of
  Discord's 40, so nothing falls back. The buttons are plain ones carrying the persistent ids;
  the `Intake*Button` dynamic items registered at startup answer them, so the
  layout keeps no callbacks. Every edit passes `content=None, embed=None`,
  which converts a card still posted as an embed on its next
  tick. Every member on the card (newcomer, inviter, each
  ticker, the closer, the Stage 2 approver) is a resolved display name
  through one `build_name_fn` per render, never `<@id>`: the card outlives
  departures, and a mention shows digits to a greeter who never saw that
  user. A completion nobody manually ticked credits "the checklist". The legacy bare `@here — has arrived` ping is
  suppressed only when a card surface actually exists: the join variant
  falls back to the ping whenever the card could not post (channel
  missing/send failed, so `post_intake_card` returns False), and the
  verified-trigger variant suppresses per-member (`is_watched`, i.e. an
  open card), not per-guild. While dark, join behavior is unchanged.
- **Steps** are snapshotted onto the card at creation from `intake_steps`
  (JSON; invalid entries drop, empty/invalid falls back to the default
  six-step list, whose two role steps are **manual** until real roles are
  configured, since an unconfigured `role_gained` step could never tick).
  Config edits never mutate in-flight cards' step rows. The step codes
  are the one live-read exception: which manual steps have a button, and
  whether a press on one counts, follow the saved config, not the
  snapshot (see § Cards and § Step codes). Kinds: manual (persistent
  toggle button, greeters + mods, first ticker preserved on races; a
  manual step with a code has no button),
  `greeted` (a greeter **or mod** @mentions/replies to the newcomer in the
  **greeting channel**: `greeter_chat_channel_id`, else
  `welcome_channel_id`; 0 means nothing greets), `verified` (verification
  signalled: `unverified_role_id` removed **or**
  `intake_verified_role_id` gained), `role_gained` (member
  gains the step's configured role via `/grant`, a manual add, or a **Give
  role** press on the card for that same role; `role_id 0`
  never ticks, and the dashboard refuses to store it), `grant` (a **Grant
  ‹role›** button on the card gives the member the step's role; see
  § Grant steps). Step keys are
  normalized to `[\w-]` and capped at 60 chars on save (leaving room for a
  numeric dedupe suffix) so persistent-button custom_ids always fullmatch the
  dispatch template's 64-char limit after a restart.
- **Where greeting is watched** is deliberately *not* the card channel.
  Cards post to a greeter-facing noticeboard; the greeting itself happens
  where the newcomer landed. Watching `intake_channel_id` would mean `greeted`
  could never tick in practice (in prod every greeting is in
  #welcome-chat while cards post to the tracker channel), so
  `greet_channel_id()` resolves `greeter_chat_channel_id` first (a room
  dedicated to handling arrivals), then `welcome_channel_id`. Mods count
  alongside greeters: the greeter role gates nothing else here either.
- **Verification wears two shapes** and both signal the step
  (`verification_signalled()`): our own gate *strips* `unverified_role_id`,
  while a third-party verifier (Double Counter) *grants* a role instead:
  `intake_verified_role_id`, admin-only, 0 = unset. Watching only the
  removal alone would leave `verified` dead on guilds using the second shape, which is
  every guild whose verification isn't ours. A verified role that is also
  a `role_gained` step's role ticks both steps; that's intended.
- **Step codes:** each step may carry its own free-text `code`. A
  greeter/mod message in **any channel** containing it, addressed to the
  newcomer, ticks **that step only**; the card stays open. The point is
  that each canned message in the procedure reference can carry the code
  for the step it corresponds to, so pasting the message ticks the step
  without anyone touching the card. One message may tick several steps;
  an empty code never matches. A coded step is the card's automatic step:
  it draws no Done or Undo button (§ Cards), so the message is the only
  way it ticks, and a paste addressed to the wrong newcomer can't be
  unticked from the card (clearing the step's code on the dashboard brings
  the Undo button back to an open card at its next redraw; there is no
  periodic redraw, only ticks and presses). Codes are matched against the **live**
  config rather than the card's snapshot (a code is a lookup phrase, not
  per-card state, so fixing a typo doesn't strand in-flight cards). A
  code whose step isn't on this card simply ticks nothing. Matching is
  case-insensitive containment, so a save where one code contains another
  (or contains the completion code) is rejected: it would silently fire
  both, and a completion code swallowed by a step code is worse than a
  double-tick: pasting that step's message would close the card and stamp
  the rest skipped. Step codes and the completion code are writable
  independently, so `intake_service.config_code_conflict` (which reads
  whichever side the caller isn't overriding) is enforced at **both**
  writers: the dashboard route, and `bot_modules/advisor/actions.validate_config_change`
  for the advisor, which writes the key straight through
  `set_config_value`.
- **Completion:** a greeter/mod message in **any channel** containing
  `intake_completion_code` and addressed to the newcomer. Wins over step
  codes (the card is closing anyway). Unticked steps are stamped
  **skipped** (code always wins), the poster becomes the welcomer of
  record, the card flips to "🎉 Intake complete", 🎉 reaction on the
  trigger message. Empty code = detection off.
- **"Addressed to the newcomer"** means an @mention **or a reply to one of
  their messages**. Discord only lists the reply target in
  `message.mentions` when the reply pings, so both the `on_message`
  pre-filter and `evaluate_message`'s mention set fold in
  `intake_service.reply_target_id`; otherwise a canned reply with its ping
  off is dropped before intake ever evaluates it. Applies to greets and
  both kinds of code alike.
- **Auto-completion:** a card closes itself the moment its **last step
  ticks** (`intake_service.autocomplete_if_finished`, called from every tick
  path: button, step code, greet auto-tick, role-gain auto-tick). A fully
  ticked checklist *is* a finished intake; if only a posted completion code
  closed a card, finished intakes would age in the queue indefinitely and the
  completion-keyed report panels would stay empty. Nothing is stamped skipped, since every step was genuinely done, and
  the card closes at the **last step's own tick time**, not the wall clock,
  so time-to-complete stays honest for a card the sweep picks up late. The
  welcomer of record is whoever ticked the final step, falling back to the
  last human to tick anything on the card when an auto step lands last (an
  automation closing a card must not drop it out of the per-welcomer
  counts); 0 when no human ever ticked. The completion code is still what
  closes a **part-done** card.
- **Finished-card sweep:** the nudge loop's tick first closes any open card
  that is already fully ticked (`intake_views.close_finished_cards` over
  `intake_service.finished_open_cards`), the backstop for cards finished
  while the bot was down. A finished card is therefore never nudged.
- **Close paths:** completion (the code, or the last step ticking), mod-only
  Dismiss button, member leave (`left`) or ban (`banned`; the ban hook
  closes first so the remove hook finds nothing). **No expiry**: cards
  otherwise stay open; the queue is always the truth.
- **Payouts:** ticking a step pays the greeter through the `intake_step`
  income source (`bot_modules/economy/intake_rewards.pay_intake_steps`, called from all
  three human tick paths (the `greeted` auto-tick, a posted step code, and
  the manual toggle button, plus a grant-step press), riding the tick's own transaction). Per *step*, not per completed card: completion credits
  whoever posted the code, so a per-card award would miss shared and
  half-finished intakes. Only ticks with a real actor pay: `verified` and
  `role_gained` record `AUTO_ACTOR` (0) and credit nobody, as do skipped
  steps. Dedup is `econ_intake_rewards (guild_id, card_id, step_key)`
  (migration 138), **not** the step's `done_at`/`done_by`, which the toggle
  button clears, so unticking and re-ticking mints nothing and a second
  greeter cannot re-claim a ticked step. Each award is savepointed so an
  economy failure can never roll back the tick. See economy_spec.md §4.5.
- **Stale nudge:** `intake_loop` (10-min tick) replies once under any open
  card with no step progress for `intake_stale_hours` (default 24; any
  tick resets the clock), pinging the greeter role; `nudged_at` stamps
  even on send failure so it can't re-nudge.
- **Screening-aware:** a stale card is checked against the member's actual
  state before the ping (`intake_service.nudge_action`, presence resolved
  from the member cache with a `fetch_member` fallback). A member still in
  Discord's membership screening (`Member.pending`) holds no roles and can't
  be greeted, so the card is **skipped unstamped**; it pings later, once
  accepting makes greeting possible. A member who has left (an explicit 404,
  never a transient error) had their leave missed by `on_member_remove`, so
  the sweep closes the orphan card as `left` instead of pinging. Anything
  unresolvable is skipped unstamped and re-decided next tick.
- **Hot path:** `on_message` pre-filters via an O(1) watch set of members
  with open cards (`intake_service.is_watched`, seeded at startup, same
  pattern as promotion review); decisions live in
  `intake_service.evaluate_message`. The warm seed covers open cards in
  **all** guilds, enabled or not, so cards survive a disable → restart →
  enable cycle; hook calls in `bot_modules/events/cog.py` are individually guarded so an
  intake failure can never abort spoiler enforcement, persistence, or the
  leave announcement.

## Two stages

The procedure runs in two passes: on join a greeter says
hello, asks how they found their way in, grants main chat and runs the SFW
questions; once the member reaches Level 5 and a role manager presses
**Grant** on the Level 5 promotion card, a greeter runs a second pass,
granting NSFW access and pastes the NSFW questions.

- **Stage** is a column on the card (`intake_cards.stage`, migration 245;
  1 = join, 2 = Level 5). Every pre-existing card is stage 1.
- **Stage 2 opens only from the Level 5 Grant button**
  (`bot_modules/xp/promotion_review_views._grant_role_only` → `intake_views.post_stage2_card`,
  after a successful grant). A role given by hand or through `/grant` opens
  nothing: the press is the approval event, and a hand-added role has no
  approver to name. `post_stage2_card` never raises, so a failure can't turn
  a granted press into an error.
- **The Level 5 press approves; greeters grant** (Ben, 2026-10-01). While
  Stage 2 is on, the mod's Level 5 button (labelled *Approve for Stage 2*)
  **adds no role**: it opens the Stage 2 card, and NSFW is unlocked only from
  that card's built-in *NSFW access* line. If the card can't open, nothing is
  granted. With Stage 2 off the button adds
  `promotion_review_grant_role_id` itself, as it always did, so it keeps
  working on a guild that doesn't run intake.
  `tests/xp/test_promotion_review_views.py::test_level_5_press_grants_only_while_stage2_is_off`
  fails if the press grants while Stage 2 is on.
- **The press says what Stage 2 did** (2026-09-30). `post_stage2_card`
  returns a `STAGE2_*` outcome and the reply names it
  (`intake_service.stage2_press_reply`): approved and opened; or *Nothing
  granted* and why: already open, no opt-in role, the opt-in role or card
  channel gone, no grant role to put on the card (`no_grant`, checked before
  anything advances), or the post failed. Before this the skip was silent:
  the first Level 5 Grant after go-live gave access, opened nothing, and only
  "Access granted." said anything. A skip for want of the opt-in is also
  logged at info.
- **Two built-in lines lead every Stage 2 card**
  (`intake_service.stage2_lead_steps`), never on the dashboard:
  *Promotion approved* (`auto_kind` `approved`), stored ticked by the
  approving mod when the card is created (no button, no undo, and it pays
  nobody because payouts follow presses); then *NSFW access*, a Give role
  step for the live `promotion_review_grant_role_id`. The NSFW line is left
  off when that role is unset or deleted, or when the configured list already
  grants the same role, so the card never shows it twice. Like any grant step
  it ticks itself when the role lands by other means, and a press for a
  member who already holds the role just ticks it.
- **Dark by default:** `intake_stage2_enabled` (off) **and** `intake_enabled`
  must both be on. While either is off the Level 5 Grant opens no card.
- **Opt-in role:** `intake_stage2_optin_role_id` limits
  Stage 2 to members who hold that role (the NSFW opt-in). Unset (0) opens
  Stage 2 for every Grant press. It is checked **at the Grant
  press only** (`intake_service.stage2_optin_verdict`); a member who opts in
  later gets no card until a later press. Without the role nothing is
  granted (since 2026-10-01 the press never grants while Stage 2 is on), no
  Stage 2 card posts, no greeter is pinged, and the join card stays open
  where it was. A configured role that **no longer exists
  fails closed**: no Stage 2 card opens for anyone and a warning is logged
  until the dial is reset, so a deleted role can't silently widen Stage 2 to
  every member.
- **The Level 5 card itself waits for the same role** (Ben, 2026-09-30).
  A member without it gets no promotion card, ping or Grant button at Level 5;
  picking the role later posts it. See
  [promotion_review_spec.md](promotion_review_spec.md) § Level 5 card and the
  NSFW opt-in. So a not-opted-in press is now rare: a card posted before
  that change, or a member who dropped the role after it posted.
- **Stage 2 steps** are their own list, `intake_stage2_steps`, following the
  two built-in lines and defaulting to one manual step, *Questions finished*,
  until an admin gives it a closing code (the NSFW grant is built in since
  2026-10-01, so the default no longer carries an *NSFW granted* step). Channel, greeter role,
  completion code and stale-nudge hours are shared with Stage 1.
- **Advancing closes the join card.** If the member's Stage 1 card is still
  open it resolves as `advanced`, unticked steps stamped skipped, the Level 5
  approver as `resolved_by`, and it re-renders "➡️ Reached Level 5,
  approved by ‹approver›; a new card follows".
  So a member never holds two open cards: the one-open-card index, the watch
  set, leave/ban closing and every `get_open_card` caller are unchanged. An
  `advanced` card is not a completion, so the Welcomers panel doesn't count it.
- **Dedupe:** a second Grant press while a Stage 2 card is open opens
  nothing (`advance_to_stage2` returns `(None, None)`). The channel is
  checked **before** advancing, so a missing channel leaves the join card
  open rather than closing it with nowhere to post the next one.
  A send that fails *after* advancing rolls the Stage 2 row back like any
  failed card post; the member is left with no open card, and pressing Grant
  again opens Stage 2 afresh.
- **The card** posts to the card channel pinging the greeter role, titled
  with the member's name like the join card, its header line reading
  "🌙 Reached Level 5, approved by ‹approver›". The approver is stored on the card (`intake_cards.opened_by`,
  migration 245; 0 on a join card) because a V2 card is rebuilt whole on
  every edit, and the header has to name them each time.
- **Codes across stages:** a pasted message is matched against the card it
  addresses using **that card's stage's** live step list, and
  `config_code_conflict` checks Stage 1 codes, Stage 2 codes and the
  completion code against one another (a Stage 2 label shows as
  "Stage 2: …" in the refusal).

## Grant steps

A `grant` step carries a role; its line reads "☐ ‹label› (‹role name›)" and
its button at the foot reads **Give role: ‹label›** in place of the Done
toggle (a deleted role drops the name; a ticked or role-less grant step has
no button).

- **Who:** greeters and mods, the same gate as the manual toggle. This is a
  new privilege for greeters, hence the rest of this list.
- **What:** only the step's own configured role, never any other dial.
- **Safety:** `core.role_safety.role_block_reason` at press time, and
  `refuse_ungrantable_role` at save for every grant step in either list, so
  a staff-level role, a role above the bot, a managed role or @everyone is
  refused. The dashboard also refuses a grant step without a role.
- **Role Required First:** the press honours the role's `/grant`
  prerequisite (`grant_roles.required_role_id`), asked at press time through
  `role_grant_logic.role_prerequisite_verdict`, the gate `/grant` and the
  role menus already run ([role_grant_spec.md](role_grant_spec.md)). The
  production case is main chat behind verification: **Give role** on *Main
  chat* refuses until the member holds the verified role.
  - A member without the required role: "They need **‹required›** before
    they can be given **‹role›**. Press again once they have it."
  - A required role that no longer exists **fails closed**: "This grant is
    misconfigured — the role it requires first no longer exists. Ask an admin
    to fix it on the dashboard.", and a warning is logged.
  - Admins override; greeters and mods don't.
  - A member who **already holds the step's role** isn't checked. That press
    hands out nothing and only ticks the row, the same ordering `/grant`
    keeps for "already has the role".
  - The grant's *Who Can Hand This Out* list is **not** consulted. The card's
    own gate is greeters and mods, and which role a step gives is an admin's
    choice on the dashboard.

  Without this check a greeter could put a newcomer who never verified into
  main chat. `tests/onboarding/test_intake_views.py` holds the press to
  it; the verdicts are tabled in `tests/roles/test_role_grant_logic.py`.
- **Audit:** every successful press writes `audit_log`
  `action = "intake_grant"` (presser as actor, member as target, role id,
  role name and step key in `extra`); `intake_grant` itself is listed in
  `MODERATION_ACTION_PREFIXES`, and the Audit Log labels it **Intake Grant**.
- **The tick:** the presser is credited and paid (`intake_step`), even if the
  gateway's `role_gained` auto-tick landed first, since `claim_grant_tick` writes
  over a `done_by 0` tick but never over a human one. A role given by hand
  ticks the grant step "automatically" through the `role_gained` path and pays
  nobody. Refusal paths (card closed, step done, member gone, role deleted,
  unsafe role, required role missing or deleted, Discord refusing) answer
  ephemerally and change nothing.
- **No announcement.** The press adds the role, audits, credits and pays,
  and posts nothing to any channel: a grant's announcement
  (`grant_roles.grant_message`, sent to its announce channel with its
  pings) comes from the `/grant` command alone
  (`bot_modules/roles/grant/role_grant_commands._execute_grant`). Where that announcement *is* the
  welcome, the step stays `role_gained` and greeters keep running
  `/grant` (§ Main guild go-live). Teaching the press to announce is a
  possible later change, not built; `tests/onboarding/test_intake_views.py` pins
  the press silent with an announce channel and message configured.
- **Rows waiting on the same role tick with the press.** Two steps may name
  one role, and the dashboard stores that. A `role_gained` step naming the
  role a button just gave is ticked in the press's own transaction
  (`auto_tick`, the call the role-update hook makes), as automatic:
  `done_by 0`, nobody credited, nothing paid. The hook alone isn't enough.
  Discord sends no role update for a member who already held the role, and
  an automatic row has no button, so that card could never finish. A
  join list can lean on this: *Found their way in* waiting on the
  main-chat role above a *Main chat* grant step, so one press ticks both
  and pays for one. The main guild keeps both rows
  automatic instead (the *No announcement* bullet above). Tests:
  `tests/onboarding/test_intake_views.py` (the press, a member who already holds the
  role, a refused press), `tests/onboarding/test_intake_logic.py` (one role event
  ticks both rows, the claim credits the pressed one) and
  `tests/web/test_config_routes.py` (the Save accepts the pair).

## Main guild go-live

How the main guild is configured:

- **Greeters give main chat with `/grant`, not the card.** The main guild's `denizen` grant (the Member role)
  requires Verified, may be run by mods and greeters, and posts the welcome
  into main chat with an @here. A **Give role** press posts no announcement
  (§ Grant steps), so *Main chat* stays a `role_gained` step and the join
  card carries no button that skips the welcome.
- **Join card, five rows:** Greeted (`greeted`), Verified (`verified`),
  Found their way in (`role_gained`, Member role, no code), Main chat
  (`role_gained`, Member role), SFW
  questions finished (manual, `dksfw`).
  Both Member-role rows tick together, as automatic, when `/grant` lands; a full
  welcome pays three rows (Greeted, SFW questions finished, then Questions
  finished on the second card), the join card two.
- **Stage 2:** on, opt-in role = the role Discord's onboarding gives
  members who answer yes to its NSFW question, one
  manual step *Questions finished* (`dkint`). A code sits in only one list,
  so it is in the Stage 2 list, not the join list.
- **Procedure reference, thirteen blocks** in posting order: the intro ·
  *Welcome workflow* (the two-stage summary, with channel links) ·
  *Stage 1: on join* (the tracker section, naming the bot's welcome,
  `/grant` and five questions) · Example Greeting · ask how they got
  here · *Give them main chat* (above the SFW list) · Example introduction
  (optional) · the SFW list ("…five questions?") · Example SFW Complete
  message (the welcome leads; ends `||dksfw||`) · *Stage 2: after Level 5*
  (the round runs in the spicy channel) · Example spicy opener · NSFW
  question bank · example closing and welcome (the welcome leads; ends
  `||dkint||`). 108 messages.
- **Order of changes when reconfiguring:** the Intake Cards Save before the
  Procedure reference Save (which publishes the channel at once). The channel
  changing before the steps would give anyone welcomed in between the SFW
  round only, and never the second.
- **The bot's welcome** is on in prod config
  (welcome channel = the newbie chat, ping the member, no ping role, a
  three-paragraph message promising a hello, the way-in question, an
  introduction and a five-question icebreaker). Greeters are pinged by the intake card only. The
  ping-role dial must hold `0`: a deleted row falls back to the legacy
  guild-0 row, which still names Greeters. A bot-authored welcome returns
  from `on_message` before the intake hook, so it can never tick Greeted.
- A card open before a step list is saved keeps its old rows; once a code
  moves lists, the old closing message no longer ticks that row, which then
  needs its Done button.

## Ledger

`intake_cards` + `intake_card_steps` (migration 115). One open card per
(guild, member) via partial unique index; resolving records
`resolved_by`/`resolution` (`completed`/`dismissed`/`left`/`banned`/`advanced`).
`stage` (migration 245, default 1) says which pass the card tracks;
`opened_by` (same migration, default 0) is the Level 5 approver on a Stage 2
card.
Steps carry `done_at`/`done_by`/`skipped`: no message content, no answers
(the question lists stay conversational by design). `econ_intake_rewards`
(migration 138) anchors the per-step payout: one row per (guild, card, step),
`user_id`/`amount` recorded for attribution only; the PK excludes them so a
re-tick by anyone pays nothing.

## Procedure reference

Blocks (`intake_reference_blocks` config JSON: `text` | `questions`, title,
body) render to messages. A block's **title is always its own bold
message**: Discord's Copy Text takes the whole message, and most text
blocks are canned messages a greeter copy-pastes, so a heading sharing the
message means trimming it off every paste. Below the heading: text chunks
on line boundaries under the 2000-char cap (rejoined with the newline they
were split on, so the channel matches the editor); questions one message
per line, and a question longer than the cap is rejected on save rather
than 400-ing mid-sync. Either half is omitted when empty (a title-only
block is a bare section heading). Sync
(`intake_reference_service.sync_channel`, run inline on dashboard save) is
a position-wise diff against `intake_reference_messages` (migration 116):
unchanged kept, changed edited in place (ids/links stable), tail posted,
surplus deleted. Only tracked messages are ever touched.

**Hand-deleted messages.** Each sync first sweeps the channel history once
(`after` the oldest tracked message, stopping as soon as every tracked id
is accounted for, normally inside one request versus a fetch per
position) to learn which tracked ids still exist. Without that check an
unchanged config hashes `keep` at every position and the sync makes **no
Discord calls at all**, so a deleted message would stay gone
however often you saved. Discord can't insert into the middle of a channel,
so the diff rebuilds from the *first* gap onward (re-send + delete the
stale copies) to preserve reading order; positions above the gap keep their
ids. A gap at or past the rendered range is ignored: that message is
surplus the delete pass removes anyway. `repaired` counts replacements that
actually landed, not ones merely planned.

A sweep Discord refuses, or one that hits the 500-message backstop, assumes
the unseen ids are **present** (guessing would delete and re-send a stretch
of live messages) and returns `incomplete: true` so the panel doesn't
report a clean sync (a deletion in the unread tail is simply not noticed
that pass).

A Discord failure mid-plan keeps the affected message tracked under its
**old** hash (so the next save retries it rather than believing it already
synced) and also sets `incomplete`. Note the delete pass still deletes ids
that a failed rebuild left in the mapping, and that pairing is what makes
the next save finish the job: the following sweep sees them missing and
rebuilds from there. Skipping those deletes instead leaves stale copies
whose hashes still match, so the next sync finds nothing to do and the
channel reads out of order permanently. One-time import
(`POST /config/intake/reference/import`) drafts text blocks from a
channel's history, oldest first; refuses a non-empty editor.

## Dashboard

- Config: `PUT /config/intake` (strict validation; steps ≤ 20 per stage, keys
  slugged + deduped, stable across re-saves; `stage2_enabled` +
  `stage2_steps` alongside `steps`), `PUT
  /config/intake/reference`, import endpoint. Panel `intake-settings.js`.
- Analytics: `GET /reports/intake-report` → Reports → Greeter → **Intake
  Queue** (`intake-report.js`): open queue oldest-first with progress and
  pending steps and a Stage column, outcome counts (including how many moved on to Stage 2) + median/mean time-to-complete,
  per-welcomer completions and manual ticks (auto-ticks never credited),
  per-step skip rates on completed cards. The queue holds only genuinely
  open cards: a finished one closes itself rather than needing filtering
  out of the report.

## Erasure

A member's **own card** and its steps are deleted by `purge_user_data`.
The card is a record about them (when they arrived,
who greeted them, how far the checklist got), and no Art 17(3) ground holds a
welcome checklist. `intake_card_steps` has no `guild_id`, so the purge
resolves the card ids and deletes the steps by id first: its connection makes
no `PRAGMA foreign_keys` promise, so the FK cascade this feature's own store
relies on cannot be assumed there.

What **survives** is the member's id as a *greeter* on somebody else's card:
`intake_cards.resolved_by`, `intake_cards.opened_by` and
`intake_card_steps.done_by`. That is the other
member's welcome record and the audit of who admitted them (Art 17(3)(e)), so
an erased greeter still shows as the person who ticked the step. Register row:
`docs/data_register.md`, `intake_cards`.

## Code

| Piece | File |
|---|---|
| Ledger + gating + reports (unit under test) | `bot_modules/onboarding/intake/service.py` |
| V2 card layout + persistent buttons + hook handlers | `bot_modules/onboarding/intake/views.py` |
| Stale-nudge loop | `bot_modules/onboarding/intake/loop.py` |
| Reference blocks/render/diff/sync/import | `bot_modules/onboarding/intake/reference_service.py` |
| Level 5 → Stage 2 hook | `bot_modules/xp/promotion_review_views.py` (`_grant_role_only`) |
| Hooks | `bot_modules/events/cog.py` (join/remove/ban/member-update/message), `dungeonkeeper/__main__.py` (warm + `add_dynamic_items` + loop) |
| Routes | `web_server/routes/config.py`, `web_server/routes/reports.py` |
| Panels | `static/js/panels/intake-report.js` (queue, Reports → Greeter) + `intake-settings.js` (Intake Cards config, Config → New Members), separate pages |
| Tests | `tests/onboarding/test_intake_logic.py`, `test_intake_views.py`, `test_intake_reference_logic.py`, `tests/web/test_config_routes.py`, `test_reports_routes.py` |

See also: [greeting_watch_spec.md](greeting_watch_spec.md),
[role_grant_spec.md](role_grant_spec.md),
[promotion_review_spec.md](promotion_review_spec.md) (the display idiom the
cards mirror), [auto_role_spec.md](auto_role_spec.md).
