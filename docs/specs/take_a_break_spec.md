# Take a Break

A member presses one button and steps away: every role comes off (so every
role ping stops), they see only the break channel, and they get it all back
by pressing **👋 Back**. There is no timer. Built from
`docs/plans/take-a-break.md` (gitignored; recover it with
`git show 678a16fa:docs/plans/take-a-break.md`), which holds the decisions and
the stages still to come; this file describes what runs.

**Status.** Built (stages 1–7 of the plan): the store, start/end, the two
panels, the dashboard page, the report exclusions, mod-action absorb, what a
break pauses, and the DM mute.

## Switching it on

Dark by default. `BreakConfig.enabled` needs both `break_role_id` and
`break_channel_id`, and only **Set Up Break Channel** on the dashboard's
*Take a Break* page (Config → Members) writes the channel. Until then both
panels answer "Breaks aren't set up on this server yet."

Set Up Break Channel (`service.setup_break_channel`), in order:
1. Makes or finds **@On a Break** through `ensure_config_role` (the
   Bot-Managed Roles registry, `feature_roles.BREAK_ROLE`). A stored `0`
   ("(none)" on that page) is honored: setup refuses and says so.
2. Lets the role view, send and read history in the picked text channel.
3. Posts the **On a Break** card there with the cog's bound persistent view.
4. Denies the role view/send on every other channel. A channel that can't
   be edited is counted and reported, not fatal.
5. Writes `break_channel_id` last (the on switch), so a failure at any
   earlier step leaves breaks off.

A channel created later isn't hidden automatically; pressing the button again
re-runs step 4 over every channel. The page says so.

**Spicy Lite** (`break_spicy_lite_role_id`) is optional. With a role picked,
a member who had the Spicy role (the Level 5 grant role, resolved with
`nsfw_grant_role_id`) wears Spicy Lite for the length of the break. Saving
refuses an ungrantable role and refuses the break or Spicy role itself.

**Re-pointing a hold role.** Moving the Spicy Lite dial here, or the break
role on Bot-Managed Roles (adopt or stop), records the old role id in
`break_retired_role_ids` (`store.retire_hold_role` /
`retire_on_dial_change`). Members already wearing it (on a break, or with a
stray role and no row) still count as on a break: Grant Audit exempts them,
a jail or inactive snapshot drops it, and I'm back takes it off
(`store.all_hold_role_ids` = the dials now plus everything they named before).
Each break row also records the hold roles it actually put on
(`break_role_id`, `lite_role_id`), so its own return never depends on the
dials staying put.

The **Take a Break** card is posted through the shared panel registry
(`break-panel`, `/api/panels/break-panel/post`). The I'm back card has no
registry entry: setup posts it, as Inactive does its own.

## Starting (`service.start_break`)

Refused, ephemeral and with a plain reason (`logic.REFUSAL_MESSAGES`), when
the feature is dark; the presser is a bot or staff (stripping staff deletes
their personal perk role); they're already on a break (row or live role);
they're jailed or inactive (row or live role); they still hold Unverified; or
any role to be stripped sits at or above the bot. The hierarchy check runs on
the stripped roles up front, so a break is never partial. The break role and
Spicy Lite come from dials, so both go through `grant_block_reason` first: one
pointed at an elevated, managed or above-the-bot role refuses the break
("aren't set up correctly") rather than hand it out, and a rejoin re-applies
only the hold roles that still pass.

Order: write the `break_holds` row → add the break role (+ Spicy Lite) →
remove each saved role one call at a time → audit row. Any failure after the
row rolls back: roles restored, hold roles dropped, row deleted, and the
member is told it didn't work. A second press racing the first loses on the
primary key.

**One change at a time per member.** Starting a break, ending one, jailing
and marking inactive each take `breaks.locks.member_role_lock(guild, member)`
around their read → roles → write stretch, so an I'm back landing while a
jail strips waits for the jail row rather than leaving the snapshot and the
live roles disagreeing. In-process is enough: the dashboard runs on the bot's
event loop.

## Ending (`service.end_break`)

"I'm back" or a moderator's **End Break** on the dashboard list. Saved roles
that still exist go back first, then the hold roles come off, so a partial
failure never leaves the member with neither; the row is kept on failure and
deleted on success. Roles deleted meanwhile are named in the reply. A
moderator end is audited with the moderator as actor. For a member who has
left, End Break just deletes the row (`end_break_absent`).

## Leaving and rejoining

`events/cog.py`'s `on_member_join` calls `reapply_on_join` (after the jail check):
an open break puts the hold roles back and nothing else: no auto-role and
no intake card. The greeter chat ping still fires, as for a jailed rejoiner.

Anonymisation wins: the departure job deletes the row in `_finish`, so a
break survives a leave only if the member rejoins inside the settle window.

## Mod action wins

Jailing a member on a break (`moderation/jail/apply.apply_jail`), or moving them to
inactive (`inactive/apply.apply_inactive`, manual or sweep, though the sweep
skips break rows), absorbs the break: `breaks.store.absorb_break`, called
inside the transaction that writes the jail/inactive row, folds the break's
saved roles into the new hold's snapshot, drops the break roles and deletes
the break row, so the saved roles are never in neither. Hold roles (current,
retired, or the row's own) are dropped from the snapshot **even with no
row**, so a member wearing a stray break or Spicy Lite role is never handed
it back on release. Releasing them returns their pre-break roles. A jailed
or inactive member can't start a break.

## What a break pauses

Run after the strip (`service._after_start` / `_after_end`), each step in its
own `try`, since the break has already happened and one failing step mustn't skip
the next:

- **Perk rentals** whose beneficiary is the member are paused
  (`economy/rentals_service.set_beneficiary_rentals_paused`); a rental they
  pay for someone else keeps running. The billing loop's gate-suspension
  sweep leaves a break-taker's rentals alone, so it can't unpause or DM about
  them mid-break. On return, rentals resume **only for perks whose feature
  gate still passes**; one whose gate closed mid-break stays paused and no
  "paused" DM is sent for it (the one accepted gap: the pause flag can't
  tell a break from a gate suspension without a schema change). The personal
  perk role is then re-projected (`perk_actions.apply_role_perks`), which
  keeps the role off while the break row exists.
- **Pen Pals** (`pen_pals/cog.end_for_break`, reached through the
  `member_break_start` bot event the service dispatches, so `breaks` never
  imports a cog): an active session ends as if
  the member had left (channel deleted, partner requeued); a member only
  waiting in the pool is taken out of it. No opt-out is written, and they are
  **not** re-pooled on return; they rejoin from the Pen Pals panel.
- **Listeners that read a role loss as a choice** check for a hold first
  (`roles/grant/audit_service.member_is_held`, covering break, jail and
  inactive by row or by a hold role worn):
  - The Guess opt-out listener waits `HOLD_SETTLE_SECONDS` (jail and inactive
    write their row after the strip), re-reads the member and ignores the
    loss if they're held, so a hold no longer withdraws Guess consent.
  - The XP level reward (`xp/service.maybe_grant_level_role`) isn't granted
    to a held member: chatting in the break (or jail) channel can cross the
    bar, and the saved roles bring the reward back on release.

## DMs are muted

There is no single DM choke point, so the mute sits in the two shared
helpers plus the callers whose "not delivered" answer would show. The rule
is the no-contact one: **suppress the delivery, give the ordinary answer.**

- `economy/service.deliver_econ_dm` drops the DM like a notify mute
  (`DmDelivery("dropped")`), and so skips the public bank-channel fallback.
- `shared/dm_branding.send_branded_dm` returns None for a recipient on a break in
  that guild, the answer a closed DM gives. `bypass_break=True` marks the
  DMs that must still arrive: receipts for the member's own action (DM
  request sent / accepted / revoked, My Data progress, voice hub setup
  notes), wellness slow-mode notices fired by their own message, the
  confession-rejection notice (moderation), and the greeting watch (staff).
  A DB fault in the check delivers: the mute is a courtesy, not a gate.
- Up-front checks, where None would ping publicly or read as a refusal:
  - **Whisper** send and reply take the no-contact path: "sent", nothing
    delivered (the reply row is still written so the one-reply cap trips).
  - **DM request** precheck treats the target as `closed`.
  - **Voice knock** reports delivered and sends nothing: no DM, no
    control-channel ping.
  - **Survivor last call** skips them: no DM and no channel mention. The
    hosts' pick status still lists them.
- Raw `.send` DMs left alone: jail notices (moderation), operator and
  community-host alerts (staff), the bio wizard timeout (their own action).
- Callers whose closed-DM path is already quiet need nothing more (AMA host
  screening says the host couldn't be reached; Needle, confession reply,
  Pen Pals, duels and wellness nudges ignore the result).

## Not reported as stripped

A member on a break had their roles taken off on purpose, so:

- **Grant Audit** (`roles/grant/audit_service.get_hold_excluded_ids`) skips
  anyone with a break row, and anyone wearing @On a Break **or Spicy Lite**
  live; the second half catches a role with no row behind it. Billy's rule:
  someone on Spicy Lite is not reported as stripped.
- **Inactive sweep** (`sweep_service.compute_candidates`) skips break rows:
  idle by choice, and a sweep would snapshot only the break roles.
- **Level 5 card** "Spicy access" reads 🌙 *On a break* (*(Spicy Lite)* when
  swapped) while Spicy sits in the break snapshot.
- The **inactivity prune** needs nothing: it walks the grant role's current
  holders, and a break-taker no longer holds it.

## Panel art

Both cards live in a channel, so both take the guild's **panel art**
(dashboard **Config → Branding → Panel Art**, nothing set by default): the
banner is the card's first component and the footer its last
(`build_break_panel_layout` / `build_return_panel_layout` take `banner_url`
and `footer_url`; the cog reads them with
`branding_service.resolve_panel_art_conn`, off the event loop). With nothing
uploaded both helpers add nothing and the cards are exactly what they were.

With the Panel Art card's **Title in the Banner** switch on and a banner set,
the bot draws "Take a Break" / "On a Break" across the banner
(`panels/title_service.with_title`) and the `## 💤` heading goes, the banner's
alt text carrying the name instead; if the drawing fails the heading stays.
The public panel repaints on the next panel refresh or restart
(`panels/registry`, `render="build_break_panel"`); the **On a Break** card is
posted once by Set Up Break Channel and keeps the art it was posted with.

The ephemeral replies to either press, and the Help & Settings hub's 💤 card
(a `BreakPanelView` with no art), stay plain
(Billy: "just the main panels that live in the channel").

## Data

`break_holds` (migration 237): one row per open break: guild, member,
saved role ids, started_at, whether Spicy was swapped, and the hold role ids
it put on. Deleted when the break
ends; no history is kept. Purged by `purge_user_data`; see
[data_register.md](../data_register.md). Not member-erasable from the **My Data** panel:
the row is what gives their roles back, and I'm back is the off switch.

## Surfaces

- Bot: `breaks/` (`store.py`, `logic.py`, `service.py`, `layouts.py`),
  `breaks/cog.py` (two persistent views bound with `core.layout.bind`).
- Dashboard: `routes/breaks.py`, `static/js/panels/take-a-break.js`. Settings
  are admin; the On a Break list and End Break are moderator, live on the
  otherwise read-only page (`data-readonly-ok`).
- Manual: `manual.html#take-a-break`.
