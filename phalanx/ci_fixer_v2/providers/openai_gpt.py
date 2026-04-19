"""OpenAI (GPT-5.4) provider adapter.

Translates the agent loop's provider-neutral messages into OpenAI chat
completions format, calls the SDK, and normalizes the response into
`LLMResponse`. Uses two module-level seams so tests can drive the adapter
without an OpenAI SDK install or real HTTP:

  - `_call_openai_api(model, messages, tools, api_key, reasoning_effort)`
    → returns the raw SDK response dict
  - `translate_messages_to_openai(messages)` → pure-function translation

The two seams are keep the wire call isolated and the translation unit-
testable without mocks.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from phalanx.ci_fixer_v2.agent import LLMCallable, LLMResponse, LLMToolUse
from phalanx.ci_fixer_v2.tools.base import ToolSchema

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Pure translators (no I/O)
# ─────────────────────────────────────────────────────────────────────────────


def translate_tool_schemas_to_openai(schemas: list[ToolSchema]) -> list[dict[str, Any]]:
    """OpenAI's tool format: {type:'function', function:{name, description, parameters}}."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.input_schema,
            },
        }
        for s in schemas
    ]


def translate_messages_to_openai(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate Anthropic-style content-block messages into OpenAI chat format.

    - assistant with tool_use blocks -> assistant + tool_calls[]
    - user with tool_result blocks   -> sequence of {"role":"tool","tool_call_id":...}
    - plain text messages pass through, content flattened to string
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            out.append({"role": role, "content": ""})
            continue

        if role == "assistant":
            text_parts = []
            tool_calls = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        }
                    )
            msg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                msg["content"] = "\n".join(text_parts)
            else:
                msg["content"] = None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        if role == "user":
            # Each tool_result block becomes its own "tool" role message
            # in OpenAI's schema. Plain text blocks become a user message.
            text_parts = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, dict):
                        tool_text = json.dumps(inner)
                    else:
                        tool_text = str(inner) if inner is not None else ""
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": tool_text,
                        }
                    )
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
            continue

        # system / other: flatten to string
        out.append({"role": role, "content": str(content)})
    return out


def normalize_openai_response(raw: dict[str, Any]) -> LLMResponse:
    """Turn an OpenAI chat completions response into LLMResponse.

    Accepts both dict and SDK-object shapes — tests mostly pass dicts.
    """
    choices = raw.get("choices") or []
    if not choices:
        return LLMResponse(stop_reason="end_turn", text="")
    choice0 = choices[0]
    finish_reason = (choice0.get("finish_reason") or "stop") if isinstance(choice0, dict) else getattr(choice0, "finish_reason", "stop")
    message = choice0.get("message") if isinstance(choice0, dict) else getattr(choice0, "message", {})
    if not isinstance(message, dict):
        message = {
            "content": getattr(message, "content", None),
            "tool_calls": getattr(message, "tool_calls", None),
        }
    text = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []

    tool_uses: list[LLMToolUse] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tc_id = tc.get("id", "")
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
        else:
            tc_id = getattr(tc, "id", "")
            fn = getattr(tc, "function", None) or {}
            name = getattr(fn, "name", "") if not isinstance(fn, dict) else fn.get("name", "")
            raw_args = (
                getattr(fn, "arguments", "{}")
                if not isinstance(fn, dict)
                else fn.get("arguments", "{}")
            )
        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except (json.JSONDecodeError, TypeError):
            parsed_args = {"__parse_error__": True, "raw": raw_args}
        tool_uses.append(LLMToolUse(id=tc_id, name=name, input=parsed_args))

    stop_reason = "tool_use" if tool_uses else "end_turn" if finish_reason == "stop" else finish_reason

    usage = raw.get("usage") or {}
    return LLMResponse(
        stop_reason=stop_reason,
        text=text,
        tool_uses=tool_uses,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        thinking_tokens=int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Wire-call seam (tests patch this)
# ─────────────────────────────────────────────────────────────────────────────


async def _call_openai_api(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    api_key: str,
    reasoning_effort: str = "medium",
) -> dict[str, Any]:
    """Real OpenAI SDK call. Tests patch this to return a canned dict.

    Separated so unit tests never import `openai`. Production imports it
    lazily inside this function so the dependency is optional at import
    time (important for environments that only run Anthropic-side code).
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
    }
    # GPT-5.x reasoning knob — harmless on non-reasoning models that
    # ignore it, but included per spec §3 ("reasoning_effort: medium").
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    response = await client.chat.completions.create(**kwargs)
    # Coerce to dict for uniform downstream handling.
    return response.model_dump() if hasattr(response, "model_dump") else dict(response)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def build_gpt_reasoning_callable(
    model: str,
    api_key: str,
    system_prompt: str,
    tool_schemas: list[ToolSchema],
    reasoning_effort: str = "medium",
) -> LLMCallable:
    """Bind the main-agent LLM call into an LLMCallable that `run_ci_fix_v2`
    can consume directly. System prompt + tools are bound once; the
    returned callable takes only the current message list."""
    openai_tools = translate_tool_schemas_to_openai(tool_schemas)
    # System prompt always sits at the head of the messages list.
    system_message = {"role": "system", "content": system_prompt}

    async def _callable(messages: list[dict[str, Any]]) -> LLMResponse:
        translated = translate_messages_to_openai(messages)
        full_messages = [system_message, *translated]
        try:
            raw = await _call_openai_api(
                model=model,
                messages=full_messages,
                tools=openai_tools,
                api_key=api_key,
                reasoning_effort=reasoning_effort,
            )
        except Exception as exc:
            log.error("v2.providers.openai.call_failed", error=str(exc))
            # Surface the error as a 'max_tokens' stop_reason with no
            # tool_uses — the loop will treat that as implicit stop and
            # escalate cleanly. Future: dedicated "provider_error" reason.
            return LLMResponse(
                stop_reason="max_tokens",
                text=f"provider_error: {exc}",
            )
        return normalize_openai_response(raw)

    return _callable
