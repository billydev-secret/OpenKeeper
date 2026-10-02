# Dungeon Keeper — working agreement

Discord bot (`src/bot_modules/`: thin cogs, logic in per-feature modules) +
FastAPI dashboard (`src/web_server/`: routes + vanilla-JS panels in `static/js/`),
SQLite-backed. Tests in `tests/`.

This file is the terse, always-loaded statement of the rules. The reasons,
the history and the detail live in the documents each rule points at —
**`docs/design_guide.md` is the entry point**, with the pre-commit
obligations checklist assembled in its Part 4.

## Design philosophy

- **Configuration lives on the web dashboard, not Discord.** Every feature's
  admin settings get an admin-gated panel in `src/web_server/`, filed under the
  right nav heading. No slash commands, modals or button flows for admin
  config; a feature that shipped command-managed moves its knobs to the web and
  **deletes** the commands. If a feature genuinely seems to need in-Discord
  admin config, ask instead of building it.
- **Discord is for member self-service and mod actions.** Prefer one ephemeral
  panel with buttons/modals over a sprawl of subcommands.
- **Collapse controls.** One dial with a few states beats several overlapping
  toggles (Voice Control's access dial). Reorganize a jumbled page rather than
  appending to it.
- **Safety & privacy defaults:** NSFW gates on `is_age_gated_channel()`, never
  a bot-side toggle. Store minimal data — message content is off by default.
  Sensitive access is opt-in. Never ship a preference or toggle that isn't
  enforced, including a config value staged into prod ahead of the branch that
  reads it (allowed only if that commit names the branch).
- **Member-facing erasure is one panel, `/mydata`, never a per-feature
  command** — an `ErasableStore` in `privacy.logic.MEMBER_ERASABLE_STORES`,
  deletes in a service taking a `conn` so `purge_user_data` runs the same
  function. A feature *off switch* that deletes a row stays with its feature.
  `tests/privacy/test_privacy_member_data_panel.py` enforces it; the why is bug #222
  (design_guide.md Part 1 § 5).
- **Any surface that puts two members in contact consults the no-contact
  list** (`is_no_contact_conn` / `no_contact_partners_conn`), refusing in a way
  the blocked party can't tell from an ordinary outcome. Prefer suppressing the
  delivery to refusing the action. See `docs/specs/no_contact_spec.md`.

## Docs

- Feature specs live in `docs/specs/`, one per feature; the how-we-work
  documents sit at `docs/` top level. Read `docs/INDEX.md` **first** — it
  classifies every spec as Reference / Design / Aspirational. When a spec and the code
  disagree, the code wins.
- **Behavior change ⇒ update the matching spec** (and its INDEX.md
  classification) **in the same commit**.
- **UI/UX change ⇒ also update `src/web_server/static/manual.html`** (the
  dashboard Help panel's guide) in the same commit. It drifts independently of
  `docs/`.
- **No real member names, handles, quotes or server/user/channel/message ids**
  in docs, tests, fixtures or comments — synthetic ids and labelled
  placeholders; `tests/tooling/test_no_real_identifiers.py` enforces the ids
  (design_guide.md Part 4 § The docs contract).
- **Per-user data ⇒ `docs/data_register.md` in the same commit** — a new table
  gets a row saying whether `purge_user_data` clears it or on what Art 17(3)
  ground it is kept; a changed retention, dropped column or `purge_user_data`
  change updates the register cell, `manual.html` § Your Data & Privacy, and
  `privacy/disclosure_copy.py`. The coverage tests, the unconventional-column
  and `guild_purge_service` rules, and the manual `privacy-tools` doc surface
  are in design_guide.md Part 1 § 5 and Part 4 § The docs contract.
- README.md is a landing page, not a reference: touch it only when a whole
  feature area arrives or leaves.

## Workflow

- Do edits in a git worktree; merge back to main when ready for user testing.
  `/dk-feature` creates a session, `/dk-ship` reviews, merges and tears it
  down (`docs/dev_sessions.md`). That is this host's flow; a developer on
  their own machine follows `CONTRIBUTING.md` (own test server, local gate,
  a pull request) and `docs/dev_setup.md`.
- **This checkout is production.** Never restart the bot or dashboard
  (`sudo systemctl restart dungeon-keeper`) unasked, and the same for every
  other live switch: a Sync/Post press that publishes to members, a write to
  the prod `config` or `todos` tables, a backfill over live data. Prepare it,
  say exactly what pressing it does, and stop.
- **Ship it dark.** Anything with a live blast radius lands inert — a dial
  defaulting off, NULL meaning "not configured". The commit body says what the
  feature does the instant it is live (design_guide.md Part 5).
- Large tasks get a plan doc in `docs/plans/`, which is **gitignored**: a plan
  is local scaffolding, and anything that must outlive the build goes into the
  feature's spec. Plans committed before 2026-09-29 are recoverable with
  `git show 678a16fa:docs/plans/<name>.md`, for code comments that still cite them.
- When touching a module with open findings in `docs/reviews/`, mention them
  and offer to fold fixes in — don't expand scope uninvited.
- **The public GitHub repo is a release mirror, not a remote of this one.**
  `scripts/publish_release.py prepare` / `publish` snapshot `main` as one
  commit per release on the `public` branch (`docs/publishing.md`); the push
  is Ben's press. Release notes accrue in `CHANGELOG.md` § Unreleased —
  a user-facing change adds a line there. Tracked files that must stay
  private go in `.publishignore`.

## Regression tests (ship with the feature, not after)

- **Every new feature and every bug fix lands with tests in the same commit**,
  at the logic/service layer (`*_logic.py` / `*_service.py`). Cogs, views and
  embeds are glue and aren't re-tested against Discord mocks — a bug seen in
  Discord gets its failing test at the layer that broke.
- **Cover** the happy path and **every guard/branch**, especially safety gates
  (age gate, opt-in, role gates); for a bug fix, a test that fails before the
  fix.
- **A deliberate anomaly ships with a test that fails if it's reverted**, plus
  a line in its spec saying why.
- Prefer a `pytest.param` row over a new test function, and a shared contract
  table (`tests/contracts/test_embed_accent_contract.py`) over a per-file copy.
- Coverage is judged on the patch (~80% of new logic lines), not the repo.
  Never lower `fail_under`; raise it when a feature adds headroom.

## Gates

- The **pre-commit hook** runs `python scripts/gate.py --scoped`: ruff, then
  the tests mapped to the staged diff. A **new** logic-layer file with no
  mapped test hard-fails it.
- **Pyright and the browser panel sweep are CI's**, not the hook's; locally
  they are opt-in (`--quick` before a push, or `--pyright` / `--browser`).
- In a session worktree a shared-file edit **defers** the full run to main.
  Run `python scripts/gate.py` **on main, solo,** once a batch of merges is
  complete; CI on a push to origin also satisfies full-suite green.
- **`/dk-regress`** is the pre-release check on main. It never restarts
  anything.
- Tiers, timings and the why: `docs/dev_sessions.md` and design_guide.md
  Part 4 § Gates.

## Dependencies

- Direct dependencies are human-edited in `pyproject.toml` (`[project]
  dependencies` + `[dependency-groups]` dev/mcp/local-llm);
  `requirements/*.lock` are compiled pins. After an edit, recompile the lock
  with the command in the `[project]` comment — a runtime dep needs both
  `base.lock` and `dev.lock`.
- Third-party GitHub Actions are pinned to a full commit SHA with a trailing
  `# vX.Y.Z` comment, never a bare tag (`tests/tooling/test_workflow_action_pins.py`).

## Commits

- Subject `Scope: descriptive summary` (~60 chars); prose body: why, edge
  cases, what tests cover it.
- **No** `Co-Authored-By` / `Claude-Session` trailers.
- **User-facing change ⇒ a `Testing:` section** ending the body, as `- [ ]`
  lines **for a volunteer tester**: one action and one observable result per
  box, no code identifiers, verifiable in one sitting. `/dk-ship` teardown
  turns a branch's sections into one QA card (`docs/dev_sessions.md`).

## Conventions

- **Node 20 is dev tooling only**, user-local at `~/.local/lib/node20`. After
  touching dashboard JS/CSS run `npx eslint src/web_server/static` and
  `npx stylelint "src/web_server/static/**/*.css"` (`npm install --no-save`
  once per worktree).
- **A cog holds `self.bot` and reaches the app context as `self.bot.ctx`.**
  Cogs take `(self, bot)` only.
- New embeds take their color from `safe_resolve_accent(source, guild)`; never
  `resolve_accent_color` directly.
- **A member named inside an embed is resolved, never a `<@id>`** — builders
  take a `name_fn` from `shared/name_resolver.build_name_fn`. A no-contact
  pair degrades to `User <id>` on purpose. More in
  `docs/embed_style_guide.md`.
- **Dashboard route ids are the bare feature name** and every existing id is
  frozen (`docs/dashboard_ia.md`).
- **Shared dashboard widgets are safe by default** — `table.js` escapes, config
  panels mount through `mountAsync` (let the loader's rejection reach it),
  report panels through `mountReloadable`, and a guild-scoped cache in
  `config-helpers.js` is cleared in `resetMetaCaches()`. The browser suite,
  authz and snowflake sweeps are in `docs/web_testing.md`.
