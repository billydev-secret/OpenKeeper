"""``ai_client.generate_json``: structured outputs instead of hand-parsed JSON.

Standards audit 3.11. The two JSON callers (LegitLibs AI prep, the QA card
rewrite) used to fish an object out of prose with an ``_extract_json`` each;
the schema now goes to the API as ``output_config.format``, which constrains
the reply to it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from anthropic.types import TextBlock

from bot_modules.games.utils import ai_client

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}


class _FakeMessages:
    def __init__(self, text):
        self.text = text
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[TextBlock(type="text", text=self.text)])


@pytest.fixture
def fake(monkeypatch):
    def install(text):
        messages = _FakeMessages(text)
        monkeypatch.setattr(
            ai_client, "get_client", lambda: SimpleNamespace(messages=messages)
        )
        return messages

    return install


def test_the_schema_goes_to_the_api_as_output_config(fake):
    messages = fake('{"n": 3}')
    assert asyncio.run(ai_client.generate_json("sys", "user", SCHEMA)) == {"n": 3}
    assert messages.kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA}
    }


def test_plain_text_calls_send_no_output_config(fake):
    messages = fake("hello")
    assert asyncio.run(ai_client.generate_text("sys", "user")) == "hello"
    assert "output_config" not in messages.kwargs


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param('{"n": 3', id="truncated"),
        pytest.param("[1, 2]", id="not-an-object"),
        pytest.param("", id="empty"),
    ],
)
def test_an_unusable_reply_is_none(fake, reply):
    fake(reply)
    assert asyncio.run(ai_client.generate_json("sys", "user", SCHEMA)) is None


def test_an_api_failure_is_none(monkeypatch):
    class Boom:
        async def create(self, **_kw):
            raise RuntimeError("down")

    monkeypatch.setattr(ai_client, "get_client", lambda: SimpleNamespace(messages=Boom()))
    assert asyncio.run(ai_client.generate_json("sys", "user", SCHEMA)) is None
