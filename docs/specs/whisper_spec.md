# Whisper — Feature Spec

An anonymous-message-with-guessing game. Members opt in to a per-guild role, then send anonymous DMs to other opted-in members. The recipient sees the message immediately and gets a configurable number of guesses (**three** by default) to identify the sender. A public feed channel hosts a persistent launcher (Send / Inbox / Sent, with Help) and announces whispers without spoiling content. Whispers can be replied to once, shared to the feed, or deleted by the recipient. The sender can optionally be told how the guessing goes (a per-guild dial, off by default).

Every card Whisper sends is a **Components V2 layout** since 2026-09-30 (`whisper/layouts.py`, per `components_v2_style_guide.md`); § Cards below names the paradigm each one is.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/whisper send` | Slash | Whisper-role member (server only) | Opens the send form (same as the launcher's Send button) |
| `/whisper sent` | Slash | Everyone (server only) | Ephemeral list of your active sent whispers |
| `/whisper optin` | Slash | Everyone (server only) | Consent card; **Opt In** grants the Whisper role |
| `/whisper optout` | Slash | Everyone (server only) | Removes the Whisper role; existing whispers are preserved |
| **My Data** → **Whisper history** | Panel button (privacy_cog) | Everyone (server only) | Two-step confirm; deletes all whispers + replies where you are sender or target. Replaced `/whisper forget-me` on 2026-09-17; see `privacy_spec.md` § Member-erasable stores |
| **Send** button | Persistent launcher | Whisper-role member | Opens the send form: a member picker and the message box |
| **Inbox** button | Persistent launcher | Everyone | Ephemeral list of received whispers; Open shows one with its actions |
| **Sent** button | Persistent launcher | Everyone | Ephemeral list of your sent whispers (active only) |
| **Help** button | Persistent launcher | Everyone | The guild's `whisper` doc from the dashboard's Docs panel, read at click time |
| Whisper audit log | Web (dashboard) | Admin | Per-guild log of every whisper with report counts |
| Whisper config | Web (dashboard) | Admin | Set the feed channel, opt-in role, and optional mod-log channel |

There is no one-shot `/whisper <target> <message>`; send is always the form, so the bot validates role membership at submit and the member never types a command argument.

## Behavior

### Persistent launcher
A single launcher message sits at the bottom of the feed channel: a hub card (paradigm 1): the title with a grey **Help** beside it, one sentence, a line, then a row per entry point (Send / Inbox / Sent) with its grey button, and a `-#` line naming `/whisper optin` and `/whisper optout`. A hub that had drawn no art before its conversion wears the **small lobby strip** with "Whisper" drawn between the leaves, and the hub footer, whenever the guild has a lobby header set (`core.layout.lobby_art`; `LOBBY_ART_BUILDERS` pins it), with no header it draws nothing and keeps its `##` heading. **Help** reads the guild's doc with key `whisper` from the `docs` table at click time (`docs/help_card.build_doc_help_layout`); no such doc says so and points at a moderator. Renaming the doc disconnects the button. A suggested first draft is at the end of this spec. It runs on the shared sticky-panel machinery (`core.sticky.StickyPanel`, migrated 2026-09 from a hand-rolled copy): a member message in that channel re-sticks it about **6 seconds** after the channel falls quiet, so a burst of chat costs one repost rather than one per message; the new launcher is posted *before* the old one is deleted, so a failed send never leaves the channel with no launcher; and the bot's own feed posts (send, share, solve) re-stick it explicitly. The `on_message` listener rejects every guild without a Whisper channel by set lookup, not a DB read. On boot the launcher is re-posted only where it is buried; one already at the bottom stays put. Its channel is stored beside its message id (`whisper_launcher_channel_id`) so an admin repointing the feed channel still gets the old launcher deleted from where it actually is; a launcher posted before that key existed (message id, no channel) is pinned to the feed channel at boot, since that is where it was posted; the placer would otherwise look for it in the *new* channel after a repoint and leave the old one live. A dashboard save (`PUT /config/whisper`) and an Apply click on the AI advisor's proposal (`/ask`, which writes the same `whisper_channel_id`) both dispatch `whisper_config_change` to the cog, which republishes that guild set and re-sticks the launcher, so a feed channel set or moved after boot gets its launcher without a restart. Launcher buttons keep working across bot restarts.

### Sending a whisper
Send is **one form** (paradigm 4, request form; Ben, 2026-09-30): Discord's own member picker and the message box (up to **1000 characters**), submitted together. Until 2026-09-30 it was a paged, filterable list of the opt-in pool followed by a compose modal; the two steps didn't depend on each other, so they collapsed. The native picker offers the whole server and cannot mark who has opted in, so a pick outside the pool is refused **at submit** with the ordinary "hasn't opted in" line and **the draft quoted back** on the card, so nothing typed is lost. Each such refusal writes one `usage_events` row (`kind='whisper'`, `name='send_refused_pool'`, no text) so how often it bites can be counted; the decision was to see, not to guess. The row carries **`user_id = 0`**, never the sender: the table is not pruned and the disclosure texts say it holds command and panel usage only, so a member id there would be undisclosed per-member data (2026-10-01).

**Every send refusal quotes the draft back** (2026-10-01): the cooldown, the hourly cap, a timed-out recipient, a missing feed channel, a failed DM and the validation lines (`layouts.build_send_refusal_layout`, heading "Whisper Not Sent") all hand the message back as an escaped quote, the same way the pool refusal does. The **no-contact** and **Take a Break** paths are deliberately not among them: they answer with the ordinary sent confirmation (see the no-contact section), so there is no refusal to attach a draft to; quoting one would be the tell. A test fails if either starts refusing.

On submit:
- **Per-sender cooldown**: 30 seconds between any two sends.
- **Per-target hourly cap**: 5 whispers to the same recipient in a rolling hour.
- Timed-out targets are refused.
- The recipient is DM'd the whisper as a card (paradigm 5): the text as an escaped quote, a `-# Whisper #N · status` line, and Guess / Share / Reply / Delete as the state allows (`logic.dm_action_buttons`); if the DM fails (closed DMs), nothing is persisted. The DM is branded by `send_branded_dm(layout=…)`: the origin server's icon beside the first line and a `-# From {server}` line. **The DM redraws from the same builder after every press** (share, delete, the last guess, a solve), so a DM sent as an embed before 2026-09-30 becomes the V2 card the first time one of its buttons is used.
- A best-effort announcement is posted to the feed: "Someone sent **{target name}** an anonymous message." (name only, no content).

### No-contact enforcement
Whisper consults the [no-contact list](no_contact_spec.md) on every send, and on a reply to a whisper that predates the pair (the DM's Reply button never expires, so an existing thread stays a live contact path even if the pair was added to the list afterward). The check runs last, after every ordinary refusal above (missing role, cooldown, hourly cap, timed-out target, broken feed channel); those still reach the sender exactly as normal, so the only case that diverges is the one that would otherwise have succeeded. A blocked send reports "Whisper delivered." with no error and no whisper arrives; no whisper row, DM, or feed post is created. This is deliberate: telling the sender "blocked" would confirm the target acted against them, which is the escalation the no-contact list exists to prevent. The quest/economy trigger for sending still fires so the silence isn't itself a tell.

### Naming people on cards
Every card that names a participant renders a **resolved display name as plain
text**, never a `<@id>` mention. With the client's `allowed_mentions` at none, a
mention inside a Text Display renders from the reader's cache exactly as an
embed mention did: a name only when the reading client already has that user
cached, a bare numeric id otherwise, which for a whisper, where the parties
are by design not interacting, was the common case.

Names come from `services.name_resolver.build_name_fn`, whose chain is:

1. `guild.get_member(uid).display_name`: the gateway member cache. It goes
   first because `intents.members` is on, so it is complete for present members
   and updates the instant someone changes their nickname.
2. `known_users.display_name`, then `known_users.username`: persistent, and the
   only source that covers members who have **left** the guild.
3. `<@id>`: genuine last resort for a user neither source knows.

Results are markdown-escaped so a name containing `*` or `_` can't reformat the
surrounding copy. The mod-log cards keep the raw id alongside the name
(`Name (\`id\`)`) so moderators retain a copyable identifier. An id the
resolver can't name reads `User <id>`, never a bare number.

No ping is lost by this: embed mentions never notified anyone in the first
place, and the feed posts send with `AllowedMentions.none()`. The "solved"
feed line, which was a bare `✅ <@id> solved the whisper!` content line until
2026-09-30, is a card naming the target the same way.

### Guessing
The target gets `whisper_guesses_per_whisper` guesses (default **3**, dashboard dial, range 1-10; a value outside it is refused at save, not clamped). The allowance is stamped on the whisper row at send time, so moving the dial only affects whispers sent afterwards; nobody loses a guess they were already promised. *Amended 2026-08-29*: the count came from the `whispers.guesses_left` schema default with no control anywhere, unlike the sibling Guess Who game's tunable cap. From a DM, the guess picker is a card (paradigm 9) listing every opted-in member except the target, 25 a page with a filter. From a server surface (the inbox) it is Discord's native member picker, which offers **every** member of the server, so a pick that isn't in the opt-in pool (or is a bot) is refused with "❌ They aren't in the Whisper pool — that one's free." **without consuming a guess**; the two entry points play by one rule (`guess_candidate_rejection`). Guess consumption is atomic; two clicks racing on the same whisper can both pass pre-checks but only one will succeed; the other sees "This whisper was solved by another tab."

The picker edits into the result card (paradigm 7):

- **Correct**: "✅ You Solved It"; a feed card announces "✅ Whisper Solved — **{target}** worked out who sent their whisper."; the DM redraws without Guess.
- **Wrong, with guesses left**: "❌ Not Them — N guesses left."
- **Wrong, last guess**: "❌ Out of Guesses"; the DM redraws without Guess; the whisper remains active for Share / Reply / Delete.
- A refusal that edits the picker (already solved, no attempts, a pick outside the pool) is the ordinary ❌ line on a bare Text Display.

#### Sender feedback (dial, default off)
When the guild's **Tell the sender how the guessing goes** dial (`whisper_sender_feedback`) is on, each guess also DMs the sender a branded digest card (`layouts.build_guess_digest_layout`); a "Guesses on Whisper #N" heading and one line per guess: "Whisper #N — they guessed {name}. Wrong, 2 left." / "… Wrong — they're out of guesses. You're safe." / "Whisper #N — they guessed you. They got you." This is the sender's half of the game; watching the target guess wrong, being caught, or getting away with it, which they previously had no signal of at all. Rules:

- It ships **off** so whispers already in flight don't start DMing their senders until an admin opts the server in; once on it applies to every whisper, including existing ones.
- The sender must still hold the Whisper role: `/whisper optout` leaves the game and its DMs with it. (**My Data** → Whisper history deletes the rows, so there is nothing left to be guessed on.)
- If the sender holds a **no-contact pair** with the guessed member, the name degrades to "someone"; the line reads like any other wrong guess, so the sender can't tell.
- The DM goes out after the target's own response, best-effort; a closed DM is logged and ignored.
- **Batched** (2026-09-28, `dm_batching_spec.md` kind `whisper_guess`, grouped per whisper): every guess on one whisper within ten minutes arrives as one DM, one line per guess in order; a single guess reads as before. The dial and the sender's role are read again at send time, and the no-contact degrade is decided then too. The guessed member is stored as the row's `actor_id` so an erasure takes the row with them, and the kind is registered **without** `dm_batch`'s send-time actor drop; dropping the line instead of degrading it would leave a gap in the "N left" countdown that a blocked name was guessed.

### Sharing, replying, deleting
- **Share** (target only, before solved): the no-content feed post is replaced with one quoting the full content (markdown-escaped, so member text can't forge a heading or a quote of the bot's own). The DM redraws keeping Guess (if still applicable) and Reply. The ephemeral confirmation ("Shared to the whisper feed.") is a jump link to the new feed post when the post succeeds.
- **Reply** (sender or target, **one reply per whisper**): a modal collects up to 1000 characters; the other party is DM'd a card quoting the original (clipped to 200 characters) under **Whisper #N** and the reply under **Their reply**, with a grey Report button. It is branded with the origin server (the reply is anonymous as to its *author*, not its origin, and the recipient sent the original whisper so the server name tells them nothing new). If the recipient's DMs are closed the reply is rolled back and the writer sees "Couldn't deliver — they have DMs disabled."
- **Delete** (target only): **red, and confirmed first** (paradigm 10; Ben, 2026-09-30: nothing destructive is one tap): the press opens an ephemeral "⚠️ Delete Whisper #N?" with **Yes, Delete** / **Cancel**. Yes soft-deletes: the whisper disappears from the target's inbox but remains in the sender's sent list, then redraws where the press came from (the DM with its buttons gone, or the inbox back on its list) and turns the confirm into a receipt. Cancel deletes the confirm and changes nothing. Idempotent. **Report** went grey in the same change: it opens a form and destroys nothing, so red was the wrong color for it.

There is no Expose step any more. The button had been dead code since 1396fb5e (never attached to a view; the audit's Exposed column froze at 31 pre-2026-05-27 rows) and was deleted in the 2026-09 games review along with `validate_hide` / the `hidden` state, which migration 025 had already folded into soft-delete. The `whispers.exposed` column is retained but nothing reads or writes it. The sender-feedback DM above is the reveal's replacement.

### Inboxes
One ephemeral card with two panes (paradigm 6; Ben, 2026-09-30: "open the whisper in a new pane, have a back button to get back to the list"). The **list** is ten rows a page: `#N · status · age` over a `-#` preview held to ~30 columns (the sent list's preview is led by `To {name}`); each with a grey **Open** beside it; the arrows (only with a second page, disabled at the ends), **Filter** and **Clear** (while a filter is on) sit in the bottom row, and a `-#` line under the heading says the page and the match count. **Open** swaps the card to that whisper: its text as a quote, the status, its actions in one row, and **◀ Back** in a row of its own, returning to the page and filter you left. An empty inbox is the heading and a sentence, no controls. The card expires after five minutes (under Discord's fifteen-minute ceiling on editing an ephemeral message, so the grey-out lands).

- **Inbox** shows received whispers in pending or shared state (soft-deleted ones are hidden). Actions on an opened whisper: Guess (if eligible) + Share (if pending) + Reply + Report + Delete.
- **Sent** shows your active sent whispers (excludes out-of-guesses and 30-day age-locked rows). Actions: **Reply only**. Delete is recipient-only (a sender's press could only refuse), so the Sent list never offers it. Only the Sent list resolves recipient names; the Inbox shows none and skips the lookup.

### Lifetime and locking
Whispers age-lock after **30 days**: no new guesses, no new replies. The row remains in the DB; only the inbox surfaces filter it out.

### Reporting
Recipients can report a whisper; reply recipients can report a reply. The reason field is free-form and optional (defaults to "(no reason provided)"). One report per reporter per item; second-clicks see "You've already reported this whisper." Reports always persist regardless of whether a mod-log channel is configured; the dashboard's audit log is the canonical view.

### Mod audit
The dashboard's audit log lists every whisper with its state, report count, sender, and target. Filters by state (exactly the two states a whisper can be in, **In inbox** (`pending`) and **Shared to feed** (`shared`); a test enumerates the panel's options against `WhisperState`) and reported-only. Sender identity is always visible here, whether or not a mod-log channel is set; the mod-log channel is a mirror, not the only record. If a mod-log channel is set, every reply, report, and reply-report also fans out there as a card (paradigm 7: `**From**`/`**To**` or `**Sender**`/`**Reporter**` lines with `Name (\`id\`)`, the text and the reason as quotes, and a `-# Whisper #N · <t:…:f>` stamp; sending a whisper and solving it do not); failures to post are logged and don't block the user action.

### Opt-out and erasure
`/whisper optout` removes the role only; sent and received whispers stay intact. Erasure is **not a whisper command**: it is the **My Data** panel's **Whisper history** button (`/mydata` until 2026-09-26), backed by `whisper_repo.forget_member`, which `privacy_service.purge_user_data` calls too. It had been `/whisper forget-me`, wired to that command and nothing else, so an operator erasure reached no whisper at all (bug #222, item 3). The button is a destructive nuke that requires a two-step confirm and deletes, **for the guild it is run in**, every whisper you sent or received (their guesses, replies, and reports cascade with them) plus any reply you wrote or received. Reports you filed on other people's whispers, and guess rows recording that someone guessed *you* on a third party's whisper, are rows about their whispers and survive. Stranded replies whose parent whisper is already gone are swept regardless of guild, with no parent left, they can't be attributed to one.

## Permissions

- The bot needs **Send Messages** in the feed channel (a V2 card needs no Embed Links), **Manage Messages** to bump the launcher, **Manage Roles** for opt-in / opt-out (with the bot's role above the Whisper role; since 2026-09-14 the **Opt In** button refuses a Whisper role that carries elevated permissions or is managed, telling the member to ask an admin), and the ability to DM each target (Discord-side; not bot-grantable, and closed DMs roll back the send).
- Slash commands have no Discord-side permission gate; they reject DMs and check role membership in-app.
- The Guess, Share, Delete, and Report buttons are **target-only**. Reply is sender-or-target. Report Reply is reply-recipient-only.
- Dashboard config and the audit log both require **admin**; the mod-log channel doxxes an anonymous sender, so this route was tightened off the games-editor (game-host) gate other game configs use; see the security fix that split it from `/config/guess`.

## User-visible errors

| When | The user sees |
|---|---|
| Feed channel or opt-in role not configured | "Whisper isn't set up yet — ask an admin." |
| Opt-in role missing from the guild | "Whisper role no longer exists. Ask an admin to fix the config." |
| Invoker lacks the Whisper role | "You need the Whisper role first. Use `/whisper optin` to join." |
| The send form's pick hasn't opted in | "❌ They Haven't Opted In — That member hasn't opted in to receive whispers." with the draft quoted under it, and one `usage_events` row |
| 30-second sender cooldown | "Slow down — wait Ns before sending another whisper." |
| Hourly per-target cap reached | "You've sent 5 whispers to that user in the last hour. Try again later." |
| Target is currently timed out | "Can't whisper a member who's currently timed out." |
| Recipient has DMs closed | "I couldn't deliver — they have DMs disabled." |
| Sender/target pair is on the no-contact list | "Whisper delivered." (looks identical to a real send; nothing is actually sent) |
| Target (or, for a reply, the recipient) is on a Take a Break | Same as no-contact: "Whisper delivered.", nothing sent. A reply row is still written so the one-reply cap holds ([take_a_break_spec.md](take_a_break_spec.md)) |
| Guess race lost | "This whisper was solved by another tab." |
| Guild-side pick outside the opt-in pool | "They aren't in the Whisper pool — that one's free." (no guess consumed) |
| Guild-side pick is a bot | "That's a bot — pick a real member." (no guess consumed) |
| Wrong guess, with remaining | "Wrong! N guesses left." |
| Duplicate report | "You've already reported this whisper." |
| Duplicate reply report | "You've already reported this reply." |
| Opt-in confirm, bot can't assign role | "I don't have permission to assign that role." |
| Slash invoked in DMs | "This command can only be used in a server." |

## Cards

Every card is built in `whisper/layouts.py` from a `Whisper` row, primitives and a `name_fn`; the cog binds the ids. The paradigm each one is (`components_v2_style_guide.md` § The paradigms):

| Card | Paradigm |
|---|---|
| Launcher | 1 hub panel; small lobby strip, Help on doc key `whisper` |
| Feed: sent / shared / solved | 7 notice |
| Whisper DM | 5 decision card; buttons by `logic.dm_action_buttons` |
| Reply DM, sender's guess digest | 7 notice / receipt |
| Inbox list, opened whisper | 6 list with per-row actions |
| Delete confirm | 10 confirm destructive |
| Guess pickers → results | 9 game turn card → 7 receipt |
| Opt-in card → receipts | 5 → 7 |
| Mod-log reply / report / reply-report | 7 notice |
| Send form | 4 request form |

Buttons are grey except **Delete** / **Yes, Delete** (red, destructive) and **Opt In** (green, the yes on a decision card). Member text is always an escaped `>` quote. `scripts/layout_samples.py whisper` renders every state.

### Suggested `whisper` Help doc

Not in the database; an admin writes it in the Docs panel under key `whisper`; until then Help says nobody has written it yet.

> # Whisper
> Send someone an anonymous message. They get a few guesses at who sent it. A wrong guess is gone for good.
>
> **Getting in:** `/whisper optin` adds you to the pool; `/whisper optout` takes you out, and the whispers you already have stay.
> **Sending:** press **Send**, pick a member who has opted in, write up to 1,000 characters. You can send one every 30 seconds, and up to five to the same person in an hour.
> **Getting one:** it arrives as a DM from the bot. **Guess** who sent it, **Share** it to the feed (with the words, without the sender), **Reply** once, anonymously, or **Delete** it from your inbox.
> **Reporting:** the recipient can report a whisper, and either side can report a reply; moderators see who sent what.
> **Privacy:** senders are anonymous to recipients, never to moderators. Your **My Data** panel deletes every whisper you sent or received here.

## Non-goals

- **No second reply.** One reply per whisper, full stop.
- **No retraction.** Once sent, the sender can't un-send a single whisper; only **My Data** → Whisper history to nuke their entire history.
- **No attachments or embeds.** Whispers and replies are text only.
- **No leaderboard or stats.** The audit log is forensic, not gamified.
- **No pool-only picker.** The send form's member picker is Discord's, so it can't hide who hasn't opted in; the refusal hands the draft back instead, and the count says whether that needs revisiting.
- **No per-sender blocklist of Whisper's own.** A member can block a specific person from whispering them via the cross-feature [no-contact list](no_contact_spec.md) (the Help & Settings hub's No Contact row) without opting out of Whisper entirely; opting out (below) is still the only way to leave the game altogether.
- **No one-shot slash form** that takes target + message directly.

## Configuration

Per-guild keys an admin sets via the dashboard:

- **Whisper opt-in role**: the role gating both send and receive. Required.
- **Feed channel**: where the launcher and announcements live. Required.
- **Mod-log channel**: optional; when set, replies and reports also fan out here as cards (sending a whisper does not). Reports persist to the audit log regardless, and sender identity is always visible on the audit page; the panel hint says so, because an earlier hint claimed "(disabled)" meant nobody could find out who sent a whisper, which was wrong in the dangerous direction.
- **Guesses per whisper** (`whisper_guesses_per_whisper`, default 3, 1-10): the recipient's allowance, stamped on each whisper when it is sent.
- **Tell the sender how the guessing goes** (`whisper_sender_feedback`, default off): the sender-feedback DM above.
- **Rate limits**: `whisper_cooldown_seconds` (default 30) and `whisper_hourly_cap_per_target` (default 5).

The "(none)" role option turns sending off; the panel hint says so; it used to promise the opposite. The launcher's current message id and channel (`whisper_launcher_message_id`, `whisper_launcher_channel_id`) are bot-managed and not user-editable.

## Stored data

Per guild: every whisper (sender, target, content, state, guess count, timestamps, soft-delete flag), every guess attempt, every reply, and every report on a whisper or a reply. Sender and target ids are stored in plaintext; anonymity is a UI affordance, not a cryptographic property, and admins can see the full sender via the audit log.

Soft-delete preserves the row so the sender's history stays intact while the target's inbox hides it. **My Data** → Whisper history (and the operator erasure behind the same function) is the only path that actually removes rows; it cascades through every whisper / reply / guess / report involving the user.

In-memory only and reset on restart: per-sender cooldown timestamps, per-target rolling counts, and the launcher's sticky-panel state (per-guild lock, id cache, debounce). A restart resets these; by design, since a sender who restarts the bot gets at most one freebie.
