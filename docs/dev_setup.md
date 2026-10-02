# Local development setup (Reference)

Getting a development copy of the bot and its dashboard running on **your own
machine**, against **your own Discord bot and a private test server**, from an
**empty database**. Nothing here needs access to the production host, its
database, or anyone's data.

This is the developer's counterpart to [deployment.md](deployment.md), which
installs the bot as a service for a real server. `dev_sessions.md` and
`dev_remote_testing.md` describe the production host's own tooling (tmux
sessions, a remote test runner) and do not apply to you.

Commands are written for Linux; a **Windows** column follows wherever it
differs. macOS is untested.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.14** | The only supported interpreter (`.python-version`, `requires-python`). Production runs 3.14.6. |
| **git** | Any recent version. |
| **A Discord account** | To register a bot application and create a test server (§ 3). |
| **Node 20** | *Optional.* Only for `eslint`/`stylelint` when you touch the dashboard's JS or CSS (§ 8). |
| **Java 17+** | *Optional.* Only for the music cog's Lavalink server (§ 9). |
| **A build toolchain** | *Fallback only.* Every dependency has a binary wheel for Python 3.14 on Linux x86-64 and Windows; `build-essential` (or Visual Studio Build Tools) is needed only if a wheel is missing for your platform. |

## 2. Clone, virtualenv, install

```bash
git clone <repo-url> dungeon-keeper
cd dungeon-keeper

python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# The dev lock pins the test/lint toolchain AND every runtime dependency,
# so this one install is the whole development environment.
pip install -r requirements/dev.lock
pip install --no-deps -e .
```

| | Windows |
|---|---|
| Activate | `.\.venv\Scripts\activate` |
| Interpreter | `py -3.14 -m venv .venv` if `python3.14` is not on PATH |

**Install from the lock, not the package.** `pyproject.toml` holds loose
floors; `requirements/dev.lock` is what CI, the production host and every
other developer actually run. `pip install -e .` on its own would resolve
whatever is newest that day and quietly diverge; `dev_remote_testing.md`
records a security test that passed vacuously for exactly that reason.

The dev lock is large (machine-learning wheels for the image and voice
features are runtime deps): a fresh install measured **4½ minutes and a
1.2 GB `.venv`** on Linux (2026-09-30). Nothing downloads model weights
until a feature that needs them runs.

## 3. Your own bot and test server

You need a Discord application whose token is yours and a server nobody else
uses. **Never put the production token in a development `.env`.**

1. [Discord Developer Portal](https://discord.com/developers/applications) →
   **New Application** → **Bot** tab → copy the **token**.
2. Under **Privileged Gateway Intents** enable all three: **Presence**,
   **Server Members**, **Message Content**. The bot requests them at connect
   and fails without them.
3. Note the bot's **user id** (Bot tab → the numeric id under the username,
   or copy it from the invite URL's `client_id`). You will set it as
   `EXPECTED_BOT_ID_DEV`.
4. Create a **new server** in Discord for testing. Turn on **Developer Mode**
   (User Settings → Advanced), right-click the server → **Copy Server ID**.
5. Invite the bot: **OAuth2 → URL Generator**, scopes `bot` +
   `applications.commands`, and the permission set listed in
   [deployment.md § 4](deployment.md#4-create-the-discord-application).
   Administrator also works on a private test server. Open the URL and add
   the bot to your test server.

A **dev bot is single-guild by design**: at startup it checks it is in the
configured server and, if it finds itself in any *other* server, leaves it
and exits (`core/safety.check_guild_membership`). So invite it to exactly one.

## 4. `.env`

```bash
cp .env.example .env
```

The minimum for development; everything else in the template stays
commented out:

```ini
BOT_ENV=dev

DISCORD_TOKEN_DEV=your_dev_bot_token
GUILD_ID_DEV=123456789012345678       # your test server's id
DB_PATH_DEV=dk_dev.db                 # the path MUST contain "dev"
AUDIT_CHANNEL_DEV=                    # optional: a channel id for the startup banner
EXPECTED_BOT_ID_DEV=your_bot_user_id  # recommended: refuses to start with the wrong token
```

Three startup rails guard the env/DB pairing, and a development machine
trips them in ordinary ways, so know what they say:

- **`BOT_ENV=dev` requires a DB path containing `dev`** (and `prod` forbids
  one). `dk_dev.db` passes; `bot.db` refuses to start.
- **`EXPECTED_BOT_ID_DEV`** makes the bot exit if the token belongs to a
  different application.
- **Guild membership**: not in `GUILD_ID_DEV` → exits with the id it
  expected; in an extra server → leaves it and exits.

`RESET_DEV_DB` and `SEED_DEV_FIXTURES` in the template belong to the
production host's prod-clone test guild: the first refreshes a dev DB from a
production DB you do not have, and the second is wired through the config
loader but has no loader behind it (`test_env_spec.md`
§ Fixtures). Leave both at `0`.

`.env` is gitignored and must stay that way; the tests scrub it out of the
environment for every test (`web_testing.md` § Environment hermeticity), so
its presence changes nothing about the suite.

## 5. First run

```bash
python -m dungeonkeeper --debug
```

- Migrations apply automatically on every start, so there is no migrate
  step. An empty `dk_dev.db` becomes the full schema (about 270
  migrations) in a few seconds.
- **`--debug` syncs slash commands to your test server only**, so they
  appear within seconds. Without it the sync is global and Discord takes up
  to an hour to show new commands. Always use `--debug` in development.
- The startup banner prints `env=DEV`, the bot user, the guild and the DB
  path. Production's banner is red; yours is plain.
- Expect one warning: `prod_snapshot.json not found … skipping ID remap`.
  The remapper translates production channel/role ids into a clone guild's;
  with no snapshot and no stored prod ids there is nothing to map.

Stop with `Ctrl+C`. To start over, delete `dk_dev.db` (and the `-wal` /
`-shm` files beside it).

### What an empty server can and cannot exercise

Almost every feature is configured **from the dashboard**, not from Discord
(`CLAUDE.md` § Design philosophy), and most of them do nothing until a
channel, role or dial is set. So the sequence for trying a feature is:
start the dashboard (§ 6), open the feature's page, point it at channels
and roles you create in your test server, then use it in Discord. The
**Configuration Problems** card on the dashboard's home page lists what is
still unset.

Some things need more than one account: anything member-to-member (whisper,
DM requests, the no-contact list, party games with a minimum player count).
A second Discord account in your test server is the practical answer;
`beta_tools_spec.md` describes the puppet bots the production host uses, but
they are extra applications to register and are not needed to start.

## 6. The dashboard

Add to `.env`:

```ini
DASHBOARD_ENABLED=1
DASHBOARD_OPEN_AUTH=1        # no login; loopback bind is forced
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8080
```

Restart the bot and open <http://127.0.0.1:8080>. The dashboard runs inside
the bot process (there is no separate server to start) and in open-auth
mode you are a full admin of your test server with no login.

**Auth fails closed**: without `DASHBOARD_OPEN_AUTH=1` *and* without the
Discord OAuth block, the dashboard refuses to start rather than guessing.
Open-auth mode must never be reachable from outside your machine; it is
forced to loopback for that reason. The OAuth path (`DISCORD_CLIENT_ID`,
`DISCORD_CLIENT_SECRET`, `SESSION_SECRET`, `DASHBOARD_BASE_URL`) is what
production uses and is only worth setting up locally when you are working
on login itself; [deployment.md § 9](deployment.md#9-optional--web-dashboard)
covers it.

The user-facing manual the dashboard's **Help** panel shows is
`src/web_server/static/manual.html`; the panels are vanilla-JS modules under
`static/js/panels/`.

## 7. Running the tests

```bash
pytest                       # the full suite, in parallel (-n auto is in addopts)
pytest tests/economy         # one feature's folder — tests mirror src/bot_modules/
pytest -n 0 -k test_name     # single-process, for a debugger or pdb
ruff check .                 # the linter
pyright                      # the type check (slow: minutes, ~1.5 GB)
```

- Roughly 6,000 tests in ~770 files; a full run is **10–15 minutes** on a
  laptop, well under that on a fast desktop. Test databases are template
  copies deleted per test, so the tmp footprint stays flat.
- The suite needs **no secrets, no database and no network**. CI sets
  `BOT_ENV=dev`, `DISCORD_TOKEN_DEV=fake-token`, `GUILD_ID_DEV=9001`,
  `DB_PATH_DEV=dk_dev.db`, `AUDIT_CHANNEL_DEV=0`; a fresh clone with the
  `.env` from § 4 gets the same result.
- Browser tests (`-m browser`) are excluded by default and skip themselves
  unless Playwright's Chromium is installed
  (`python -m playwright install chromium`). They are CI's job; run them
  locally only when you change a dashboard panel (`web_testing.md`).

| | Windows |
|---|---|
| Portability | The suite runs natively; the production host dispatches it to a Windows runner every commit (`dev_remote_testing.md` § Why native Windows is fine). |
| Encoding | Any file read in a test must pass `encoding="utf-8"`; the locale default is cp1252 and the repo is full of em-dashes. `tests/tooling/test_encoding_portability.py` fails an omission before it reaches you. |
| Open files | Windows cannot delete an open file, so a test that leaves a SQLite connection open fails at teardown, not in its body. |

## 8. The gate and the hooks

`scripts/gate.py` is what the pre-commit hook runs and what CI mirrors:

```bash
python scripts/gate.py --scoped   # ruff + the tests mapped to your changes (the hook)
python scripts/gate.py --quick    # ruff + pyright + browser panel checks — before a push
python scripts/gate.py            # ruff + the full suite
```

`--scoped` maps each changed file to its tests by filename token, so a
change in `economy/logic.py` runs `tests/economy/`. A **new** logic-layer
file (`logic.py`, `store.py`, `service.py`, `*_logic.py`, `*_service.py`)
with no test that the mapping can find is a hard failure, so write the test
first (design_guide.md Part 3).

Install the hooks once per clone:

```bash
pre-commit install                          # the gate, on every commit
pre-commit install --hook-type commit-msg   # refuses attribution trailers
```

`git commit --no-verify` skips the gate in a pinch; CI runs everything on
the pull request regardless. The remote-runner dispatch in `gate.py` is
inert unless `REMOTE_TEST_HOST` is set; you run locally.
`scripts/hooks/install.sh` installs the *QA-card* hooks, which post to the
production Discord and exit silently without a production token; you do not
need them.

### Dashboard JS and CSS

```bash
npm install --no-save                                   # once per clone
npx eslint src/web_server/static
npx stylelint "src/web_server/static/**/*.css"
```

Both are blocking CI jobs. `package.json` carries the eslint rules that
encode dashboard conventions (`fetch` only through `api.js`, `confirmDialog`
not `window.confirm`), so a rule failure is a convention you have not met
yet, not a style nit.

## 9. Optional stacks

None of these is needed for ordinary feature work; each is off until its
`.env` block is filled in.

| Stack | Needed for | Where it is documented |
|---|---|---|
| **Lavalink + Spotify** (Java) | The music cog | [deployment.md § 8](deployment.md#8-optional--music-lavalink--spotify); `python scripts/setup_lavalink.py` downloads the server |
| **Anthropic API key** (`ANTHROPIC_API_KEY`) | The dashboard Help ask box / `/ask`, LegitLibs AI prep, generated copy | `games/utils/ai_client.py`; the client returns `None` on any API error and callers degrade, so without a key those features are simply unavailable |
| **Local LLM** (`llama-cpp-python`, the `local-llm` dependency group) | The `/watch` relay filter and the wellness note | [deployment.md § 10](deployment.md#10-optional--llm-backend-lan-gpu-server); the only dependency that builds from source |
| **Playwright Chromium** | The browser test suite | `web_testing.md` |

## 10. Troubleshooting

| Symptom | Cause |
|---|---|
| `SAFETY: env=dev but db_path=… does not contain 'dev'` | Rename the DB file; the rail is deliberate. |
| `SAFETY: bot is not in configured guild …` | `GUILD_ID_DEV` is not the server you invited the bot to, or the invite failed. |
| `SAFETY: dev bot is in unexpected guild … leaving` | The bot was invited to a second server. It has left it; start again. |
| Slash commands do not appear | You started without `--debug` (global sync, up to an hour), or the bot lacks the `applications.commands` scope; re-invite. |
| Dashboard refuses to start, names `DASHBOARD_OPEN_AUTH` | Auth fails closed; set `DASHBOARD_OPEN_AUTH=1` for local work. |
| `UnicodeDecodeError: 'charmap'` in a test | A read without `encoding="utf-8"`, on Windows. |
| Tests pass locally, fail in CI, or the reverse | Your venv drifted from the lock. `pip install -r requirements/dev.lock` again. |
| The gate says a new file is **unmapped** | A logic-layer file with no test named after it. Add `tests/<feature>/test_<name>.py`. |
| `git commit` fails with ``Executable `python` not found`` | The hook runs `python scripts/gate.py`; activate the venv in the shell you commit from (or in your editor's git integration). |
| `Your pre-commit configuration is unstaged` | You edited `.pre-commit-config.yaml`; stage it (`git add`) before committing anything. |

---

Next: [`CONTRIBUTING.md`](../CONTRIBUTING.md) for how a change lands, then
[`design_guide.md`](design_guide.md) before building a feature, and
[`codebase_tour.md`](codebase_tour.md) for where things are.
