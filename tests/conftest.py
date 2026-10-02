"""Shared pytest fixtures for Dungeon Keeper tests (spec §9.5)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# Ensure project root is on sys.path so all project modules are importable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import aiosqlite

from bot_modules.core.config import Config
from bot_modules.core.db_utils import TEST_PROFILE_ENV
from bot_modules.core.sticky import clear_placed_registry
from tests.db_template import migrated_db, reap
from tests.fakes import FakeGuild, FakeRole, FakeUser, fake_interaction as _fake_interaction


#: Application settings a developer's ``.env`` defines and the code reads at
#: run time. ``bot_modules.core.config`` calls ``load_dotenv(override=True)`` at
#: **module scope**, so importing almost anything merges the real ``.env`` into
#: ``os.environ`` — and the production checkout has one. That is not undoable
#: from outside pytest (the file wins over the process environment by design),
#: so it is undone here instead, after the import and before every test.
#:
#: It is global rather than per-directory for the same reason the scrubbers
#: below are: the dashboard's surface is not ``tests/web/``. ``tests/
#: test_web_routes.py`` builds its own dashboard app outside that directory,
#: and ``DASHBOARD_BASE_URL`` / ``SUPPORT_USER_ID`` are read by two bot-side
#: services as well. A per-directory fixture cannot reach either.
#:
#: ``tests/tooling/test_env_hermeticity.py`` fails if a name read by ``src/web_server``
#: or ``src/bot_modules`` is in neither this tuple nor ``ENV_NOT_SCRUBBED``,
#: so a new ``os.getenv`` forces the choice rather than inheriting one.
SCRUBBED_ENV_VARS = (
    # Dashboard / OAuth
    "DASHBOARD_BASE_URL",
    "SUPPORT_INVITE_URL",
    "DASHBOARD_RETURN_TO_URLS",
    "DASHBOARD_OPEN_AUTH",
    "DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SECRET",
    "SESSION_SECRET",
    "SUPPORT_USER_ID",
    # Integrations reached from both the bot and the dashboard
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "ANTHROPIC_API_KEY",
    "LAVALINK_HOST",
    "LAVALINK_PORT",
    "LAVALINK_PASSWORD",
    "LAVALINK_HEAP_MB",
    "LLAMA_MODEL_PATH",
    "LLAMA_HF_REPO",
    "LLAMA_HF_FILE",
    "LLAMA_N_CTX",
    "LLAMA_N_BATCH",
    "LLAMA_N_THREADS",
    "LLAMA_N_GPU_LAYERS",
    "LLAMA_SERVER_URL",
    "LLAMA_SERVER_TIMEOUT",
    "LLAMA_SERVER_ALLOW_PUBLIC",
)

#: Reads that must **survive** the scrub, and why. Machine plumbing rather than
#: application settings: removing these changes where a library looks for a
#: toolchain or a cache, which has nothing to do with test hermeticity and can
#: break a runner outright.
ENV_NOT_SCRUBBED = {
    "JAVA_HOME": "toolchain location — Lavalink needs the real JVM path",
    "HF_HOME": "HuggingFace cache dir; scrubbing it would re-download models",
    "LOCALAPPDATA": "Windows path lookup on the remote runner",
    "BOT_ENV": "selects dev/prod config; a test that cares sets it explicitly",
    "GUILD_ID": "resolved from the DB in tests; see resolve_guild_id's fallback",
    "RESET_DEV_DB": "dev-bootstrap flag, inert under BOT_ENV=dev in tests",
    "SEED_DEV_FIXTURES": "dev-bootstrap flag, inert under BOT_ENV=dev in tests",
    "DK_SQLITE_TEST_PROFILE": "set by _sqlite_test_profile below; the profile is the point",
    "PYTEST_CURRENT_TEST": "pytest's own marker; db_utils requires it beside the flag",
}


@pytest.fixture(scope="session", autouse=True)
def _sqlite_test_profile():
    """Open every SQLite connection in the suite without fsyncs.

    Thousands of tests each copy a template DB, write a little and throw it
    away; the fsyncs that make the bot's data durable are pure cost here,
    and on the remote runner they saturated the SSD with the cores idle.
    Two hooks, because pragmas are per connection:

    * ``DK_SQLITE_TEST_PROFILE=1`` — ``open_db`` / ``open_db_immediate`` set
      ``synchronous=OFF`` instead of NORMAL and skip optimize-on-close. They
      also require ``PYTEST_CURRENT_TEST``, so the flag can never reach a bot.
    * ``sqlite3.connect`` is wrapped to set ``synchronous=OFF`` on every other
      connection: ``temp_db`` (aiosqlite calls ``sqlite3.connect`` through
      the module attribute), the migration runner building the template, and
      the tests that open sqlite3 directly. Journal mode is left alone, so a
      test that asserts on WAL, or leaves it, sees real behaviour.

    Session-scoped: it runs once per xdist worker, before the first test's
    own fixtures. ``tests/test_sqlite_test_profile.py`` covers the wrapper;
    ``tests/core/test_db_utils.py`` proves the helpers' prod path is unchanged.
    """
    real_connect = sqlite3.connect

    def connect_without_fsync(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        try:
            conn.execute("PRAGMA synchronous=OFF")
        except sqlite3.Error:  # a file that is not a database, opened on purpose
            pass
        return conn

    mp = pytest.MonkeyPatch()
    mp.setenv(TEST_PROFILE_ENV, "1")
    mp.setattr(sqlite3, "connect", connect_without_fsync)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _hermetic_env():
    """Unset every application setting a developer's ``.env`` might define.

    Scrubbed to *absent*, not pinned to a fixed value: absent is what CI and a
    fresh clone see, so it is the configuration the assertions were written
    against. It also makes the dashboard's ``_auto_detect_auth`` fail closed if
    a test forgets to pass ``auth=`` rather than quietly picking up the real
    Discord client id.

    A test that needs a value sets it with ``monkeypatch.setenv``, which runs
    after this fixture and therefore still wins.
    """
    import os  # noqa: PLC0415

    saved = {n: os.environ.pop(n) for n in SCRUBBED_ENV_VARS if n in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reset_shared_module_state():
    """Clear process-wide module caches that leak between tests.

    Production keeps small in-memory caches at module level; tests in one
    xdist worker share the process, so entries left by one test (often keyed
    by the small fake guild/user ids every file reuses) poison later tests:

    * ``bot_modules.core.branding._avatar_cache`` — avatar-derived accents,
      keyed by guild id.
    * ``bot_modules.games.guess.cog._submit_history`` — /guess submit
      rate-limiter (5/hour per user id).
    * ``web_server.server._buckets`` — per-IP rate-limit token buckets; every
      TestClient shares the "testclient" IP, so the search/auth tiers drain
      across tests and start returning 429s.

    Only modules that are already imported are touched — this must not force
    imports for tests that never load them.
    """

    def _clear() -> None:
        branding = sys.modules.get("bot_modules.core.branding")
        if branding is not None:
            branding._avatar_cache.clear()
        guess_cog = sys.modules.get("bot_modules.games.guess.cog")
        if guess_cog is not None:
            guess_cog._submit_history.clear()
        web_server = sys.modules.get("web_server.server")
        if web_server is not None:
            web_server._buckets.clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _clear_sticky_placement_registry():
    """Forget which message ids ``core.sticky`` thinks a panel placed.

    That registry is process-global on purpose (a panel has to recognise *any*
    sticky panel's placement, not just its own), so it leaks across tests. Tests
    pick small hand-written message ids, and a leaked id makes a panel silently
    treat a member's message as another panel's repost and skip the restick —
    a passing test proving nothing. Global rather than per-module so a future
    test file cannot hit it unknowingly.
    """
    clear_placed_registry()
    yield
    clear_placed_registry()


@pytest.fixture(autouse=True)
def _reap_template_dbs():
    """Delete every template-copied DB (and WAL sidecars) after each test.

    tmp_path retention only prunes *previous* sessions, so without this each
    of the thousands of per-test DBs (plus -wal/-shm sidecars) survives the
    whole run — the inode/disk churn that once exhausted the remote runner.
    Autouse fixtures tear down last, so test-owned connections close first.
    """
    yield
    reap()


@pytest_asyncio.fixture
async def temp_db(tmp_path):
    """Open an aiosqlite connection with the full schema applied."""
    path = migrated_db(tmp_path / "test.db")
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    yield db
    await db.close()


@pytest.fixture
def test_config(tmp_path) -> Config:
    """A dev Config pointing at tmp_path — no real env vars required."""
    return Config(
        env="dev",
        token="fake-token",
        guild_id=9001,
        db_path=str(tmp_path / "test.db"),
        audit_channel_id=9999,
        reset_dev_db=False,
        seed_dev_fixtures=False,
    )


@pytest.fixture
def fake_interaction():
    """A MagicMock discord.Interaction with standard AsyncMock response methods."""
    return _fake_interaction()


@pytest.fixture
def guild_with_mods() -> FakeGuild:
    """A FakeGuild pre-populated with Mod and Jailed roles."""
    g = FakeGuild()
    g.roles[5001] = FakeRole(id=5001, name="Mod")
    g.roles[5002] = FakeRole(id=5002, name="Jailed")
    g.roles[5003] = FakeRole(id=5003, name="Admin")
    return g


@pytest.fixture
def mod_user(guild_with_mods) -> FakeUser:
    """A FakeUser with the Mod role."""
    mod_role = guild_with_mods.roles[5001]
    return FakeUser(id=2001, name="mod_user", roles=[mod_role])


@pytest.fixture
def regular_user() -> FakeUser:
    return FakeUser(id=3001, name="regular_user", roles=[])


@pytest.fixture
def sync_db_path(tmp_path: Path) -> Path:
    """Sync SQLite DB at tmp_path with the full schema applied.

    Use in tests that call open_db() directly (sync sqlite3 code).
    """
    return migrated_db(tmp_path / "test.db")


# ── cog-test stubs, requested by name ────────────────────────────────────
#
# These were autouse in tests/cogs/conftest.py, which scoped them by folder.
# Cog tests now live beside their feature, so a folder can't carry them; a
# test that needs one asks for it (``pytestmark = pytest.mark.usefixtures``).
# Never make them root-level autouse: a no-contact stub reaching tests that
# didn't ask for it is how the real gate once went missing for a whole worker.


@pytest.fixture
def stub_whisper_cog(monkeypatch):
    """Neutralise the whisper cog's display-name prefetch and break check.

    ``build_name_fn`` reads ``guild.get_member`` and the ``known_users`` table.
    Cog tests build guilds from bare ``MagicMock()``s, whose members' ``display_name``
    is itself a mock rather than a string, and ``escape_markdown`` raises on that.
    Stubbing at the use-site namespace makes embeds fall back to plain ``<@id>``
    text; the resolver's real fallback chain is covered directly in
    ``test_name_resolver_logic.py``. The Take a Break table sits beside the
    no-contact gate against the same stub db path, so "not on a break" is the
    default for the same reason.
    """

    async def _fake(**kwargs):
        return lambda uid: f"<@{uid}>"

    monkeypatch.setattr("bot_modules.whisper.cog.build_name_fn", _fake)
    monkeypatch.setattr("bot_modules.whisper.cog.is_on_break", MagicMock(return_value=False))


@pytest.fixture
def stub_no_contact(monkeypatch):
    """"These two have no no-contact entry", for cog tests on a stub db path.

    Cog tests run against stub db paths (``:memory:``, a mocked
    ``interaction.client``), so the no-contact gate has no table to read and
    would raise.

    Reach, precisely: this patches attributes on the *service module*, so it
    covers cogs that call it attribute-style (whisper, AMA, guess). It does
    **not** reach callers that bind the name at import (``is_no_contact_conn``
    / ``no_contact_partners_conn`` imported by name); those run against a real
    migrated DB in their own tests.

    A test that wants the gate to fire patches it itself; an inner ``patch``
    wins over this one (see test_guess_no_contact.py).

    Applied through ``monkeypatch`` rather than ``mock.patch`` so it shares one
    undo stack with the tests. A test that reaches for
    ``monkeypatch.setattr(no_contact_service, "is_no_contact", ...)`` records
    *whatever is there at the time* as the original — which is this stub — and
    a separate ``mock.patch`` teardown could then run first, put the real
    function back, and let monkeypatch write the stub back over it, for the
    rest of the session. That is not hypothetical: it left the real
    no-contact gate mocked out for every later test in the same worker, and
    test_no_contact_service.py failed with a MagicMock where the gate should
    have been. Sharing the stack makes the restores strictly LIFO.
    """
    from bot_modules.no_contact import service as no_contact_service

    monkeypatch.setattr(no_contact_service, "check_and_record", MagicMock(return_value=False))
    monkeypatch.setattr(no_contact_service, "no_contact_partners", MagicMock(return_value=set()))
    monkeypatch.setattr(no_contact_service, "is_no_contact", MagicMock(return_value=False))
