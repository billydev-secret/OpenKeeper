# Changelog

Release notes for the public mirror, newest first. `scripts/publish_release.py
prepare vX.Y.Z` retitles the **Unreleased** section with the version and date
and leaves a fresh, empty one above it; the section becomes the public
commit's message and the tag's notes, so write it for someone reading the
mirror, not for us. An empty Unreleased section blocks the next release until
it has notes. See `docs/publishing.md`.

## Unreleased

## v1.0.0 (2026-10-01)

First public release of Dungeon Keeper: a Discord bot for communities that
have outgrown a handful of utilities, with its configuration on a web
dashboard rather than in slash commands.

**What an admin gets**
- Moderation with a memory: jail with role restore, warnings, tickets and a
  per-member profile; an opt-in watch list and an on-demand AI reviewer that
  flag rather than act.
- Member safety between members: a no-contact list enforced on every surface
  that can carry a message between two people, age-gated content keyed off
  Discord's own channel gate, and message content not stored by default.
- XP, levels and analytics: retention, activity and churn reports, cached on
  the dashboard.
- Voice rooms with an owner panel, and music through Lavalink.
- A coin economy with a perk shop, bounties, raffles and a casino, every
  payout behind a dial.
- A games suite: party games, head-to-head and elimination duels, Guess Who,
  Meadow Mahjong, Survivor pools, with lobbies that recover across restarts.
- Engagement tooling: question of the day, chat revive, flash themes, pen
  pals, quote cards and banners drawn by the bot.
- Onboarding with a two-stage intake, a wellness check-in module, and
  background services for backups, inactivity pruning and departure cleanup.
- Over 130 dashboard panels, a manual inside the dashboard, and `/mydata`
  for member self-service export and erasure.

**What a developer gets**
- 276 schema migrations with a hash manifest, a data register that every
  per-member table must appear in, and a privacy coverage sweep.
- A test suite of about 25,000 tests at the logic layer, ruff and pyright
  clean, a browser sweep over every panel, and a pre-commit gate that maps
  the staged diff to its tests.
- Specs under `docs/specs/`, classified by how far they match the code.

**Not in this repository**
- The maintainers' server art, fonts and tile images (`assets/`), their
  deployment and session tooling, and their MCP server — see
  `docs/deployment.md` § Assets you supply.
