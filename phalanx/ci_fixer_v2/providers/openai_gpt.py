"""OpenAI (GPT-5.4) provider adapter — Responses API.

Migrated from Chat Completions → Responses API because gpt-5.x models
reject the `{tools + reasoning_effort + /v1/chat/completions}` combination
with HTTP 400:
    "Function tools with reasoning_effort are not supported for gpt-5.4
     in /v1/chat/completions. Please use /v1/responses instead."

OpenAI's guidance is explicit: for reasoning + tool use, use
`client.responses.create`. This adapter translates the agent loop's
provider-neutral content-block messages into the Responses API `input`
list, calls `responses.create`, and normalizes `response.output` back
into LLMResponse.

Stateless by design — each call re-sends the full message history. We
do NOT use `previous_response_id` chaining; the cost savings would be
modest and the added complexity (tracking response ids across turns,
handling retry after failures) isn't worth it for MVP. This can be
added as an optimization once we have live traffic data.

Test seams (module-level; tests patch these directly):
  - `_call_openai_api(...)` — the SDK call
  - `translate_messages_to_responses_input(...)` — pure translation
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog

from phalanx.ci_fixer_v2.agent import LLMCallable, LLMResponse, LLMToolUse
from phalanx.ci_fixer_v2.tools.base import ToolSchema

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Pure translators (no I/O)
# ─────────────────────────────────────────────────────────────────────────────


def translate_tool_schemas_to_responses(
    schemas: list[ToolSchema],
) -> list[dict[str, Any]]:
    """Responses API tool shape (FunctionToolParam):
        {type: "function", name, description, parameters, strict}

    Note: flatter than Chat Completions — no nested `function: {...}`
    wrapper. `strict` is required by the SDK type but can be False.
    """
    return [
        {
            "type": "function",
            "name": s.name,
            "description": s.description,
            "parameters": s.input_schema,
            "strict": False,
        }
        for s in schemas
    ]


def translate_messages_to_responses_input(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate Anthropic-style content-block messages into the
    Responses API `input` list.

    Mapping:
      user text          → {type: "message", role: "user", content}
      assistant text     → {type: "message", role: "assistant", content}
      assistant tool_use → {type: "function_call", call_id, name, arguments}
      user tool_result   → {type: "function_call_output", call_id, output}

    System messages are NOT included here — the caller passes them as
    the `instructions` parameter to `responses.create`.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")

        # System messages skip — they go into `instructions` at the call site.
        if role == "system":
            continue

        if isinstance(content, str):
            out.append({"type": "message", "role": role, "content": content})
            continue

        if not isinstance(content, list):
            # Unknown shape — coerce to empty user message so we don't crash.
            out.append({"type": "message", "role": role, "content": ""})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    # Emit the message text first (if any), then the
                    # function_call as a separate item. Items land in
                    # order which is what the Responses API expects.
                    if text_parts:
                        out.append(
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": "\n".join(text_parts),
                            }
                        )
                        text_parts = []
                    out.append(
                        {
                            "type": "function_call",
                            "call_id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        }
                    )
            if text_parts:
                out.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "\n".join(text_parts),
                    }
                )
            continue

        if role == "user":
            text_parts = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, dict):
                        output_text = json.dumps(inner)
                    else:
                        output_text = str(inner) if inner is not None else ""
                    out.append(
                        {
                            "type": "function_call_output",
                            "call_id": block.get("tool_use_id", ""),
                            "output": output_text,
                        }
                    )
            if text_parts:
                out.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": "\n".join(text_parts),
                    }
                )
            continue

        # Any other role (e.g. "developer") falls through as a plain message.
        out.append({"type": "message", "role": role, "content": str(content)})
    return out


def normalize_responses_api_response(raw: dict[str, Any]) -> LLMResponse:
    """Walk `response.output` and produce LLMResponse.

    `response.output` is a list of ResponseOutputItem items:
      - {type: "message", content: [{type: "output_text", text: ...}, ...]}
      - {type: "function_call", call_id, name, arguments}   (strings)
      - {type: "reasoning", ...}                             (skipped)

    stop_reason:
      - "tool_use" when any function_call items present
      - otherwise "end_turn"
    """
    output = raw.get("output") or []
    text_parts: list[str] = []
    tool_uses: list[LLMToolUse] = []

    for item in output:
        itype = item.get("type") if isinstance(item, dict) else getattr(item, "type", "")
        if itype == "message":
            content = (
                item.get("content") if isinstance(item, dict) else getattr(item, "content", [])
            )
            if not content:
                continue
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", "")
                if btype == "output_text":
                    text_parts.append(
                        block.get("text", "")
                        if isinstance(block, dict)
                        else getattr(block, "text", "")
                    )
                elif btype == "refusal":
                    # Surface refusals as text so the loop can decide to escalate.
                    refusal = (
                        block.get("refusal", "")
                        if isinstance(block, dict)
                        else getattr(block, "refusal", "")
                    )
                    text_parts.append(f"[refusal] {refusal}")
        elif itype == "function_call":
            call_id = (
                item.get("call_id")
                if isinstance(item, dict)
                else getattr(item, "call_id", "")
            ) or (item.get("id") if isinstance(item, dict) else getattr(item, "id", ""))
            name = item.get("name", "") if isinstance(item, dict) else getattr(item, "name", "")
            raw_args = (
                item.get("arguments", "{}")
                if isinstance(item, dict)
                else getattr(item, "arguments", "{}")
            )
            try:
                parsed_args = (
                    json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                )
            except (json.JSONDecodeError, TypeError):
                parsed_args = {"__parse_error__": True, "raw": raw_args}
            tool_uses.append(LLMToolUse(id=call_id, name=name, input=parsed_args))
        # Skip reasoning items — tokens are reported in usage.

    stop_reason = "tool_use" if tool_uses else "end_turn"

    usage = raw.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    # Reasoning tokens live under `output_tokens_details.reasoning_tokens`
    # in the Responses API usage block.
    output_details = (
        usage.get("output_tokens_details")
        if isinstance(usage, dict)
        else None
    ) or {}
    reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)

    return LLMResponse(
        stop_reason=stop_reason,
        text="\n".join(text_parts) if text_parts else "",
        tool_uses=tool_uses,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=reasoning_tokens,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Wire-call seam (tests patch this)
# ─────────────────────────────────────────────────────────────────────────────


_LLM_CALL_TIMEOUT_SECONDS: float = 180.0
"""Hard wall-clock timeout on a single LLM request, enforced via
asyncio.wait_for. SDK's `timeout=` parameter is an httpx read timeout
that resets on every byte; reasoning/tool-use responses can trickle
for 20+ minutes without ever hitting it. asyncio cancellation is the
only bound we can trust."""


async def _call_openai_api(
    model: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    api_key: str,
    instructions: str,
    reasoning_effort: str | None = "medium",
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Real OpenAI SDK call. Tests patch this to return a canned dict.

    Uses the Responses API (`client.responses.create`) which is the
    supported endpoint for reasoning + tool use on gpt-5.x / o-series.
    """
    from openai import AsyncOpenAI

    # max_retries=0 so the SDK can't silently stack retries past our
    # wall-clock budget. The asyncio.wait_for below is the only timeout
    # we trust; SDK-internal retry+backoff happily burns 10+ minutes
    # without yielding control back to user-level timeouts.
    client = AsyncOpenAI(
        api_key=api_key, timeout=_LLM_CALL_TIMEOUT_SECONDS, max_retries=0
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "instructions": instructions,
        "tools": tools,
        # Don't chain via previous_response_id — stateless for simplicity.
        "store": False,
    }
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    if max_output_tokens is not None:
        kwargs["max_output_tokens"] = max_output_tokens

    log.info(
        "v2.providers.openai.call_start",
        model=model,
        input_items=len(input_items),
        reasoning_effort=reasoning_effort,
    )
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            client.responses.create(**kwargs),
            timeout=_LLM_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        log.error(
            "v2.providers.openai.call_timeout",
            model=model,
            elapsed_s=round(time.monotonic() - started, 2),
            limit_s=_LLM_CALL_TIMEOUT_SECONDS,
        )
        raise TimeoutError(
            f"openai call exceeded {_LLM_CALL_TIMEOUT_SECONDS}s wall-clock"
        ) from exc
    log.info(
        "v2.providers.openai.call_end",
        model=model,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    return response.model_dump() if hasattr(response, "model_dump") else dict(response)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def build_gpt_reasoning_callable(
    model: str,
    api_key: str,
    system_prompt: str,
    tool_schemas: list[ToolSchema],
    reasoning_effort: str | None = "medium",
    max_output_tokens: int | None = None,
) -> LLMCallable:
    """Bind GPT-5.x Responses API call into an LLMCallable the agent
    loop can consume. system_prompt + tools + reasoning settings are
    bound once; the returned callable takes only the current message
    list.
    """
    responses_tools = translate_tool_schemas_to_responses(tool_schemas)

    async def _callable(messages: list[dict[str, Any]]) -> LLMResponse:
        input_items = translate_messages_to_responses_input(messages)
        try:
            raw = await _call_openai_api(
                model=model,
                input_items=input_items,
                tools=responses_tools,
                api_key=api_key,
                instructions=system_prompt,
                reasoning_effort=reasoning_effort,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            log.error("v2.providers.openai.call_failed", error=str(exc))
            # Surface as end_turn + text so the loop escalates cleanly
            # (IMPLICIT_STOP) rather than crashing.
            return LLMResponse(
                stop_reason="end_turn",
                text=f"provider_error: {exc}",
            )
        return normalize_responses_api_response(raw)

    return _callable
