"""Cog-layer tests for Needle's thread-rename confirmations.

``tests/needle/test_needle_logic.py`` covers ``_apply_variables`` (the welcome
template's mention-injection guard). These two tests cover a different,
lower-stakes gap: the rename confirmation each rename path echoes back to
the same member who just typed the title.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.needle.cog import NeedleCog, NeedleTitleModal
from tests.fakes import fake_interaction

RAW_TITLE = "__spooky__ [click](https://evil.example)"
ESCAPED_TITLE = discord.utils.escape_markdown(RAW_TITLE)


def _thread_interaction() -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.edit = AsyncMock()
    return fake_interaction(channel=thread)


@pytest.mark.asyncio
async def test_title_modal_escapes_the_new_title_in_its_confirmation():
    """A title typed with markdown delimiters must not break the bold
    wrapper of the modal's own "Renamed to **…**." ack — low-impact (it is
    shown only to the person who just typed it) but the file's own
    ``_apply_variables`` establishes that member text gets escaped here.
    """
    modal = NeedleTitleModal()
    modal.new_title._value = RAW_TITLE
    interaction = _thread_interaction()

    await modal.on_submit(interaction)

    interaction.channel.edit.assert_awaited_once_with(name=RAW_TITLE)
    msg = interaction.response.send_message.await_args.args[0]
    assert msg == f"Renamed to **{ESCAPED_TITLE}**."
    # escape_markdown only prefixes leading delimiters it can neutralise
    # this way — assert on the exact message, not substring containment.
    assert f"**{RAW_TITLE}**" not in msg


@pytest.mark.asyncio
async def test_slash_title_command_escapes_the_new_title_in_its_confirmation():
    """Exact sibling of the modal path above, reached via ``/title`` instead
    of the Edit-Title button — same two-line pattern (edit, then echo).
    """
    bot = MagicMock()
    cog = NeedleCog(bot)
    interaction = _thread_interaction()

    with patch(
        "bot_modules.needle.cog._has_thread_perm",
        new=AsyncMock(return_value=True),
    ):
        await cog.title.callback(cog, interaction, RAW_TITLE)

    interaction.channel.edit.assert_awaited_once_with(name=RAW_TITLE)
    msg = interaction.response.send_message.await_args.args[0]
    assert msg == f"Thread renamed to **{ESCAPED_TITLE}**."
    assert f"**{RAW_TITLE}**" not in msg
