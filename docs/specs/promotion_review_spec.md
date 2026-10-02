# Promotion-review cards

**Status:** Reference (built). Extends the existing Level 5 promotion card.

The **Level 5 Log Channel** (`xp_level_5_log_channel_id`) is the
promotion-reviews channel. Three triggers post a review card there, each
carrying a persistent **Grant** button (survives restarts) so a roles
manager can action a return without leaving Discord, with no slash commands.

The button is **labelled with the role it grants**, `Grant <role name>`,
resolved from `promotion_review_grant_role_id` when the card is posted. A
generic "Grant access" tells a reviewer nothing about what they are handing
over, and a guild whose dial points at the wrong role only finds out from the
audit log afterwards. It falls back to **"Grant access"** in three cases: no
grant role configured, a dial pointing at a deleted role, and the **sleeper**
card, whose Grant runs a full reactivate rather than adding the configured role.
Labels are truncated to Discord's 80-character ceiling. The label is fixed at
post time: cards posted before a dial change keep the old wording, since the
rendered message carries the label, not the reconstructed view.

## Triggers

All three cards ping `promotion_review_ping_role_id` when it is set, and the
guild's `mod_role_ids` when it is not (see "The two roles are different
settings").

| Kind | Fires when | Grant button does |
|------|-----------|-------------------|
| Level 5 | Member reaches `role_grant_level` (existing card in `maybe_log_level_5`), or, if that card was withheld for want of the NSFW opt-in, picks the opt-in role later | With intake Stage 2 on: **Approve for Stage 2** opens the greeters' Stage 2 card and adds no role. Off: adds `promotion_review_grant_role_id` |
| `pruned_return` | A member with an **open** `role_prune_events` row (auto-sweep removed a role, migration 098) posts a message **anywhere** | Adds `promotion_review_grant_role_id`, then `restored_at` on their open prune events |
| `sleeper` | A member currently held inactive (`inactive_members`, migration 057) posts in the **sleeper channel** (`inactive_channel_id`) | Full `reactivate_member`: restores stored roles, removes `@Inactive` |

The `pruned_return` and `sleeper` cards also carry a **Dismiss** button.

## Gating / config (dashboard, not Discord)

- Ships **dark**: the return/sleeper triggers do nothing until the Promotion
  Reviews Channel is set (`xp_level_5_log_channel_id`). `pruned_return`
  additionally requires `promotion_review_grant_role_id` (else its button would
  no-op).
- These settings live on **Moderation → Role Management → Promotion Reviews**
  (`promotion-reviews.js`), which submits only its own three keys to
  `PUT /api/config/xp`. On the XP & Leveling form, saving any XP dial would
  rewrite all three (that is how the ping role reached prod as 0 and cards
  fell back to pinging the mod roles).
- Buttons are limited to staff as `AppContext.member_is_mod` defines it:
  Manage Server, Administrator, or a configured mod/admin role. Manage Roles
  alone does not count.

### The two roles are different settings

| Key | Role it names | Used for |
|-----|---------------|----------|
| `promotion_review_grant_role_id` | The role handed **to** the promoted member | What the Grant button adds (`_do_grant_role`) |
| `promotion_review_ping_role_id` | Your **role managers** | Pinged when a card posts, so a human sees it. Unset ⇒ falls back to `mod_role_ids` |

Neither is the approver gate. That is `AppContext.member_is_mod` (Manage
Server, Administrator, or a configured mod/admin role), checked on every press.

**The grant role is safety-checked and every grant is audited.**
The guard exists because a grant dial in a second guild once pointed at STAFF, and two
routine Level 5 presses handed it out (2026-09-03); the only trace was the bot's `reason`
string in Discord's own audit log. `_do_grant_role` runs
`core.role_safety.role_block_reason` before `add_roles`, refusing an elevated
role (no `allow_elevated`), a role above the bot, a managed role or @everyone,
and tells the presser which role and why (they are a mod, so naming the server's
config is fine). A successful grant writes an `audit_log` row,
`action = "promotion_review_grant"`, with the presser as `actor_id`, the member
as `target_id`, and `role_id`/`role_name` in `extra`. `role_events` already
records the change from the gateway, but without an actor, and the Level 5 card
has no ledger row, so this is the only record naming who pressed. It shows on the
Moderation → Audit Log page as **Level 5 Grant**: `promotion_review_` is one of
`MODERATION_ACTION_PREFIXES`, which also counts it in the "recent actions"
stats. The dashboard refuses to save an unsafe Grant Role in the first place
(`web_server.helpers.refuse_ungrantable_role`); the press-time check stays for
a role that gained permissions after it was picked.

**While Stage 2 is on, the press approves rather than grants**
(Ben: NSFW unlocks only from the greeter's card). `_grant_role_only` calls
`post_stage2_card` first and adds no role whatever it returns; the Stage 2
card's built-in *NSFW access* line hands out `promotion_review_grant_role_id`
(audited as an intake grant, naming the greeter). No Level 5 Grant audit row
is written for an approval: the Stage 2 card records the approving mod. With
Stage 2 off the press grants as described below. New cards label the button
*Approve for Stage 2* while Stage 2 is on (`level_5_button_label`, read at
post time; older cards keep their Grant label but behave the new way).

**The Level 5 press opens intake Stage 2** (approve-only while Stage 2 is on,
above). `_grant_role_only` calls `intake_views.post_stage2_card`
first. Only when `intake_enabled` and `intake_stage2_enabled` are both on, that
closes the member's still-open join card as `advanced` and posts a Stage 2
intake card to the greeters, naming the presser as approver; the press then
grants nothing and its reply names the outcome
(`intake_service.stage2_press_reply`: opened, already open, or why it couldn't
open). With Stage 2 off, `post_stage2_card` returns `dark` and only then does
the press add the grant role and answer "Access granted.". Only this button
opens Stage 2; a role added by hand or through `/grant` does not. It never
raises, and a second press while a Stage 2 card is open opens nothing. The
sleeper and pruned-return cards don't open it. See
[intake_spec.md](intake_spec.md) § Two stages.

**Level 5 card and the NSFW opt-in (Ben, 2026-09-30).** The Grant button
hands out NSFW access, so the Level 5 card waits for the member to have opted
in: with `intake_stage2_optin_role_id` set, a member who doesn't hold that role
gets **no card at all** (no post, no ping, no button)
(`promotion_review_service.level_5_card_opted_out`, checked in
`maybe_log_level_5`, so the deferred-tenure path obeys it too). The level still
counts as announced, and a log line records the skip. When the member later
**picks the role**, the role-change hook (`events/cog.py` →
`xp_service.maybe_post_level_5_on_optin`) posts the withheld card, tenure bar
included, if `level_5_card_due_on_optin` says one is owed: Level 5 already
announced, no access yet, and no card ever recorded or parked for them, so
toggling the role can't post a second card. Unset (0) keeps every card. A
**deleted** opt-in role posts cards for everyone with a warning, the opposite
of Stage 2's fail-closed: a card that failed closed would hide every new
Level 5 member from mods without a word. Cards posted before migration 141 have
no recorded row, so picking the role could post a second card for such a
member. That is accepted: those cards are months old.

A guild that has **never touched** the ping dial gets a
`@Promotion Reviewers` role created when the first card posts
(`core/role_provision.py`).

It is a **staff job ping**: moderators switch it on and off
for themselves on the Mod Settings panel ([staff_pings_spec.md](staff_pings_spec.md)),
it is offered there only while the review channel is set, it is not
offerable in Discord onboarding, and a member who stops being a moderator
loses it.

**Mod-role fallback.** With no provisioned or configured ping
role, the card falls back to the guild's `mod_role_ids` rather than posting to
nobody (`promotion_review_service.ping_role_ids`), which the dial always
overrides when set. A stored 0 here is usually an *artifact*, not a decision:
XP Settings saves as one form, so changing any XP dial writes a 0 over this
one, and prod showed exactly that: the main guild sat at 0 while its cards
posted silently. The fallback targets the roles that can actually action a
card, matching what the economy's approval cards do with `manager_role_id`.
Admin roles are deliberately excluded: they overlap the mod roles in every
configured guild, so including them would only ping the same people twice.

The mention goes in the message **content** (a role mention inside an embed
renders but never notifies) and allow-lists exactly those roles by id via
`core.utils.role_ping_kwargs`, never a blanket `roles=True`. Note
`discord.AllowedMentions`' unset fields default to *allow*, so
`everyone`/`users`/`replied_user` are pinned `False` explicitly, since the bare
`AllowedMentions(roles=[...])` form still serializes
`parse: ['everyone', 'users']`.

## Mechanics

- **Ledger:** `promotion_review_cards` (migration 112). One **open**
  (`resolved_at IS NULL`) card per member: a partial unique index enforces the
  dedup so multiple messages never spawn multiple cards. `kind` drives the Grant
  action; resolving records `resolved_by` + `resolution`
  (`granted`/`reactivated`/`dismissed`). The Level 5 card is **not**
  ledger-backed (its button is keyed by member id).
- **Names, not mentions:** the pruned-return / sleeper card names
  the member and, once actioned, the resolver through `build_name_fn`, never
  `<@id>`, which shows digits to a reviewer who hasn't seen that member. The
  resolved re-render falls back past the member cache, since they may have left.
- **Level 5 card refresh:** the Level 5 card's **Spicy access** field is the one
  live element on an otherwise at-promotion snapshot. `xp_level_5_cards`
  (migration 141) stores where each card was posted, and the `on_member_update`
  role diff in `events_cog` re-renders just that field whenever the spicy-access
  role is added or removed, so the card tracks access granted by `/grant` or by
  a hand-added role. A removal made by
  Take a Break reads **🌙 On a break** (or **🌙 On a break (Spicy Lite)** when
  Spicy Lite was swapped in), not ❌: the break row is written before the strip,
  so the refresh finds Spicy in the saved roles and reports it held. **Which role that
  is** (`xp_service.nsfw_grant_role_id`): the dashboard's
  **Promotion Review Grant Role** (`promotion_review_grant_role_id`, on the
  Promotion Reviews panel) when set, since the role the button hands out is by definition the
  access being reviewed and button and field move together. It falls back to
  the grant whose internal key is literally `nsfw`, and 0 (field omitted) when
  neither exists. (Reading `grant_roles["nsfw"]` alone would lose the field silently for an
  admin who renamed the grant, or a guild that never had one.)
  Deliberately a separate table from `promotion_review_cards`: a Level 5 card
  never resolves, so it would squat that table's one-open-card-per-member slot
  forever. Cards whose message *or* channel has been deleted are forgotten (a
  cache miss is confirmed with a fetch first, so a card in an archived thread is
  kept). The refresh never raises, and is spawned **detached**
  (`asyncio.create_task`) so its two HTTP round trips neither delay the
  verified-welcome / greeter ping nor get skipped when that later work raises.
  Refreshes are **serialized per member** (`_member_refresh_lock`): two quick
  toggles otherwise race, the loser no-ops against a not-yet-edited card, and the
  winner's stalled edit lands the older state permanently.
  **Best-effort on events only, not eventually consistent:** a role change while
  the bot is down is never replayed, so that card stays wrong until the member's
  next NSFW role change. Nothing reconciles it; a repair pass on
  `promotion_review_recheck_loop` (which already owns deferred Level 5 posting)
  is the natural home if that becomes a problem. Cards posted before migration
  141 have no stored row and stay stale.
- **Hot path:** `on_message` filters with an O(1) in-memory watch set
  (`promotion_review_service.is_watched`), seeded at startup (`warm`) and fed by
  the prune sweep (`note_pruned`) and the inactive-hold path (`note_inactive`).
  The DB (`evaluate_trigger`) is the source of truth; the watch set is only a
  cheap pre-filter, so a stale entry is harmless.
- Posting reserves the card slot **before** the Discord send; a failed send
  rolls the reservation back and leaves the member on the watch set to retry.

## Code

- Service (ledger + gating + watch set): `bot_modules/xp/promotion_review_service.py`
- Embed + persistent buttons: `bot_modules/xp/promotion_review_views.py`
- Level 5 card: `bot_modules/xp/service.py` (`maybe_log_level_5`)
- Level 5 card refresh: `promotion_review_views.refresh_spicy_field` (pure) +
  `refresh_level_5_cards` (Discord edit)
- Hooks: `bot_modules/events/cog.py` (`on_message`, `on_member_update`),
  `bot_modules/inactive/prune_service.py`,
  `bot_modules/inactive/apply.py`, `dungeonkeeper/__main__.py` (warm + button registration)

See also: [role_grant_spec.md](role_grant_spec.md), [inactive_spec.md](inactive_spec.md), [xp_spec.md](xp_spec.md).
