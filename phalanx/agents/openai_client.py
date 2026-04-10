"""
OpenAI GPT-4o client — used exclusively by PromptEnricher.

Thin wrapper around the openai SDK with:
  - JSON mode enforced (response_format: json_object)
  - Structured logging
  - Basic retry on rate limit / timeout
  - Synchronous (called from async code via asyncio.get_event_loop().run_in_executor)
"""
from __future__ import annotations

import json
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Max retries on transient errors (rate limit, timeout)
_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 5


class OpenAIClient:
    """
    Synchronous OpenAI client.

    Usage:
        # JSON mode (Commander, Planner, QA, Reviewer, Release):
        client = OpenAIClient(model="gpt-5.4-pro")
        result = client.call(messages=[...], system="...", max_tokens=4096)
        # result is a parsed dict (JSON mode guaranteed)

        # Text mode (free-form responses):
        text = client.call_text(messages=[...], system="...", max_tokens=4096)

    Pass model= explicitly to use the reasoning model. Omit to use
    openai_model_default (gpt-4o) — preserves existing PromptEnricher behaviour.
    """

    def __init__(self, model: str | None = None) -> None:
        from openai import OpenAI  # noqa: PLC0415

        from phalanx.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._model = model or settings.openai_model_default
        self._log = log.bind(model=self._model)

    def call(
        self,
        messages: list[dict[str, str]],
        system: str,
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        """
        Call GPT-4o with JSON response mode. Always returns a parsed dict.

        Raises ValueError if JSON parsing fails after all retries.
        """
        full_messages = [{"role": "system", "content": system}] + messages

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=full_messages,  # type: ignore[arg-type]
                    max_tokens=max_tokens,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content or "{}"
                usage = response.usage

                self._log.debug(
                    "openai.call.done",
                    attempt=attempt,
                    input_tokens=usage.prompt_tokens if usage else 0,
                    output_tokens=usage.completion_tokens if usage else 0,
                )
                return json.loads(content)

            except json.JSONDecodeError as exc:
                self._log.error("openai.json_parse_failed", attempt=attempt, error=str(exc))
                raise ValueError(f"GPT-4o returned invalid JSON: {exc}") from exc

            except Exception as exc:
                last_exc = exc
                self._log.warning(
                    "openai.call.error",
                    attempt=attempt,
                    error=str(exc),
                    retrying=attempt < _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SECONDS * attempt)

        raise RuntimeError(f"OpenAI call failed after {_MAX_RETRIES} attempts: {last_exc}") from last_exc

    def call_text(
        self,
        messages: list[dict[str, str]],
        system: str,
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> str:
        """
        Call OpenAI without JSON mode — returns raw text string.
        Use when the response is free-form (not guaranteed JSON).
        """
        full_messages = [{"role": "system", "content": system}] + messages

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=full_messages,  # type: ignore[arg-type]
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                content = response.choices[0].message.content or ""
                usage = response.usage

                self._log.debug(
                    "openai.call_text.done",
                    attempt=attempt,
                    input_tokens=usage.prompt_tokens if usage else 0,
                    output_tokens=usage.completion_tokens if usage else 0,
                )
                return content

            except Exception as exc:
                last_exc = exc
                self._log.warning(
                    "openai.call_text.error",
                    attempt=attempt,
                    error=str(exc),
                    retrying=attempt < _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SECONDS * attempt)

        raise RuntimeError(f"OpenAI call_text failed after {_MAX_RETRIES} attempts: {last_exc}") from last_exc
