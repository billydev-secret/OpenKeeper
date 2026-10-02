"""Tier 2 component tests: GuessCog structural/import assertions.

These tests verify the cog module is importable, the class exists,
the async setup function is present, and the /guess_status slash command
is registered — without making any network calls or loading Discord state.
"""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock

import discord

from bot_modules.games.guess.cog import GameView


def test_guess_cog_module_imports_cleanly():
    """Importing cogs.guess_cog must not raise ImportError."""
    importlib.import_module("bot_modules.games.guess.cog")


def test_guess_cog_class_exists():
    """GuessCog class must be defined in cogs.guess_cog."""
    mod = importlib.import_module("bot_modules.games.guess.cog")
    assert hasattr(mod, "GuessCog"), "GuessCog not found in cogs.guess_cog"


def test_setup_function_exists():
    """Module-level setup must be an async (coroutine) function."""
    mod = importlib.import_module("bot_modules.games.guess.cog")
    assert hasattr(mod, "setup"), "setup() not found in cogs.guess_cog"
    assert asyncio.iscoroutinefunction(mod.setup), "setup() must be a coroutine function"


def test_guess_group_and_submit_registered():
    """`guess` app_commands.Group and `submit` subcommand must be on GuessCog."""
    from discord import app_commands

    mod = importlib.import_module("bot_modules.games.guess.cog")
    cog_cls = mod.GuessCog

    # There should be a Group named "guess" registered on the cog
    group_names = [
        item.name
        for item in cog_cls.__cog_app_commands__
        if isinstance(item, app_commands.Group)
    ]
    assert "guess" in group_names, (
        f"'guess' Group not found in GuessCog app commands; found: {group_names}"
    )

    # The "guess" group must have a "submit" subcommand
    guess_group = next(
        item for item in cog_cls.__cog_app_commands__ if item.name == "guess"
    )
    sub_names = [cmd.name for cmd in guess_group.commands]
    assert "submit" in sub_names, (
        f"'submit' not found in /guess subcommands; found: {sub_names}"
    )


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def test_game_view_is_the_round_card_with_one_guess_button():
    """The count and the redacted submitter are a line of text now, not the
    two disabled chip buttons the embed era used as labels."""
    view = GameView(MagicMock(), round_id=42, guess_count=7)
    buttons = [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]
    assert [b.label for b in buttons] == ["Guess"]
    text = _text(view)
    assert text.splitlines()[0] == "## 🎭 Round #42"
    assert "Guesses: **7**" in text
    assert "Submitted by ▒▒▒▒▒▒▒" in text


def test_game_view_keeps_the_round_scoped_guess_id_and_is_persistent():
    """Open rounds posted as embeds carry this id; the restart re-registers
    the converted view against it."""
    view = GameView(MagicMock(), round_id=99, guess_count=0)
    ids = {
        i.custom_id for i in view.walk_children()
        if isinstance(i, discord.ui.Button)
    }
    assert ids == {"guess_guess:99"}
    assert view.is_persistent()
