# Reporting — Feature Spec

Reporting is the analytics backbone of the dashboard. A handful of small services (incident detection and invite attribution) produce the data (there is no interaction tracking or voice-follow capture, `docs/plans/remove-social-graph.md`); the dashboard renders it as charts and tables. The Discord surface is empty apart from an unrelated `/invite` command that returns the bot's install URL.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/invite` | Slash | Everyone | Returns the bot's OAuth install URL. Unrelated to invite attribution |
| Dashboard report tiles | Web | Admin | Read-only analytics surfaces; see Behavior |
| Cache clear | Web | Admin | Drop every cached report payload for the active guild |

The bot needs **Manage Server** to read invite codes for attribution. When missing, invite attribution degrades to "no inviter detected" and **logs a warning on every refresh and every unattributed join**; no other report is blocked. The collector (`bot_modules/invites/tracker.py`) diffs cached invite snapshots on each join: it drains join bursts one use at a time, attributes consumed single-use invites via their disappearance (Discord deletes them before the use count is ever observable), and keeps the cache current through `on_invite_create` / `on_invite_delete`. Attribution misses (vanity URL joins, diff races) are logged, never silent.

## Behavior

### Dashboard report tiles

Every report is admin-only, GET-only (cache clear is the single POST), and read through a per-route cache keyed by guild + parameters. Most tiles use a 60-second TTL; heavier tiles that scan the message archive (time-to-level, activity drop-off) use 5 to 10 minutes. An hourly background warmer additionally precomputes the default-parameter view of the heavy tiles so a cold page load rarely pays the compute; non-default parameter combinations still compute on demand. The cache only invalidates on TTL expiry or explicit clear; there are no realtime pushes.

Day-bucketed charts roll over at the guild's local 6 am, not midnight. Names on every row are resolved live from the guild cache when the bot is online and fall back to the historical name archive when offline; some tiles (role listings, guild-wide inactivity) return a service-unavailable error when the bot is offline since they depend on live role membership.

The **Community Health** panel (`/api/health/*`) has its own cache: the DB-backed `health_metrics_cache` table, 15-minute TTL, keyed by `(guild_id, metric_key)`. Every surface uses it, not just the grid, and the deep-dive endpoints (dau-mau, heatmap, channel-health, gini, sentiment, newcomer-funnel, cohort-retention, mod-workload, mod-engagement) cache under a `deep:` key prefix, because a deep dive returns a superset of its tile (per-channel breakdowns, full graphs), so the two shapes must never share a key. Every parameter that changes the payload is part of the key (`+bots` for `include_bots`, `|days=N` for the mod-engagement window and the dau-mau trend chart's window; the DAU/WAU/MAU tiles it sits beside keep their fixed 1/7/30-day definitions regardless of it). **Deep dives are therefore up to 15 minutes stale.** Display names are the exception: they are resolved after the cache read and never stored, so a rename shows up on the next request rather than after the TTL. `clear_cache(conn, guild_id)` drops tile and deep-dive entries together.

**A degraded payload is never cached.** `mod-workload`, `mod-engagement`, `newcomer-funnel` and `cohort-retention` derive their `mod_ids` / `recent_joins` inputs from the **live** guild object. While the bot is mid-startup, the gateway member cache is cold, or the bot isn't in the guild, that member list is empty and those metrics compute to zeroes. The zeroed payload is still returned (a blank tile beats an error), but it is not written to `health_metrics_cache`; otherwise a few seconds of startup would be served as fact for the next quarter of an hour. `_guild_extras` reports this as `degraded` (no non-bot member visible; every real guild has at least an owner) and `_cache_unless_degraded` is the only writer for those four metrics, on both the tiles and deep-dive paths. Because the cache is read before compute, an existing good value keeps being served through the whole degraded window. Metrics that read only the database (dau-mau, heatmap, gini, sentiment, channel-health) are unaffected and cache as normal.

Sentiment reads go against the `messages` table's own `sentiment` / `emotion` columns (indexed `(guild_id, sentiment)`), not the `message_sentiment` side table. The two are written by the same code paths and carry identical data, but only `messages` is indexed for the queries the panel actually runs. As of 2026-08 this covers **every** reader on the health hot path, `compute_sentiment` in `health_metrics.py` included. The side table is still written (ingest + backfill) and still read by `reports_data.py`; nothing has been dropped. One trap: the old join supplied `sentiment IS NOT NULL` implicitly, since a `message_sentiment` row only exists for a scored message. `AVG` skips NULLs on its own, but `COUNT(*)` does not, so every query reading `messages` must state the predicate or unscored messages inflate `scored_count`, the per-channel counts, and the negative-spike `HAVING cnt >= 3`.

**Bots are excluded from every metric by default.** Bot traffic was ~21% of stored messages, so counting it would make every message-volume number wrong by up to a fifth; the worst single case was a channel that was 99% one bot. `bot_filter_clause()` in `bot_modules/core/bot_exclusion.py` is the single source of truth: a `NOT IN (SELECT user_id FROM known_users WHERE guild_id=? AND is_bot=1)` fragment appended to a query's `WHERE`. Every affected route takes `include_bots: bool = False` to opt back in; the health cache namespaces bot-inclusive payloads under a separate key (`cache_key()`) so the two variants can't poison each other.

**A channel metric counts channels, not thread ids.** Every per-channel aggregate groups `messages` by `channel_id`, which for a message posted in a thread is the *thread's* own id, so threads showed up as rows in views meant to answer "which channels are alive": 243 of the 367 rows in a 30-day window were not channels. `bot_modules/core/channel_rollup.build_resolver()` is the single source of truth, and every channel-grouped surface folds through it: `channel-comparison`, `channel-health`, the heatmap's per-channel breakdown, and the homepage top-five. Its rules, in order: an ephemeral bot-made channel is dropped; a current guild channel counts as itself; a thread counts toward its parent; anything else is dropped.

Threads are **attributed, not excluded**: the conversation belongs to the channel it started in, so the parent's volume, unique authors, Gini, depth and last-activity all include its threads. That is why the merged figures are recombined in Python from per-`(channel, author)` rows rather than taken from SQL: summing distinct-author counts would double-count anyone active in both, and averaging two averages would weight a three-message thread like its thousand-message parent. Ordering and any `LIMIT` must be applied *after* folding.

The ephemeral families are identified from their own registries, never by name-guessing: `pen_pals_sessions` (which keeps closed rows, so even a long-deleted pairing stays recognizable), `voice_master_channels`, and `jails`. The bios wizard is the sole exception: it keeps no registry, so its `bio-<user id>` rooms are matched by name.

Nothing in the schema distinguished a thread before this, so `known_channels` gained `parent_id` / `is_thread` (migration 163), written at ingest from discord.py's `Thread.parent_id`, an attribute no other channel type has, so a text channel can never be rolled into its category. History was recovered in two passes: the migration's offline self-join (a thread started from a message takes that message's id, so the parent is recoverable wherever the starter was archived, about a fifth of them), then `scripts/archive/backfill_thread_parents.py` walking the live guild's active and archived threads. **Whatever neither pass reached is dropped, not shown**: roughly 3% of a 30-day window's messages sit in deleted threads and removed channels. Channel *names* are unaffected: they still resolve from the guild cache first, `known_channels` second.

One degraded mode: with no guild cache (bot offline, guild uncached, or a channel list still empty because the gateway hasn't filled it; every real guild has at least one channel, so empty means "don't know", never "none") the resolver cannot check which ids are current, so it classifies from the database alone and **keeps** what it cannot rule out, because a stale row beats an empty panel reading as a broken report. Threads still reach their parents; ids known to be threads with no parent still drop.

`known_users` is the only source consulted (a live `guild.members` scan would miss bots that had left the server). Authors with no `known_users` row count as human: in prod all 40 such accounts are departed members (39 of 40 have XP rows, and no bot has ever earned XP). XP was already human-only, so XP totals are unaffected.

Not filtered, deliberately: the live Watch relay check (`moderation/ai_service.py`), the ingest layer (`message_store`) and privacy deletion. `games_external` and `bump_tracker` exist to read bot messages and are untouched. Mod workload/engagement need no filter, since they are already scoped to a mod-id list.

Tiles group into a few areas:

- **Activity**: the merged **Channels** panel (per-channel health status/score over 30 days plus a windowed comparison of messages / XP / sentiment / trend; both `/api/health/channel-health` and `/api/reports/channel-comparison` feed it), top voice users, generic activity (messages or XP) with user/channel exclusions and a **Show Bots** opt-in. The Activity panel also hosts the post control for the **moderator stats panel** (the same day-overlay charts as a self-refreshing sticky panel in a Discord mod channel; see [mod_stats_panel_spec.md](mod_stats_panel_spec.md)). The finer-grained message-rate/cadence/burst experiments were removed in the 2026-07 reports cleanup.
- **Membership health**: join-time histogram, cohort retention, NSFW-channel activity over time, activity drop-off profiles, and the merged **Inactive Report** (one member list over last-activity data: scoped to everyone / role holders / role non-holders, filtered by idle days, where 0 lists the whole scope oldest-first, optionally measured within one channel; logic in `inactive/report_service.py`).
- **Greeter performance**: greeter response time and missed joins, derived from the configured greeter chat channel and welcome / leave audit.
- **XP**: top-N leaderboard for a window, days-to-level-5 histogram, and a generalised days-to-level-N report (level 2–100). Source data is owned by [[xp-spec]].
- **Invite effectiveness**: per-inviter table of active invitees joined through them.

### Removed: Flagged Messages and Message Review

Both read stored message text, and were deleted when the bot
stopped keeping it (`docs/plans/remove-message-storage.md`).

- **Flagged Messages** (`health-sentiment`) listed recent messages scoring `<= -0.5`, with the wide
  `health-sentiment-feed` home widget and `/api/health/sentiment-feed` behind
  it. A saved home layout that held the widget loses it silently:
  `getLayout()` drops widget ids it no longer knows.
- **Message Review** (`message-search`) was the moderator search over the
  archive, with its regex rails, context view, export and AI question box
  (`/api/messages/*`).

Both route ids stay retired rather than redirected: neither has a successor.
`/api/health/sentiment` and `compute_sentiment` remain, reading only the
per-message scores; no panel renders them any more.

The feed's cache rows (`sentiment_feed` and `deep:sentiment_feed*` in
`health_metrics_cache`) held message excerpts, as did the older `sentiment`
and `sentiment_outliers` rows the Sentiment & Tone widget left behind. None has
a reader, and migration 228 deletes them with the rest of the stored text.

### Incident detection

A per-process velocity tracker keeps a 10-minute sliding window of message rate per guild. Against a 30-day baseline (mean + standard deviation per hour-of-day × day-of-week, refreshed every 15 minutes), a velocity spike fires when the current rate is at least mean + 3·stddev **and** above 5 messages per minute. Severity is `critical` past 1.5× the threshold, otherwise `warning`. The same guild can't emit another velocity incident within 5 minutes.

A join raid fires when at least 3 accounts younger than 7 days join within a 2-minute window. Severity is always `critical`. Incidents are stored for the health-metrics tiles to read.

### Invite attribution

The bot caches the current `uses` count for every guild invite at startup and refreshes per join. When a member joins, the bot diffs the live invite list against the cache; the first code whose `uses` ticked up is recorded as the inviter on that join. If two joins land in the same window, only one inviter is detected; the rest record without an attribution. Re-joins after a leave never overwrite the original inviter.

## Permissions

- `/invite`: open to everyone.
- All dashboard report routes: admin tier.
- Bot-side: no Discord permissions are required to read reports. Invite-cache refresh needs **Manage Server**; a missing permission is a soft degrade, not an error.

## User-visible errors

| When | The user sees |
|---|---|
| Generalised time-to-level requested with level outside 2–100 | HTTP 400 |
| Greeter-response asked for a period with no resolvable greeters | HTTP 404 "No greeter response data found for the selected period." |
| Role-listing or guild-wide inactivity tile while bot is offline | HTTP 503 "Guild not available." |
| Other report tile while bot is offline | Tile renders with archive-only names (no error) |
| A report tile worker raises | HTTP 500; the cache entry is not stored so the next request retries fresh |

## Non-goals

- **No realtime dashboard updates.** Every tile is poll-driven; cache TTL is the freshness floor.
- **No write endpoints on report tiles.** Only the cache-clear endpoint is non-GET, and it never touches data tables.
- **No precise invite tracking.** Concurrent joins race the cache diff and may mis-attribute or fail to attribute.
- **No historical baseline retention.** The 30-day rolling baseline overwrites older numbers; there is no audit of how baselines drifted.
- **No incident review UI.** Velocity spikes and raid attempts are stored but only surfaced through health tiles; there is no dedicated dashboard list or slash command.

## Configuration

Reporting reads guild config but owns very little of its own. The dashboard reads:

- The XP-excluded channel list (passed through to activity-by-XP).
- The recorded-bots list (lets activity tiles include or exclude their messages).
- Greeter role, greeter chat channel, welcome / leave / join-leave log channels, for the greeter-response tile.
- The NSFW role, for the oldest-SFW-members tile.

Reporting owns no configuration of its own.

## Stored data

Per-guild and per-user: per-join invite-attribution rows (one per invitee, never overwritten); an append-only incident log for velocity spikes and raid attempts; a per-hour-of-day × day-of-week baseline of message velocity, refreshed in the background. No filesystem cache: chart payloads are JSON returned through the per-route memory cache. The velocity tracker and invite cache are per-process in-memory state and rebuild from the database on restart.

## Removed

- **Interaction graph, Connection Graph, One-Sided Attention and the Health Social Graph tile**: removed with the member-to-member interaction store they read (`docs/plans/remove-social-graph.md`). Their route ids (`interaction-graph`, `connection-graph`, `one-sided-attention`) are retired, not free for reuse.
- **Contributors**: removed at Billy's ask (`docs/plans/remove-contributors.md`): the five ranked views (Popular Content, Conversation Catalyst, Connectors, Welcomers, Lifts the Under-Attended), `contributors_service.py`, its route and its hourly cache warm. It had itself replaced the Member Quality Score; the backtest that retired that score, and why no composite should come back, are in `docs/plans/quality-score-revisit.md`. The `quality-score` id redirects to Activity. Migration 234 dropped the empty `quality_score_leaves` table the old score read.
