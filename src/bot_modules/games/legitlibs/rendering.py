"""The text a LegitLibs template is filled with.

Three renderers shared by the two modes: the anonymous filled story, the
redacted story that shows which blank is which, and Classic's attributed
story with each writer in a spoiler. The cards these land on are built in
``layouts.py``; the embed builders that lived here went with the
Components V2 conversion (2026-09-30).
"""

import re

import discord

from bot_modules.games.utils.names import plain_name

_MARKER_RE = re.compile(r"\{(\w+)\}")


def render_filled_body(body: str, blanks: list[dict], fills: dict[str, str]) -> str:
    """Replace {blank_id} markers with bolded fill text."""
    def replacer(m):
        bid = m.group(1)
        val = fills.get(bid, "___")
        return f"**{discord.utils.escape_markdown(val)}**"
    return _MARKER_RE.sub(replacer, body)


def render_redacted_body(body: str, blanks: list[dict]) -> str:
    """Replace {blank_id} markers with numbered placeholders like **[1]**, matching modal labels."""
    id_to_pos = {b["id"]: b.get("position", i + 1) for i, b in enumerate(blanks)}
    def replacer(m):
        bid = m.group(1)
        pos = id_to_pos.get(bid)
        if pos is None:
            return m.group(0)
        return f"**`[{pos}]`**"
    return _MARKER_RE.sub(replacer, body)


def render_filled_body_attributed(
    body: str,
    blanks: list[dict],
    fills: dict,
    names,
) -> str:
    """Render template body with inline spoiler-tag attribution.

    Filled blank  -> **value** (-||Display Name||)
    Unfilled blank -> ___
    """
    def replacer(m):
        bid = m.group(1)
        fill = fills.get(bid)
        if not fill:
            return "___"
        name = plain_name(names, int(fill["by"]))
        safe_val = discord.utils.escape_markdown(str(fill["value"]))
        safe_name = discord.utils.escape_markdown(str(name))
        return f"**{safe_val}** (-||{safe_name}||)"
    return _MARKER_RE.sub(replacer, body)
