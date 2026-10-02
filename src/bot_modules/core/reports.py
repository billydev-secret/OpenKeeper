"""Shared text helpers used by command modules.

This module previously held an unregistered `/report` command tree
(`register_reports`) that was migrated into `cogs/reports_cog.py`. The cog
now owns the live commands; only the chunking/ephemeral helpers remain.
"""

from __future__ import annotations

import discord

from bot_modules.core.branding import drop_split_escape

SAFE_TEXT_CHUNK = 1900


def _cut(text: str, limit: int, boundary: str, min_fill: float) -> tuple[str, str]:
    """Split *text* at *limit*, preferring the last *boundary* that fits.

    A boundary is taken only past ``limit * min_fill`` — otherwise a lone
    separator near the start would leave most of the message empty. With no
    usable boundary the cut is made at *limit*, which is the only way text
    holding no separator at all can be shown.

    Callers routinely pass text that has already been through
    ``discord.utils.escape_markdown`` (the voice-transcript chunkers do, on
    text that can run for thousands of characters with no space at all, which
    forces the no-boundary fallback on every chunk), so a hard cut at a bare
    *limit* can land inside an escape pair. ``drop_split_escape`` moves the
    boundary back in front of the orphan backslash; the ordinary
    ``text[len(head):]`` slice below then picks that backslash back up as the
    first character of the remainder, so no character is dropped and the pair
    reassembles intact one chunk later.

    That retreat is refused when *head* is a single character, because this
    function's contract is progress: ``chunk_text`` advances by the length of
    what comes back, so a *head* shortened to nothing returns the remainder
    unchanged and spins forever. It takes a per-chunk budget of exactly 1 to
    reach — no caller passes one, and ``chunk_text``'s own ``max(1, ...)``
    floor is what makes it expressible at all — but the floor already has a
    test saying it must terminate, and a degenerate budget shipping a bare
    backslash beats one that hangs the bot.
    """
    window = text[: limit + 1]
    at = window.rfind(boundary)
    head = window[:at] if at > 0 and at > limit * min_fill else text[:limit]
    if len(head) > 1:
        head = drop_split_escape(head)
    return head.rstrip(), text[len(head):].lstrip()


def chunk_text(
    text: str,
    limit: int = SAFE_TEXT_CHUNK,
    *,
    boundary: str = "\n",
    min_fill: float = 0.0,
    prefix: str = "",
    max_parts: int | None = None,
    overflow_note: str = "",
) -> list[str]:
    """Split *text* into messages that each fit within *limit* characters.

    *boundary* is what the cut prefers to land on. ``"\n"`` suits text already
    broken into lines; ``" "`` suits text that is not — a voice transcript
    arrives as one long paragraph, so a newline cut would find nothing and
    every join would land mid-word.

    *min_fill* rejects a boundary that would waste the message: the cut is
    taken only past ``limit * min_fill``. At the default 0 any boundary will
    do, which is the line-splitting behaviour this helper has always had.

    *prefix* rides on the first chunk and is **paid for out of its budget**,
    so a header counts against the limit Discord actually measures instead of
    riding on top of it and pushing the message over.

    *max_parts* bounds the result. The last allowed chunk is shortened to make
    room for *overflow_note*, appended to say the text was cut rather than
    letting it stop mid-sentence.
    """
    if not text:
        return [prefix]

    limit = max(1, limit)
    chunks: list[str] = []
    remaining = text
    while remaining:
        head = prefix if not chunks else ""
        budget = max(1, limit - len(head))
        if max_parts is not None and len(chunks) + 1 >= max_parts:
            if len(remaining) <= budget:
                chunks.append(head + remaining)
            else:
                body, _ = _cut(
                    remaining, max(1, budget - len(overflow_note)), boundary, min_fill
                )
                chunks.append(head + body + overflow_note)
            break
        if len(remaining) <= budget:
            chunks.append(head + remaining)
            break
        body, remaining = _cut(remaining, budget, boundary, min_fill)
        chunks.append(head + body)

    return chunks


async def send_ephemeral_text(interaction: discord.Interaction, text: str) -> None:
    for chunk in chunk_text(text):
        await interaction.followup.send(chunk, ephemeral=True)
