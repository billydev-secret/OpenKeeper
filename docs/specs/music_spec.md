# Music — Feature Spec

A music playback cog for shared listening in voice channels. Supports YouTube tracks and Spotify URLs (tracks, playlists, albums and artists, all resolved to YouTube). Slash commands plus a persistent now-playing card with button controls. There is no 24/7 mode (see below).

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/play query:<text\|url>` | Slash | Same-voice-channel | Play a search query, a YouTube URL, or a Spotify track/playlist/album/artist URL |
| `/skip` | Slash | Same-voice-channel | Skip the current track |
| `/queue [page]` | Slash | Everyone | Show current track + upcoming queue (paginated) |
| `/shuffle` | Slash | Same-voice-channel | Shuffle the upcoming queue (does not interrupt the current track) |
| `/loop mode:<off\|track\|queue>` | Slash | Same-voice-channel | Set the loop mode |
| `/pause` / `/resume` | Slash | Same-voice-channel | Pause or resume playback |
| `/stop` | Slash | Same-voice-channel | Clear the queue and disconnect |
| `/nowplaying` | Slash | Everyone | Repost the now-playing card |
| `/disconnect` | Slash | Same-voice-channel | Force-disconnect from voice |

The now-playing card is a persistent message with five buttons: **Pause/Resume**, **Skip**, **Stop**, **Shuffle**, and **Loop** (cycles off → track → queue). Buttons require the clicker to be in the same voice channel as the bot.

**One card per session, edited in place.** Every track change edits the existing card rather than posting another; a new one is posted only when there is none, or when the card has been deleted. Refreshes are coalesced to at most one every 2 seconds per guild, so a run of skips costs one edit showing the track that actually won. The card stays in the channel it was posted in even if `/play` is later run somewhere else; `/nowplaying` is how it moves: it posts the replacement first and deletes the old one after, so a track change landing mid-command edits the card still on record rather than adding a third. Ending the session (`/stop`, `/disconnect`, the Stop button, the idle disconnect, the bot being dragged or disconnected out of voice, or the cog unloading) deletes the card, so the next session starts clean rather than beside a card naming a track that stopped playing. Refreshes for one guild never overlap: a slow edit finishes before the next one starts, so the card can't end up showing the track before last.

## Behavior

### `/play`

Joins the invoker's voice channel if the bot isn't already connected there. If the bot is connected to a different channel in the same guild, the command is rejected. Resolution:

- **Search query** → looks up on YouTube, queues the top match.
- **YouTube URL** → queues directly.
- **Spotify track URL** → resolved to YouTube via ISRC where available, falling back to a title+artist search.
- **Spotify playlist or album URL** → resolves up to 500 tracks (warns if the playlist is larger), queues them in order. A playlist is only readable when the connected Spotify account owns or collaborates on it; see below.
- **Spotify artist URL** → queues the artist's top tracks.

If the queue is empty, playback starts immediately; otherwise the new tracks are enqueued and the user gets an ephemeral "queued" confirmation.

### Now-playing card

On track start, the bot posts a card to the text channel where `/play` was invoked: title (linked), artist, requester, duration, position in queue, current loop mode, artwork thumbnail. Subsequent track starts edit the same card rather than spamming new posts. The card's buttons stay live across bot restarts.

### 24/7 mode (removed)

There is no pinned-channel mode: no `/247` or `/247_status`, no always-on
rejoin on startup, no idle-disconnect exemption, no autoplay refill (it only
ever ran for an always-on channel) and no per-channel settings table
(`music_channel_settings`, dropped by migration 193; it held a stored
preference with no reader plus an `updated_by_user_id`, an unregistered
personal-data store). The bot leaves every channel on the normal idle sweep.
Reviving 24/7 playback is a new build.

### Track-failure fallback

YouTube refuses some tracks outright, most commonly a rights-holder content
claim (e.g. "blocked due to the claimed content by SME") that no client
rotation can bypass. When the current track raises a Lavalink exception, the
cog attempts **one** recovery before giving up:

1. Search YouTube for an alternate upload (excluding the failed video id),
   then SoundCloud (`scsearch:`, requires `soundcloud: true` in
   `lavalink/application.yml`). Each source tries two queries: title + uploader first, then the bare title. A YouTube "author" is the uploader
   channel, which for re-uploads is unrelated to the song and poisons the
   search (verified against real failures).
2. Each candidate must pass the guard in `music/logic.py::pick_substitute`:
   duration within ±20% of the original (floor ±15s), ≥60% of the original
   title's core words present, and no variant term (cover / remix / sped up /
   slowed / nightcore / live / "1 hour" / etc.) the original title didn't
   already contain.
3. The first survivor plays, with a visible one-line note (substitution is
   never silent): "⚠️ Couldn't play **X** — it's blocked by the rights holder
   on YouTube. Playing the closest match from SoundCloud instead: **Y**."
   The requester carries over to the substitute's now-playing card.
4. If no candidate survives (or the substitute itself fails; recovery never
   loops), the plain-language failure line posts and the queue advances.

`on_wavelink_track_end` only advances the queue on reason `finished`:
Lavalink also ends tracks with `replaced` (/skip, substitutes), `loadFailed`
(owned by the exception handler), `stopped`, and `cleanup`, and advancing on
those double-advanced the queue (a /skip with ≥3 queued tracks dropped one).

### Spotify URL handling

Track, playlist, album and artist URLs from `open.spotify.com` and `spotify:` URIs are recognized. Playlists cap at 500 tracks per submission. Tracks that can't be matched on YouTube (no ISRC, no clear search match) are skipped with a warning, not a hard failure.

**Playlists need the connected account's access.** The Spotify app runs in Development mode, and since Spotify's February 2026 rules a Development-mode app can read a playlist's *items* only when the connected account (Connect Spotify on the dashboard) owns the playlist or collaborates on it. The name still reads, then the items call answers 403. There is no code-side way around this short of leaving Development mode (Spotify's extended quota is for registered businesses) or scraping, which is off the table. So `/play` says so plainly instead of claiming nothing was found; the fix for a given playlist is to add the connected account as a collaborator. Tracks and albums are unaffected.

## Permissions

- **Bot:** Connect and Speak in the target voice channels; Send Messages and Embed Links in the text channels where the now-playing card posts.
- **User:** Must be in a voice channel for `/play`. For playback-control commands (skip, shuffle, loop, pause, resume, stop, disconnect) and now-playing buttons, must be in the same voice channel as the bot.

## User-visible errors

| When | The user sees |
|---|---|
| User runs `/play` while not in voice | "Join a voice channel first." |
| User runs `/play` while in a different channel from the bot | "I'm currently in #other-channel. Join me there or wait for the queue to finish." |
| Spotify playlist exceeds 500 tracks | The usual "Queued **N** tracks from playlist **Name**." confirmation followed by "(Playlist truncated to first 500 tracks.)" |
| YouTube track fails to load (rights-holder block, age gate, removed, etc.) | The bot tries one substitute (alternate YouTube upload, then SoundCloud) and announces it: "⚠️ Couldn't play **X** — [reason]. Playing the closest match from SoundCloud instead: **Y**." If nothing passes the match guard, one plain line naming the track and the reason posts and the next track plays. Full Lavalink exception goes to the log only. |
| Spotify playlist Spotify can't find (404) | "Spotify couldn't find that playlist. It may be private or deleted." |
| Spotify playlist the connected account neither owns nor collaborates on (403 on its items) | "Spotify only lets the bot read playlists that the bot's Spotify account owns or collaborates on, and this isn't one of them. Ask the playlist's owner to add that account as a collaborator, or paste single tracks or an album instead." Two variants: no account connected, and an account on file whose login refresh failed ("…has lapsed. An admin needs to reconnect Spotify on the dashboard…"), told apart because the fixes differ. |
| Spotify refuses the playlist itself (403 on its name) | "Spotify won't let the bot open this playlist. Paste single tracks or an album instead." |
| Spotify rejects the bot's login (401) | "Spotify turned down the bot's login. An admin needs to reconnect Spotify on the dashboard." |
| Any other Spotify failure (credentials missing, rate limit, network, other statuses) | "Spotify couldn't be reached just now. Try again in a bit, or paste a YouTube link." The detail goes to the log only: those messages are operator text (credential hints, raw response bodies) and never reach a channel. Member-facing failures are raised as `SpotifyMemberError`; everything else is not. |
| Spotify link resolved but has no readable tracks | "That Spotify link has no tracks the bot can read." |
| Spotify tracks resolved but none matched on YouTube | "Couldn't find a YouTube match for that Spotify link." |
| Spotify URL is malformed | "Not a valid Spotify URL." |
| A search or YouTube link finds nothing | "Nothing found for that query." |
| Now-playing button clicked from outside the voice channel | "You need to be in the voice channel." |
| Music backend isn't running | Cog fails to load with a clear error; the rest of the bot keeps running |

## Non-goals

- Apple Music, Deezer, Tidal sources. SoundCloud is enabled as the fallback
  when YouTube refuses a track; a pasted SoundCloud URL therefore also plays,
  but SoundCloud *search* stays YouTube-first and isn't a promoted feature.
- Saved community playlists / named presets.
- Lyrics, audio filters, EQ.
- Vote-skip, per-user request limits.
- Persistent queue across restarts (queue is in-memory only).

## Configuration

The music cog has no per-guild configuration of its own.

### Audio quality and Opus passthrough

Lavalink forwards the source's Opus frames to Discord untouched (no decode, no
re-encode, near-zero CPU), but **only** while the player's volume is exactly 100
and no filter is active. Any other volume forces a full decode → resample →
re-encode, which adds a lossy generation on top of a source that is usually
already Opus (YouTube itag 251, ~149kbps).

`_DEFAULT_VOLUME` in `music/cog.py` is therefore **100 and must stay there**.
A low default (the old 20, chosen so the bot wasn't startling on connect) re-encodes every track, on a voice channel running at
384kbps. Members who want it quieter use Discord's own per-user volume slider.

The Lavalink dials live in `lavalink/application.yml`:

| Setting | Value | Why |
|---|---|---|
| `bufferDurationMs` | 1000 | Koe's UDP send buffer. The 400 default leaves no headroom for a GC pause or network hiccup, both of which are audible as stutter. Raising it slows skip/seek response, not quality. |
| `frameBufferDurationMs` | 10000 | Source-side pre-buffer; guards against YouTube throttling mid-track. Costs heap per player. |
| `opusEncodingQuality` | 10 | Already the maximum. |
| `resamplingQuality` | HIGH | Only in the path when passthrough is off (non-Opus sources such as the SoundCloud fallback, or YouTube tracks offering only AAC). Free on tracks that pass through. |

Lavalink exposes no encode-bitrate setting; passthrough on or off is effectively
the quality switch.

**YouTube sign-in.** Some videos are refused by every anonymous client with
"This video requires login" (age gates, bot checks). The `TV` client is signed
in via the plugin's OAuth flow with a **dedicated Google account, not anyone's
primary**; the plugin warns the account can be terminated. The refresh token
is `YT_REFRESH_TOKEN` in `.env`, read by `refreshToken: ${YT_REFRESH_TOKEN}`;
the token grants playback as that account, so never commit it. If it is ever
revoked, drop that line, restart, approve the google.com/device code printed in
`lavalink/logs/spring.log`, and store the new token the log prints.

As of 2026-09-12 two upstream breakages shape the config:

- **The plugin is pinned to a snapshot** (`f45bbb7`), not the latest release.
  1.18.2 fails on the signed-in `TV` client with "The page needs to be
  reloaded"; the fix (a PlayStation User-Agent) is on the plugin's main branch
  but unreleased. Return to a release once one ships past that commit.
- **Signature deciphering is delegated** to kikkia's public yt-cipher instance
  (`remoteCipher`). The plugin's own deciphering is unmaintained and fails,
  which surfaced as the `WEB` client's "No supported audio streams". The
  public instance is a third-party dependency on every YouTube load and is
  rate-limited to 10 req/s; if it goes down, self-host yt-cipher and point
  `remoteCipher.url` at it.

**Accepted risk.** Playing YouTube through a signed-in client and a third-party
cipher service is against YouTube's terms of service. That is a platform risk,
not a legal one we are carrying for members: the realistic costs are the
dedicated Google account being terminated or the public cipher instance going
away, either of which stops YouTube playback until it is replaced (a new
account and token, or a self-hosted yt-cipher). No member data travels to
either. Accepted on that basis; revisit if Discord or YouTube acts against
music bots more broadly.

Lavalink runs as a **child process of the bot** (`music/lavalink_manager.py`),
not a systemd unit, with its heap from `LAVALINK_HEAP_MB` (2048 in prod; the 512
default sat at ~72% resident, which means frequent GC and audible stutter). That
variable is read when the bot constructs the manager, so a heap change needs a
full bot restart; respawning the JVM alone only picks up `application.yml`.

## Stored data

No persistent tables. All queue state, playback position, and the now-playing card's channel and message ids are in-memory only and don't survive a restart. After a restart the first track change posts a fresh card, and the old one keeps working buttons because the view is persistent.
