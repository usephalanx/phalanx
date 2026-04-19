"""Tests for the Anthropic Sonnet provider adapter."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2.config import SONNET_THINKING_BUDGET
from phalanx.ci_fixer_v2.providers import anthropic_sonnet as sonnet
from phalanx.ci_fixer_v2.tools.base import ToolSchema


def test_translate_tool_schemas_to_anthropic_shape():
    schemas = [
        ToolSchema(
            name="apply_patch",
            description="apply a diff",
            input_schema={"type": "object", "properties": {"diff": {"type": "string"}}},
        )
    ]
    out = sonnet.translate_tool_schemas_to_anthropic(schemas)
    assert out == [
        {
            "name": "apply_patch",
            "description": "apply a diff",
            "input_schema": {"type": "object", "properties": {"diff": {"type": "string"}}},
        }
    ]


def test_normalize_sonnet_text_only():
    raw = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "fixed"}],
        "usage": {"input_tokens": 200, "output_tokens": 30},
    }
    resp = sonnet.normalize_anthropic_response(raw)
    assert resp.stop_reason == "end_turn"
    assert resp.text == "fixed"
    assert resp.tool_uses == []
    assert resp.input_tokens == 200
    assert resp.output_tokens == 30


def test_normalize_sonnet_tool_use():
    raw = {
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "applying..."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "apply_patch",
                "input": {"diff": "..."},
            },
        ],
        "usage": {"input_tokens": 500, "output_tokens": 120},
    }
    resp = sonnet.normalize_anthropic_response(raw)
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_uses) == 1
    assert resp.tool_uses[0].id == "toolu_1"
    assert resp.tool_uses[0].name == "apply_patch"
    assert resp.tool_uses[0].input == {"diff": "..."}
    assert resp.text == "applying..."


def test_normalize_sonnet_with_thinking_tokens():
    raw = {
        "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": "let me consider..."}],
        "usage": {"input_tokens": 100, "output_tokens": 50, "thinking_tokens": 1500},
    }
    resp = sonnet.normalize_anthropic_response(raw)
    assert resp.thinking_tokens == 1500


def test_normalize_handles_empty_content():
    raw = {"stop_reason": "end_turn", "content": [], "usage": {}}
    resp = sonnet.normalize_anthropic_response(raw)
    assert resp.text == ""
    assert resp.tool_uses == []


async def test_build_sonnet_coder_callable_passes_thinking_budget(monkeypatch):
    captured = {}

    async def fake_call(
        model,
        messages,
        tools,
        api_key,
        max_tokens,
        thinking_budget,
        system_prompt,
    ):
        captured.update(
            {
                "model": model,
                "api_key": api_key,
                "max_tokens": max_tokens,
                "thinking_budget": thinking_budget,
                "system_prompt": system_prompt,
                "tools": tools,
                "messages": messages,
            }
        )
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(sonnet, "_call_anthropic_api", fake_call)

    callable_ = sonnet.build_sonnet_coder_callable(
        model="claude-sonnet-4-6",
        api_key="sk-ant-test",
        system_prompt="coder sys",
        tool_schemas=[
            ToolSchema(name="read_file", description="", input_schema={"type": "object"})
        ],
    )
    resp = await callable_(
        [{"role": "user", "content": "apply the patch"}]
    )
    assert resp.text == "done"
    assert captured["model"] == "claude-sonnet-4-6"
    assert captured["api_key"] == "sk-ant-test"
    assert captured["thinking_budget"] == SONNET_THINKING_BUDGET
    assert captured["system_prompt"] == "coder sys"
    # Tool schema translated to Anthropic shape.
    assert captured["tools"][0]["name"] == "read_file"


async def test_build_sonnet_coder_callable_surfaces_provider_error(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(sonnet, "_call_anthropic_api", boom)
    callable_ = sonnet.build_sonnet_coder_callable(
        model="claude-sonnet-4-6",
        api_key="sk",
        system_prompt="sys",
        tool_schemas=[],
    )
    resp = await callable_([{"role": "user", "content": "x"}])
    assert "provider_error" in resp.text
    assert resp.tool_uses == []
