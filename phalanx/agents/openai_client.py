"""
OpenAI synchronous client wrapper with retry logic and JSON parsing.

Used by the PromptEnricher pipeline (IntentExtractor, PhaseGenerator,
DryRunValidator) which need synchronous GPT-4.1 calls.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from phalanx.config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds between retries


class OpenAIClient:
    """
    Thin synchronous wrapper around openai.OpenAI for JSON-returning calls.

    Retries on transient errors (rate limits, timeouts, network issues).
    Raises ValueError on invalid JSON response.
    Raises RuntimeError after max retries exhausted.
    """

    def __init__(
        self,
        model: str | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        import openai  # noqa: PLC0415

        self._client = openai.OpenAI(api_key=settings.openai_api_key)
        self._model = model or settings.openai_model_default
        self._max_retries = max_retries

    def call(
        self,
        messages: list[dict[str, str]],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """
        Call the OpenAI chat completions API and return parsed JSON.

        Retries on exceptions up to max_retries times.
        Raises ValueError if response is not valid JSON.
        Raises RuntimeError if all retries are exhausted.
        """
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=all_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                content = response.choices[0].message.content or ""

                # Strip markdown fences if present
                stripped = content.strip()
                if stripped.startswith("```"):
                    lines = stripped.splitlines()
                    inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
                    stripped = inner.strip()

                try:
                    return json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON from OpenAI: {stripped[:200]}") from exc

            except ValueError:
                raise  # don't retry JSON parse errors
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "openai_client.retry",
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    error=str(exc),
                )
                if attempt < self._max_retries - 1:
                    time.sleep(_RETRY_DELAY)

        raise RuntimeError(
            f"OpenAI call failed after {self._max_retries} retries: {last_exc}"
        )
