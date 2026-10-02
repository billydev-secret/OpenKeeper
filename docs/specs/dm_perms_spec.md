# DM Permissions — Feature Spec

A consent gate for Discord DMs **within this community**. Every member carries one of three "DM mode" roles (Open / Ask / Closed). When someone wants to contact an Ask-mode member, they open a request via a persistent server panel; the target gets a DM with Accept / Decline buttons. Accept records a **bidirectional consent pair**, both sides get a confirmation DM, and either side can revoke at any time. Every state transition writes to an audit log and optionally fan-outs to a mod-visible audit channel.

This is **not** a Discord-friend system; it lives entirely inside the bot. The only Discord-side artifacts are role assignments and the DM messages carrying the buttons.

Every card this feature sends is a **Components V2 layout**, not an embed (`docs/components_v2_style_guide.md`). The flag is per message and one-way, so the feature converted whole: a settings panel beside an embed receipt would read as two different bots. The builders live in `bot_modules/dm/perms/layouts.py` and are pure: primitives and a `name_fn` in, components out.

## Surface

| Command | Type | Permission | Purpose |
|---|---|---|---|
| **Help** button | Persistent panel | Everyone | Shows the server's DM FAQ doc (`doc_key='dmperms'`, as last saved in the dashboard Docs panel) on a private card |
| **Open Form** button | Persistent panel | Everyone | Opens the one-step request modal. Also the **📨 Ask to DM** row of the Help & Settings hub (`docs/specs/server_features_spec.md`), which sends the same modal |
| **DM Settings** button | Persistent panel | Everyone | Opens the ephemeral settings panel below. Also the hub's **📬 DM Settings** row |
| DM mode dropdown (Open / Ask / Closed) | Ephemeral settings panel | Everyone | Assign yourself one of the `DMs: Open / Ask / Closed` roles; the current one is pre-selected |
| Your Connections list | Ephemeral settings panel | Everyone | One row per connected member: name and kind (`DM` / `Friend`) |
| **Remove** button (per row) | Ephemeral settings panel | Everyone | Removes that consent pair, behind a Yes/Cancel confirm card |
| **◀** / **▶** buttons | Ephemeral settings panel | Everyone | Page through the connection list, ten rows a page; present only past ten connections |
| **Accept** / **Decline** / **Decline + Note** buttons | Persistent in-DM | Target only | Routes via the message id back to the pending request |
| DM config | Web (dashboard) | Admin | Set the audit channel, the status-role overrides, and the two request-lifecycle dials |
| Post panel | Web (dashboard) | Admin | Force-(re)post the panel into a chosen channel |
| DM audit log | Web (dashboard) | Admin | Paginated audit-log browser with optional action/type filters |

The panel itself is one card with its two entry points in **separate headed groups**: a title with **Help** beside it and one sentence, a visible divider, then `### Ask Someone` with the row "Ask to DM someone or add them as a friend." and **Open Form** beside it, a visible divider, then `### Your Preference` with "Choose who can message you directly." and **DM Settings** beside it. Each group closes on its own `-#` line under the group it is about: "If they accept, you're connected — either of you can undo it in DM Settings." under *Ask Someone*, "Open, Ask or Closed — change it whenever you like, and see who you're connected to." under *Your Preference* (Billy, 2026-09-24: a blurb under the preferences "so they're even", and friendlier rows than the first cut's "Pick who, and say why."). Stacked back to back the two rows read as one list although one reaches out to another member and the other configures yourself (Billy, 2026-09-20: "can you put some sort of separation between requesting and setting your preference"). This is now the general hub rule in `docs/components_v2_style_guide.md` § Controls, not a choice local to this panel. The three-status legend the first cut carried under *Your Preference* came off the same day (Billy: "let's drop the 'your status shows on your profile' line"); the settings panel spells the modes out at the moment a member is choosing one. The two buttons' `custom_id`s are on-disk state and never change; their labels are not (the boot-time `place_or_refresh` repaints the panel), which is how **Settings** became **DM Settings**, the name of the panel it opens, matching its heading and every card that points back to it.

**The panel is bare, like every other card.** A box would let the text and the buttons overhang the banner's gold frame, and Discord can neither pad a line of text nor hide a box's fill, so the panel art's frame sits at the picture's edge, which is the edge the text and the buttons keep (guide § No container, § Media). The builder takes no color and the cog resolves none (`test_posted_panel_is_bare`; `tests/contracts/test_layout_no_container.py` has no allow-listed container). When the guild has a **panel banner** (dashboard **Config → Server → Branding → Panel Art**, none by default), it is the message's first item, above the title; a **panel footer** set there is the last, closing the card (`build_panel_layout(banner_url=…, footer_url=…)`, both read with `branding_service.resolve_panel_art_conn` when the sticky builds). The ephemeral cards the panel opens never carry it (Billy: "just the main panels that live in the channel"; guide § Media). Uploading or removing either image repaints the posted panel at once (the dashboard dispatches `panel_art_change`). With the Panel Art card's **Title in the Banner** switch on (off by default) and a banner set, the bot draws "DM Requests" across the foot of the banner (`panels.title_service.with_title`, guide § Media), the `## 📬 DM Requests` line goes, and **Help** moves up beside the intro line; the banner's alt text carries the title (`build_panel_layout(title_in_banner=True)`). If the drawing fails the heading stays. Change the art and the switch **on the dashboard**, never by writing `branding_config` directly: only the dashboard's save dispatches `panel_art_change`, so an out-of-band write shows nothing until the sticky next reposts.

**Help** reads the guild's `dmperms` doc at click time (`docs.help_card.load_doc_body` with `dm_perms_service.HELP_DOC_KEY`) and shows it on an ephemeral card built by the shared `docs.help_card.build_doc_help_layout`, so the answer never drifts from the FAQ admins edit and post from the Docs panel. The doc's own leading heading becomes the card's `## 📬` title (a second, larger heading under the card title would read as two cards), capped at 200 characters; a `---` line in the doc draws a line on the card and an image shows as a picture; a doc past the message's text budget stops at a paragraph (never inside a fenced code block) with a `-#` line pointing to a moderator for the rest (the doc may be saved but never posted anywhere, so the card doesn't claim it's on the server); a guild with no such doc gets one line saying there's no FAQ yet. The card sends with no allowed mentions, so an admin-written role mention in the doc never pings. The doc is found by its key, so **renaming `dmperms` on the dashboard disconnects the button**. The button's `custom_id` is `dm_request:help`, persistent like the other two.

The words on the cards follow `docs/components_v2_style_guide.md` § Words, written from this feature's design pass: one name per thing ("DM Settings", "the DM Requests panel", `DM` / `Friend`, a requester's "reason" vs a decliner's "note"), a card addressed to one member naming the *other*, one heading glyph per outcome, a member's words as a `>` quote. `tests/dm/test_dm_perms_layouts.py` sweeps every card for the retired names and for a heading glyph outside the set.

## Behavior

### The three DM modes
- **Open**: anyone can DM. No request needed.
- **Ask**: a request is required; the target chooses to accept or deny.
- **Closed**: no requests possible; the system refuses up front.

Picking a mode removes the other two role assignments and grants the chosen one, against the member's roles **as of that click**: the settings panel re-seats its cached member on every press, since a second change in one sitting would otherwise compute the removal from the role set the panel opened with. If a member somehow ends up with multiple DM-mode roles (race, manual edit), the role added in that same update (the one the member just chose) is kept and the rest are stripped; a duplicate with no such role falls back to keeping the highest-position one. Every mode change writes `mode_set` to the audit log with `mode=<open|ask|closed>` in its notes. The dial **defers before the role writes** (up to five REST calls: three ensure-role, a remove, an add), so the press is acknowledged inside Discord's 3-second window and a refusal travels by followup.

### Sending a request
The persistent panel's **Open Form** button (and the Help & Settings hub's **📨 Ask to DM** row, so this panel's channel can be dropped once every row it needs is on the hub; its **Help** FAQ card has no hub row, and goes with the channel) opens **one modal** carrying the whole request: a user select ("Who?"), a radio group for Direct Message vs Friend Request with a line of explanation on each, and an optional reason capped at **250 characters**. One modal replaces a multi-press flow, none of whose steps depend on the answer to the one before.

On submit the system pre-checks:
- Target is in the guild.
- Target isn't the requester (no self-requests).
- Target isn't a bot.
- Target isn't already Open (no request needed) or Closed (refused).
- No existing consent pair (no duplicate-requesting).
- No existing pending request from the same requester to the same target.

A requester can hold at most as many pending requests as the guild's cap allows (**5** by default).

Every one of those checks runs at **submit**, and answers with the same refusal line the stepped flow gave earlier, including the no-contact case, which is presented as the target having their DMs closed (below).

A target on a **Take a Break** is refused the same way, as though their DMs were closed: the consent card could never reach them, since bot DMs are muted for the break ([take_a_break_spec.md](take_a_break_spec.md)).

When the pre-checks pass, the target is DM'd a decision card: the requester's avatar as a thumbnail beside one line saying who, from which server, wants what ("**Ada** from The Keep wants to DM you."), the reason as a quote if given, the three decision buttons, and a `-#` line whose expiry is a live countdown (`<t:…:R>`). The requester gets a receipt DM naming the target and the same countdown. If the target's DMs are closed, nothing is persisted and the requester is told.

### Accept / Decline / Expire
The DM's buttons survive bot restarts; per-request state is recovered via the DM message id.

- **Accept** records the consent pair (bidirectional), edits the DM in place with the ✅ Connected card, DMs both sides their own copy, each naming the *other* party ("You and **Bo** can now DM each other."), never both, and writes `request_accepted` to the audit log.
- **Decline** edits the target's DM to a ❌ Request Declined card ("You declined **Ada**'s DM request."), DMs the requester their own ("**Bo** declined your DM request in The Keep."), and writes `request_denied`.
- **Decline + Note** is the same, plus a modal for a short note. It is quoted on both cards, under "**Your note**" on the decliner's, under "**Note from Bo**" on the requester's, where unlabelled it would read as the reason for declining. The bot's DM is edited **before** the DB delete, so a failed edit leaves the request pending rather than half-resolved, and the note the member typed is not lost. The plain **Decline** button does *not* bail: its card edit swallows a vanished or unreachable message, and the decline is recorded regardless, because the press is the member's answer and a card that could not be repainted is a stale one (the next press on it gets ⌛ No Longer Active). `tests/dm/test_dm_perms_cog.py` pins both orders.
- **Expire**: pending requests age out after the guild's configured window (**24 hours** by default). A background sweep runs hourly (and once at startup, so requests aged-out during downtime are swept on next boot), flips them to `expired`, DMs the requester an expiry notice, and writes `request_expired`. Aged-out rows are retained as evidence.

An edit of a consent DM sent before the conversion nulls `content`, `embed` and `attachments` explicitly: the V2 flag flips on that very edit, and an edit that only set the view would leave the old embed stacked above the new card.

A card that **replaces** one in a DM carries the same guild attribution a freshly sent one does: the server's icon beside the heading and a `-# From <server>` line under it. It is the same card on the same surface as the copy the other party is sent, and without it the two halves of one event disagree: the decliner's kept receipt would name no server while the requester's copy of that decline names one, and the attribution the original request DM carried would vanish on the very edit that answers it. Where there is no server left to name (a request whose row is gone, or a guild the bot has been removed from) the replacement goes out unbranded rather than half-branded.

If the bot has been removed from the guild between the request being sent and the click, the target's DM is replaced with a "server unavailable" card and the request row is dropped.

The panel auto-bumps to the bottom of its channel on new activity there, via a **trailing-edge** debounce per guild: a burst of chat settles into one repost once the channel has been quiet for **6 seconds**, rather than the panel jumping on the first message of every burst.

The panel is also **posted automatically on boot** into each guild's configured panel channel. It is the only route to a member's own DM settings, so it can't depend on an admin remembering to press "post panel" on the dashboard. The bootstrap uses `place_or_refresh`, which edits in place when the panel is already the configured channel's, so restarts refresh it rather than stacking duplicates. A guild with no configured panel channel is skipped (there is nowhere to put it); a configured channel that has been deleted or is no longer a text channel logs a warning and is skipped. The dashboard's **Post panel** action still exists for moving the panel to a different channel.

### Revoke
Each row of **Your Connections** carries its own red **Remove**, which opens a small confirm card ("Yes, Remove" / "Cancel") on its own ephemeral message, so nothing destructive is one tap, and a red button beside a row is one mis-scroll away from one on a phone. Confirming removes the consent pair; if the original request DM is still on file, it is edited in place with a 🚫 Connection Removed card (buttons cleared). Both sides receive their own copy ("You and **X** are no longer connected."), which never says who pressed Remove: the other party can work it out, and a card that said it would read as an accusation. The panel behind the confirm redraws with a subtext line saying what happened. **Cancel** deletes the confirm card and says nothing; the panel behind it is exactly as it was. The panel is ephemeral, so nothing about the revoke is visible in the channel. The audit log records `relationship_revoked` with the actor's name. A Remove that finds nothing left (the other member got there first) says so rather than claiming success.

### Your connections
The settings panel lists **who the member is connected to**: the name and the kind of connection (`DM` / `Friend`) per row, and nothing else. A Section's button sits *beside* its text, so a row that wraps on a phone pushes its own Remove button down the card; the since-date the first draft carried took the row past that on its own, because `<t:…:D>` renders as a full written date. The date stays in the audit log, and the confirm card the Remove opens echoes the row as pressed (`**Name** · DM`) so a mis-tap is visible before it is confirmed; the long "Direct Message" / "Friend Request" wording lives in the request form's radio descriptions and on the mod feed's audit line (`request_type_label`), which a third party reads. Listing connections, rather than asking about one member at a time, is what lets a member Remove a connection they had forgotten. The list **pages at ten rows** (the 40-component ceiling: ten rows plus the two arrows is exactly 40). Past ten, a `◀` / `▶` pair joins **Done** in the bottom row (disabled at the ends rather than hidden, so the row keeps its shape), and the subtext says which page this is and how many connections there are; a page turn drops the "Changed to …" lead and any removal note, since the page is the answer to that press. A plain cut at ten (the pilot's first shape) left a member's *oldest* connections unreachable from the one surface that can remove them, and prod had members with eleven and twelve the day it shipped; `connection_page` clamps a page whose last row was just removed back onto the new last page. A no-contact partner is filtered out of it: the consent row is suppressed, not deleted, exactly as `_is_mutual` already reports one as unconnected, and listing it would grant the visibility the suppression exists to remove.

Every member named on a card is resolved through `shared/name_resolver` rather than written as `<@id>`: a mention inside a layout renders from the *reading* client's cache, and these cards are read in DMs where that cache is usually empty.

### Mod audit
The dashboard's audit log lists every state transition: requested, accepted, denied, expired, revoked, and a member setting their own DM mode. Optional filters: action name and request type (DM vs friend-request label). The audit channel, if configured, also receives a one-line notice card for each event in real time, timestamped in its subtext (a layout has no `embed.timestamp`), framed in the guild's thin card strips once a **Card Header** is uploaded in Config → Server → Branding → Panel Art (`core.layout.card_strips`; Ben, 2026-09-30). The send lives in the cog rather than the service: its card comes from `dm/perms/layouts.py`, which reads the service's own request-type vocabulary, and a service importing the layouts back would close that loop.

## Permissions

- The bot needs **Manage Roles** to create and assign the three DM-mode roles, with its top role above them. They are provisioned through `core/role_provision.py`: an existing role of the same name is adopted rather than twinned, the created roles carry no permissions, and a Discord failure (a rate limit, a 5xx) degrades to leaving the member's current modes alone instead of escaping into their button click. A pinned mode-role id that resolves to a role with elevated permissions, a managed role, or one above the bot is refused the same way: logged, current modes left alone (`core.role_safety.grant_block_reason`); **Send Messages** + **Embed Links** in the panel channel and the audit channel; **Read Message History** in the panel channel for the bump-to-bottom guard.
- The settings panel is guild-only (it reads mode roles) and refuses to open from a DM; no other Discord-side gate.
- The panel button is open to everyone; Accept / Deny hard-check that the clicker is the target.
- Dashboard **writes** require the **admin** role; since IA6 a moderator opens
  the page read-only, so the reads behind it admit them.
- DM delivery to the target is best-effort (Discord-side, not bot-grantable). A target with closed DMs can't be reached at all.

## User-visible errors

| When | The user sees |
|---|---|
| Target is Open mode | "X has their DMs open — no request needed, just message them!" |
| Target is Closed mode | "X isn't accepting DM requests right now." |
| Requester targets themselves | "You can't send a request to yourself!" |
| Target is a bot | "Bots don't accept DM requests." |
| Already connected | "You two already have a connection — no need to request again." |
| Already a pending request to that target | "You already have a pending request to them — wait for them to respond." |
| At the pending cap | "You already have N pending DM requests. Wait for some to be answered or expire (max N)." |
| Target's DMs are closed | "I couldn't DM that user — they may have DMs disabled." |
| Non-target clicks Accept / Deny | "This request isn't for you." |
| Accept/Deny click on an already-resolved request | (DM edits to ⌛ Request No Longer Active: "This request was already answered, expired, or cancelled — there's nothing left to do with it.") |
| Bot removed from the guild between request and click | (DM edits to ❌ Server Unavailable: "The server this request came from is no longer reachable, so it can't be answered.") |
| Accept when one member has left | "Couldn't find one or both users in this server." |
| Mode change fails on role permission | "I don't have permission to manage roles here." |
| Remove confirmed, but the pair was already gone | ❌ Nothing to Remove: "You and **X** were no longer connected — they may have removed it first." |
| Post Panel fails (dashboard) | "Discord rejected the post in #channel — panel was not posted" |
| No connections at all | "Nobody yet — a member appears here once they accept your request, or you accept theirs." |

## Non-goals

- **No Discord-friend integration.** A "Friend Request" type is just a label on the consent pair; the bot cannot send platform-level friend requests.
- **No per-channel scoping.** Consent is guild-wide; revoke is all-or-nothing.
- **No specific-user blocklist.** Refuse via Deny, or switch to Closed.
- **No mod-side force-disconnect.** Mods can audit but cannot retroactively break two members' consent.
- **No expiry on accepted pairs.** Once accepted, a pair survives until revoked.
- **No retraction by the requester.** A pending request can't be cancelled; wait for the target to act or for the expiry window to pass.
- **No offline-revoke notification beyond the best-effort DM.** If the revoke DM fails, only the audit log records it.

## Configuration

Per-guild settings an admin chooses via the dashboard:

- **Audit channel**: where one-line audit posts fan out (optional; events still persist when unset).
- **Status roles**: an existing role per mode, or the bot's own `DMs: …` roles.
- **Requests expire after** (`dm_request_expiry_hours`, default 24, range 1–720): how long a request waits for an answer. The figure is quoted to the member in the request DM and in the expiry notice.
- **Requests one member may have waiting** (`dm_request_max_pending`, default 5, range 1–50): the per-requester cap on outstanding requests.

Where the panel lives is set by posting it (Post Panel), not by a channel dial: the channel and message id are bot-managed and not user-editable. There was once a separate "request channel" setting; nothing ever read it, and it has been removed rather than left looking like a control.

Built-in behavioral constants (not user-tunable): the expiry sweep runs hourly, reason fields cap at 250 characters, the connection list pages at ten rows, and the panel-bump debounce is a 6-second trailing-edge quiet period per guild.

## Stored data

Per guild: the live consent map (one row per **pair**, with the request type, optional reason, and a pointer to the originating DM so revoke can edit it in place), and every pending request (requester, target, type, reason, message id, timestamp).

Per event: the audit log records every asked / accepted / denied / expired / revoked transition with actor, both parties, and a free-form notes field carrying the request type. Accepted and denied requests delete the pending row; expired requests are kept with a flipped status. Revoked pairs are hard-deleted from the consent table, so only the audit log retains evidence the pair existed.

In-memory and rebuilt on restart: a per-guild consent set (both orderings, for O(1) mutual checks), the pending request map keyed by requester+target, panel-bump locks, and the background expiry task.
