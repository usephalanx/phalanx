"""Anthropic (Claude Sonnet 4.6) provider adapter for the coder subagent.

Input messages are already in Anthropic's native format (the agent loop
uses it natively), so translation is essentially a pass-through. We
still add normalization on the response side and a wire-call seam for
tests.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse
from phalanx.ci_fixer_v2.coder_subagent import SonnetCallable
from phalanx.ci_fixer_v2.config import SONNET_THINKING_BUDGET
from phalanx.ci_fixer_v2.tools.base import ToolSchema

log = structlog.get_logger(__name__)


_DEFAULT_MAX_TOKENS: int = 16384
"""Sonnet output ceiling for a single coder turn. Raised from 8096
after a coverage-case simulate run silently truncated the coder's
apply_patch diff mid-function (ends of test bodies dropped), which
git apply still accepted, which shipped a broken file to CI. 16k is
generous for any single-file patch; larger changes should be split
into multiple delegate_to_coder rounds, not crammed into one turn."""

_LLM_CALL_TIMEOUT_SECONDS: float = 300.0
"""Hard wall-clock timeout on a single Sonnet request, enforced via
asyncio.wait_for. We do NOT rely on the Anthropic SDK's own `timeout=`
parameter: that becomes an httpx read timeout, which resets on every
byte (including server-sent keep-alives during extended thinking), so
it can silently run for 20+ minutes even when set to 180s. asyncio
cancellation is the only ironclad cap.

Raised from 180s → 300s on 2026-04-28 after bug #10: the v3 coverage
cell on testbed PR #17 timed out at 186s with coder_attempts=0 (the
first turn of multi-file test-writing didn't finish). lint/test_fail/
flake cells all comfortably finish in <30s coder time; coverage and
similar multi-file code-gen tasks need ~1.5-2× that headroom. 300s
keeps the per-turn cap bounded (3-turn loop = 15min hard ceiling)
while covering realistic worst-case extended-thinking turns.

If a future caller needs a different value, refactor build_sonnet_coder_callable
to accept a timeout_seconds= parameter rather than rebumping this constant."""


def translate_tool_schemas_to_anthropic(
    schemas: list[ToolSchema],
) -> list[dict[str, Any]]:
    """Anthropic's tool format: {name, description, input_schema}."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "input_schema": s.input_schema,
        }
        for s in schemas
    ]


def normalize_anthropic_response(raw: dict[str, Any]) -> LLMResponse:
    """Turn an Anthropic Messages API response into LLMResponse."""
    stop_reason = raw.get("stop_reason") or "end_turn"
    content = raw.get("content") or []
    text_parts: list[str] = []
    tool_uses: list[LLMToolUse] = []
    thinking_tokens_from_blocks: int = 0

    for block in content:
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", "")
        if btype == "text":
            text_parts.append(
                block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
            )
        elif btype == "tool_use":
            tu_id = block.get("id", "") if isinstance(block, dict) else getattr(block, "id", "")
            tu_name = (
                block.get("name", "") if isinstance(block, dict) else getattr(block, "name", "")
            )
            tu_input = (
                block.get("input", {}) if isinstance(block, dict) else getattr(block, "input", {})
            )
            tool_uses.append(LLMToolUse(id=tu_id, name=tu_name, input=tu_input or {}))
        elif btype == "thinking":
            # Extended thinking blocks report their own size; token count
            # also shows up in usage, so avoid double-counting.
            pass

    usage = raw.get("usage") or {}
    # Anthropic returns `input_tokens`, `output_tokens`; thinking budget
    # is counted inside output_tokens unless the model returns a
    # separate `thinking_tokens` field (newer models).
    thinking_tokens = int(
        usage.get("thinking_tokens")
        or usage.get("cache_creation_input_tokens")  # placeholder — not thinking
        or 0
    )
    if thinking_tokens_from_blocks:
        thinking_tokens = max(thinking_tokens, thinking_tokens_from_blocks)

    return LLMResponse(
        stop_reason=stop_reason,
        text="\n".join(text_parts) if text_parts else "",
        tool_uses=tool_uses,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        thinking_tokens=thinking_tokens,
    )


async def _call_anthropic_api(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    api_key: str,
    max_tokens: int,
    thinking_budget: int,
    system_prompt: str,
) -> dict[str, Any]:
    """Real Anthropic SDK call. Tests patch this to return a canned dict."""
    from anthropic import AsyncAnthropic

    # max_retries=0 — SDK retries can stack past our asyncio.wait_for
    # budget silently. We'd rather see a TimeoutError / one hard fail
    # than a 20-minute black box.
    client = AsyncAnthropic(api_key=api_key, timeout=_LLM_CALL_TIMEOUT_SECONDS, max_retries=0)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
        "tools": tools,
    }
    if thinking_budget and thinking_budget > 0:
        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget,
        }
    log.info(
        "v2.providers.anthropic.call_start",
        model=model,
        messages=len(messages),
        max_tokens=max_tokens,
        thinking_budget=thinking_budget,
    )
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            client.messages.create(**kwargs),
            timeout=_LLM_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        log.error(
            "v2.providers.anthropic.call_timeout",
            model=model,
            elapsed_s=round(time.monotonic() - started, 2),
            limit_s=_LLM_CALL_TIMEOUT_SECONDS,
        )
        raise TimeoutError(
            f"anthropic call exceeded {_LLM_CALL_TIMEOUT_SECONDS}s wall-clock"
        ) from exc
    log.info(
        "v2.providers.anthropic.call_end",
        model=model,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    return response.model_dump() if hasattr(response, "model_dump") else dict(response)


def build_sonnet_coder_callable(
    model: str,
    api_key: str,
    system_prompt: str,
    tool_schemas: list[ToolSchema],
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    thinking_budget: int = SONNET_THINKING_BUDGET,
) -> SonnetCallable:
    """Bind the coder-subagent LLM call into a SonnetCallable that the
    `coder_subagent` loop can consume directly."""
    anthropic_tools = translate_tool_schemas_to_anthropic(tool_schemas)

    async def _callable(messages: list[dict[str, Any]]) -> LLMResponse:
        try:
            raw = await _call_anthropic_api(
                model=model,
                messages=messages,
                tools=anthropic_tools,
                api_key=api_key,
                max_tokens=max_tokens,
                thinking_budget=thinking_budget,
                system_prompt=system_prompt,
            )
        except Exception as exc:
            log.error("v2.providers.anthropic.call_failed", error=str(exc))
            return LLMResponse(
                stop_reason="end_turn",
                text=f"provider_error: {exc}",
            )
        return normalize_anthropic_response(raw)

    return _callable
