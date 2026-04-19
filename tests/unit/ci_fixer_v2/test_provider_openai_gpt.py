"""Tests for the OpenAI GPT provider adapter (translators + normalizers)."""

from __future__ import annotations

import json

import pytest

from phalanx.ci_fixer_v2.providers import openai_gpt as gpt
from phalanx.ci_fixer_v2.tools.base import ToolSchema


# ── translate_tool_schemas_to_openai ──────────────────────────────────────
def test_translate_tool_schemas_to_openai_shape():
    schemas = [
        ToolSchema(
            name="read_file",
            description="read",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    out = gpt.translate_tool_schemas_to_openai(schemas)
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "read",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]


# ── translate_messages_to_openai ──────────────────────────────────────────
def test_translate_assistant_with_tool_use_becomes_tool_calls():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll read the file."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "read_file",
                    "input": {"path": "app/api.py"},
                },
            ],
        }
    ]
    out = gpt.translate_messages_to_openai(msgs)
    assert len(out) == 1
    m = out[0]
    assert m["role"] == "assistant"
    assert m["content"] == "I'll read the file."
    assert m["tool_calls"] == [
        {
            "id": "t1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "app/api.py"}),
            },
        }
    ]


def test_translate_user_tool_result_becomes_tool_role_message():
    msgs = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": {"ok": True, "content": "def hello():\n"},
                }
            ],
        }
    ]
    out = gpt.translate_messages_to_openai(msgs)
    assert len(out) == 1
    m = out[0]
    assert m["role"] == "tool"
    assert m["tool_call_id"] == "t1"
    # content is a JSON-serialized dump of the tool_result content.
    assert "def hello" in m["content"]


def test_translate_multiple_tool_results_in_one_user_message():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": {"ok": True}},
                {"type": "tool_result", "tool_use_id": "b", "content": {"ok": False, "error": "x"}},
            ],
        }
    ]
    out = gpt.translate_messages_to_openai(msgs)
    assert len(out) == 2
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "a"
    assert out[1]["tool_call_id"] == "b"


def test_translate_plain_string_content_passes_through():
    msgs = [{"role": "user", "content": "Fix the lint error."}]
    out = gpt.translate_messages_to_openai(msgs)
    assert out == [{"role": "user", "content": "Fix the lint error."}]


def test_translate_assistant_text_only_sets_content_only():
    msgs = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Done."}],
        }
    ]
    out = gpt.translate_messages_to_openai(msgs)
    assert out == [{"role": "assistant", "content": "Done."}]


def test_translate_assistant_tool_only_sets_tool_calls_no_content():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t", "name": "grep", "input": {"pattern": "x"}}
            ],
        }
    ]
    out = gpt.translate_messages_to_openai(msgs)
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["function"]["name"] == "grep"


# ── normalize_openai_response ─────────────────────────────────────────────
def test_normalize_simple_text_response():
    raw = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "hello", "tool_calls": []}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    resp = gpt.normalize_openai_response(raw)
    assert resp.stop_reason == "end_turn"
    assert resp.text == "hello"
    assert resp.tool_uses == []
    assert resp.input_tokens == 10
    assert resp.output_tokens == 2


def test_normalize_tool_calls_response():
    raw = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "x.py"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 15},
    }
    resp = gpt.normalize_openai_response(raw)
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_uses) == 1
    assert resp.tool_uses[0].name == "read_file"
    assert resp.tool_uses[0].input == {"path": "x.py"}
    assert resp.input_tokens == 50
    assert resp.output_tokens == 15


def test_normalize_handles_unparseable_tool_arguments():
    raw = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "grep", "arguments": "{not json"},
                        }
                    ],
                },
            }
        ],
    }
    resp = gpt.normalize_openai_response(raw)
    # Error is surfaced in the input payload rather than raising.
    assert resp.tool_uses[0].input.get("__parse_error__") is True


def test_normalize_captures_reasoning_tokens_when_present():
    raw = {
        "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 2000},
        },
    }
    resp = gpt.normalize_openai_response(raw)
    assert resp.thinking_tokens == 2000


def test_normalize_empty_choices_becomes_end_turn_empty_text():
    resp = gpt.normalize_openai_response({"choices": []})
    assert resp.stop_reason == "end_turn"
    assert resp.text == ""


# ── build_gpt_reasoning_callable integration ──────────────────────────────
async def test_build_gpt_reasoning_callable_binds_system_and_tools(monkeypatch):
    captured = {}

    async def fake_call(model, messages, tools, api_key, reasoning_effort):
        captured["model"] = model
        captured["api_key"] = api_key
        captured["reasoning_effort"] = reasoning_effort
        captured["tools"] = tools
        captured["messages"] = messages
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "pong", "tool_calls": []}}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }

    monkeypatch.setattr(gpt, "_call_openai_api", fake_call)

    callable_ = gpt.build_gpt_reasoning_callable(
        model="gpt-5.4",
        api_key="sk-test",
        system_prompt="You are phalanx ci fixer.",
        tool_schemas=[
            ToolSchema(name="read_file", description="", input_schema={"type": "object"})
        ],
        reasoning_effort="high",
    )
    resp = await callable_(
        [{"role": "user", "content": "ping"}]
    )
    assert resp.text == "pong"
    assert captured["model"] == "gpt-5.4"
    assert captured["api_key"] == "sk-test"
    assert captured["reasoning_effort"] == "high"
    # System prompt inserted at the head of the message list.
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == "You are phalanx ci fixer."
    # Tool schema was translated to OpenAI shape.
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "read_file"


async def test_build_gpt_reasoning_callable_surfaces_provider_error(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("HTTP 529 overloaded")

    monkeypatch.setattr(gpt, "_call_openai_api", boom)
    callable_ = gpt.build_gpt_reasoning_callable(
        model="gpt-5.4",
        api_key="sk-test",
        system_prompt="sys",
        tool_schemas=[],
    )
    resp = await callable_([{"role": "user", "content": "hi"}])
    # Provider error becomes a non-fatal response that the loop treats
    # as implicit stop → escalates cleanly rather than crashing.
    assert "provider_error" in resp.text
    assert resp.tool_uses == []
