# `/info` — the member's own card

> **Classification: Reference**. Matches current behavior (a Components V2 card with rank and a level bar).

The member-facing counterpart to `/modinfo`. One ephemeral Components V2 card
showing a member their own account, activity, level and rank, wallet and
opt-in states, with a button beside each opt-in to change it.

## Two doors, one card

`/info` and the Help & Settings hub's **ℹ️ Your Info** row (the **About You**
group, `docs/specs/server_features_spec.md`) both call
`MemberInfoCog.open_panel(interaction)`, so there is one render path and the
two can't drift. `open_panel` defers with `thinking=True`: a button press's
defer otherwise acknowledges silently, and the chart render is slow enough
that a silent press reads as a dead button.

The card is a V2 layout (`docs/plans/components_v2_migration.md`, recoverable with
`git show 678a16fa:docs/plans/components_v2_migration.md`), so the hub row
opens a card that looks like its neighbors. Builder:
`member_info/layouts.build_member_info_layout`, paradigm 2 (settings panel):
bare, no container, the avatar a `Thumbnail` beside the `## ℹ️ Your Info`
heading, a line between groups, the chart in a described `MediaGallery`.

## Scope: self only

There is no member argument, and adding one is not a small change. A lookup of
*another* member would have to consult the no-contact list
(`docs/specs/no_contact_spec.md`; every surface that puts two members in contact
does) and re-argue every field's leak surface. None of that is needed for a
card you can only point at yourself, so the command takes no target.

## What it shows

| Section | Source |
|---|---|
| Account | `member.created_at`, `member.joined_at`, the join shown relative (`<t:…:R>`, "4 months ago"), not as a date; `/modinfo` matches. Discord keeps it current and shows the date on hover |
| Level | `member_xp`, plus `xp_events` grouped by source, plus the current level's floor and the next level's threshold from `xp_required_for_level` against the guild's own curve factor (a `▰▱` bar and "N XP to Level L"), plus `xp_system.get_member_xp_rank` ("Rank #12 of 340") |
| Roles | live Discord roles, highest first, `@everyone` dropped, capped at 12 with a `+N more` |
| Activity | `processed_messages`: 30-day count, last-seen, top 3 channels **and threads** |
| Wallet | `load_econ_settings` / `get_balance` / `list_member_rentals` / `get_streak_shields` / `get_streak_summary` (passed today's local day, so a lapsed streak reads as zero rather than as the stale stored number) |
| Your opt-ins | one row per configured feature (see below); the last, **My data**, opens the data panel |
| Footer (`-#`) | `/ask` (only when `AdvisorCog` is loaded, named via `resolve_assistant_name_conn`, since the assistant's name is per-guild branding) and a pointer to the Help guide's Your Data & Privacy |

A 30-day XP-by-source chart is attached, rendered by `render_activity_chart`,
the same builder `/modinfo` uses.

### What it deliberately omits

- **The watch-list count.** `/modinfo` shows how many mods are watching a
  member. Telling the member is telling the subject of an investigation that
  there is one.
- **Warnings, jail history and tickets.** Their own moderation record is not
  shown here. This is an explicit product decision: if it is ever revisited, it changes in
  `member_info/layouts.py` and in what the cog fetches, not by widening an
  existing field.
- **Any no-contact count.** See below.

## The opt-in rows

`member_info/logic.py` holds one `_FeatureSpec` per feature: its label, and
how each of the three states (`in` / `out` / `unset`) reads and what it
offers. `build_optin_rows` is pure: the cog reads state, the table decides
copy and buttons.

| Feature | "Configured" means | Member state read from |
|---|---|---|
| Pen Pals | `pen_pals_config.enabled` | `pen_pals_optouts` → out; `pen_pals_pool` or a live session → in |
| Whispers | whisper `role_id` set | holds the role |
| Guess pool | `guess_role_id` set | holds the role |
| DMs | the cog is loaded | `resolve_mode` → open / ask / closed |
| Wellness | wellness `role_id` set | `wellness_users.is_active` |
| Birthday | the cog is loaded | `has_birthday` |
| No-contact | the cog is loaded | *nothing* (see below) |
| My data | always | *nothing*; one door, same copy in every state |

Button labels are one word (Join, Leave, Set, Remove, Set Up, Redo, Open): a
button sits beside its row, which already names the feature, and a label over
`core.layout.MAX_ACCESSORY_LABEL` columns drops under its row on a phone.
`test_every_action_label_fits_beside_its_row` holds every state to it. All
buttons are grey; none is a destructive press or a decision's yes.

Two rules are enforced in the logic layer rather than the view, because they
are the ones that leak if a later edit gets them wrong:

1. **An unconfigured feature produces no row at all.** `/info` must not
   advertise a feature the guild never set up, nor offer to join it.
2. **A button is only offered when the member can actually act.** Pen Pals
   with an opt-in role the member lacks renders the status and no button,
   because the flow would only refuse.

`unset` is kept distinct from `out` on purpose: someone who never joined Pen
Pals gets an invitation, someone who left gets an acknowledgement. Re-pitching
a decision someone just made is the bot arguing with them.

### No-contact carries no count

The No Contact card (`no_contact.logic.own_entries`) filters out entries the *other* party created against the
viewer (`is_visible_to`). A count on this panel derived from the raw table
would leak exactly what that filter exists to hide; a count derived after
filtering could still be differenced against other surfaces. So the row's text
is identical in every state and contains no digits (enforced by
`test_no_contact_row_states_are_indistinguishable`), and the button opens the
existing filtered view.

### Two numbers that were half-told

The Level field carries the XP still owed for the next level, not just "Level 7 · 12,345 XP". The
threshold comes from `xp_required_for_level` against the guild's live curve
factor, and `xp_to_next_level` clamps at zero: the stored level lags the XP
that earns it, so "already past the threshold" is reachable and must not render
as a negative.

The bar (`logic.level_progress`) runs from where the current level began to
the next threshold, not from zero: on a quadratic curve a bar from zero reads
nearly full at every level past the first few. It clamps both ways for the
same lag reason, and its percentage is truncated rather than rounded, so it
never says 100% beside "1 XP to Level 8". It is `core.meters` text in a code
span: the style guide prefers an application-emoji meter set, which has not
been uploaded, and names a text bar as the fallback.

Rank is by all-time `member_xp.total_xp` (what the level is built on), not
per XP source like `/xp_leaderboards`. Members on equal XP share a rank, and
only members with some XP are ranked or counted. A member with none sees no
rank line.

The wallet line printed "🛡️ streak shield held" beside no streak, because the
shield helpers read `econ_streaks` for its `shields` column only and nothing
read the streak itself. `get_streak_summary` fills that gap and takes
today's local day, because `current_streak` is **stored, not live**: only
`process_login` rewrites it, on a message or a voice award. A member who
stopped posting a week ago still has the old number in the column, so reading
it verbatim would announce a run their next message resets to 1. The helper
replays the stored state through `evaluate_login`, the same pure rules a real login applies, and reports zero when the run cannot survive. The personal
best is left alone: that is history, not a live claim.

### Resilience

Every feature block in `_feature_states` is independently guarded, and the
whole render is wrapped. Both matter for the same reason: the command
`defer()`s first, and `events.cog._on_tree_error` only speaks when the
interaction is still unanswered. An exception escaping after the defer is not an error message; it is a member left on "thinking…" forever. Reusing seven
features' internals is seven chances for a helper to drift or a table to be
missing (wellness's are created by the web server's startup, not by a
migration), so one failure costs its own row and nothing else.

The same reasoning caps the Roles section by length, not just by count:
Discord allows 100-character role names, and a card over the 4,000-character
layout limit is rejected whole: past the defer, a card that never arrives.
Each variable-length section is held to `layouts.SECTION_LIMIT` (1,024), the
share that keeps the fullest card inside the limit.

### Threads count as channels — and archived ones can't be named

`award_message_xp` accepts a `discord.Thread` and stores `message.channel.id`,
so `processed_messages` holds rows keyed by *thread* id, while `guild.channels` excludes threads. `_viewable_channel_ids` unions
`guild.threads` in for exactly this reason.

That covers live threads only, and it cannot be made to cover archived ones.
discord.py drops a thread from the guild cache the moment it archives
(`state.parse_thread_update` → `guild._remove_thread`), Discord archives after
24h–1 week of quiet, and `processed_messages` stores no parent id, so for an
archived thread there is no cached object *and* no parent channel whose
visibility could stand in. `get_channel_or_thread` does not help: it reads the
same two caches. Those rows stay excluded, because visibility that cannot be
verified is not visibility.

What that must not do is make the field contradict its own header. A member
whose month was spent in since-archived threads has a real 30-day count and
nothing nameable to attribute it to, so `_activity_text` distinguishes the
two empty cases: "no messages recorded" only when the count is genuinely zero,
and otherwise a line saying the places can't be listed. Fixing this properly
would mean storing `parent_id` on `processed_messages` ; a migration plus an
ingest change in the XP path, out of scope here.

## The buttons re-enter existing flows

`member_info/views.py` grants no role, writes no opt-in and clears none. Every
button calls the flow that already owns its feature:

| Feature | Entry point |
|---|---|
| Pen Pals | `pen_pals.cog._handle_join` / `_handle_leave`, `source="info_panel"` |
| Whispers | `WhisperCog._optin_impl` / `_optout_impl` |
| Guess | `GuessCog._optin_impl` / `_optout_impl` |
| DMs | `dm.perms.cog.open_dm_settings` |
| Wellness | `WellnessCog.open_setup`, labelled "Redo" (not "Settings") for an opted-in member, because that is what it is: finishing the wizard re-runs `opt_in_user`, whose upsert overwrites `enforcement_level` with whatever the wizard's select just collected. There is no member-facing read-only settings view to point at. `opt_in_user` no longer touches `notifications_pref` unless a caller explicitly passes one: the wizard never asks about notifications, so a re-opt-in now leaves it alone; that reset (which once silently switched a member's DMs back on) was fixed on 2026-08-22 |
| Birthday | `_BirthdayModal`, or `BirthdayCog.remove_impl` |
| No-contact | `ServerFeaturesCog.open_no_contact`, the hub's No Contact card |
| My data | `PrivacyCog.open_panel` |

This is a safety property, not a convenience. Each of those flows carries the
gate that is its feature's whole compliance story: Guess and Whispers show
consent copy and only grant the role once it is accepted, Pen Pals checks the
guild's opt-in role, wellness cannot opt anyone in without a timezone. A panel
that flipped roles itself would be a second, ungated door into all of them.

Several of those entry points were extracted from command bodies in this
change (Guess, wellness, DM settings, birthday-remove, no-contact-list). The
commands now call the same impls, so there is one implementation per flow.

If a feature's cog is not loaded, `FeatureState.actionable=False`: the status
row still renders, the button is never built.

`MemberInfoView` is an `ExpiringLayoutView`: after five minutes every button
grays out in place rather than failing on a press. A stale press inside the
window is still safe: each flow re-reads state and refuses on its own terms.

### Where /info is listed

`/help`'s **General** page is a hand-maintained literal in
`moderation.cog._build_help_pages`, not generated, so a new member command has to be
added there by hand or it is reachable only through the secondary
"Browse by Module" pager. CLAUDE.md names `/help` and `manual.html` as the
command reference, so both carry `/info`, and a test fails if the help list
loses it.

## Data

No new tables, so no `docs/data_register.md` row. Every read is either the
caller's own data or guild configuration. The panel is always ephemeral.

## Tests

`tests/member_info/test_member_info_logic.py` covers every feature's configured /
unconfigured branch, every state's action and its label width, the
not-actionable degradation, the top-channel visibility filter, the no-contact
indistinguishability rule, `level_progress`'s clamps and `xp_to_next_level`.
`tests/member_info/test_member_info_layouts.py` renders the fullest card against the V2
budget and carries the embed's regressions (no moderation fields, the roles
cap, the activity empty cases, the brand-new member) plus the bar, rank and
view binding. `get_member_xp_rank`'s ties and exclusions are in
`tests/xp/test_xp_system.py`. The card carries no accent, so it left
`tests/contracts/test_embed_accent_contract.py` with the embed.
