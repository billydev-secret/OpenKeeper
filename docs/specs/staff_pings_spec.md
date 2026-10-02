# Staff Pings & Mod Settings

**Status:** Reference. Promotion Reviewers, the staff ping channel, and six
job pings (tickets, chores, Chat Revive, QOTD, Greetings, Spicy QOTD); a
seventh, bump, is retired (see *Retired subjects*).
Moderators manage their own pings on the posted Mod Settings panel (Ben:
"should have been a panel, not a slash command"); there is no per-moderator
card and no `/modsettings` command.

## What it is

A **staff job ping** tells the moderators who asked for it that a staff job
needs doing. It is a permission-less, non-mentionable role in
`feature_roles.STAFF_ROLES` (`staff=True`). A moderator switches it on for
themselves from the Mod Settings panel, and the bot mentions the role when the job
comes up.

These are not the member pings of similar names. `@QOTD` and `@Chat-revive`
tell *members* there's something to join; they stay on Help & Settings →
Roles & Pings and are untouched.

## The Mod Settings panel

No slash command. An admin posts
it from the **Mod Settings Panel** card on the dashboard's Moderation &
Privacy page (`panel_registry` key `mod-settings`), into whichever channel
the moderators use; nothing appears until they do. It is a posted-once panel,
**Paradigm 1 (hub panel)**: recorded in `posted_panels`, moved rather than
duplicated on a repost, redrawn on every restart, and redrawn in place
(`panel_refresh_service.refresh_panel_key`) whenever a dashboard save could
change which jobs are offered: `staff_ping_service.announce_change`
dispatches `staff_pings_config_change` from the Moderation & Privacy save
(staff channel, job roles, QOTD deadline, the ticket notify dial), the
Economy QOTD page's spicy QOTD role, the XP
page's promotion-reviews channel, Chat Revive's
settings and channel saves, the Greeting Watch save, and adding, pausing,
resuming or deleting a recurring chore. Anything that changes an offer without one of those saves
(a role deleted or made unsafe in Discord) shows at the next restart, or when
a moderator presses its row.

One row per staff ping the guild has set up, each with an **On / Off**
button. The panel is public, so a row shows no one's state: a press switches
that ping on for the presser, or off when they already hold it, and answers
ephemerally with where it left them (`logic.toggled_text`). The persistent
view is registered at load with a button for **every** staff ping
(`logic.every_row`), so a copy drawn before a job was dropped still reaches a
handler. Button ids are `mod_settings:ping:<config key>` and frozen.

Every press re-checks that the presser is a moderator (`service.admitted_mod`,
`AppContext.member_is_mod`) and re-reads their card (`service.toggle`), so it
is judged against the server as it is now, not as the panel was drawn. A press
on a job that is no longer offered answers so and redraws the posted panels.

**Double taps.** Discord's echo of a role change lags the reply, so the
cached roles can still show the old state on a quick second press. A
moderator's presses run one at a time (a lock per member), and each press's
own result outranks the cache for `logic.RECENT_SECONDS` (30 s,
`logic.roles_after_recent`), so a second tap undoes the first instead of
repeating it. Kept in memory: a restart outlasts the lag.

What the panel will never do (`moderation.settings.logic`, each a row in
`tests/moderation/test_mod_settings_logic.py`; the "card" in the code is one
moderator's read of it, rebuilt on every press):

- act for anyone who isn't a moderator;
- list a ping whose delivery isn't set up (`staff_ping_service.delivered_keys`):
  a switch for a ping that can't fire would be a preference nothing enforces;
- list a ping whose dial an admin set to "(none)";
- hand out a role failing `core.role_safety.role_block_reason`;
- change anything for a moderator under a hold (jailed, inactive, on a break).

A ping whose role hasn't been made is still listed, and a press turns it on.
Turning it on makes the role,
or adopts a same-named role, through `core.role_provision.ensure_config_role`,
which records it in `bot_managed_roles`. A deleted role reads as not made, and
the next press recreates it and says so in the mod log.

## Step-down strip

A member who **stops being a moderator** (mod before an `on_member_update`,
not after) loses every staff ping role (`logic.roles_to_strip`). A member who
was never a moderator is left alone, because an admin may have given them one
of these roles by hand. Mod status changed while the bot was offline is not
caught.

A **hold** (jailed by role or by an active jail row, inactive, or on a
break) is not a step down, even when it takes the moderator role away: the
opt-ins wait it out. They are stripped only when the member comes out of
the hold no longer a moderator. "Before" is judged by the hold roles alone,
since the jail row may be written either side of the role change.

The panel's gate (a moderator, on every press) is `service.admitted_mod`,
so a moderator who steps down is refused on their next press.

## The staff ping channel

Promotion Reviewers rides on the card its feature already posts, in the
promotion-reviews channel. Every other staff ping posts into one channel,
`staff_ping_channel_id`, set on the dashboard's **Moderation & Privacy → Staff
Pings** card. The same card has a role picker per staff ping but Promotion
Reviewers (`CHANNEL_PINGS`; that one keeps its own dial), so an admin
can point a job at an existing role; "(none)" takes that job off the
Mod Settings panel. A role picker is sent only when it changed, because a whole-form
save's "0" would otherwise read as "(none)". The PUT refuses an unknown key,
a malformed id and a role failing `refuse_ungrantable_role`.

`staff_ping_service.post_staff_ping` is the only way to send one. It mentions
the role in content through `role_ping_kwargs`, and skips (never raises) when
the subject's job isn't running (`_JOB_RUNNING`, the same check that keeps
it off the Mod Settings panel), the channel or role is unset or deleted, **nobody but
bots holds the role**, Discord refuses, or the subject pinged within its
hard-coded cooldown (`COOLDOWN_SECONDS`, kept in memory, so a
restart costs at most one extra ping). A refused post doesn't start the
cooldown. A trigger never makes a role.

**Ships dark:** nothing appears in Discord until an admin posts the Mod
Settings panel, and with the channel unset nothing posts and the panel offers
none of the channel pings.

The "nobody holds it" skip is what makes a *replacing* ping safe: a staff
ping that replaces an older notice returns False until a moderator has opted
in, and the caller sends the older notice on False. Configuring the channel
alone therefore changes nothing for tickets.

## Subjects

| Ping | Role | Delivered when | Fires on |
|---|---|---|---|
| Promotion Reviewers | `promotion_review_ping_role_id` | the promotion-reviews channel (`xp_level_5_log_channel_id`) is set | a promotion-review card ([promotion_review_spec.md](promotion_review_spec.md)) |
| Staff · Tickets | `staff_ping_tickets_role_id` | the staff ping channel is set **and** "Notify moderators when a ticket is opened" (`ticket_notify_on_create`) is on | any path opens a ticket; no cooldown. **Replaces** the panel-button path's DM-every-mod whenever it posts ([dungeon_keeper_jail_ticket_spec.md](dungeon_keeper_jail_ticket_spec.md)) |
| Staff · Chores | `staff_ping_chores_role_id` | the staff ping channel is set **and** the guild has an active recurring chore | the todo board loop spawns fresh chore instances: one ping per guild per tick naming them (up to ten, then "N more"; names escaped). A skip or write-off alone doesn't count; no cooldown ([todo_spec.md](todo_spec.md)) |
| Staff · Chat Revive | `staff_ping_revive_role_id` | the staff ping channel is set **and** Chat Revive's mode is *Post questions* or *Ping moderators only* with at least one enabled channel | in *Post questions*: a lull Chat Revive **confirmed** but couldn't fill: no eligible question (re-confirmed against Discord's newest message first, since the ingest ledger can lag), or the post refused (`chat_revive.loop.consider_channel`); 3-hour cooldown. In *Ping moderators only* (2026-09-30): **every** confirmed lull, re-confirmed the same way, as "#channel has gone quiet — a good moment to start something." and nothing posts in the channel. That ping skips the 3-hour cooldown: it is recorded in the revive ledger when it posts, so Chat Revive's own rest period, gap and daily budget pace it, and the 3-hour cooldown would silently override those dials. It runs on its own clock (`post_staff_ping(lane=REVIVE_LULL_LANE)`, keyed apart so neither kind of revive ping holds the other back) with a 30-minute backstop, because the ping posts before the lull is recorded and a failed record would otherwise re-ping every tick. Quiet hours, the budget and rest periods are checked before silence in `decide`, so a lull behind one is invisible to it in either mode ([chat_revive_spec.md](chat_revive_spec.md)) |
| Staff · QOTD | `staff_ping_qotd_role_id` | the staff ping channel is set **and** the QOTD deadline hour (`staff_ping_qotd_deadline_hour`, 0–23 guild-local, blank = off) is set | the todo board loop's tick finds no `econ_qotd` row of kind `'qotd'` for today's guild-local `local_day` at or past the deadline hour (`overdue_qotds`); once per guild per local day, marked only when the post lands; a post that can't land (nobody opted in, channel unset) is retried every 15 minutes (`QOTD_RETRY_SECONDS`) rather than every tick. The mark is in memory, so a restart after the deadline can repeat that day's ping. The text shows the deadline as a `<t:…:t>` timestamp, so each moderator reads it in their own zone. Nothing else detects a missed QOTD: there is no scheduler ([economy_spec.md](economy_spec.md)) |
| Staff · Spicy QOTD | `staff_ping_spicy_qotd_role_id` | the staff ping channel is set, the QOTD deadline hour is set, **and** the Economy QOTD page's spicy QOTD role (`econ_qotd_spicy_role_id`) is set | exactly Staff · QOTD's rule for rows of kind `'spicy'`, sharing its deadline hour (Ben, 2026-09-30: separate job, shared deadline). The kinds are checked and marked apart: a spicy question doesn't satisfy the regular reminder, nor the other way round, and one kind's post failing doesn't hold back the other's mark ([economy_spec.md](economy_spec.md) § spicy QOTD) |
| Staff · Greetings | `staff_ping_greetings_role_id` | the staff ping channel is set **and** Greeting Watch is on (`greeting_watch_enabled`) with at least one watched channel | `greeting_watch_loop` gives a greeting the `unanswered` verdict: one ping per greeting naming the greeter (a resolved name, never a mention), the channel, and a jump link; no cooldown, since every hello is its own job. **Replaced** Greeting Watch's DM list outright (Ben, 2026-09-30, "channel ping, retire DM list"): unlike tickets there is no fallback, so with nobody opted in an unanswered greeting is resolved and alerts no one ([greeting_watch_spec.md](greeting_watch_spec.md)) |

## Retired subjects

**Staff · Bump** (`staff_ping_bump_role_id`, 2026-09-28 → 2026-09-30) mentioned
its role in the staff ping channel whenever a Bump Tracker site came off
cooldown. It only ever repeated the Bump Tracker's own reminder, which already
pings its configured role in the bump channel; in the main guild both pointed
at the same role, so every ready site was announced twice. Ben asked for it
gone. The bump loop no longer calls `post_staff_ping`, the role left
`MANAGED_ROLES` (so the bot neither offers it on the Mod Settings panel nor
strips it from non-moderators, which mattered, since that role is the
member-facing bump role), and a Bump Tracker save no longer redraws the panel.
A leftover `staff_ping_bump_role_id` config row is read by nothing.
`test_a_running_bump_tracker_offers_no_staff_ping` fails if it comes back.

## Why new roles, not the existing ones

The live `Chat-revive` and `QOTD` roles are *member* content pings with
dozens of holders each. Converting them would start sending staff job pings
to members, so the staff roles are new and separate. They live in config KV
keys (`staff_ping_tickets_role_id`, …) rather than `feature_roles.MANAGED_ROLES`'
columns, because config can tell "never configured" apart from "(none)" and
those columns cannot. That is why the member-facing bump and revive dials
are separate. Settled with Ben: the dedicated
`staff_ping_channel_id`, cooldowns hard-coded
per subject, QOTD as its own subject with a deadline-hour dial, a ticket ping
replacing the DM-every-mod once configured, and losing the mod role stripping
every staff ping role.

## Not member data

Opting in is holding a Discord role, like every other ping role. The bot
keeps no per-user table, so there is no `data_register.md` row.

## Not a staff ping: economy approvals

Economy approvals are deliberately not a staff ping subject (a `@Staff · Approvals`
role to replace the approval card's manager-role mention was built and dropped): the economy manager role
is handed out by hand to the specific people who can press Approve, so it is
already the right audience and needs no opt-in. Approval cards ping
`manager_role_id` ([economy_spec.md](economy_spec.md)).
