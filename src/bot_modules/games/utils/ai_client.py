"""The shared Anthropic client, and a one-shot text call that never raises.

Owns: one lazily built ``AsyncAnthropic`` (``get_client``, keyed from
``ANTHROPIC_API_KEY`` and reused by ``advisor_service``), ``generate_text``,
a single-turn call that returns the text or ``None`` on any failure, logging
the error, and ``generate_json``, the same call constrained to a JSON schema
through structured outputs — the API guarantees the reply parses and matches,
so no caller hand-extracts JSON from prose.

Leaves to callers: prompts, rate limiting (the dashboard route puts it behind
the ``ai`` tier), and what an empty result means — a ``None`` is the caller's
to render, never an exception to catch.
"""

import json
import logging
import os
from typing import Any

from anthropic import AsyncAnthropic, APIError, APITimeoutError
from anthropic.types import TextBlock

log = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


DEFAULT_MODEL = "claude-haiku-4-5-20251001"


async def generate_text(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 200,
    temperature: float = 0.9,
) -> str | None:
    """
    Call Anthropic messages API. Returns the response text or None on error.
    """
    return await _generate(system_prompt, user_prompt, model, max_tokens, temperature, None)


async def generate_json(
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> dict[str, Any] | None:
    """One call whose reply is an object matching *schema*, or None.

    Structured outputs (``output_config.format``) constrain decoding to the
    schema, so the reply is valid JSON of that shape. A reply cut off at
    ``max_tokens`` is the one way it can still fail to parse; that, like any
    API error, is logged and returns None.
    """
    text = await _generate(system_prompt, user_prompt, model, max_tokens, temperature, schema)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        log.warning("Structured reply did not parse (len=%d) — truncated?", len(text))
        return None
    return parsed if isinstance(parsed, dict) else None


async def _generate(
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
    schema: dict[str, Any] | None,
) -> str | None:
    try:
        client = get_client()
        extra: dict[str, Any] = {}
        if schema is not None:
            extra["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        response = await client.messages.create(
            model=model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            **extra,
        )
        parts = [b.text for b in response.content if isinstance(b, TextBlock)]
        return "".join(parts).strip() or None
    except (APIError, APITimeoutError):
        log.exception("Anthropic API error.")
        return None
    except Exception:
        log.exception("Unexpected Anthropic error.")
        return None
