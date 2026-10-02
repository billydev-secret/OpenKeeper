"""Usage telemetry cog — the glue: which invocations become rows.

Aggregation/reporting behaviour lives in test_usage_telemetry_service.py.
The failure path depends on ``events_cog`` re-dispatching ``tree.error`` as an
``on_app_command_error`` bot event, which is asserted here too — that contract
spans two cogs and would otherwise break silently.

``test_tree_error_fires_even_when_a_cog_swallows_it`` pins a fact one level
lower, in discord.py itself: a cog's own ``cog_app_command_error`` (three
cogs define one — ``confessions_cog``, ``risky_roll_cog``, ``advisor_cog``)
does not stop ``CommandTree._call`` from also calling ``tree.on_error``. A
failed ``/ask``, ``/confess`` or Risky Rolls command still gets its ``ok=0``
row.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from bot_modules.analytics.cog_usage_telemetry import UsageTelemetryCog, command_name
from bot_modules.core.db_utils import open_db

GUILD = 123
USER = 1001


def _make_cog(db_path: Path):
    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=db_path, open_db=lambda: open_db(db_path))
    return UsageTelemetryCog(bot)  # type: ignore[arg-type]


def _interaction(*, guild_id=GUILD, user_id=USER, is_bot=False, name="bank"):
    return SimpleNamespace(
        guild_id=guild_id,
        channel_id=555,
        command=SimpleNamespace(qualified_name=name, name=name),
        user=SimpleNamespace(id=user_id, bot=is_bot),
    )


def _rows(db_path):
    with open_db(db_path) as conn:
        # open_db sets row_factory=sqlite3.Row; tuples compare readably.
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT name, user_id, channel_id, ok FROM usage_events"
            ).fetchall()
        ]


# ── command_name ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SimpleNamespace(qualified_name="quest board", name="board"), "quest board"),
        (SimpleNamespace(qualified_name="", name="bank"), "bank"),
        (None, ""),
    ],
)
def test_command_name_prefers_qualified_name(command, expected):
    assert command_name(command) == expected


# ── recording ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completion_records_success(sync_db_path: Path):
    cog = _make_cog(sync_db_path)
    await cog._on_completion(_interaction(), MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == [("bank", USER, 555, 1)]


@pytest.mark.asyncio
async def test_error_listener_records_failure(sync_db_path: Path):
    cog = _make_cog(sync_db_path)
    await cog._on_error(_interaction(), MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == [("bank", USER, 555, 0)]


@pytest.mark.asyncio
async def test_events_cog_dispatches_the_error_event():
    """The failure path only works because events_cog re-broadcasts its
    single-slot tree.error handler as a bot event. Nothing else asserts that
    cross-cog contract, and breaking it would silently zero the error count."""
    from bot_modules.events import cog as events_cog

    client = MagicMock()
    interaction = SimpleNamespace(
        client=client, guild=None, guild_id=GUILD,
        user=SimpleNamespace(id=USER, bot=False),
        response=SimpleNamespace(is_done=lambda: True),
    )
    err = RuntimeError("boom")

    await events_cog._on_tree_error(interaction, err)  # type: ignore[arg-type]

    client.dispatch.assert_any_call("app_command_error", interaction, err)


class _SwallowingCog(commands.Cog):
    """Stand-in for confessions_cog / risky_roll_cog / advisor_cog: defines
    its own cog_app_command_error and does not re-raise."""

    @app_commands.command(name="boom", description="boom")
    async def boom(self, interaction: discord.Interaction) -> None:
        raise app_commands.AppCommandError("boom")

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        pass  # swallow -- exactly what confessions/risky_roll/advisor do


@pytest.mark.asyncio
async def test_tree_error_fires_even_when_a_cog_swallows_it():
    """The other half of the 'Known gap' the docs used to claim: a cog's own
    cog_app_command_error does NOT stop CommandTree from also calling
    tree.on_error. discord.py 2.7.1's CommandTree._call runs
    _invoke_error_handlers (which includes the cog's handler) and then
    unconditionally calls self.on_error -- there's no way for a swallowed
    error to suppress that second call. Exercises the real discord.py
    CommandTree/Command machinery, not a mock, since the claim being pinned
    lives in that library, not in DK's own code."""
    from bot_modules.events import cog as events_cog

    client = discord.Client(intents=discord.Intents.none())
    client.dispatch = MagicMock()  # type: ignore[method-assign]
    tree = app_commands.CommandTree(client)
    tree.error(events_cog._on_tree_error)

    cog = _SwallowingCog()
    tree.add_command(cog.boom)

    interaction = SimpleNamespace(
        client=client,
        type=discord.InteractionType.application_command,
        data={"name": "boom", "type": 1, "options": []},
        guild_id=GUILD,
        guild=None,
        channel_id=555,
        user=SimpleNamespace(id=USER, bot=False),
        response=SimpleNamespace(is_done=lambda: True),
        command_failed=False,
        _cs_command=None,
        _cs_namespace=None,
        _state=client._connection,
    )

    await tree._call(interaction)  # type: ignore[arg-type]

    # events_cog._on_tree_error ran and re-dispatched exactly like the
    # non-swallowed path, so usage_telemetry_cog's on_app_command_error
    # listener still fires and still records ok=0 (see
    # test_error_listener_records_failure above).
    assert client.dispatch.call_args_list[0].args[0] == "app_command_error"


# ── guards ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interaction",
    [
        pytest.param(_interaction(guild_id=None), id="dm_has_no_guild"),
        pytest.param(_interaction(is_bot=True), id="bot_actor"),
        pytest.param(_interaction(name=""), id="unnamed_command"),
    ],
)
async def test_no_row_for_guarded_invocations(sync_db_path: Path, interaction):
    cog = _make_cog(sync_db_path)
    await cog._on_completion(interaction, MagicMock())  # type: ignore[arg-type]
    assert _rows(sync_db_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("listener", ["_on_completion", "_on_error"])
async def test_db_failure_does_not_propagate(sync_db_path: Path, listener):
    """A telemetry write must never break the command it is measuring."""
    cog = _make_cog(sync_db_path)
    cog.bot.ctx.open_db = MagicMock(side_effect=RuntimeError("db is gone"))  # type: ignore[attr-defined]
    await getattr(cog, listener)(_interaction(), MagicMock())
    assert _rows(sync_db_path) == []
