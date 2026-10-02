"""Contract: a raw write to a key ``GuildConfig`` caches drops the cache.

``AppContext.guild_config()`` memoises one ``GuildConfig`` per guild, and
nothing expires it. The ctx mutators (``ctx.set_config_value`` and friends)
invalidate on their own; a write through ``db_utils`` directly does not, so the
function making it must call ``invalidate_guild_config`` — or the bot keeps the
old welcome message, mod channel or grant role until a restart.

Enforced on *cached* keys only: the literal keys ``GuildConfig.load`` reads,
the XP coefficient prefix, and every ``grant_roles`` row. A write whose key
can't be resolved statically (a parameter, a loop variable) is either inside an
invalidating function or pinned in ``REVIEWED`` with the reason its keys are
never cached. Shrink only.

Born green on 2026-09-14, after fixing the two real misses it found: the
advisor's Apply button (``advisor_cog``) wrote cached keys and grant roles
without invalidating, and the Bot Roles page's adopt/stop wrote
``welcome_ping_role_id`` the same way.

Hand-written SQL counts too (row 1.23): a string literal that inserts into,
updates or deletes from ``config`` is a write the helper names above never see.
A literal naming a cached key, or any raw write to ``grant_roles``, must
invalidate; any other raw ``config`` write is treated as a dynamic-key site and
needs the same ``REVIEWED`` reason.
"""

from __future__ import annotations

import ast
import importlib
import re

from bot_modules.core.xp_system import _XP_COEFF_PREFIX
from tests.paths import REPO_ROOT

SRC = REPO_ROOT / "src"
APP_CONTEXT = SRC / "bot_modules/core/app_context.py"

#: The raw writers themselves, and the ctx wrappers that already invalidate.
EXEMPT_FILES = {"bot_modules/core/db_utils.py", "bot_modules/core/app_context.py"}

CONFIG_WRITERS = {
    "set_config_value", "delete_config_value", "add_config_id",
    "remove_config_id", "clear_config_id_bucket", "replace_config_id_bucket",
}
#: ``GuildConfig.grant_roles`` is the whole table, so every write is cached.
GRANT_WRITERS = {"upsert_grant_role", "delete_grant_role"}
#: ``routes/config.py``'s spec-driven writer: arg 3 maps field → (key, coerce).
SPEC_WRITER = "_apply_config_fields"
KEY_ARG = 1
SPEC_ARG = 3

#: Writes with a dynamic key and no invalidation, reviewed as never cached.
REVIEWED = {
    ("bot_modules/inactive/cog.py", "_set_config"): "inactive_* dials",
    ("bot_modules/whisper/cog.py", "_clear_guild_config"): "whisper_* keys",
    ("bot_modules/advisor/actions.py", "apply_config_change"):
        "advisor_cog invalidates after the call (test_advisor_cog)",
    ("bot_modules/advisor/actions.py", "_apply_grant_role"):
        "advisor_cog invalidates after the call (test_advisor_cog)",
    ("bot_modules/shared/ai_config.py", "set_config"): "ai_* keys",
    ("bot_modules/shared/ai_config.py", "reset_prompt"): "ai_prompt_* keys only",
    ("bot_modules/core/command_sync.py", "_write_stored_hash"): "command hash",
    ("bot_modules/games/guess/repo.py", "set_guess_config_value"): "guess_* keys",
    ("bot_modules/roles/grant/audit_service.py", "save_card_ref"): "card refs",
    ("bot_modules/roles/grant/audit_service.py", "clear_card_ref"): "card refs",
    ("bot_modules/voice/master/service.py", "set_voice_master_config_value"):
        "voice_master_* keys, checked against _CONFIG_DEFAULTS",
    ("bot_modules/whisper/repo.py", "set_whisper_config_value"): "whisper_* keys",
    ("web_server/routes/config.py", SPEC_WRITER): "call sites are checked by spec",
    ("web_server/routes/config.py", "_set_guild_removal_purge"): "purge dial",
    ("web_server/routes/config.py", "update_privacy"): "retention switches",
    ("web_server/routes/config.py", "update_bulk_cleanup"): "bulk cleanup keys",
    ("web_server/routes/config.py", "update_risky"): "risky_* dials",
    ("web_server/routes/todo.py", "_set_change_cards"): "todo change-card keys",
    ("bot_modules/games/risky_roll/store.py", "_set_ping_role"): "risky_* dials",
    ("bot_modules/games/risky_roll/store.py", "_set_min_game_time"): "risky_* dials",
    ("bot_modules/games/risky_roll/store.py", "_set_max_games_per_channel"):
        "risky_* dials",
    ("bot_modules/games/risky_roll/store.py", "_delete_guild_data"):
        "risky_* dials, on the Risky Rolls reset",
}

#: Hand-written SQL that writes a table ``GuildConfig`` caches from.
RAW_WRITE = re.compile(
    r"\b(?:(?:INSERT|REPLACE)(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+(config|grant_roles)\b(?!_)",
    re.IGNORECASE,
)
_SQL_LITERAL = re.compile(r"'([^']*)'")

_DYNAMIC = object()


def _cached_keys() -> frozenset[str]:
    tree = ast.parse(APP_CONTEXT.read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "GuildConfig"
    )
    load = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "load")
    return frozenset(
        node.args[0].value
        for node in ast.walk(load)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("_val", "_int", "_ids")
        and node.args
        and isinstance(node.args[0], ast.Constant)
    )


CACHED = _cached_keys()


def _is_cached(key: str) -> bool:
    return key in CACHED or key.startswith(_XP_COEFF_PREFIX)


def _prefix_may_be_cached(prefix: str) -> bool:
    return (
        not prefix
        or prefix.startswith(_XP_COEFF_PREFIX)
        or _XP_COEFF_PREFIX.startswith(prefix)
        or any(key.startswith(prefix) for key in CACHED)
    )


class _Module:
    """Lazy import, so only modules with a non-literal key get imported."""

    def __init__(self, rel: str) -> None:
        name = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        self._name, self._ns = name, None

    def eval(self, expr: ast.expr):
        if any(isinstance(n, ast.Call) for n in ast.walk(expr)):
            return _DYNAMIC
        if self._ns is None:
            self._ns = vars(importlib.import_module(self._name))
        try:
            return eval(compile(ast.Expression(expr), "<key>", "eval"), dict(self._ns))
        except (NameError, AttributeError):  # a local or parameter
            return _DYNAMIC


def _resolve(expr: ast.expr, module: _Module):
    """``True``/``False`` for cached-or-not, ``_DYNAMIC`` when unknowable."""
    if isinstance(expr, ast.JoinedStr):
        prefix = ""
        for part in expr.values:
            if isinstance(part, ast.Constant):
                prefix += part.value
                continue
            value = module.eval(part.value)
            if not isinstance(value, str):
                break
            prefix += value
        else:
            return _is_cached(prefix)
        return _DYNAMIC if _prefix_may_be_cached(prefix) else False
    value = module.eval(expr)
    if value is _DYNAMIC:
        return _DYNAMIC
    if isinstance(value, str):
        return _is_cached(value)
    return _DYNAMIC


def _writer_name(call: ast.Call, aliases: dict[str, str]) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        name = aliases.get(func.id, func.id)
    elif isinstance(func, ast.Attribute):
        # ctx.set_config_value(...) and friends invalidate for themselves.
        if isinstance(func.value, (ast.Name, ast.Attribute)) and (
            ast.unparse(func.value).rsplit(".", 1)[-1] == "ctx"
        ):
            return None
        name = func.attr
    else:
        return None
    return name if name in CONFIG_WRITERS | GRANT_WRITERS | {SPEC_WRITER} else None


def _outer_functions(tree: ast.Module):
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield top
        elif isinstance(top, ast.ClassDef):
            yield from (
                m for m in top.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            )


def _raw_sql_verdict(sql: str) -> str | None:
    """"missing", "dynamic", or None when *sql* writes no cached table."""
    match = RAW_WRITE.search(sql)
    if match is None:
        return None
    if match.group(1).lower() == "grant_roles":
        return "missing"
    if any(_is_cached(lit) for lit in _SQL_LITERAL.findall(sql)):
        return "missing"
    return "dynamic"


def _sweep_raw_sql(fn, rel: str, missing: list[str], dynamic: set) -> None:
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        verdict = _raw_sql_verdict(node.value)
        if verdict == "missing":
            missing.append(f"src/{rel}:{node.lineno} raw SQL in {fn.name}")
        elif verdict == "dynamic":
            dynamic.add((rel, fn.name))


def _sweep() -> tuple[list[str], set[tuple[str, str]]]:
    """(cached writes with no invalidation, uninvalidated dynamic-key sites)."""
    missing: list[str] = []
    dynamic: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in EXEMPT_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if not RAW_WRITE.search(text) and not any(
            w in text for w in CONFIG_WRITERS | GRANT_WRITERS | {SPEC_WRITER}
        ):
            continue
        tree = ast.parse(text)
        aliases = {
            alias.asname or alias.name: alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        module = _Module(rel)
        for fn in _outer_functions(tree):
            invalidates = "invalidate_guild_config" in ast.unparse(fn)
            if not invalidates:
                _sweep_raw_sql(fn, rel, missing, dynamic)
            for call in ast.walk(fn):
                if invalidates or not isinstance(call, ast.Call):
                    continue
                name = _writer_name(call, aliases)
                if name is None:
                    continue
                where = f"src/{rel}:{call.lineno} {name}() in {fn.name}"
                if name in GRANT_WRITERS:
                    dynamic.add((rel, fn.name))
                    continue
                if name == SPEC_WRITER:
                    if fn.name == SPEC_WRITER or len(call.args) <= SPEC_ARG:
                        dynamic.add((rel, fn.name))
                        continue
                    spec = module.eval(call.args[SPEC_ARG])
                    if spec is _DYNAMIC:
                        dynamic.add((rel, fn.name))
                    elif any(_is_cached(key) for key, _ in spec.values()):
                        missing.append(where)
                    continue
                if len(call.args) <= KEY_ARG:
                    dynamic.add((rel, fn.name))
                    continue
                cached = _resolve(call.args[KEY_ARG], module)
                if cached is _DYNAMIC:
                    dynamic.add((rel, fn.name))
                elif cached:
                    missing.append(where)
    return missing, dynamic


def test_sweep_reads_the_cached_key_set():
    """A renamed loader helper would empty the set and pass everything."""
    assert {"welcome_message", "mod_channel_id", "auto_role_ids"} <= CACHED


def test_raw_sql_is_recognised():
    """The regex sees every write spelling and no read or sibling table."""
    for sql in (
        "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
        "insert or replace into config VALUES (?, ?, ?)",
        "UPDATE config SET value = ? WHERE key = ?",
        "DELETE FROM config WHERE guild_id = ?",
    ):
        assert _raw_sql_verdict(sql) == "dynamic", sql
    for sql in (
        "SELECT value FROM config WHERE key = ?",
        "DELETE FROM games_config WHERE guild_id = ?",
        "UPDATE config_history SET x = 1",
        "DELETE FROM configs",
    ):
        assert _raw_sql_verdict(sql) is None, sql
    assert _raw_sql_verdict("DELETE FROM grant_roles WHERE guild_id = ?") == "missing"
    assert _raw_sql_verdict(
        "INSERT INTO config VALUES (?, 'welcome_message', ?)"
    ) == "missing"


def test_raw_writes_to_cached_keys_invalidate():
    missing, _ = _sweep()
    assert not missing, (
        "These write a key GuildConfig caches without calling "
        "invalidate_guild_config in the same function (or use the ctx mutator, "
        "which does):\n  " + "\n  ".join(missing)
    )


def test_dynamic_key_writes_are_reviewed():
    _, dynamic = _sweep()
    unreviewed = sorted(f"src/{rel} {fn}()" for rel, fn in dynamic - REVIEWED.keys())
    assert not unreviewed, (
        "A config write whose key can't be resolved statically, in a function "
        "that doesn't invalidate. If its keys can never be one GuildConfig "
        "caches, add it to REVIEWED with the reason; otherwise invalidate:\n  "
        + "\n  ".join(unreviewed)
    )


def test_reviewed_only_shrinks():
    _, dynamic = _sweep()
    stale = sorted(f"src/{rel} {fn}()" for rel, fn in REVIEWED.keys() - dynamic)
    assert not stale, f"Now invalidating, resolvable or gone — remove from REVIEWED: {stale}"
