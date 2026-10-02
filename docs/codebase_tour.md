# Codebase tour (Reference)

Where things are and how a request moves through them. A map with pointers:
every section names the module or document that owns the detail, and that
owner wins on any disagreement. `design_guide.md` says how to build; this
says where you are.

Layout as of 2026-10-01, after the per-feature relayout
(`scripts/relayout.py`). An older branch or note that cites
`bot_modules/services/…` or `bot_modules/cogs/…` predates it;
`dev_sessions.md` § A branch that predates a layout move is the catch-up.

---

## The repository

```
src/
  dungeonkeeper/    the bootstrap: python -m dungeonkeeper
  bot_modules/      the bot — one package per feature, plus four shared homes
  web_server/       the dashboard: FastAPI routes + vanilla-JS panels
  migrations/       NNN_description.sql, applied in order at every start
  beta_tools/       dev-only puppet bots for synthetic activity (beta_tools_spec.md)
  dk_mcp/           a read-only MCP server over docs/ and src/ (dk_mcp_server.md)
tests/              mirrors src/bot_modules/ folder for folder, plus tests/web/
scripts/            gate.py and the maintainer tooling; every script has a docstring
docs/               specs (INDEX.md classifies each), runbooks, reviews
requirements/       the compiled locks pip installs from (pyproject.toml holds the floors)
assets/             logo, panel art, tile emoji; fonts/ is licensed and gitignored
deploy/             the systemd units the production host runs
lavalink/           the music server's config (the jar is downloaded, not committed)
var/                runtime data the bot writes — DB snapshots, model weights, uploads; gitignored
                    (absent until the operator gathers the old root-level dirs into it: core/paths.py)
.claude/            skills and agents for AI coding sessions; not needed by a person
.github/            CI: ruff, pyright, the suite and the browser sweep on every PR
```

## The process

One Python process: the Discord client, the background loops and (when
enabled) the dashboard all share one event loop.

`src/dungeonkeeper/__main__.py`, top to bottom:

1. **Config**: `core/config.load_config` reads `BOT_ENV` and the matching
   `DISCORD_TOKEN_*` / `GUILD_ID_*` / `DB_PATH_*` triple; `core/safety`
   refuses a dev env with a non-dev DB path (`dev_setup.md` § 4).
2. **Migrations**: `migrations.apply_migrations_sync(db_path)`. Numbered
   SQL files, each idempotent, tracked in `schema_version`. A shipped
   migration is immutable: `tests/migrations/` hashes them
   (`scripts/migration_manifest.py`) and fails an edit.
3. **`AppContext`** (`core/app_context.py`): the object every cog and route
   reaches for: `ctx.open_db()` for a SQLite connection, `ctx.guild_config(
   guild_id)` for the cached per-guild settings snapshot, `ctx.member_is_mod`,
   the home `guild_id`. A cog holds `self.bot` and reaches it as
   `self.bot.ctx`; the dashboard gets the same instance.
4. **Extensions**: an explicit list, `bot.extension_names`, of every cog
   module to load. `core/extension_discovery` finds cogs on disk by what
   they define (`async def setup(`) and the boot-time drift warning names
   any the list forgot.
5. **`on_ready` rails**: token identity, guild membership, and in dev the
   ID remapper (`core/id_remap`, a no-op without a prod snapshot).
6. **Persistent views**: buttons that must survive a restart are
   registered here with `add_dynamic_items` / `add_view`.
7. **Background loops**: `core/background.run_forever` and `spawn`
   (`common_modules.md` § Embeds, DMs and panels) for the sweeps, the
   sticky-panel machinery, game crash recovery and the report-cache warmer.
8. **Dashboard**: with `DASHBOARD_ENABLED=1`, `web_server.server.
   serve_forever` is appended to the startup tasks and uvicorn runs in
   the same loop.

Runtime configuration lives in the database (`config` and `config_ids`
tables, per guild), set from the dashboard. `.env` holds only what must
exist before the DB does: tokens, ids, paths, the optional stacks
(`deployment.md` § Bootstrap config keys).

## A feature package

`src/bot_modules/<feature>/`, one per dashboard-visible feature. Fifty-odd of
them; a typical one:

```
birthday/
  cog.py        glue: slash commands, listeners, resolves Discord objects, calls one function
  service.py    the behaviour — takes a conn or a db_path, returns plain data
moderation/jail/
  cog.py, jail_commands.py      glue
  logic.py, policy_ballot_service.py   behaviour
  apply.py                      the one write path other features call
  embeds.py                     pure build_* functions: dicts in, discord.Embed out
economy/
  cog/          a package when the cog outgrows a file
  *_service.py, submission_store.py, logic.py
  *_layouts.py, *_views.py      Components V2 cards and their views
```

Sibling features share a parent the way the games do (`games/<game>/`):
`moderation/` holds the `jail`, `watch`, `tickets` and `settings` packages
beside its own files; `onboarding/` holds `welcome`, `intake` and
`greeting_watch`; `roles/` holds `menus`, `grant` and `bot_roles`; `dm/`
holds `perms` and `batch`; `voice/` holds `master` and `transcription`;
`music/` holds `playlist`. A child that repeated its parent's name dropped
the prefix (`role_grant` became `roles/grant`); the tests sit in
`tests/<parent>/`. The dashboard route id and the feature's spec keep the
old name. (Written without trailing slashes on purpose: the layout codemod
rewrites every old `<package>/` path it finds, prose included.)

**The layer is decided by the file's name, not its folder**
(`design_guide.md` § Layering, enforced by
`tests/contracts/test_import_layering.py`):

| Layer | Files | May import |
|---|---|---|
| Glue | a cog (`cog.py`, `cog_*.py`, anything under `cog/`), a command module (`*_commands.py`) | anything |
| Presentation | `views`, `embeds`, `layouts`, `modals`, `panel(s)`, bare or as a `_`-joined part of the name | glue and logic |
| Logic | every other file: `logic.py`, `service.py`, `store.py`, `*_service.py`, `*_logic.py` … | logic only: never a cog |

The gate refuses a **new** logic-layer file with no test mapped to it, and
maps files to tests by filename token: `economy/bounty_service.py` runs
`tests/economy/test_bounty*.py` (`scripts/gate.py` § scoping heuristic).

Four packages are shared homes rather than features:

| Package | Holds |
|---|---|
| `core/` | Infrastructure everything imports: `app_context`, `db_utils` (`open_db`, `sql_identifier`), `config`, `safety`, `settings_registry`, `branding`, `sticky`, `background`, `utils`, `layout`, `role_safety`, `xp_system` |
| `shared/` | Helpers several features use: `embeds` (accent, `take_to_fit`), `replies`, `dm_branding`, `name_resolver`, `age_gate`, `anon_audit_service`, `quote_renderer` |
| `panels/` | The posted-panel machinery: the registry, refresh service and the cog that redraws every panel on boot and on an art change |
| `no_contact/` | The no-contact list, read by every member-to-member surface (`no_contact_spec.md`) |

Before writing a helper, check `common_modules.md`: it is the list of
seams a new feature reaches for by mistake when it doesn't know they exist.

`games/` is the largest feature: the party-game suite, one sub-package per
game (`games/wyr/`, `games/ama/`, …) over shared machinery in
`games/utils/` (`games_system_spec.md`). `duels/` is the head-to-head suite
on a template-method base class (`dk_pvp_games_suite_spec.md`).

## The dashboard

`src/web_server/`:

```
server.py            the FastAPI app factory: auth backend, security headers,
                     the routers, static files, uvicorn
auth.py, deps.py     Discord OAuth or open-auth; require_perms and the
                     game-host / economy-manager / survivor-host tiers
routes/              ~40 route modules, one per feature area, all under /api
schemas.py           shared Pydantic models; a request body — here or beside
                     its route — carries extra="forbid", never a loose dict
helpers.py           require_in_range, member_name, refuse_ungrantable_role
static/
  index.html + js/   the single-page app: panels/ (~140 modules), one per
                     dashboard page, over api.js, config-helpers.js, ui.js,
                     states.js, report-helpers.js, table.js, charts.js
  manual.html        the user-facing manual the Help panel renders — it is a
                     doc surface, updated in the same commit as a UI change
  tokens.css, app.css   the visual language (dashboard_visual_language.md)
```

Where a setting's page lives, the frozen route ids and the nav taxonomy:
`dashboard_ia.md`. The shared JS modules and what each owns:
`common_modules.md` § Dashboard. The eslint rules in `package.json` encode
the conventions (`fetch` only through `api.js`, `confirmDialog` not
`window.confirm`, forms through `guardedSubmit`).

A route reads through `ctx.open_db()` and writes settings through the same
`set_config_value` the bot reads, then `ctx.invalidate_guild_config` so the
bot's cached snapshot follows (`tests/contracts/` has the guard).

## The tests

`tests/` mirrors `src/bot_modules/` folder for folder, with:

| Path | What |
|---|---|
| `conftest.py` | The autouse fixtures, notably `_hermetic_env`, which scrubs your `.env` out of every test (`web_testing.md` § Environment hermeticity) |
| `db_template.py` | `migrated_db()`: one migrated template per worker, copied per test, which is why the suite's tmp footprint stays flat |
| `fakes.py` | Typed fakes for Discord objects (`FakeRole`, `FakeMember`, …). Use these, not ad-hoc mocks |
| `contracts/` | Repo-wide guards: import layering, embed accent, mention defaults, role-grant safety, view timeouts, docs-as-data. Most "convention" rules live here as tests |
| `web/` | The dashboard: the authz sweep at every permission tier, the snowflake-precision sweep, and the browser suite (`-m browser`, off by default) |
| `migrations/`, `tooling/`, `dk_mcp/`, `docs/` | The runner, the scripts, the MCP server, the doc pipeline |

Tests are written at the logic layer: a bug seen in Discord gets its
failing test at the function that broke, not a re-enactment through
Discord mocks (`design_guide.md` Part 3).

## Scripts

`scripts/gate.py` is the one you run. Of the rest, `remote_test.py`,
`dk_session.py`, `ci_watch.py`, `relayout.py`, `relayout_docs.py` and `hooks/` are the
production host's tooling (`dev_sessions.md`, `dev_remote_testing.md`);
`export_user_data.py`, `anonymize_user_data.py`, `backup_to_nas.sh`,
`watchdog.py`, `refresh_dev_db.py` and `deploy_dk_mcp.sh` are operator
scripts that need production credentials (`gdpr_runbook.md`,
`disaster_recovery_runbook.md`); `migration_dryrun.py`,
`migration_manifest.py` and `privacy_coverage.py` are the schema and
register checks the tests wrap. Each script's docstring says which.
`scripts/archive/` holds one-shot backfills and migrations that have already
run against prod, kept as a record and never run again (its README has the
convention). `scripts/systemd/` holds the per-user units for dev-session
snapshot and restore; the system units root installs on the prod host live
in `deploy/`, whose README explains the split.

## Docs

`docs/INDEX.md` first. It classifies every spec, and an Aspirational one
describes things nobody built. Feature specs sit in `docs/specs/`, one file
per feature; everything at `docs/` top level is a how-we-work document (the
design guide, the dev and ops docs, the style guides, the data register).
Then:

| Question | Document |
|---|---|
| How do I build a feature here, and what must be true before I commit? | `design_guide.md` (Part 4 is the checklist) |
| Does something already own this? | `common_modules.md` |
| What does the bot do, feature by feature? | `features.md`; `/help` and `manual.html` are the command reference |
| How should this card / copy look? | `embed_style_guide.md`, `components_v2_style_guide.md` |
| Which dashboard page, which id? | `dashboard_ia.md` |
| What data may it store, and who can erase it? | `data_register.md`, `privacy_spec.md` |
| Who must it never put in contact? | `no_contact_spec.md` |
| How do the dashboard tests work? | `web_testing.md` |
