# Needle (Auto-Thread) — Feature Spec

Automatically spawns a thread from each new message in designated text channels (inspired by [discord-needle](https://github.com/MarcusOtter/discord-needle)). Each thread gets a configurable name and an optional pinned welcome message with **Archive Thread** / **Edit Title** buttons, the starter message can be given a fixed set of decorative reactions, a channel can offer reply DMs to posters who opt in to them, and a channel can require an upload on every post so that chat typed into it is removed instead of threaded. Keeps Q&A and discussion channels tidy at a glance.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/close` | Slash | The thread's starter or Manage Threads | Archive the current thread (also unlocks it) |
| `/title name:<name>` | Slash | The thread's starter or Manage Threads | Rename the current thread (max 100 chars) |

Both work only inside a thread. The welcome-message buttons duplicate these commands with the same permission check (Edit Title opens a modal). Channel and global configuration has **no slash commands**; it lives entirely in the web dashboard.

**Who counts as the starter.** Needle opens every thread itself, so Discord records the *bot* as the thread's owner. `thread.owner_id` alone would mean no poster could ever close or rename their own thread, so the starter is the author of the message the thread was opened from (a thread shares that message's id); `owner_id` still counts too, for a thread a member opened by hand, but never when it is the bot's own id. If the starter message is gone and the owner is the bot, only a moderator can close the thread. Who the starter is lives in `bot_modules/needle/logic.resolve_starter_id`, and whether they may act is `can_manage_thread`; the cog only fetches the starter message and caches the answer.

## Behavior

### Thread creation
On every new message in a configured text channel (system messages, the bot's own messages, and, unless *include bots* is on, other bots are skipped), Needle creates a thread on the message with the channel's **Threads Archive After** duration (below) and the configured slowmode. The thread name comes from the channel's title style:

| `title_type` | Thread name |
|---|---|
| `first_fifty` (default) | First 50 characters of the message, newlines flattened |
| `first_line` | First line of the message |
| `user_date` | `{display name} ({YYYY-MM-DD})` |
| `custom` | Custom template; supports `$USER` and `$DATE` |

Names are clamped to 100 characters; an empty result becomes "New Thread". If the bot lacks permission or Discord rejects the thread, the failure is logged and nothing else happens.

### Threads archive after

Per channel (`auto_archive_minutes`, migration 252; labelled
*Threads Archive After* on the dashboard): how long a thread sits with no new
reply before Discord archives it. Discord accepts exactly four durations, and
the select offers exactly those: **1 hour** (60), **1 day** (1440, the
default), **3 days** (4320), **1 week** (10080). Until this dial every thread
got one day, hard-coded, so the default is the old behavior and the migration
ships dark.

Any other value (one written straight into the table, or sent to the route)
is read as one day by `needle.logic.auto_archive_minutes`. The store clamps on
write (`_upsert_channel`) and on read (`_row_to_config`), and
`needle.logic.thread_create_options`, which builds the arguments the cog hands
to `create_thread`, clamps once more; the route passes the field through
unclamped and stores the fallback rather than answering 400, since there is no
way to pick a bad value from the select. Discord rejects an
`auto_archive_duration` outside its four, and a rejected thread is a lost
thread; the normalizer means a bad value costs nothing. The helper exists so a
test can prove the dial's value is what reaches Discord: a return to the old
hard-coded day would otherwise leave every test green.

Changing the dial affects threads opened from then on. Discord keeps the
duration on the thread itself, so existing threads keep whatever they were
opened with. An archived thread is reopened by anyone replying in it, as always.

### Require an upload on every post

Per channel, **off by default** (`require_attachment`, migration 246;
labelled *Require an Image or File on Every Post* on the
dashboard). Ben: "i'm having users send normal messages in threaded channels.
is there a way to prevent that and still allow anyone to post images and texts
together?" Discord itself cannot: removing Send Messages blocks a captioned
image too, and no permission means "must attach a file". So the bot does it.

With the dial on, a member's post in the channel must carry **at least one
Discord attachment** (image, video, voice message, any file). Text posted with
an upload is the caption and is kept; the post is threaded as usual. A post
with no attachment is **deleted before any thread is opened**, and the bot
posts a short note in the channel that mentions the poster, says posts need an
upload, and points them to the threads. The bot removes the note after 15
seconds (`TEXT_ONLY_NOTICE_SECONDS`).

"Attachment" is the auto-delete media-only rule's definition
(`auto_delete_spec.md`), so the two features agree: a real upload. These have
none and are removed: a link that unfurls into an image, a GIF from the GIF
picker, a sticker, a poll, a forwarded message. Discord adds link previews a
moment *after* a message is posted, so counting them would mean holding every
post for a few seconds before judging it; Ben chose uploads only.

Exempt, and threaded as usual:

- a member with **Manage Messages** in the channel, so a moderator can post the
  channel's rules or an announcement as text;
- every **bot and webhook**, so a game prompt another bot posts is never
  eaten. Whether a bot's post opens a thread at all is still *include bots*,
  which is checked first.

The whole decision is `needle.logic.starter_post_verdict`; the cog passes it
`bool(message.attachments)` and the author's permissions.

Details:

- **Delete first, thread never.** The post is deleted before a thread exists,
  so `delete_behavior` has nothing to act on.
- **One note per member per minute** (`TEXT_ONLY_NOTICE_COOLDOWN_SECONDS`). A
  member who types five lines has all five removed and is told once. The
  memory is per channel and member, in process memory only, and bounded. A
  member counts as told only if the note went out: the claim is made before
  the send, so two posts landing together cannot both send, and released if
  the send fails.
- **The note names nobody but the poster and quotes nothing.** The removed
  text is not stored anywhere, and a member cannot get it back.
- **If the delete fails** (the bot lacks Manage Messages, or Discord errors)
  the post stays, still without a thread, and no note is sent. If someone
  else already deleted it, nothing more happens.
- **The dashboard says when the rule cannot work.** A ticked box in a channel
  where the bot lacks Manage Messages would remove nothing, and a setting that
  isn't enforced must not look enforced. The channel's card shows a red
  warning naming the missing permission, on load and straight after a save
  (`needle.logic.removal_blocked`; both `GET /config` and the `PUT` carry
  `removal_blocked`). It is read from the bot's cache: if the bot is down or
  the channel isn't cached the state is unknown, and unknown shows no
  warning. The log gets one line per channel per boot, not one per post.
- **The moderator exemption looks the author up.** If Discord hands over a
  bare user rather than a member, the bot checks its cache and then Discord
  before deciding. An author who can't be found is not a member, so not a
  moderator.
- **Replies inside threads are never inspected.** The rule covers the parent
  channel only.
- **Edits are not re-checked.** A member who posts an upload and then removes
  the attachment by editing keeps the post and its thread.

### Welcome message
Unless the channel's reply type is `none`, the bot posts the reply template (`custom` → per-channel text, `default` → the guild-wide template) into the new thread with the persistent Archive/Edit-title buttons. Templates support `$USER`, `$CHANNEL`, and `$THREAD`. An empty template posts nothing. With Manage Messages the bot pins the welcome message and deletes its own "pinned a message" system notice. The poster's display name is mention-escaped and the send allows no mentions, so a nickname cannot ping.

### Reply DMs

**Two switches, both off by default, and both must be on.** The admin's, per
channel (`notify_op_on_reply`, migration 223, labelled *Allow Reply DMs* on the
dashboard), and the member's own: **🧵 Replies to Your Threads** under **Bot
DMs** on Help & Settings → Roles & Pings (`needle_reply_optins`, migration
244). Reply DMs are **opt-in** (Billy: "make the DMs
notifications for threaded posts opt in"): nobody is enrolled by default, so a
channel's dial alone DMs no one. The member's switch is server-wide, not per
channel or per thread, and it stays when a channel stops auto-threading.

The Roles & Pings row appears while at least one auto-threaded channel in the
guild has its dial on (`needle.store.reply_dms_offered`), since with every dial off
it would be a switch that does nothing, and always to a member who is opted
in, so a stored opt-in never loses its off switch
(`server_features.logic.reply_dms_row_state`). Showing it costs the card one
Customize dropdown (`MAX_MENUS_WITH_REPLY_DMS`, the 40-component budget). It
is a table row, not a role, so nobody else can see who asked for DMs.

With both on, every message a member posts in a thread under that channel is
reported to the thread's starter, **batched** (through `dm_batch`;
`docs/specs/dm_batching_spec.md`). The first reply opens a ten-minute window for that
thread and starter; the window closes into one DM: "**Anna**, **Bo** and 3
others replied to your thread **Title** in **Server** (7 replies)", linking the
first new reply. A single reply reads as the old one-per-reply DM did
("**Name** replied to your thread … Reply: <link>"). The replier names come from
`name_resolver`; the thread title is markdown-escaped; the DM allows no
mentions. A reply deleted inside the window drops out, and if it was the only
one no DM goes; a deleted thread takes its waiting replies with it.

The gates below are checked when the reply is queued **and again when the DM
is sent**: a channel that stopped auto-threading or dropped reply DMs, a
starter who turned reply DMs off during the window, a no-contact pair added during it, or a
starter who left, all send nothing.

No DM is sent when:

- the replier is the starter, or any bot (whatever *include bots* says; that
  dial is about opening threads, not announcing replies);
- the starter can't be found, or is no longer a member of the server;
- the starter hasn't opted in (Roles & Pings), or turned it off from a DM;
- the starter and replier are a **no-contact pair**. Only the DM is dropped:
  the reply is the replier's own message and stays up, and neither side is told
  (CLAUDE.md: suppress the delivery, not the action).

These are all terms of `needle.logic.should_dm_starter`, checked before the
reply is queued, so a starter who never opted in leaves no `dm_batch_pending`
row.

The DM's button is a persistent `DynamicItem` (`needle_dm:<mute|unmute>:guild:channel:thread`),
so it works after a restart. **🔕 Turn off reply DMs** deletes the presser's
opt-in (the same switch as Roles & Pings, for the whole server) and swaps
to **🔔 Turn reply DMs back on**, which restores it. It only ever changes the
presser's own row, and only a current member may turn it back on: Off only
deletes, but On from an old DM would let someone who left (and may have been
erased) re-create a row (`needle.logic.dm_button_may_write`). The channel and thread fields in the button id are not read, but the
format is frozen: old DMs carry a per-thread "Stop"
button, and a press on one means the same thing (turn reply DMs off).

The starter is remembered in memory when Needle opens the thread; after a
restart it is re-read from the starter message (one REST call per thread,
cached). A deleted starter message is cached as "unknown"; any other fetch
error (a rate limit, a 5xx) is not cached, so the next reply retries rather
than silencing the thread until the next restart.

### Auto-reactions

Channels can list **default reactions** (comma-separated emoji) that the bot
adds to every new message it threads. They are a **cue and nothing more**: a
nudge to vote, or just something to react to. The bot adds them once and never
looks at them again: it does not read them back, swap them, or take them off,
and no bot behavior anywhere depends on which of them are present.

That is the whole of Needle's reaction surface. There is deliberately no
**status** marker (🔵 open / ✅ archived / 🔒 locked; removed in migration 215):
a reaction on a Needle-threaded post carries no meaning the bot put there.

### Deleted starter messages
When a message that owns a thread is deleted, the channel's `delete_behavior` decides the thread's fate:

| `delete_behavior` | Effect |
|---|---|
| `archive_if_empty` (default) | Delete the thread if its whole history contains only the OP and the bot; otherwise archive it. **If the history can't be read, archive**, since a failed read must not look like an empty thread and delete it, replies and all (`needle.logic.starter_deleted_fate`) |
| `archive` | Archive the thread |
| `delete` | Delete the thread (falls back to archiving if the bot lacks Manage Threads, or if the delete call fails) |
| `nothing` | Leave the thread alone |

## User-visible errors

| When | The user sees |
|---|---|
| `/close`, `/title`, or a button used outside a thread | "This command can only be used inside a thread." / "Not in a thread." |
| Neither the starter nor Manage Threads tries `/close` or the **Archive Thread** button | "Only the member who started this thread or a moderator can close it." / "...archive it." |
| Neither the starter nor Manage Threads tries `/title` or the **Edit Title** button | "Only the member who started this thread or a moderator can rename it." |
| Empty title submitted | "Title can't be empty." |

A member whose text-only post is removed (channels with *require an upload* on) sees the note described above, for 15 seconds, at most once a minute. Thread-creation and welcome-message failures are logged as warnings; most reaction failures are silently ignored (only a failed default reaction is logged, at debug level). A reply DM that can't be delivered (DMs closed) fails silently. Either way, members see nothing.

## Non-goals

- No forum, voice, or announcement channel support; only regular text channels.
- No thread-status markers. Reactions Needle adds are decoration; nothing reads them.
- No slash-command configuration; setup is dashboard-only.
- No retroactive threading of messages sent before a channel was configured.
- No retroactive removal either: switching *require an upload* on leaves text posts already in the channel, and their threads, alone.
- No returning a removed post's text to its author. Message content is not stored.

## Configuration

All configuration is per-guild via the web dashboard (admin permission required
to change it; moderators open the page read-only; see dashboard_ia.md § IA6):

- **Per channel** (`PUT /config/needle/{channel_id}`, `DELETE` to remove): title style + custom title, include bots, slowmode (0–21600 s), delete behavior, reply type + custom reply, default reactions, **Allow Reply DMs** (off by default; members still have to opt in), **Require an Image or File on Every Post** (off by default), **Threads Archive After** (1 hour / 1 day / 3 days / 1 week; 1 day by default, which is what every thread got before the dial).
- **Guild-wide** (`PUT /config/needle/settings`): the default reply template (default "Thread created by $USER in $CHANNEL"), used by any channel whose reply type is `default`.

## Stored data

- `needle_channels` table: one row per configured channel: `(guild_id, channel_id)` primary key plus the per-channel settings above.
- `needle_reply_optins` table: one row per member who switched **Replies to Your Threads** on: `(guild_id, user_id)` primary key plus `created_at`. Turning it off deletes the row, so Off stores nothing. Per-user data (row in `docs/data_register.md`): cleared by `purge_user_data`. Replaced `needle_reply_mutes` (the per-thread opt-out, migration 223), which migration 244 dropped; it had no rows in prod.
- `dm_batch_pending` rows of kind `needle_reply`: reply DMs waiting out their ten-minute window (`group_key` = thread, `actor_id` = replier, `ref_id` = the reply). Owned by `dm_batch`; row in `docs/data_register.md`.
- Guild config key `needle_default_reply` in the shared config store.

Nothing in Discord is treated as state: there is no reaction the bot reads back. The starter-author cache is in memory
only, and so is the record of who was recently sent the *require an upload*
note. A removed post leaves no row anywhere: `require_attachment` is a channel
setting, not per-user data, and so is `auto_archive_minutes`.
