# Chat Revive — Product Spec (Dungeon Keeper)

**Codename:** Ember. It stirs the coals when the hearth goes quiet.

## What it is

Chat Revive notices when a channel has gone unusually quiet *for that channel, at that time of day* and gently restarts conversation by posting a question from a curated bank, optionally tagging an opt-in "chat revive" role of members who've said "yes, wake me when the room needs a spark."

It is not a timer bot. It never posts on a schedule, never spams, and never talks over an active room. Done right, members shouldn't think of it as a bot feature at all. It should feel like someone tossed a good question into a lull.

## Product principles

1. **It knows the room's rhythm.** A channel that's always sleepy at 3am should never get poked at 3am. A channel that normally buzzes at 8pm and has been silent for 90 minutes is a real lull worth catching. "Quiet" is always relative to that channel's own history.
2. **It never talks to itself.** If the last message in the channel is the bot's own revive, it stays silent until humans have spoken again. No chains, ever.
3. **Rare is powerful.** A revive that shows up a few times a week feels like a treat; one that shows up daily is noise people learn to ignore. Frequency limits are the heart of the product, not a setting buried at the bottom.
4. **Pings are earned, opt-in, and scarce.** Only members who took the chat-revive role get tagged, and and never beyond the per-channel cap (three tags a local day, an hour apart, unless an admin changes it).
5. **It learns what works.** Every revive quietly measures whether conversation actually followed. Questions that spark chat come around more often; duds fade out on their own.

## Member experience

A quiet Tuesday evening in #general, normally lively at this hour. After a long unusual lull, this appears:

> 🔥 *stirring the coals…* @chat-revive
> *[a banner card: the question set over the server icon, headed "Ember"]*

That's the whole footprint. A quote-renderer card plus its one-line caption: no embed, no buttons, no follow-up nudge. If it lands, people answer each other, not the bot. If it doesn't land, the bot doesn't try again for hours.

Details of the moment:

- The question rides on the card; the ping and flourish ride in the message text beside it, because a role mention can't live inside an image. With neither, the card posts bare.
- The card is the same renderer behind `/banner` and the game launch cards (midnight theme, server icon as the graded background), so a revive reads as the same object as the rest of the server's cards. Its heading is the persona, "Ember", not the feature name: a revive works better as an organic nudge than as a bot announcing its own machinery.
- **Plain text is the fallback, not a mode:** a guild with no icon, an unreadable icon, a question longer than the card's 280-character limit, or any renderer failure posts the original one-line text form (`🔥 *flourish* @role question`) instead. The ping fires either way; the question is never silently trimmed.
- The little flourish line ("stirring the coals…") rotates and can be turned off per guild for a bone-dry delivery.
- The role tag appears only if that channel has pinging enabled *and* the channel is under both ping limits (by default three tags per local day, at least an hour apart). Otherwise the question posts un-pinged.
- Members join or leave the chat-revive role themselves through the existing self-role flow. Taking the role means "I like being summoned to restart conversation". It should be pitched that way wherever roles are advertised.
- Questions never repeat within a month, and spicier questions only ever appear in channels marked for them. Each channel can have its own flavor mix: #deep-thoughts pulls from reflective prompts, #shitposting pulls from silly ones.

## When it fires (and when it refuses to)

**Fires when all of these are true:**

- The current silence is genuinely unusual for this channel at this time of day: longer than the gap that normally sits *between conversations* here, not just between two messages. (A busy channel where people trade replies every minute has to go quiet for tens of minutes, not four, before a conversation counts as over.)
- The channel is normally alive right now. If this hour is typically dead for this channel, nothing happens.
- Humans have spoken since the last revive here.
- All the frequency protections below are clear.

**Refuses to fire when any of these hold (defaults, all adjustable):**

| Protection | Default behavior |
|---|---|
| Channel rest period | Once a revive fires in a channel, that channel rests 8 hours |
| Guild daily budget | No more than 3 revives per day across the whole server |
| Guild breathing room | At least 90 minutes between revives anywhere in the server |
| Quiet hours | Nothing fires overnight (midnight–8am server time by default) |
| Ping scarcity | Role tagged at most 3 times per channel per local day, at least 60 minutes apart |
| Mods slowed the room | If a channel is in slowmode, the bot assumes that's intentional and stays out |
| Something's happening | *Temporarily disabled* (see implementation notes below); this protection is off for now; the decision runs on channel silence history alone |
| Not invited | Revive only operates in channels an admin explicitly enabled |

**When it can't fill a lull it has confirmed** (Post questions mode), either because no question in the bank is eligible for that channel (checked against the channel's newest message first, so a lull someone has just broken pings nobody) or because Discord refuses the post, it tells the moderators who opted into `@Staff · Chat Revive` on the Mod Settings panel, in the staff ping channel, at most once every three hours per server ([staff_pings_spec.md](staff_pings_spec.md), 2026-09-28). The protections run *before* the silence check, so a lull during quiet hours, after the daily budget or inside a rest period is never confirmed and pings nobody.

## What a lull does (the mode dial)

One dial on the Settings tab, stored as `revive_guild_config.mode` (migration 251, which replaced the old `enabled` column):

| Mode | A confirmed lull… |
|---|---|
| **Post questions** (`post`) | posts a question card, as described above. The default for every server. |
| **Ping moderators only** (`ping_staff`) | posts nothing in the channel. It mentions `@Staff · Chat Revive` in the staff ping channel with "#channel has gone quiet — a good moment to start something." It is just the lull, with no question (Ben's call). |
| **Off** (`off`) | nothing: no lull is looked for. |

Lull detection is identical in both running modes: the same protections, the same rhythm model, the same re-check of the channel's newest message right before acting. Only the outcome routes differently. Details of ping-moderators mode:

- **Paced exactly like posts.** A ping that goes out is written to `revive_events` as `trigger_kind = 'staff'` (no question, no message), so the channel's rest period, the guild's breathing room, the daily budget and the "humans spoke since" rule all hold the next ping back the way they would hold the next post. The staff pings' own three-hour cooldown does **not** apply to these: it would silently override the rest/gap/budget dials this page shows. They run on a clock of their own (`staff_ping_service.REVIVE_LULL_LANE`), which (a) never holds back a later can't-fill ping after switching back to *Post questions*, and (b) allows at most one lull ping per 30 minutes per server. That 30 minutes is a backstop, not pacing: the ping posts before its lull is recorded, so a failed record would otherwise leave the lull standing and re-ping every two-minute tick (both found in review, 2026-09-30).
- **Recorded only when it posts.** No staff ping channel, nobody holding `@Staff · Chat Revive`, or Discord refusing the post means nothing is recorded, and the lull is looked at again next tick.
- **It is no revive.** Staff events are excluded from the scoreboard and from the 30-minute follow-up measure, which also pays the "answer a chat-revive prompt" quest, and there was no prompt to answer. The bank is never drawn from, so no question's use count moves, and the first save in this mode doesn't seed the starter pack.
- **Member ping settings are inert.** The opt-in role, per-channel "ping" ticks and flourish only shape a posted card; switching back to *Post questions* picks them up again.
- The dashboard's **Check** says "would ping the moderators" in this mode; **Fire** still posts a question on demand in either running mode, since a manual press is a deliberate choice, not a lull.
- An unreadable channel counts toward the same five-failure pause as it does when posting.

**New channels / new installs:** for the first couple of weeks, before the bot has learned a channel's rhythm, it runs in a conservative fallback mode: only firing after a long fixed silence, and only during daytime/evening hours. It quietly graduates to rhythm-aware behavior once it knows the room.

## The question bank

- Guild-owned and curated. Ships with a starter pack of ~60 questions across categories (general, deep, silly, spicy, photo, music, …) so it's useful the moment it's turned on.
- Admins and trusted members can add questions one at a time or in bulk, tag them by category, mark adult-only ones (which can only ever surface in NSFW channels), and retire anything that's gone stale.
- Every question carries a track record: how often it's been used and how much conversation it tends to generate. The picker favors proven sparkers and lets flops drift to the bottom; the bank self-tunes without anyone doing gardening.
- Attribution is kept (who contributed each question), opening the door to a light "your question revived chat" moment later if we want it.

## Admin experience

**First run:** a guided setup. Pick the revive role (or create one), choose which channels are eligible and what flavor of questions each gets, confirm quiet hours and the daily budget. Five minutes, done.

**Day to day:** nothing. The steady state is that admins forget it exists and members occasionally enjoy a well-timed question.

**When they do check in, they can:**

- **Preview the brain.** A "would it fire right now?" check for any channel that explains, in plain language, the current lull, what the channel's normal rhythm looks like, which protection (if any) is holding it back, and which question it would choose. This is the trust-builder: admins can see exactly why it's quiet or why it spoke.
- **Fire it manually.** Post a revive on demand in any channel (respecting question selection but skipping the lull detection), handy for kicking off an evening.
- **Tune the dials.** Everything in the protections table, plus per-channel overrides: rest period, question categories, ping on/off, a different role for a specific channel.
- **Review the scoreboard.** A stats view answering: how often are we reviving, how often does it actually work (did people start talking within the half hour?), which channels benefit most, and which questions are carrying the team vs. dead weight.

## How we'll know it's working

- **Revive success rate:** share of revives followed by real member conversation within 30 minutes. This is the headline metric; the feature exists to make this number high while firing rarely.
- **Scarcity held:** revives per channel per week stays low (roughly ≤3) without admins intervening.
- **Role health:** chat-revive role retention: people keep the role rather than shedding it, the clearest signal pings feel like an invitation rather than an interruption.
- **Zero embarrassments:** no double-posts, no overnight pings, no reviving a room mid-conversation. One violation of principle 2 or 4 costs more trust than fifty good revives earn.

## Explicitly out of scope for v1

- Web panel management screen: built. Bank and settings live in the Config → Channels & Messages → Chat Revive panel and there is no `/revive` command group (see the implementation notes below).
- AI-generated or context-aware questions: v1 is bank-only so tone stays fully in the community's control
- Per-member ping preferences beyond the simple opt-in role
- Anything that replies to, reacts to, or follows up on its own revive

## Implementation notes (v1, as built)

- **Rhythm model (session-gap):** 2-hour local bands learned from the last
  60 days of `processed_messages` (human messages only). Within each band the
  message stream is segmented into conversations: a silence longer than
  `SESSION_GAP_SECONDS` (10 min) ends one, and the band's fire threshold is a
  high quantile (`INTERSESSION_QUANTILE`, p85) of the *between-conversation*
  gaps, floored by `MIN_LULL_SECONDS` (15 min). On the busiest channels p85
  lands the auto-fire cadence at ~1.3/day with a typical ~40-min lull.
  The quantile was tuned against production history. On the busiest
  channel's evening band the old inter-message rule crossed its ~4-minute
  threshold 3,950 times in 60 days, against 145 for the session model at p90.
  p90 gave ~0.9 revives/day at a ~50-minute typical lull; p85 gives 0.9–1.3/day
  at ~40 minutes with the earliest fire still ~30 minutes; p80 overshot to
  1.4–1.6/day at a ~31-minute lull, which read as twitchy. A very quiet band
  can learn a threshold of a day or more and so never fires, and that
  self-suppression is deliberate. A per-band threshold cap and a data-driven
  session boundary (instead of the fixed 10 minutes) were deferred until live
  data shows a need.
  Auto-fire needs silence ≥
  `threshold × patience` for the current band **and** the band to be normally
  alive (≥ 20% of the channel's busiest band, min 5 msgs/day). Bands with
  < 8 sampled conversation gaps fall back to the whole-day profile.
  *Why not inter-message gaps:* chat is bursty, so the median inter-message gap
  is seconds and a small multiple of it fired within a few minutes on lively
  channels. Modeling the silence *between* conversations, not *between*
  messages, is what "a real lull" actually means. The **Patience ×** dial
  (per channel, default 1.0, range 0.5–3.0) multiplies the learned threshold.
- **Cold start:** < 14 days of channel history → fallback mode (fixed 6 h
  silence, fires 10:00–22:00 local only).
- **Success metric:** ≥ 3 human messages from ≥ 2 distinct people within
  30 minutes of the revive.
- **Ping scarcity** is counted per channel per local day (`ping_max_per_day`, default 3), with a minimum spacing between two tags (`ping_cooldown_minutes`, default 60). Both are guild dials on the Settings tab.
- **Opt-in role:** the dashboard action `POST /api/chat-revive/optin-post`
  publishes a persistent join/leave
  button for the configured role. The Role Menus feature
  is the richer alternative: a toggle-mode menu pointing at the same
  role works identically, so use whichever fits the guild.
- **Manual fire** skips every lull/frequency gate but keeps ping scarcity;
  manual revives still count toward the daily budget ledger.
- **Adult-only questions** are gated on Discord's channel age-restriction
  flag on the channel or its category (`channel_allows_nsfw` →
  `is_age_gated_channel`), never on a bot-side toggle.
- **Dashboard-managed, no slash commands** (revised same-day: the `/revive`
  group was built and then stripped to keep the command surface clean).
  Everything lives in the dashboard's Config → Channels & Messages → Chat Revive panel, backed
  by `/api/chat-revive/*` (moderator-gated): guild settings, per-channel
  dials, the question bank (add/bulk/retire), the scoreboard, the
  would-it-fire "Check" explainer, manual "Fire", and posting the opt-in
  button. The only in-Discord surfaces are the monitor loop's posts and the
  persistent opt-in button. (The button refuses to hand out a
  ping role that carries elevated permissions, is managed, or sits above the
  bot (`core.role_safety.grant_block_reason`), and tells the member to ask a
  mod. Taking the role back off is never refused.)
- **Busy-check disabled:** the active-game/round busy check
  (`channel_is_busy()` in `chat_revive/actions.py`) silently blocked a
  qualifying revive with no log trace (a genuine lull went unfired
  for 90+ minutes). The loop and the dashboard
  "Check" preview always pass `busy=False`, so the decision runs purely
  on channel silence history; `channel_is_busy()` itself is untouched and
  still used by Economy Drops. Re-enabling this gate for Chat Revive should
  come with an INFO log line on every busy-blocked fire so this failure mode
  is diagnosable next time.
- **A channel the bot can't post in is paused, not retried forever
  .** An unpostable channel stays quiet, so its lull never
  ends and the loop would try it every tick for good. Five failures in a
  row, a send or the pre-send history re-check, pause that channel
  (`core/background.FailureStreak`, logged once at error); it is then tried
  once an hour, and the first revive that lands ends the pause. The
  dashboard's manual Fire does not go through the loop and is never paused,
  and a Fire that lands ends the channel's pause (`note_delivered`).
