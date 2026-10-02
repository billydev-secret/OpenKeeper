# Post Monitoring — Feature Spec

A side-effect layer that runs message-content checks as part of the normal `on_message` pipeline. Two checks live here: **spoiler enforcement**, which removes images posted without Discord's spoiler blur in channels Discord marks NSFW, and **GIF Guard**, which removes GIFs from channels an admin lists.

There is no image classifier and no admin-picked Spoiler-Required Channels list: a classifier rejected innocent images in SFW channels (Billy: "It should only be based on discord setting"). Nothing about a picture is read, scored or recorded.

## Commands

Post monitoring has **no commands** of its own: no slash, no context menu. It is invoked from gateway message listeners (see [[events-spec]]). Spoiler enforcement has no setting of its own: the scope is Discord's NSFW flag, and the only knob is the bypass-role list on the dashboard's **Global** panel. The retired `config-spoiler` panel redirects there. GIF Guard's one setting, its channel list, is on its own **GIF Guard** panel (`gif-guard`, Config → Channels & Messages).

## Behavior

### Spoiler enforcement

In a channel Discord marks NSFW (the channel's own age-restricted flag or its category's, read by `shared/age_gate.is_age_gated_channel`):

1. A member with a bypass role posts an image. Nothing happens. Bypass roles are configured per-guild.
2. A member without a bypass role posts an image that's already marked spoiler. Nothing happens.
3. A member without a bypass role posts a non-spoilered image: the message is deleted and the bot posts an inline reminder ("Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.") that self-destructs after 5 seconds.

The rule is the same for every image: a meme, a screenshot and explicit content are treated alike. The only inputs are the filename and Discord's spoiler flag. The image is never downloaded.

The check only fires on attachments ending `.png/.jpg/.jpeg/.gif/.webp` (`SPOILER_IMAGE_EXTENSIONS`). That list is deliberately narrower than the general image predicate `attachment_is_image` (which XP and Auto React use, and which also accepts `.bmp`, `.tiff` and any `image/` content type), because this check deletes what it matches; a test pins the subset. Non-image attachments and text-only messages are ignored.

On a message carrying several images, one spoilered attachment does not clear the message: any unspoilered image triggers deletion.

Channels not marked NSFW are never checked: nothing is removed from SFW channels for its content. A channel whose flag can't be read (a partial object, a thread whose parent isn't cached) is also left alone: this gate deletes posts, so it fails toward keeping them. Threads follow their parent channel.

There is deliberately no bot-side list: an admin-picked list drifts from the age gate (an audit found most listed channels weren't age-restricted), which is how SFW channels lose images.

Webhooks, bots, and any author the bot can't resolve as a guild member skip enforcement entirely.

When a message is deleted by spoiler enforcement, the rest of the `on_message` pipeline short-circuits: no XP, no interaction tracking, no wellness checks fire on the deleted message.

### GIF Guard

Billy's ask: "a image guard like feature that deletes gifs posted in a channel", with its own config page holding a list of channels. Detection is `core/gif_guard_logic.py`; the delete is `core/post_monitoring.enforce_gif_guard`.

In a channel on the list, or a thread whose parent is on it:

1. A member with a bypass role posts anything. Nothing happens. These are **the spoiler gate's bypass roles** (Global panel), by Billy's decision, so there is one exemption list rather than two.
2. A member without one posts a message carrying a GIF: the **whole message** is deleted (text too; Billy chose this over stripping only the preview) and the bot posts "Beep Boop - friendly bot helper: GIFs aren't allowed in this channel.", which self-destructs after 5 seconds.

What counts as a GIF, each rule narrow because everything it matches becomes deletable:

- **A link in the message text** to `tenor.com`, `giphy.com` or `gph.is` (any subdomain), or any link whose path ends `.gif` / `.gifv`. Discord's GIF picker posts a Tenor link as the message text, so the commonest GIF is caught on `on_message`, before a preview exists. A link wrapped in `<…>` is **not** a GIF: Discord shows no preview for it. The words "tenor" or "giphy" outside a link never count.
- **An upload** ending `.gif`, typed `image/gif`, or carrying Discord's own `animated` attachment flag (an animated WebP). A still `.webp` is not matched.
- **A preview** (embed) of type `gifv`, or of type `image` pointing at a `.gif`. Previews arrive in a later message edit, so `events_cog.on_message_edit` re-runs the gate; this catches GIF links from hosts the list above doesn't name, and a GIF link edited into an existing message.

Out of scope: animated custom emoji and stickers.

An empty list guards nothing. The feature ships dark. Webhooks, bots and authors that don't resolve as guild members are skipped, as for spoiler enforcement. A message already deleted by spoiler enforcement is not checked again; either deletion short-circuits the rest of `on_message`.

## Permissions

- **User-side**: none. Enforcement is automatic.
- **Bot-side**: **Manage Messages** and **Send Messages** in every NSFW channel, and in every GIF Guard channel. Without either, the offending image or GIF survives and the failure is logged silently.

## User-visible errors

| When | The user sees |
|---|---|
| Non-spoilered image is deleted | Inline reply (auto-deletes after 5s): "Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler." |
| GIF deleted by GIF Guard | Inline reply (auto-deletes after 5s): "Beep Boop - friendly bot helper: GIFs aren't allowed in this channel." |
| Bot lacks Manage Messages | No user-facing message; the image or GIF survives, the failure is logged operator-side |

## Non-goals

- **No content inspection.** The picture is never fetched or judged; see the history note above.
- **No embed / linked-image spoiler enforcement.** Only uploaded attachments carry a spoiler flag the bot can read.
- **Link scanning is GIF Guard's alone**, and only for GIF hosts and `.gif`/`.gifv` paths. Wellness Guardian has a separate keyword pipeline; see [[wellness-guardian-spec]].
- **Edits re-run GIF Guard only.** A non-spoilered image edited after posting isn't re-evaluated by spoiler enforcement.
- **No file scanning of non-image attachments.** PDFs, archives, executables pass through untouched.
- **No incident audit log.** Deletions are logged operator-side only, not into the incident pipeline ([[reporting-spec]]).

## Configuration

| Key | Owner | Purpose |
|---|---|---|
| Bypass role ids | [[events-spec]] | Roles exempt from spoiler enforcement and GIF Guard (dashboard: Global) |
| `gif_guard_channel_ids` (id bucket) | this spec | Channels GIF Guard deletes GIFs in; empty = off (dashboard: GIF Guard) |

Which channels enforce is Discord's NSFW flag, set in Discord's own channel settings.

## Stored data

None. Both checks are stateless: deleted messages are not recorded, and GIF Guard's log line names the author and channel but not the message text. Migration 230 dropped the classifier's tables (`nsfw_classifications`, `nsfw_detections`, `nsfw_blocks`), its six config keys, and the `spoiler_required_channels` bucket.
