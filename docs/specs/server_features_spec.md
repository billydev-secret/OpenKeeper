# Help & Settings (Server Features) — Feature Spec

**Name.** Members and admins see this panel as **Help & Settings**. The
code uses the older name **Server Features**: the `server_features` module, the dashboard route id
`server-features` and the `server_features:*` custom ids. All three are frozen
(`docs/dashboard_ia.md`, and the ids route presses on copies already posted),
so the name differs in copy only.

One public panel where a member does things for themselves (bug-list row
259: "combine the make a ticket, ask poppy, feature opt-ins,
no contact and data services into a single server features panel"). Built from
`docs/plans/server-features-panel.md` (gitignored; recover it with
`git show 678a16fa:docs/plans/server-features-panel.md`).

Code: `bot_modules/server_features/cog.py` (views and dispatch),
`bot_modules/server_features/{logic,service,layouts}.py`, and
`bot_modules/no_contact/service.py` (`member_add` / `member_remove` /
`member_entries`).

## The hub

Bare, the same as the DM Requests hub (guide
§ No container): `hub_banner`/`hub_footer` art, titled `## 🧭 Help & Settings`. It has four
groups and one Section row per area. Every row has the same grey **Open**
button.

| Group | Row | Press opens (ephemeral) | Owner |
|---|---|---|---|
| Get Help | 🎫 Open a Ticket | the ticket modal (`_TicketOpenModal`), which answers "not set up" itself on submit | `JailCog` |
| | 🤖 Ask *{assistant}* | the Ask cooldown check, then the Ask modal, the same two steps as the Ask panel | `AdvisorCog` |
| Your Settings | 🔔 Roles & Pings | the **Roles & Pings** card (below) | this cog |
| | 📨 Ask to DM | the DM Requests panel's **Open Form** button: `dm.perms.cog.DmRequestModal`, the one-step request form, which runs every pre-check itself at submit. It sits above DM Settings (Ben: "to the left of the dm preferences"; reaching out, then your own preference, the order that panel keeps) so the panel's channel can be dropped. The panel's **Help** (the `dmperms` FAQ card) does not move with it. **A deliberate exception** to the style guide's § Controls rule that a row reaching *another member* gets a group of its own, away from rows that configure *yourself*: Ben asked for it beside DM Settings, and a fifth group's heading and divider would take the hub with art from 38 to the 40-component ceiling | `DmPermsCog` |
| | 📬 DM Settings | `dm_perms_cog.open_dm_settings` | `DmPermsCog` |
| | 💤 Take a Break | the Take a Break panel, sent ephemerally as BreaksCog's own bound `BreakPanelView` (no art), so this row adds no break code. It must be the bound view: discord.py answers a press from the pressed message's own view before any persistent view, so a control-free card carrying the `break:start` id swallowed the press silently (fixed 2026-09-30; `tests/server_features/test_server_features_hub_break.py` fails if it comes back) | `BreaksCog` |
| About You | ℹ️ Your Info | `MemberInfoCog.open_panel`: the `/info` card itself, one render path for both doors (`docs/specs/member_info_spec.md`). Sits above Safety & Privacy on Billy's call ("do it above safety") | `MemberInfoCog` |
| Safety & Privacy | 🛡️ No Contact | the **No Contact** card (below) | this cog |
| | 📜 My Data | `PrivacyCog.open_panel`, the panel that was `/mydata` | `PrivacyCog` |

Every row always shows. The hub is redrawn only on a restart or an art change
(below), not when a feature's settings are saved, so hiding a row because a
feature wasn't configured would outlive the fix until the next restart. A row whose
owning cog isn't loaded answers "isn't available right now". A feature that
isn't configured answers with that feature's own "not set up" line.

The assistant's name in the Ask row is read with `resolve_assistant_name_conn`
when the panel is posted.

**Title in the banner.** The hub follows the DM Requests hub (style guide § The
paradigms, the hub checklist). When the guild ticks **Title in the Banner** on
the Branding page, `panel_title_service.with_title` draws "Help & Settings"
into the banner at post time, in a worker thread. The builder then drops its
`## 🧭 Help & Settings` line, and the banner's alt text carries the name. With
no banner, or when the drawing fails, the text heading stays.

**Repaint on restart and art changes.** The hub is posted once, not kept
sticky, but each post is recorded in `posted_panels`. The panel refresher
(`docs/plans/panel-refresh.md`, recoverable with
`git show 678a16fa:docs/plans/panel-refresh.md`) then edits every recorded copy in place once
per boot and on every `panel_art_change`, through
`ServerFeaturesCog.build_server_features_panel` (the spec's `render`). Posting
again into a channel that already holds the hub moves it: the new copy goes
up, then the old one there is deleted. A copy posted before the record existed
needs one re-post to join.

Persistent custom ids `server_features:{ticket,ask,roles,ask_to_dm,dms,break,info,no_contact,my_data}`
are **frozen** (`tests/server_features/test_server_features_layouts.py`). The view is registered
once in `cog_load`, so every posted copy survives a restart.

**Posting.** The panel has no slash command. The dashboard's **Config → Members →
Help & Settings** page posts it through the shared panel registry
(`PanelSpec(key="server-features")`, `POST /api/panels/server-features/post`).
The page has no settings of its own; it is a static map from each row to the
page where that feature is set up. **Ships dark:** nothing appears until an
admin presses Post. The older Ticket, Ask, DM Requests and Take a Break panels
keep working, and admins can stop posting them.

## Roles & Pings card

Paradigm 2 (settings). Every press re-reads the card
(`service.read_card`) before acting, so the press is judged against the server
as it is now, not as it was when the card opened.

- **Bot pings.** The bot's own member-facing ping roles
  (`logic.MEMBER_PINGS`) whose role exists and whose dial isn't "(none)". Each
  row reads On/Off in words and has a toggle. Staff roles are never listed
  (`logic.EXCLUDED`); every staff job ping (`feature_roles.STAFF_ROLES`,
  `staff=True`) is kept out of `CONFIG_ROLES` itself, so neither this card nor
  Discord onboarding offers one; moderators opt in on the Mod Settings panel
  ([staff_pings_spec.md](staff_pings_spec.md)). **The card never provisions a role:** a `create_on_offer`
  role that hasn't been created yet is simply absent.
- **Bot DMs.** One row, **🧵 Replies to Your Threads**: the Auto-Thread
  reply-DM opt-in (`needle_reply_optins`; `needle_spec.md` § Reply DMs). Not a
  role but a table row, so nobody can see who asked for DMs, and drawn like a
  ping row (On/Off in words, Turn On/Turn Off). Shown while some
  auto-threaded channel in the guild has its reply-DM dial on
  (`needle.store.reply_dms_offered`), since with every dial off the switch would do
  nothing, and always to a member who is opted in, so they keep an off switch
  (`logic.reply_dms_row_state`). A press whose fresh card no longer offers it writes nothing and
  says the option isn't offered any more.
- **From Customize.** One select per Discord onboarding prompt (live
  `guild.onboarding()` per open). A single-select prompt allows at most 1; a
  multi-select prompt allows up to all. The select's current picks are
  preselected. An option's value is its sorted role-id set, not its position,
  so a pick survives an admin reordering the prompt. Options with an identical
  role set collapse to one. The following are dropped, not shown and then
  refused:
  - channel-only options;
  - any role failing `role_block_reason` or above the bot.

  The card holds at most 25 options per menu and 5 menus, or 4 when the Bot
  DMs row is showing, which costs the fifth menu's components
  (`logic.MAX_MENUS_WITH_REPLY_DMS`, 40 exactly). Past that, the card
  says so and points at **Channels & Roles**. A role offered in both sections
  shows once, under Customize. If the `Forbidden` exception comes back from
  `guild.onboarding()`, the card falls back to the Bot pings section alone.
- **Grant path.** `service.apply_change` re-checks `role_block_reason` against
  the bot's own top role immediately before `add_roles` (the role-grant safety
  contract). Removals always go through.
- **Held members.** A member who is jailed, inactive or on a break
  (`service.HOLD_ROLES`, or an active jail row) sees `HOLD_TEXT` and no
  controls. Changing roles under a hold would fight the hold's own role
  bookkeeping.
- The redraw after a change is built from `logic.with_change`, not the member
  cache, which is stale right after the REST call.

## No Contact card

Paradigms 2, 4, 6 and 10. There is no `/nocontact` command. The indistinguishability rules in
[no_contact_spec.md](no_contact_spec.md) moved into the service layer with the
copy, and are pinned there (`tests/no_contact/test_no_contact_service.py`,
`tests/no_contact/test_no_contact_logic.py`).

- The list shows the entries visible to the member (`is_visible_to` via
  `own_entries`), paged at ten.
- **Remove** appears only beside an entry the member may lift. A moderator-set
  entry reads "· set by a moderator" and has no button. Remove leads to a
  confirm step ("Yes, Remove" / Cancel). A remove that finds nothing answers
  "there's no entry between you two", never "you can't".
- **Add Someone** opens a modal with a user select and an optional reason. The
  reason is described as "Only moderators see this — never the person you
  add". Adding yourself or a bot is refused with its own line. A duplicate add
  reads exactly like a fresh one.
- The card re-reads the list after every change and after a page turn.

## Removed commands

- `/nocontact add|remove|list` (the `no_contact_cog` now holds only the
  on_message alert watcher).
- `/mydata` (the panel body is `PrivacyCog.open_panel`).

`/info`'s buttons reach both cards: **No contact** opens this cog's card, and
**My data** was added for the data panel. Members therefore keep a way in
before an admin posts the hub.

`tests/privacy/test_privacy_member_data_panel.py` still hard-fails on any erasure
command. Its walk anchor moved to `/ask` when `/mydata` left.
