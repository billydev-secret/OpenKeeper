# Beta Tools — Feature Spec

## Intent

The dev guild is empty by default. An empty server is a bad place to test against: leaderboards show nothing, dashboards have no signal, edge cases never fire, and most user-experience decisions can't be evaluated until real members start using the bot in prod. That's too late.

Beta Tools puts believable activity into the dev guild so the bot can be exercised the way it'll be used. It is a **sidecar**: a separate process from the main bot (`python -m beta_tools`, code in `src/beta_tools/`), so a crash on one side can't take the other down. Built today:

- **Three puppet bot accounts** (alice, bob, clara) that connect to the dev guild as real bot members and can post as themselves.
- **Webhook ghosts**: a per-channel webhook that posts with any display name and avatar. Today a mod fires it by hand; nothing drives it on a schedule.
- **The ambient sim**: a loop that drives the puppets to post canned filler messages (and replies to each other's recent messages) at a steady cadence.

Designed but **not built**, and described below only so nobody mistakes them for working features: a roster of ~50 webhook and DB-only personas, scenario triggers (`jail-full-cycle`, `ticket-claim`, and the like), the 90-day historical seed, `source`-tag cleanup, and `/beta nuke`.

The whole thing is meant to be **physically incapable of running in prod**: the process refuses to start outside `BOT_ENV=dev`, and the other rails in § Safety rails back that up.

Companion infrastructure to [[dungeon-keeper-test-env-spec]]: same dev guild, same dev database, additive on top.

## Commands

Every command is registered to the dev guild only (the sidecar's guild id is the main config's `guild_id`) and checks, in-app, that the caller holds a role named **Mod** or **Admin**. Anyone else gets the ephemeral "This command is restricted to moderators." The commands are flat hyphenated names, not subcommand groups. Registered by the DK Tools sidecar, not the main bot.

| Command | Permission | Purpose |
|---|---|---|
| `/beta-help` | Mod | Overview embed of every `/beta-*` command |
| `/beta-ambient-start` / `/beta-ambient-stop` / `/beta-ambient-status` | Mod | Start or stop the ambient chat loop; status shows state, posts this session, the last post and the corpus size |
| `/beta-puppets-list` / `/beta-puppets-reload` / `/beta-puppets-reconnect <key>` | Mod | Show the roster and connection state; re-read `beta_puppets.yaml` and reapply names and avatars; reconnect one puppet |
| `/beta-puppets-impersonate <key> <channel> <text>` | Mod | Force one puppet to post a message |
| `/beta-ghosts-impersonate <display_name> <avatar_url> <channel> <text>` | Mod | Post a message through the channel's ghost webhook with a custom name and avatar |

There is no pause, no rate command (the cadence is an env var), no scenario runner, no seed or cleanup command, no `/beta health` and no `/beta nuke`. Invocations are not written to an audit channel or a command-log table.

## Behavior

### The three identity classes

The sidecar uses three different kinds of fake "users" depending on what a test needs:

- **Puppets (3 real bot members)**: real Discord bot accounts (alice, bob, clara) that the sidecar drives via the gateway. They post messages and appear in autocompletes, which exercises every code path that needs an actual `Member` object. Each persona in `src/beta_tools/beta_puppets.yaml` has a display name, avatar, activity weight, channel affinities, a `voice_likely` flag and a message-length bias. The `voice_likely` flag is loaded but nothing reads it yet: puppets don't join voice, react or get jailed on their own.
- **Webhook ghosts**: Discord webhooks (one per channel, named `dk-tools-ghost`, created on first use and reused after) posting with whatever name and avatar the caller supplies. They show up as scrollback chatter but **don't appear in the member list, can't react, can't join voice, can't be jailed**. Only `/beta-ghosts-impersonate` uses them.
- **DB-only ghosts**: not built. The design was a roster of ~50 personas pre-populated as backdated DB rows under synthetic IDs in a reserved high range (≥ 9 × 10¹⁸), so they could never collide with real Discord IDs.

### Ambient sim

A background loop in the sidecar drives the three puppets to post chat in the dev guild. It starts automatically once the sidecar is ready (`BETA_AMBIENT_AUTOSTART`, on by default) and a mod can stop and restart it. Cadence: a base interval of 15 s with ±20 % jitter, divided by `BETA_AMBIENT_RATE_MULTIPLIER` (floored at 0.1); after each post a 30-second burst window kicks in where the next interval drops to 5 s. The base interval is not matched to prod traffic.

On each tick the loop picks a puppet by activity weight (never the one that just posted, when it has a choice), picks a channel by that puppet's affinity weights, generates a message in the puppet's length bias, and posts it. Half the time the post is a reply to one of the last ten messages the sim sent in that channel. Message text comes from `SillyTextSource`, a baked-in library of off-the-shelf joke "ipsum" (bacon, corporate, hipster, cupcake, pirate) plus a few lines of generic small talk, assembled to fit the length bias. It needs no database or fixtures, so the sim is always available. Channel affinities reference channel names; a missing channel is skipped with one WARNING log (not spammed). A puppet that isn't ready, or can't see the channel, skips the tick. A Discord rate limit or HTTP error sleeps 10 s, a forbidden channel skips the tick, and any other exception sleeps 30 s; the loop continues either way.

### Scenarios

Not built. The design was one end-to-end trigger per cog or feature (jail lifecycle, ticket claim, starboard hit, welcome-on-join and so on), each logging step by step to a scenario-log channel and reversing itself on cleanup. Flows that ambient chat can't reach are tested by hand with the puppets and ghost impersonation.

### Seed

Not built. The design was a one-shot `/beta seed run` that backdates 90 days of activity so leaderboards and dashboards have depth on day one. Message text is no longer stored by the bot (message storage was removed 2026-09-14), so a seed would write metadata only, and the old `--with-content` idea of filler messages in a message archive no longer has anywhere to land.

### Source tagging and cleanup

Not built on the sidecar side. Migration 007 added a `source` column to `messages` and `member_xp` for it, but nothing in `src/beta_tools/` writes a `source` tag today, and there are no cleanup commands. The planned tags were `beta_seed`, `beta_sim` and `beta_scenario_<name>`, with cleanup operating on those tags only. If it is built, the rule stands: every sidecar write carries a tag so cleanup is reversible.

### Safety rails

Layered so accidental prod activation is effectively impossible (`src/beta_tools/safety.py`, `db_gate.py`):

1. **Process refuses to start** unless `BOT_ENV=dev`, the database path contains `dev`, `BETA_TOOLS_ENABLED=1`, and the tools bot's expected id differs from `EXPECTED_BOT_ID_PROD`. It fails loud and exits before any handler registers.
2. **Tools bot checks itself and leaves non-test guilds.** On ready it compares its own bot user id with `EXPECTED_BOT_ID_TOOLS` and exits on a mismatch. It leaves any guild other than the configured dev guild, both at startup and on a join event.
3. **Puppets validate themselves.** Each puppet checks its own bot user id against its expected id and leaves any guild other than the dev guild.
4. **DB writes are gated.** `beta_write` raises outside dev. Nothing calls it yet, because the sidecar doesn't write to the database today.

Mandatory source tagging was meant to be a fifth rail; it is not built (see above).

## Permissions

- All commands: a role named **Mod** or **Admin** in the dev guild (checked by role name, not id).
- The sidecar bot itself needs **Send Messages** and **Manage Webhooks** (for ghosts) in the dev guild. It does **not** need Manage Server.
- Puppet bots need only the standard message and voice gateway intents (members and voice states on, message content off).

## User-visible errors

The "user" here is a mod tester running `/beta-*` commands in the dev guild.

| When | The tester sees |
|---|---|
| Caller has neither the Mod nor the Admin role | "This command is restricted to moderators." |
| `/beta-ambient-start` while already running | "Ambient sim is already running." |
| `/beta-ambient-stop` while not running | "Ambient sim is not running." |
| A puppet or ghost command before startup finishes | "Puppet manager not initialized yet." or "Webhook fleet not initialized yet." |
| Unknown puppet key | "Unknown puppet key '{key}'." |
| Puppet not connected, or can't see the target channel | "Puppet `{key}` is not connected." or "Puppet `{key}` cannot see channel {channel}." |
| `/beta-puppets-reload` when the file's persona count differs from the connected puppets | "Reload failed: fixture has N personas but M puppets are connected." |
| Ambient sim hits a channel with missing send perms | Logged WARNING; the tick is skipped; tester sees nothing |
| Discord rate-limits the sim | Logged WARNING; loop sleeps 10 s and continues |

## Non-goals

- **Time-travel / global clock fudging.** Tests that depend on time mutate specific DB rows instead.
- **Audio playback by puppets.** They don't join voice at all today.
- **Full member-list parity with prod.** Three puppets plus real mod testers. No hundreds-of-avatars illusion.
- **Performance / load testing.** Cadence is a gentle drip, not a stress test.
- **Replacing the main test-env story** in [[dungeon-keeper-test-env-spec]]. This is additive.
- **Running in prod.** The safety rails reject any attempt.

## Configuration

| Env var | Purpose |
|---|---|
| `BOT_ENV=dev` | Required. The sidecar refuses to start if missing or set to anything else. |
| `BETA_TOOLS_ENABLED` | Must be `1` to launch. Defaults to `0` in `.env.example`. |
| `DISCORD_TOKEN_TOOLS`, `EXPECTED_BOT_ID_TOOLS` | The sidecar bot's token and the id it must connect as. |
| `BETA_PUPPET_TOKEN_1` to `_3`, `EXPECTED_BOT_ID_PUPPET_1` to `_3` | The three puppet bots' tokens and expected ids, in roster order. |
| `BETA_AMBIENT_AUTOSTART` | Defaults to on: the ambient sim's `start()` is called when the sidecar finishes initializing. Set to anything other than `1` to start it by hand with `/beta-ambient-start`. |
| `BETA_AMBIENT_RATE_MULTIPLIER` | Scales the base interval (default 1.0). Setting it to 2.0 doubles the posting rate. Read once at startup; there is no runtime override. |
| `BETA_LLM_BLEND` | Reserved for a future phase that blends LLM-generated text with the filler text. No-op today. |

The puppet roster is one fixture, `src/beta_tools/beta_puppets.yaml`: three entries (alice, bob, clara) with display name, avatar URL, activity weight, channel-affinity weights, voice tendency, and a short/medium/long message-length bias. The shipped avatar URLs are placeholders to replace once the puppet apps are registered. `/beta-puppets-reload` re-reads it.

Ambient text needs no fixture: `SillyTextSource` carries its corpus in code. There was once a Markov corpus built from real server messages; it was deleted and purged from git history (2026-09-13) because it was member data. Never build sim text from real messages.

## Stored data

Nothing today. The sidecar does not write to the database: puppets and ghosts post to Discord only, and Discord-side state is the only trace. The ghost webhooks (one per channel the sidecar has posted in, named `dk-tools-ghost`) are managed through the Manage Webhooks permission, created idempotently and not tracked in the database.

There is no separate beta-tools schema. Anything the sidecar writes in future goes through the same dev database as the main bot, behind `beta_write`, and nothing it writes can exist in the prod database because the sidecar can't start outside `BOT_ENV=dev`.
