# Dashboard information architecture

Reference. How the dashboard's sidebar is organized, where a feature's settings
live, and the naming rules new panels follow. The nav itself is defined in one
place (`SECTIONS` in `src/web_server/static/js/app.js`) and the Help nav is
generated from `static/js/panels/help-sections.js`.

## Where settings live

CLAUDE.md's rule is "configuration lives on the web dashboard, not Discord".
That is about the *surface*, and it still holds. Within the dashboard the rule
is narrower:

> **Settings live with the data they produce.** A feature that already has a
> report or a queue keeps its dials at the bottom of that page, one pane. A
> feature with no such page keeps them under **Config**.

**Amendment: a moderator *job* may take a heading, and the settings
that job needs live under it.** Role Management is the first: five pages
ordered the way the job runs. Promotion
Reviews is settings-only and still sits there rather than under Config,
because the question "who can hand out what, and who is up for more" is one
someone answers in one sitting, and sending them to Config mid-task is the
same split IA3 undid for new members. The rule's spirit is intact: nothing
is *duplicated*, and if a promotion-review queue is ever built it lands on
that same page, not beside it.

One feature remains on the first side of that line: **Policy Tickets**, whose
settings half is a single field (the voting deadline) that never justified a
page of its own; it renders read-only for non-admins (`lockUnlessAdmin`) at
the bottom of the proposal queue, Moderation → Policy Tickets.

The other four pages that were once merged (XP & Leveling, Voice, Birthdays,
Intake) are split by owner decision; see
`docs/plans/dashboard-config-ia.md` for the audit and rationale. Each has its
settings page under Config (`config-xp`, `config-voice-master`,
`config-birthday`, `config-intake`) and its report/queue half on
the frozen merged-page id in its original section. The halves cross-link via
`related:` chips, and `lockUnlessAdmin` stays in the settings modules as
defense in depth. Moderators get a read-only view of the settings by design (IA6); see *Read-only for
moderators* below.

Roughly thirty other features keep their settings under **Config**, grouped by
theme (Server, New Members, Members, Moderation & Safety, Channels &
Messages, Voice, AI & Maintenance).

The user-facing statement of the same rule is the **Where a Setting Lives**
subsection of `manual.html` Configuration Reference (help page
`help-config`). Keep the two in step.

## Sections

| Section | Gate | Contents |
|---|---|---|
| Dashboard | everyone | Home, Everyday Commands |
| Reports | moderator | Moderation / Activity / Engagement / Greeter / Member Lists |
| Moderation | moderator | Queues & Workflows / Role Management / Audit Logs / Privacy |
| Config | moderator (most pages read-only for moderators, IA6) | Server / New Members / Members / Moderation & Safety / Channels & Messages / Voice / AI & Maintenance |
| Economy | admin **or** the economy-manager role | Operations / Earning / Spending / Wagering |
| Wellness | opted-in members, plus manage-server/admin | Member-facing wellness surface |
| Games | admin **or** the game-host role | One alphabetical list of the 22 game pages, then Operations |
| Social | moderator | Guess Who, Whisper, Pen Pals, Confessions (read-only for moderators) |
| Help | everyone | Generated from `help-sections.js` |
| Dev | admin | Live log, system stats, QA tracker (+ guide page), command & panel usage, owner tools |

**IA3 (2026-08-29)**: the regroup that landed with the merged-page splits,
decided in `docs/plans/dashboard-config-ia.md`:

* Reports: "General" → **Activity** (the one heading that named nothing) and
  DAU/MAU joined its volume-metric siblings there; NSFW by Gender moved to
  the Moderation heading beside Flagged Messages (content analytics, not an
  engagement metric; both pages are since removed). Command & Panel Usage lives under **Dev**, its actual audience.
* Moderation: the eight bare items sit under the **Queues & Workflows**
  heading. The Image Guard pages and `config-spoiler` are retired and
  redirect to `config-global`. Audit↔config `related:` links are bidirectional across all sibling
  pairs.
* Config: new **New Members** heading carries the whole newcomer job in
  order: Welcome & Leave, Intake Cards, Auto-Role, Discord Onboarding,
  Greeting Watch (from three headings before).
* Games → Operations gained Event Echo, the one games page that lived
  outside Games.
* Labels: "Auto React" → "Auto-React"; Home's item label "Dashboard" →
  "Home" (the IA2 Bank fix, applied to the same collision here);
  the inactive pair reads as a parallel pair: "Inactive Role Removal" /
  "Inactive Kick Sweep"; "Blocked Images"/"Image Tags" → "Image Guard
  Blocks"/"Image Guard Tags"; "Anonymous Features" → "Anonymity Audit";
  "Moderation" → "Moderation & Privacy" (it holds the retention dials). Every old label survives as a search keyword.

**Mod Coverage (2026-08-30)**: `mod-coverage`, added under Reports →
Moderation as a *third* moderator report rather than absorbing the two that
exist, deliberately: Mod Workload counts moderation *actions*, and this server
records roughly 66 of them a month, so a report built on those counts is
nearly signal-free. Coverage asks a question the message archive can actually
answer: when is the server busy, and is a moderator talking then? It
uses a **wider** moderator population (anyone with Manage Messages) than the
kick/ban/manage-guild set the other two count. The two populations are
`_guild_extras`' `msg_mod_ids` and `mod_ids` respectively; they are not
expected to agree, and neither is a bug in the other.

Consolidating the three remains open. If it happens, Mod Workload is the one
to fold in; Mod Engagement measures something genuinely separate.

**Bot-Managed Roles (2026-09-03)**: `bot-roles`, third under Config → Roles
after Role Grants and Reaction Roles (the heading is Moderation → Role Management). A new id rather than a section on an
existing page, and the reasoning is an IA one worth keeping: `config-roles`
*means* Role Grants (`/grant` allowlists): hanging the bot's own roles off it
would be a naming coincidence, not a decision, and Discord Onboarding's job is
narrower (getting roles to members) and can only see the seven opt-in ones.
This page is the only surface that can list all sixteen, including the nine no
page has a dropdown for. It is an audit surface rather than a form, which is why
it opens with a sentence instead of a settings grid.

**Role Management (2026-09-07)**: a new Moderation heading, second after
Queues & Workflows, holding five pages in the order the job runs: **Role
Grants** (decide what can be handed out) → **Reaction Roles** (let members
self-serve) → **Bot-Managed Roles** (provision the roles the bot needs) →
**Promotion Reviews** (review who is up for more) → **Grant Audit** (read back
who granted what). Grant Audit sits here, beside the page whose grants it
records, not under Audit Logs. No id was renamed, so no
`MOVED_PAGES` entry is needed and every deep link lands.

**Privacy (2026-09-13)**: a new Moderation heading, last, holding one
admin-only page: **Member Data** (`member-data`). The three GDPR paths in
`privacy/service.py`: export (Art 15/20), purge (Art 17) and anonymisation,
get one page, with a subject picker at the top and a tab
per operation, plus a tab holding the Discord-message deletion and a
Departures tab. There is no command for any of it, per
CLAUDE.md's rule that an admin job lives on the web. The page is `adminOnly`,
so moderators have no message-deletion surface. Deliberate.

`member-data`, not `privacy`: the bare feature name per § Naming, and it must
not read as a sibling of Config → **Moderation & Privacy** (`config-moderation`),
which owns the *retention dials*. That page sets policy for everyone; this one
runs operations on one member, which is why it is filed under Moderation
(where the work happens) rather than Config (where the policy lives).

Anonymise's Apply is **primary-guild only** and says so on the page rather than
hiding the control, per § Bot-global controls: a reader who was *sent* to a control
gets told why it is unavailable. It rewrites the member's id across every guild
the bot serves at once, and one of those guilds is an economy someone else runs.

Two role-adjacent pages stayed where they were, deliberately. **Auto-Role**
and **Discord Onboarding** are steps in Config → New Members, the ordered
narrative IA3 assembled for the whole newcomer job; pulling them out would
break a sequence to fix a category. **Inactive Role Removal** stays paired
with Inactive Kick Sweep under Config → Members: it is an inactivity policy
that happens to act on a role, and it is cross-linked to its report.

`server-features` (2026-09-26, bug-list row 259) sits under **Config →
Members** beside Take a Break. It posts the members' hub
([server_features_spec.md](specs/server_features_spec.md)). The hub has no settings
of its own, so the page holds only the Post control and a static map from each
of the hub's nine rows to the page where that feature is configured. It gives
no "is this set up" status per row. Each row already answers "not set up" when
pressed, and a second copy of nine features' readiness rules would drift from
the real ones.

`promotion-reviews` has its own id for three dials: Promotion Reviews
Channel, Ping Role and Grant Role. They do not belong on the XP & Leveling
form, and not only for filing reasons. Two of the three things that post a
review card (a pruned member returning, a sleeper waking) have nothing to do
with XP, and because the XP page saves as one payload, changing any XP dial
rewrote all three (that is how `promotion_review_ping_role_id` reached
production as 0, sending every card's ping to the fallback @moderator for
months). Its own page means its own PUT: the panel sends exactly its three
fields, and `PUT /api/config/xp` leaves every field it is not sent alone. `level_5_role_id` and the Level-Up Log
Channel stayed on XP & Leveling, the latter with a hint pointing here, since
pointing both channels at one place is what suppresses the duplicate level-5
notice.

## Read-only for moderators (IA6, 2026-09-16)

A locked, unclickable row would be visible proof a page exists, with no way to see what it says. A moderator asking "is the welcome message
still pointing at the old channel?" had to ask an admin.

Those pages now carry `mod: "read"`. An admin gets the
page as before; a **moderator opens it and every control on it is held
disabled**, behind a banner saying so; everyone else still sees nothing. The
hold is central: `holdReadOnly` in `config-helpers.js`, applied by the router
in `app.js` when the resolved nav item carries `readOnly`, not by each panel,
so a new page opts in by changing one word in `SECTIONS` and cannot forget to
lock a control it renders later. It is a `MutationObserver`, because these
panels render their rows and pickers after mount; the older one-shot
`lockSection`/`lockUnlessAdmin` helpers stay where they are, guarding a
*region* of an otherwise-writable page.

`adminOnly` and `modReadOnly` were two booleans on one axis, and nothing
stopped an item carrying both (whereupon the read-only branch silently won).
They are one field now: `mod: "locked" | "read"`, absent meaning the item
follows its section and `perms` like any other. To hide a page from moderators
entirely, give it `perms: ["admin"]`; that is the axis's third state and it
already existed. `tests/web/test_moderator_read_access.py` fails on a `mod`
value `resolveItem` does not handle, since the value is now a bare string in a
file with no types.

Anything inside `[data-readonly-ok]` stays live. A control qualifies when
**pressing it cannot change anything on the server**, not when it makes no
request at all. The first draft of this rule said "cannot reach the server"
and was wrong on its own only instance: the shared tab strip (`mountTabs`)
carries the opt-out, and switching a tab does fetch, because each pane loads
through `mountAsync`. Those are GETs a moderator is already allowed to make,
which is the actual test.

Two things carry it today. The tab strip, which covers every tabbed page at
once: disabling it would strand a moderator on whichever tab opened first
with the rest of the page unreadable. And Feature Rotation's **Refresh**
button, which re-runs the same moderator-readable `GET /feature-rotation`
the page already ran on mount. A refresh button is the shape most likely to
want this next; a **Save**, **Post**, **Sync** or **Test** button never does,
whatever it is labelled.

The lock is a **courtesy, not the enforcement**. Every write behind these pages
stays admin-gated server-side, and `tests/web/test_moderator_read_access.py`
pins each opened read next to a write that must still refuse.

Three decisions shape the edges:

* **Anonymity audits do not open.** DM, Whisper, Confessions, Anonymity, Guess
  Who and Quotes each name the member behind something the server presents as
  unattributed, so they keep `mod: "locked"` and stay locked. The moderation Audit
  Log (a record of what moderators themselves did) is the one log that
  opened. Member Data stays `mod: "locked"` too.
* **Announcements is the one write grant.** Moderators compose, schedule, post
  now, clone and delete, with no channel or mention restriction (`@everyone`
  included). The bot sends the message, so Discord's own Mention @everyone
  permission never applies; that is the grant, understood and accepted.
* **A page whose read was admin-only had to be opened to match.** Bot-Managed
  Roles, Discord Onboarding, Music Playlist, Feature Rotation, Quote Tool, AI
  Assistant and the moderation Audit Log had admin-gated GETs; opening the page
  without opening its read would have shipped a page that renders an error. One
  read stayed shut on purpose: `GET /help/suggestions`, the assistant's setup-gap
  list, because an inventory of what a server has not configured is
  reconnaissance.

The audit that produced this, page by page, is
`docs/plans/moderator-dashboard-access.md`.

## Naming: label-vs-id drift

Ids are frozen (deep links, `help:` mappings and usage telemetry key off
them); labels move freely. This table records the drift so future sessions
stop re-deriving it, and are not tempted to "fix" an id. The merged-page
splits healed the worst of it (`voice-activity`, `xp-leaderboard`,
`intake-report`, `birthday-calendar` all match their labels again).

| id | label today |
|---|---|
| `retention` | Activity Drops |
| `config-spoiler` | retired 2026-09-15 → `config-global` (`MOVED_PAGES`) |
| `config-needle` | Auto-Thread |
| `config-prune` | Inactive Role Removal |
| `config-inactive` | Inactive Kick Sweep |
| `config-quote-border` | Quote Tool |
| `config-moderation` | Moderation & Privacy |
| `anon-audit` | Anonymity Audit |
| `economy-sinks` | Shop & Perks |
| `economy-bank-manager` | Bank |
| `mod-todo` | Todo List |
| `config-advisor` | AI Assistant |
| `config-ai` | AI Models |
| `usage-telemetry` | Command & Panel Usage |
| `server-features` | Help & Settings |

**Games** is a flat list (todo #165), deliberately, not by drift:

* **the 22 game pages**, one alphabetical list of section-level `items`, no
  heading. Splitting them by a property of the *page* (a bank is
  a prompt library you edit, a live game is dials for something that runs in a
  channel) is a real distinction but not one you hold in mind while
  looking a game up. You know the game's name; a split makes you decide which
  of two lists it is in first, and check both when you guess wrong.
  Hot Takes and Fantasies & Dealbreakers sit in the list under
  the bare ids `hottakes` / `fantasies`; like AMA they are member-written,
  so they carry one pacing dial and no bank.
* **Operations**: Play Statistics, Scheduling, Global Config, External
  Tracking, Event Echo. Kept its heading: those five are server plumbing, not
  a game. It stays a `group` so it renders *below* the games (bare `items`
  always render above `groups` in `renderNav`), which also avoids a "Games"
  heading inside the Games section (the Dashboard/Dashboard collision IA2
  fixed).

The flattening is safe because "has a question bank" is not a
nav-level fact. The eight bank games' pages open on a **Questions** tab
with a **Settings** tab beside it (`games-panel-shared.js`), so the
distinction the Question Banks heading carried is stated on the page itself,
where it is actionable. Those eight carry `question bank` in their `keywords`:
the nav filter matches heading text (`app.js`, the `dataset.search` line), so
dropping the heading would otherwise stop a search for "question bank"
from finding them. `byLabel` alphabetises at render, so
22 rows read as a list rather than a pile.

**Economy** has four subgroups (IA2):

* **Operations**: Bank, Statistics, Settings.
* **Earning**: Income Sources, Quests, Claims, Mention Awards, QOTD.
* **Spending**: Approvals, Shop & Perks, Pricing. Three pages; see below.
* **Wagering**: Casino, Pools.

**Spending is three pages** (it was one 1,339-line page), split by what each part
*is* rather than by what data it touches:

| Page | id | Kind | Gate | Cadence |
|---|---|---|---|---|
| Approvals | `shop-approvals` | work queues: emoji submissions, custom-item orders | **not** adminOnly | whenever something is bought |
| Shop & Perks | `economy-sinks` | what's on sale (per-perk switches) + catalogs: palette, role icons, custom items | adminOnly | setup burst, then dormant |
| Pricing | `pricing` | one form of priced dials | adminOnly | launch, then a few times a year |

`economy-sinks` keeps its id because the page still exists, so no `MOVED_PAGES`
entry is owed: that mechanism is for **retired** ids. The `order` field states
the frequency deliberately: the queue you work is first and the prices you
rarely touch are last, rather than leaving it to an alphabetical accident that a
future relabel would silently reshuffle.

Three things forced the split, and none of them were the page's height:

1. **Approvals must not be adminOnly.** `/api/economy/emoji-submissions` is
   gated `require_economy_manager`, so the backend grants the economy-manager
   role access, but the queue lived on an adminOnly page, so a manager could
   never reach it. Claims and QOTD, the comparable queues, were already open.
   Loosening the settings page's gate instead would have handed managers the
   hoard-tax dial.
2. **One Save governed six unrelated groups.** All six dial cards sat in a single
   `<form>`, so saving a raffle price also wrote the tax rate. The form is now
   the whole of `pricing`.
3. **A shared dirty bit.** `guardForm`'s unsaved-edits flag is a module global in
   `config-helpers.js` and `showStatus(el, true, …)` clears it. The old page had
   41 `showStatus` call sites, so approving an emoji (or merely *starting* an
   image upload) cleared the warning protecting a half-typed price.

The precedent that settles the "a price belongs beside its queue" objection was
sponsored QOTD, whose price and submission queue sat on separate pages for as long as the
feature existed.

Cross-page hints are the maintenance cost. A palette row priced 0 falls through
to the flat dial on `pricing`, and the icon catalog overrides another; those
hints are links, not "above" and "further down". When
moving a dial away from the thing it prices, grep the hint text.

Two rules are at work and both are load-bearing. Headings are **the job**: run
it, pay it out, spend it, wager it. Inside a heading a **feature that spans
pages stays whole**: Claims is the quest sign-off queue, so it sits under
Quests rather than in a queues-only group that reads tidy and then sends
someone hunting in the wrong place.

**One stated exception, for "is anyone waiting on us?".**
Billy's walkthrough note was that all economy approvals should be in one place,
which is exactly the queues-only group the rule above argues against. The rule
holds for *settings*; it does not survive contact with **work queues**,
because the two answer different questions. A setting is found by knowing what
feature you want to change, so keeping it beside its feature is right. A queue
is found by asking whether anybody is waiting: a question that is not about
any one feature, and that a split makes unanswerable without opening several
pages.

The exception is deliberately narrow, and it is a **finder, not a move**. The
`shop-approvals` page has an "Everything Waiting" list that unions all six
pending queues; every row says where it is handled and links there. Nothing
that resolves a request moved: each product keeps its own endpoint, its own
copy, its own side effects and its own page. So a feature still spans pages and
is still whole: there is one index over the pending work, which is not the
same as a queues-only group.

QOTD **merged** into one page. Its settings (`economy-qotd`, one role id) did not earn a nav slot; the retired id
redirects through `MOVED_PAGES`. The sponsored queue is gone, so the page
(`economy-qotd-submissions`, its id keeping the `-submissions` suffix because ids are frozen) is the settings card alone and
is `mod: "locked"` (admin-only), since a manager would otherwise open an empty
page. Merging worked because QOTD was genuinely a
one-knob page: Quests + Claims was considered and rejected, since burying a
daily sign-off queue behind a tab on an 800-line authoring page costs more than
the sidebar line it saves.

Two labels differ from older names (ids untouched, both old names kept as
search keywords). **Bank** is what the feature is called everywhere else
(`/bank`, the bank channel), and "Operations" would collide with the heading above it.
"Sinks" was economics jargon, so the page reads **Shop & Perks**, a sibling of the shop pages.

**Social** holds Guess Who, Whisper, Pen Pals and Confessions.
They are not games; they are ongoing social surfaces a moderator runs, and
Confessions' audit trail already lives under Moderation. In a moderator-gated
section none needs an explicit `perms: ["moderator"]` marker. Confessions renders for a plain moderator
**read-only and openable** (IA6). Its writes stay admin-gated server-side, the Post Button included (it
goes through the shared panel registry, not a route of its own).
`tests/web/test_nav_visibility.py` pins the whole table per audience.

## Naming and route ids

* **Route ids are frozen.** Deep links, bookmarks, the `help:` mappings and the
  never-opened list in usage telemetry all key off them. Regroup and relabel
  freely; never rename an id.
* **New route ids are the bare feature name**: `pen-pals`, `role-menus`,
  `chat-revive`, `photo-challenge`, with no `config-`/`games-`/`mod-` prefix.
  The prefixes on older ids are historical (they encoded the section a page
  happened to sit in, which is exactly the thing that moves).
* **A new nav entry carries a `help:` id** in `SECTIONS` (`static/js/app.js`),
  naming the `manual.html` section that documents the page (`help:
  "help-ping-response"`). Every Config entry has one: where the manual has
  no section of its own for a page, `help-config` (the Configuration Reference
  table) is the answer, and `tests/web/test_help_links.py` fails if a Config
  entry lacks one or any `help:` id names no help page. Older entries in the
  other sections without one are backlog, not precedent.
* **A `help:` id points at an `<h2>` or `<h3>` anchor** in `manual.html`. The
  help panel's `extractSectionContent` starts a section only at those two
  levels, so an `<h4>` anchor falls through to its enclosing `<h2>` and the
  panel renders the whole chapter. A page that outgrows a subsection is
  promoted to `<h3>` in the manual (Pools was). A page whose name collides with
  another feature's vocabulary carries the specific terms in `keywords`:
  "Pools" alone also matches confession pools and the pen-pals pool.
* **Labels name the thing, not its implementation.** "AI Models" rather than
  "AI (Local LLM)", which only disambiguated it from the adjacent "AI Assistant"
  for someone who already knew the architecture. Old names live on in
  `keywords`, so the sidebar filter still finds a page by what it used to be
  called.
* **No hardcoded branding in a label.** The assistant's name is per-guild
  (Config → Branding). The Help nav entry carries `brand: "assistant"` and is
  re-labelled from `/api/help/advisor/name` at boot and on every guild switch;
  the static label is only the fallback, and it must keep matching the manual's
  own `ask-guide` heading so the help panel still de-duplicates the title.

## Bot-global controls on a secondary guild

Some controls are bot-global rather than per-guild (the AI prompts, server file
paths), and their routes 403 off the primary guild.
Panels handle that in one of two ways, and the difference is deliberate: do
not harmonize them:

* **Explain it** when something else points the admin at the control. A
  panel that the manual or a sibling page names as the home of a bot-global
  control renders a hint saying the control is shared by every server and can
  only be edited from the primary server's copy of the page; a secondary-guild
  admin sent there who found nothing would reasonably conclude the page was
  broken.
* **Omit it silently** when nothing does. `config-global.js` simply doesn't
  render its "Server File Paths" card off-primary. No manual section, help
  entry or sibling panel mentions that card, so a secondary-guild admin has no
  reason to expect it, and a note explaining an absence they never noticed is
  just noise on a page they can't act on.

The test is whether a reader was *sent* to the control. If you add a pointer to
a bot-global control from anywhere a secondary-guild admin can read, the
destination panel needs the explanatory hint.

## Panel-local URL state

Route convention: `#/<page-id>?key=val&…`. Panel state that a user would expect
a refresh (or a pasted link) to preserve (tab, filter, search, selected row)
belongs in the query part, written with `syncHash()` from `report-helpers.js`
(`history.replaceState`, so the panel is never remounted for its own updates)
and read back from the `initialParams` argument to `mount()`.

Every enumerated param is validated against its own value list on read, and id
params are parsed as numbers that simply match no row when they're junk: a
stale or hand-edited URL must fall back to the default view, never error.

Adopted by seventeen analytics panels under Reports (Ping Response included),
by Grant Audit under Moderation → Audit Logs, by Command & Panel Usage under
Dev, and by all four mod workflow panels (tickets, jails, qa-tracker,
todo). Tests:
`tests/web/test_mod_queue_deeplinks.py` (round trip in a browser) and the
`syncHash`-id sweep in `tests/web/test_frontend_wiring.py`.

## Command palette (Ctrl/Cmd+K)

Additive to the sidebar filter, which is unchanged. `Ctrl`/`Cmd`+`K` opens an
overlay (`app.js`, "Command palette") that searches:

1. **Pages**: over `ALL_PAGES`, which `rebuildIndex()` has already narrowed to
   what this viewer may open, so the palette can never surface a page the nav
   wouldn't. Results are a flat ranked list showing the label over its section,
   ranked label-prefix → label-substring → keyword-only.
2. **The guide**: headings from `manual.html`, via `manualHeadings()` exported
   from `panels/help.js`. Loaded lazily on the first query and imported with the
   router's own `?v=` specifier so it shares one module instance and one parse
   of the manual with the help panel.

Combobox semantics: focus stays in the input, options are referenced by
`aria-activedescendant`, arrows move the selection, `Enter` opens, `Esc` closes
and returns focus to wherever it came from. Tests live in
`tests/web/test_nav_visibility.py`.
