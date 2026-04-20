"""Tests for the OpenAI GPT provider adapter (Responses API).

Covers:
  - tool schema translation (ToolSchema → FunctionToolParam)
  - message translation (content blocks → input items)
  - response normalization (output items → LLMResponse)
  - build_gpt_reasoning_callable end-to-end wiring
"""

from __future__ import annotations

import json

import pytest

from phalanx.ci_fixer_v2.providers import openai_gpt as gpt
from phalanx.ci_fixer_v2.tools.base import ToolSchema


# ── translate_tool_schemas_to_responses ────────────────────────────────────
def test_translate_tool_schemas_flat_shape():
    """Responses API expects name/parameters at top level, not nested
    in a `function` wrapper."""
    schemas = [
        ToolSchema(
            name="read_file",
            description="read a file",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    out = gpt.translate_tool_schemas_to_responses(schemas)
    assert out == [
        {
            "type": "function",
            "name": "read_file",
            "description": "read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            "strict": False,
        }
    ]


# ── translate_messages_to_responses_input ──────────────────────────────────
def test_translate_plain_user_message():
    msgs = [{"role": "user", "content": "diagnose please"}]
    out = gpt.translate_messages_to_responses_input(msgs)
    assert out == [{"type": "message", "role": "user", "content": "diagnose please"}]


def test_translate_assistant_text_block():
    msgs = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "I will read the file."}],
        }
    ]
    out = gpt.translate_messages_to_responses_input(msgs)
    assert out == [
        {"type": "message", "role": "assistant", "content": "I will read the file."}
    ]


def test_translate_assistant_tool_use_becomes_function_call():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading."},
                {
                    "type": "tool_use",
                    "id": "call_001",
                    "name": "read_file",
                    "input": {"path": "app/api.py"},
                },
            ],
        }
    ]
    out = gpt.translate_messages_to_responses_input(msgs)
    # Text becomes a message item, tool_use becomes a function_call item.
    assert len(out) == 2
    assert out[0] == {"type": "message", "role": "assistant", "content": "Reading."}
    assert out[1] == {
        "type": "function_call",
        "call_id": "call_001",
        "name": "read_file",
        "arguments": json.dumps({"path": "app/api.py"}),
    }


def test_translate_user_tool_result_becomes_function_call_output():
    msgs = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_001",
                    "content": {"ok": True, "content": "def hello():\n"},
                }
            ],
        }
    ]
    out = gpt.translate_messages_to_responses_input(msgs)
    assert len(out) == 1
    item = out[0]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call_001"
    parsed = json.loads(item["output"])
    assert parsed == {"ok": True, "content": "def hello():\n"}


def test_translate_multiple_tool_results_in_one_user_message():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": {"ok": True}},
                {
                    "type": "tool_result",
                    "tool_use_id": "b",
                    "content": {"ok": False, "error": "x"},
                },
            ],
        }
    ]
    out = gpt.translate_messages_to_responses_input(msgs)
    assert len(out) == 2
    assert [i["call_id"] for i in out] == ["a", "b"]
    assert all(i["type"] == "function_call_output" for i in out)


def test_translate_system_messages_are_skipped():
    """System role is delivered via `instructions`, not in the input list."""
    msgs = [
        {"role": "system", "content": "you are a bot"},
        {"role": "user", "content": "hi"},
    ]
    out = gpt.translate_messages_to_responses_input(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "user"


def test_translate_assistant_tool_only_emits_function_call_alone():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t", "name": "grep", "input": {"pattern": "x"}}
            ],
        }
    ]
    out = gpt.translate_messages_to_responses_input(msgs)
    assert len(out) == 1
    assert out[0]["type"] == "function_call"
    assert out[0]["name"] == "grep"


def test_translate_preserves_order_when_text_follows_tool_use():
    """Text after a tool_use should appear AFTER the function_call."""
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "First I'll grep."},
                {"type": "tool_use", "id": "t", "name": "grep", "input": {"pattern": "foo"}},
                {"type": "text", "text": "Done."},
            ],
        }
    ]
    out = gpt.translate_messages_to_responses_input(msgs)
    assert [i["type"] for i in out] == ["message", "function_call", "message"]
    assert out[0]["content"] == "First I'll grep."
    assert out[2]["content"] == "Done."


# ── normalize_responses_api_response ───────────────────────────────────────
def test_normalize_simple_text_response():
    raw = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "all done"}],
            }
        ],
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    resp = gpt.normalize_responses_api_response(raw)
    assert resp.stop_reason == "end_turn"
    assert resp.text == "all done"
    assert resp.tool_uses == []
    assert resp.input_tokens == 12
    assert resp.output_tokens == 3
    assert resp.thinking_tokens == 0


def test_normalize_tool_call_response():
    raw = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call_42",
                "name": "read_file",
                "arguments": '{"path": "x.py"}',
            }
        ],
        "usage": {"input_tokens": 50, "output_tokens": 15},
    }
    resp = gpt.normalize_responses_api_response(raw)
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_uses) == 1
    assert resp.tool_uses[0].id == "call_42"
    assert resp.tool_uses[0].name == "read_file"
    assert resp.tool_uses[0].input == {"path": "x.py"}


def test_normalize_mixed_message_and_tool_call():
    raw = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Let me check."}],
            },
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "grep",
                "arguments": '{"pattern": "foo"}',
            },
        ],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }
    resp = gpt.normalize_responses_api_response(raw)
    assert resp.stop_reason == "tool_use"
    assert resp.text == "Let me check."
    assert len(resp.tool_uses) == 1


def test_normalize_reasoning_tokens_from_output_details():
    raw = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 2000},
        },
    }
    resp = gpt.normalize_responses_api_response(raw)
    assert resp.thinking_tokens == 2000


def test_normalize_skips_reasoning_items():
    raw = {
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "actual answer"}],
            },
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    resp = gpt.normalize_responses_api_response(raw)
    assert resp.text == "actual answer"
    assert resp.tool_uses == []


def test_normalize_handles_unparseable_tool_arguments():
    raw = {
        "output": [
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "grep",
                "arguments": "{not json",
            }
        ],
    }
    resp = gpt.normalize_responses_api_response(raw)
    assert len(resp.tool_uses) == 1
    assert resp.tool_uses[0].input.get("__parse_error__") is True


def test_normalize_empty_output_becomes_end_turn_empty_text():
    resp = gpt.normalize_responses_api_response({"output": []})
    assert resp.stop_reason == "end_turn"
    assert resp.text == ""


def test_normalize_refusal_surfaced_as_text():
    raw = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "refusal", "refusal": "cannot help with that"}
                ],
            }
        ],
    }
    resp = gpt.normalize_responses_api_response(raw)
    assert "[refusal]" in resp.text
    assert "cannot help" in resp.text


# ── build_gpt_reasoning_callable integration ──────────────────────────────
async def test_build_gpt_reasoning_callable_binds_instructions_and_tools(monkeypatch):
    captured = {}

    async def fake_call(
        model,
        input_items,
        tools,
        api_key,
        instructions,
        reasoning_effort=None,
        max_output_tokens=None,
    ):
        captured["model"] = model
        captured["api_key"] = api_key
        captured["instructions"] = instructions
        captured["reasoning_effort"] = reasoning_effort
        captured["tools"] = tools
        captured["input_items"] = input_items
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "pong"}],
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr(gpt, "_call_openai_api", fake_call)

    callable_ = gpt.build_gpt_reasoning_callable(
        model="gpt-5.4",
        api_key="sk-test",
        system_prompt="You are phalanx ci fixer.",
        tool_schemas=[
            ToolSchema(name="read_file", description="read", input_schema={"type": "object"})
        ],
        reasoning_effort="high",
    )
    resp = await callable_([{"role": "user", "content": "ping"}])

    assert resp.text == "pong"
    assert captured["model"] == "gpt-5.4"
    assert captured["api_key"] == "sk-test"
    assert captured["reasoning_effort"] == "high"
    assert captured["instructions"] == "You are phalanx ci fixer."
    # Tool schema is flat: {type, name, parameters, strict}
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["name"] == "read_file"
    # Input list is the translated messages (system is not in here — it's `instructions`)
    assert captured["input_items"] == [
        {"type": "message", "role": "user", "content": "ping"}
    ]


async def test_build_gpt_reasoning_callable_surfaces_provider_error(monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("HTTP 529 overloaded")

    monkeypatch.setattr(gpt, "_call_openai_api", boom)
    callable_ = gpt.build_gpt_reasoning_callable(
        model="gpt-5.4",
        api_key="sk-test",
        system_prompt="sys",
        tool_schemas=[],
    )
    resp = await callable_([{"role": "user", "content": "hi"}])
    # Provider error surfaces as end_turn + text so loop escalates cleanly.
    assert resp.stop_reason == "end_turn"
    assert "provider_error" in resp.text
    assert resp.tool_uses == []


async def test_build_gpt_reasoning_callable_omits_reasoning_when_none(monkeypatch):
    """Non-reasoning models can opt out by passing reasoning_effort=None."""
    captured = {}

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
            ],
            "usage": {},
        }

    monkeypatch.setattr(gpt, "_call_openai_api", fake_call)

    callable_ = gpt.build_gpt_reasoning_callable(
        model="gpt-4.1",
        api_key="sk",
        system_prompt="sys",
        tool_schemas=[],
        reasoning_effort=None,
    )
    await callable_([{"role": "user", "content": "x"}])
    assert captured["reasoning_effort"] is None
