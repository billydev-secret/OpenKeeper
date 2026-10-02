"""ai_check_watched_message: the NSFW tag that tells the model Rule 3 can't apply."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot_modules.moderation import ai_service as ai_moderation_service
from bot_modules.shared import ollama_client


class _Category:
    def __init__(self, nsfw: bool):
        self._nsfw = nsfw

    def is_nsfw(self) -> bool:
        return self._nsfw


class _Channel:
    def __init__(self, nsfw: bool = False, category: _Category | None = None):
        self.id = 1
        self.name = "general"
        self.nsfw = nsfw
        self.category = category

    def is_nsfw(self) -> bool:
        return self.nsfw


def _message(channel: _Channel) -> SimpleNamespace:
    return SimpleNamespace(
        guild=None,
        channel=channel,
        created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
        content="hello",
        author=SimpleNamespace(display_name="Member"),
    )


@pytest.mark.parametrize(
    "channel,tagged",
    [
        pytest.param(_Channel(nsfw=True), True, id="own-flag"),
        # Gated only through its category: the raw `nsfw` flag missed it, so the
        # model was told the channel was SFW and could flag Rule 3 there.
        pytest.param(_Channel(category=_Category(True)), True, id="category-gated"),
        pytest.param(_Channel(), False, id="sfw"),
    ],
)
async def test_prompt_tags_age_gated_channels(monkeypatch, channel, tagged):
    seen: dict[str, str] = {}

    async def fake_chat(*, system, user_content, max_tokens, temperature):
        seen["prompt"] = user_content
        return "OK"

    monkeypatch.setattr(ollama_client, "chat", fake_chat)

    assert await ai_moderation_service.ai_check_watched_message(_message(channel)) == (False, "")
    assert ("[NSFW-designated]" in seen["prompt"]) is tagged
