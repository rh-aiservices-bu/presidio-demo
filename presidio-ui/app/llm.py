"""OpenAI-compatible LLM client.

Two hard constraints from this lab's model (qwen3-235b-a22b, Bedrock-backed via
litellm), both verified the hard way:

  * never send `stop` -- the upstream rejects it with
    "This model doesn't support the stopSequences field"
  * never let a provider exception reach a span. httpx errors can echo the request
    body, which would put the very text we are masking into the trace's error
    status. Everything is re-raised as a sanitized LlmError.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator

from openai import AsyncOpenAI

from .config import Settings

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """Upstream call failed. Deliberately carries no prompt or response text."""


@dataclass(frozen=True)
class LlmUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in self.__dict__.items() if isinstance(v, int)}


def build_client(s: Settings) -> AsyncOpenAI:
    # Default trust store on purpose: this endpoint has a public certificate, and
    # pointing SSL_CERT_FILE at MLflow's service CA would break it.
    return AsyncOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        timeout=s.llm_timeout_s,
        max_retries=1,
    )


def build_messages(s: Settings, text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if s.system_prompt:
        messages.append({"role": "system", "content": s.system_prompt})
    messages.append({"role": "user", "content": text})
    return messages


async def stream_completion(
    oai: AsyncOpenAI, s: Settings, messages: list[dict[str, str]]
) -> AsyncIterator[tuple[str, LlmUsage | None]]:
    """Yield (delta, usage). usage is None until the final chunk."""
    try:
        stream = await oai.chat.completions.create(
            model=s.llm_model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            # No `stop=` -- see module docstring.
        )
        async for chunk in stream:
            usage = None
            if getattr(chunk, "usage", None):
                usage = LlmUsage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                )
            delta = ""
            if chunk.choices:
                delta = chunk.choices[0].delta.content or ""
            if delta or usage:
                yield delta, usage
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, then sanitized
        log.error("llm call failed: %s", type(exc).__name__)
        raise LlmError("upstream model call failed") from None
