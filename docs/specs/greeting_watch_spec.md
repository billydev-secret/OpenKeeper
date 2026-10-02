# Greeting Watch

**Flavor: Reference.** Matches current behavior.

## What it does

Catches "good morning" / "hello" style messages in your main chat that go
**unanswered**, so nobody who says hi to the room falls through the cracks. When
a greeting isn't answered within a configurable window, the bot mentions
**`@Staff · Greetings`** in the staff ping channel with the greeter's name and
a jump link. That is a staff job ping ([staff_pings_spec.md](staff_pings_spec.md)):
each moderator switches it on for themselves on the Mod Settings panel.

There is no Discord command surface. It's configured entirely on the dashboard
(**Config → New Members → Greeting Watch**, admin to change, read-only for moderators per
dashboard_ia.md § IA6), per the project's "config lives on the
web" rule.

## How it works

1. **Detection (live, at ingest).** In `EventsCog.on_message`, for a real member
   message posted in a watched channel, `is_greeting(message.content)` decides
   whether it opens with a greeting token. This must happen live: the bot
   stores no message text, so a greeting can't be matched after the fact. A match writes a lightweight
   row to `greeting_watch` (ids + timestamp only; **no message text stored**).

   `is_greeting` is a heuristic, not a classifier: it matches a short message
   (≤ 8 words) that *starts* with a hello-ish token: "good morning", "gm",
   "morning", "hello", "hey", "hi", "hiya", "howdy", "good afternoon/evening"
   (the "good" is optional; bare "afternoon"/"evening"/"evenin'" count, like
   bare "morning"), "g'day", "yo", "oi", "sup", "what's up", "greetings",
   meme-speak ("henlo", "hewwo", "heyo", "ello", "hai"), a few borrowed hellos
   ("hola", "bonjour", "ciao", "aloha", "salut"), the `o/` wave (also `\o/`)
   and a bare 👋, or a check-in phrase standing in for one: "good timezone"
   (a jokey stand-in for "good morning/afternoon/evening" in servers spread
   across timezones) and "how's everyone's/your
   morning/afternoon/evening/day/night/weekend (going)". A word boundary keeps
   "history" / "gaming" / "morningstar" / "oil" from matching, and the
   check-in phrase requires a plural/2nd-person subject right after "how's" so
   generic questions ("how's this bug possible") don't match. Tune the
   built-in vocabulary in `onboarding/greeting_watch/service.py` as real misses surface;
   server-specific in-jokes belong in the dashboard's **Extra Greeting Words**
   instead (`greeting_watch_extra_words`, folded into the compiled matcher
   per guild, escaped literally so tokens can't inject regex syntax).

   One open watch per (channel, author): a second greeting from the same person
   while the first is still pending is a no-op, so a "gm 🙂 … hey all" double
   post can't queue two alerts.

   **Replies never count**, even when the text opens with a hello-ish token;
   a Discord reply is addressed to whoever it replies to, not the room (e.g.
   "hey, no worries" as a reply shouldn't queue an unanswered-greeting alert).

2. **Acknowledgment (live, at ingest).** While Greeting Watch is enabled, four
   things answer a pending greeting, each stamping `outcome = 'acknowledged'`
   on the row and leaving `resolved_at` NULL, so the row stays pending until
   its window closes (a later answerer still earns the `greeting_answered`
   quest):

   - **A reply or an @mention.** Every member message's directed targets,
     the author of the message it replies to and each @mentioned member, bots
     and the author excluded, are checked against pending greetings in the
     same channel (or the thread's parent) by `acknowledge_greetings`.
   - **A hello back.** Another member's greeting in the same watched channel
     answers every pending greeting there (`record_greeting`). No reply or
     tag needed: "morning!" after someone's "good morning" is the answer.
   - **A hello that follows a hello.** The second greeting is itself recorded
     already acknowledged, because the two members have greeted each other.
     "Follows" means the earlier greeting is still pending, which is within
     its window give or take a loop tick.
   - **A reaction.** Any non-bot member other than the greeter reacting to the
     greeting message (`acknowledge_by_reaction`, from
     `EventsCog.on_raw_reaction_add`).

   **Who answered is never stored.** There is no social graph recording
   reply/mention edges (plan recoverable with `git show 678a16fa:docs/plans/remove-social-graph.md`), and each check processes the signal
   without persisting it.

3. **Verdict (background loop).** `greeting_watch_loop` ticks every 60s. For each
   guild with pending rows, it reads config fresh from the DB (so dashboard
   changes apply without a restart) and picks up greetings whose window has
   closed. A row ingest stamped is resolved `acknowledged`; otherwise it's
   `unanswered` and the loop posts the Staff · Greetings ping
   (`staff_ping_service.post_staff_ping`). Either way the row is resolved so
   it's never re-processed, including when the ping can't post (no staff
   ping channel, nobody opted in), since the verdict is true regardless. If
   the feature is turned off mid-window, still-pending rows are retired as
   `skipped`.

Definition of "answered": a Discord **reply** to the greeter, a message that
**@mentions** them, **another member's greeting**, or a **reaction** on the
greeting, all **in the greeting's channel** (a reply or mention in a thread
off it counts too). An answer elsewhere in the server doesn't count. The loop
resolves rows within a tick of the window closing, so an answer landing in
that last minute can still count.

**What still reads as unanswered, on purpose:** other members chatting in the
channel without greeting, replying to or tagging the greeter. A hello talked
over by someone else's conversation is the "ignored in a crowd" case the
feature exists for, so ordinary chat does not answer it.

### Why the wider definition (2026-09-30)

A narrower definition (only a reply or @mention counts) leaves a bare "hey!"
back unanswered. Ben: "There's times where people don't reply
but say hello and I still get a message." Measured over the 30 days before
(993 greetings in the one watched channel, 5-minute window), by reading the
channel history after each of the 242 `unanswered` verdicts:

| What happened inside the window | Alerts |
|---|---|
| Someone else said hello back, no reply or tag | 55 |
| Others chatted, undirected, not hello-shaped | 61 |
| Others only replied to or tagged someone else | 36 |
| Nobody spoke, but the greeting carries a reaction | 10 |
| Nobody spoke at all | 76 |
| A reply existed and was missed | 4 |

In 54 of the 242 another member had greeted the room in the five minutes
before, and 35 greetings carry a reaction (counted as of the day measured, so
an upper bound on "within the window"). The three added rules would have left
126 alerts; counting any message from anyone would have left 86, at the price
of silencing the crowd case above. The four missed replies all fell between
2 and 8 September, each seconds before its verdict, and none in the three
weeks after. The host had a failing disk at the time and has since been
replaced; no cause was found in the code.

## Configuration (Config → Greeting Watch)

| Field | Config key | Meaning |
|---|---|---|
| Enable Greeting Watch | `greeting_watch_enabled` | Master on/off. |
| Watched Channels | `greeting_watch_channel_ids` | CSV of channel ids for your "main chat". Empty = nothing watched. |
| Unanswered Window (minutes) | `greeting_watch_window_minutes` | Wait before flagging (default 10). The API accepts 1–1440 and refuses, rather than rounds, anything outside that; the page's own input stops at 180. |
| Extra Greeting Words | `greeting_watch_extra_words` | CSV of server-specific greetings ("henlo, good yawn"). Normalized on save (trim/lowercase/dedupe); matched at message start, case-insensitive, alongside the built-ins. Empty = built-ins only. |

There is no "who to notify" field: the staff ping replaces the old member
list (`greeting_watch_notify_user_ids` and a legacy single key), and migration 248 deletes both keys. Saving
this page redraws the posted Mod Settings panel, because Greeting Watch being
on with a watched channel is what puts Staff · Greetings on it.

`greeting_watch_enabled`, `greeting_watch_channel_ids` and
`greeting_watch_extra_words` are read on the ingest hot path via the cached
`GuildConfig` snapshot (invalidated on save); the loop reads its own keys
straight from the DB each tick.

## Data

Migration `078_greeting_watch.sql`:

```
greeting_watch(guild_id, message_id, channel_id, author_id,
               created_ts, resolved_at, outcome)   PK (guild_id, message_id)
```

`resolved_at IS NULL` = pending; `outcome` ∈ {`acknowledged`, `unanswered`,
`skipped`}. Partial indexes cover the pending sweep and the per-author dedup
lookup. No message content is ever stored.

## Economy hook

A reply/mention landing on a member whose greeting is still **pending** fires
the `greeting_answered` quest trigger for the answerer
(`pending_greetings_for`, wired in `events_cog._econ_work`; occurrence = the
greeting message id, so one hello credits an answerer once). See
`economy_spec.md` §4.5. The quest stays reply/mention-only on purpose: a hello
back or a reaction answers the greeting for the alert but earns nothing, so
widening the alert's definition did not change who earns coins.

## Not built / possible follow-ups

- **Silence variant.** Only the "ignored in a crowd" definition ships (nobody
  acknowledged the greeter). A "channel went dead-silent after the greeting"
  detector could be added off `processed_messages` if wanted.
- **Dashboard log.** Resolved rows carry a verdict but aren't surfaced anywhere;
  a small "recent unanswered greetings" view could live under Reports.
