# Word Cloud

> **Classification: Reference.** Matches current behavior.

`/wordcloud` renders a picture of the words a channel has been using over a
window. It is **moderator-only** and replies **ephemerally**.

## Why it is mod-only

The command reads channel history live from Discord, and a moderator asking for
one member's words is a moderation read, not a member feature. Keeping it
mod-gated and ephemeral means the picture only ever reaches someone who could
scroll that channel themselves. An open member-facing version would need a
member opt-out; none exists, because none was built.

**Read permission is the gate.** A moderator can cloud any channel they can
read, and no channel they cannot. Every gate lives in `logic.plan_scope`, which
takes plain data and returns a `Scope` or a `Refusal`, so each denial is
asserted directly rather than through a Discord mock. The order is: in a guild,
then staff, then able to read what was asked for; a non-moderator gets the
same wording whether or not the channel exists. `word_cloud/cog._can_read`
is the single definition of "may read", used both for a named channel and for
every candidate in the `everywhere` fan-out.

There is deliberately **no NSFW gate**: the reply never leaves the invoking
moderator, and an age-gated room is one they can already read.

`everywhere` and `channel` overlap. `everywhere` wins, and the card says so
rather than discarding the picked channel silently.

**Private threads need their own check.** `Thread.permissions_for` only
inherits the parent channel's overwrites; it knows nothing about who was
invited, so a private thread the moderator was never added to would otherwise
come back "readable" and land in an `everywhere` cloud. Discord's own rule is
invitation *or* Manage Threads, and only the second half is knowable from the
cache, so `_readable_channel_ids` requires `manage_threads` for any private
thread. Archived threads are absent from `guild.threads` and so are never
clouded by `everywhere`; running the command *inside* a thread still clouds
that thread, since `interaction.channel` is proof of access.

## Command

`/wordcloud [window] [channel] [member] [everywhere] [preset] [color]`

| Option | Default | Notes |
|---|---|---|
| `window` | `10m` | `5m`, `10 minutes`. Anything longer is clamped to 10 minutes and the reply says so. |
| `channel` | the current one | Any channel the invoker can read. |
| `member` | everyone | One person's words only. |
| `everywhere` | off | Every channel the invoker can read, instead of one. |
| `preset` | the guild dial | One of six visual styles. |
| `color` | by mood | `sentiment` or `palette`. |

## Corpus: always live

The bot stores no message text, so every cloud reads `channel.history()` off
Discord and stores nothing. The window is **clamped to `LIVE_FETCH_MAX` (10
minutes)**; a longer ask is shortened and the card carries
`logic.live_clamp_note()`, because silently returning ten minutes of a week-long
ask would be a lie. The card's window label is the span actually read.

The cloud has no stored-text archive to reach back years (`docs/plans/remove-message-storage.md`); the live path is the only one.

A live `everywhere` fans out over at most `LIVE_CHANNEL_FANOUT` (25) channels,
ranked by their most recent message (`corpus.recent_channel_ids`). The ranking
reads `messages` timestamps, which are metadata and remain stored.

## What is filtered, and why

Measured over seven days of home-guild traffic, the top raw tokens were `white`
(17,357) and `square` (11,861): both a bot's board art. With bot authors excluded,
`https`/`com`/`gifs`/`klipy` rose to replace them. So:

- **Bot authors excluded** (`message.author.bot`). Naming an author turns that
  exclusion off, which is the behavior wanted: asking for one account's words
  means that account, bot or not.
- **Deleted messages** never appear: Discord does not serve them.
- **URLs, custom emoji, mentions, code fences and inline code stripped** before
  tokenising.
- **Typographic apostrophes folded to ASCII.** U+2019 outnumbers the ASCII
  apostrophe 185 to 73 in `don't` alone; unfolded, every contraction splits and
  the orphaned stem (`don`, `that`, `it`) walks past the stopword list. This was
  a real defect caught on live data, not a hypothetical.
- **Stopwords and words under 3 characters dropped.**

## Sentiment colouring

Each fetched message is scored with `sentiment_service.score_text`, the same
VADER scorer ingest uses for `messages.sentiment` (`logic.score_docs`, run in a
worker thread, and only when mood coloring was asked for). Each word is tinted
by the mean score of the messages it appeared in: warm for happier, cool for
unhappier, the preset's neutral stop for the middle. Averaged per *occurrence*,
matching the way the count sizes the word.

Text too short to score stays unscored rather than counting as neutral. When
nothing in the window could be scored, the cloud falls back to the preset
palette and the reply says so.

## Dials

Dashboard → Config → Channels & Messages → **Word Cloud** (route id
`word-cloud`, `PUT /api/config/word-cloud`, admin-gated).

| Key | Default | Notes |
|---|---|---|
| `word_cloud_message_cap` | 12000 | Clamped to 100–12000 on save. A cap that bites is reported on the card; the fetch reads `cap + 1` messages so "exactly full" can be told from "truncated". |
| `word_cloud_default_preset` | `midnight` | An unknown key degrades to the default rather than raising. |

Both are read **without** the legacy `guild_id=0` fallback, by the cog and by
`GET /api/config` alike. They are new keys, so a row at `0` could only ever be
the home guild's, and a second guild must not inherit it.

## Rendering

`wordcloud==1.9.6` (added to the runtime dependencies with this feature; no new
transitive deps: numpy, Pillow and matplotlib were already locked). It pulls
matplotlib for colormaps, so `render.py` sets `MPLCONFIGDIR` **before** the
import exactly as `analytics/activity_graphs` does; the unit runs
`ProtectHome=read-only` and matplotlib resolves the path at import time.

`pyplot` is never touched (`WordCloud.to_image()` returns a PIL image), so this
needs none of the serialization `shared/pyplot_lock` exists for. The render
runs in `asyncio.to_thread`.

## Layering

`logic.py` owns the dial keys and `clamp_cap`, the window parsing and clamp,
tokenising and counting, `plan_scope`, and every line of reply copy.
`embeds.py` builds the card and escapes the member's display name: an
unescaped `__Robin__` would reformat the description. `corpus.py` reads,
`render.py` draws, `presets.py` styles (its fonts are keys into
`quote_renderer.FONT_STYLES` rather than a second catalog of the same
files). **Poppy** is the server's own look: Thught Regular in the panel
titles' petal golds; like Neon's Bebas it is Latin-only, so a word in another
script draws as boxes. The cog resolves Discord objects, calls those, and sends.

The cap's floor and ceiling exist once, in `logic.py`. `GET /api/config`
returns them alongside the preset list so the dashboard panel renders its own
bounds instead of re-typing the numbers, and the panel uses `selectValueOrAdd`
so a stored preset key this build doesn't know is never silently overwritten.

## No new tables

The two dials are guild config rows. No per-user data is stored (the fetched
text lives only in memory for the length of one command), so there is no
`docs/data_register.md` row to add.

## Tests

- `tests/word_cloud/test_word_cloud_logic.py`: window parsing, the 10-minute clamp, every
  stripping rule, apostrophe folding, stopwords, the cap, sentiment averaging,
  preset resolution and color.
- `tests/word_cloud/test_word_cloud_corpus.py`: channel ranking for the live fan-out.
