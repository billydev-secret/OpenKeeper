"""XP leaderboard rendering — the pure half split off from the DB read."""
from __future__ import annotations

from types import SimpleNamespace

import discord

from bot_modules.xp.cog import _LEADERBOARD_SOURCES, _build_xp_leaderboard_embed

NAMES = {1: "Top", 2: "Second", 99: "Caller"}


def _section(title: str, entries, rank):
    return (
        title,
        [SimpleNamespace(user_id=uid, xp=xp) for uid, xp in entries],
        SimpleNamespace(member_count=len(entries), median_xp=1.0, stddev_xp=0.5),
        SimpleNamespace(rank=rank, xp=3.0),
    )


def test_leaderboard_names_ranked_members_and_caller_never_mentions_them():
    # An embed <@id> shows digits for anyone the reader's client hasn't cached.
    sections = [
        _section(f"{icon} {name}", [(1, 9.0), (2, 4.0)] if i == 0 else [], 3 if i == 0 else None)
        for i, (name, icon, _key, _empty) in enumerate(_LEADERBOARD_SOURCES)
    ]
    embed = _build_xp_leaderboard_embed(
        sections, 99, "Weekly", "Last 7 days", discord.Color.blurple(), NAMES.__getitem__
    )
    text = embed.fields[0].value
    assert "🥇 Top" in text and "🥈 Second" in text
    assert "Your standing: #3 Caller" in text
    assert "Your standing: Caller has no tracked XP here." in embed.fields[1].value
    assert not any("<@" in str(f.value) for f in embed.fields)
