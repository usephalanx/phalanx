"""Unit tests for the record/replay harness."""

from __future__ import annotations

import pytest

from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse
from phalanx.ci_fixer_v2.replay import (
    Fixture,
    LLMCallRecord,
    LLMRecorder,
    LLMReplayer,
    ReplayDriftError,
    ToolCallRecord,
    _deserialize_llm_response,
    _serialize_llm_response,
    tool_replay_patcher,
)


class TestLLMResponseSerde:
    def test_roundtrip_end_turn_with_text(self):
        r = LLMResponse(
            stop_reason="end_turn",
            text="done",
            input_tokens=10,
            output_tokens=20,
            thinking_tokens=5,
        )
        back = _deserialize_llm_response(_serialize_llm_response(r))
        assert back.stop_reason == r.stop_reason
        assert back.text == r.text
        assert back.input_tokens == r.input_tokens
        assert back.output_tokens == r.output_tokens
        assert back.thinking_tokens == r.thinking_tokens

    def test_roundtrip_tool_use(self):
        r = LLMResponse(
            stop_reason="tool_use",
            tool_uses=[
                LLMToolUse(id="t1", name="fetch_ci_log", input={"job_id": "42"})
            ],
        )
        back = _deserialize_llm_response(_serialize_llm_response(r))
        assert len(back.tool_uses) == 1
        assert back.tool_uses[0].id == "t1"
        assert back.tool_uses[0].name == "fetch_ci_log"
        assert back.tool_uses[0].input == {"job_id": "42"}


class TestLLMRecorder:
    async def test_wraps_and_captures_in_order(self):
        inner_responses = [
            LLMResponse(stop_reason="tool_use", text="first"),
            LLMResponse(stop_reason="end_turn", text="done"),
        ]
        inner_idx = {"i": 0}

        async def inner(_messages):
            r = inner_responses[inner_idx["i"]]
            inner_idx["i"] += 1
            return r

        recorder = LLMRecorder(role="main")
        wrapped = recorder.wrap(inner)

        r1 = await wrapped([{"role": "user", "content": "hi"}])
        r2 = await wrapped([{"role": "user", "content": "hi"}, {"role": "assistant"}])

        assert r1.text == "first"
        assert r2.text == "done"
        assert len(recorder.calls) == 2
        assert recorder.calls[0].turn_index == 0
        assert recorder.calls[0].role == "main"
        assert recorder.calls[0].messages_len == 1
        assert recorder.calls[1].turn_index == 1
        assert recorder.calls[1].messages_len == 2


class TestLLMReplayer:
    async def test_serves_canned_in_order(self):
        calls = [
            LLMCallRecord(
                role="main",
                turn_index=0,
                messages_len=1,
                response={"stop_reason": "tool_use", "text": "x", "tool_uses": []},
            ),
            LLMCallRecord(
                role="main",
                turn_index=1,
                messages_len=3,
                response={"stop_reason": "end_turn", "text": "y", "tool_uses": []},
            ),
        ]
        rep = LLMReplayer(role="main", calls=calls)
        r1 = await rep([{}])
        r2 = await rep([{}, {}])
        assert r1.text == "x"
        assert r2.text == "y"

    async def test_filters_by_role(self):
        calls = [
            LLMCallRecord(
                role="coder",
                turn_index=0,
                messages_len=1,
                response={"stop_reason": "end_turn", "text": "coder_call", "tool_uses": []},
            ),
            LLMCallRecord(
                role="main",
                turn_index=0,
                messages_len=1,
                response={"stop_reason": "end_turn", "text": "main_call", "tool_uses": []},
            ),
        ]
        rep_main = LLMReplayer(role="main", calls=calls)
        r = await rep_main([{}])
        assert r.text == "main_call"

    async def test_drift_error_when_past_end(self):
        rep = LLMReplayer(
            role="main",
            calls=[
                LLMCallRecord(
                    role="main",
                    turn_index=0,
                    messages_len=1,
                    response={"stop_reason": "end_turn", "text": "x", "tool_uses": []},
                )
            ],
        )
        await rep([{}])  # fine
        with pytest.raises(ReplayDriftError, match="ran past recorded"):
            await rep([{}])


class TestToolReplayPatcher:
    async def test_serves_canned_results_in_order(self):
        calls = [
            ToolCallRecord(
                turn=0,
                tool_name="fetch_ci_log",
                tool_input={"job_id": "1"},
                tool_result={"log": "x", "lines": 100},
                error=None,
            ),
            ToolCallRecord(
                turn=0,
                tool_name="read_file",
                tool_input={"path": "a.py"},
                tool_result={"content": "print(1)"},
                error=None,
            ),
        ]
        handler, cursor = tool_replay_patcher(calls)

        r1 = await handler("fetch_ci_log", None, {"job_id": "1"})
        assert r1.ok is True
        assert r1.data["lines"] == 100
        assert cursor["i"] == 1

        r2 = await handler("read_file", None, {"path": "a.py"})
        assert r2.ok is True
        assert r2.data["content"] == "print(1)"

    async def test_surfaces_recorded_errors(self):
        calls = [
            ToolCallRecord(
                turn=0,
                tool_name="fetch_ci_log",
                tool_input={},
                tool_result=None,
                error="github_401",
            )
        ]
        handler, _ = tool_replay_patcher(calls)
        r = await handler("fetch_ci_log", None, {})
        assert r.ok is False
        assert "github_401" in (r.error or "")

    async def test_drift_on_wrong_tool(self):
        calls = [
            ToolCallRecord(
                turn=0,
                tool_name="fetch_ci_log",
                tool_input={},
                tool_result={},
                error=None,
            )
        ]
        handler, _ = tool_replay_patcher(calls)
        with pytest.raises(ReplayDriftError, match="order drift"):
            await handler("read_file", None, {})

    async def test_drift_on_past_end(self):
        calls = [
            ToolCallRecord(
                turn=0,
                tool_name="fetch_ci_log",
                tool_input={},
                tool_result={},
                error=None,
            )
        ]
        handler, _ = tool_replay_patcher(calls)
        await handler("fetch_ci_log", None, {})
        with pytest.raises(ReplayDriftError, match="ran past"):
            await handler("read_file", None, {})


class TestFixtureRoundtrip:
    def test_to_from_json(self):
        fx = Fixture(
            cell="python_test_fail",
            initial_context={"repo": "acme/w", "sha": "abc"},
            llm_calls=[
                LLMCallRecord(
                    role="main",
                    turn_index=0,
                    messages_len=1,
                    response={"stop_reason": "end_turn", "text": "x", "tool_uses": []},
                )
            ],
            tool_calls=[
                ToolCallRecord(
                    turn=0,
                    tool_name="fetch_ci_log",
                    tool_input={},
                    tool_result={"lines": 200},
                    error=None,
                )
            ],
            expected_outcome={"verdict": "committed"},
        )
        back = Fixture.from_json(fx.to_json())
        assert back.cell == fx.cell
        assert back.initial_context == fx.initial_context
        assert len(back.llm_calls) == 1
        assert back.llm_calls[0].role == "main"
        assert len(back.tool_calls) == 1
        assert back.tool_calls[0].tool_name == "fetch_ci_log"
        assert back.expected_outcome == {"verdict": "committed"}
