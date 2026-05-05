"""v1.7.2.9 — TL efficiency follow-ups.

Goal:
  - Reduce TL tool calls to ≤8 by enforcing find_symbol BEFORE read_file
    on large files at the dispatcher (not just prompt guidance).
  - Stop TL from hedging on correct diagnoses — confidence calibration
    validator rejects 0 < confidence < 0.7 on localized deterministic
    fixes, forcing commit (≥0.7) or escalate (0.0).

Production guards (always on; not opt-in):
  G7. Loop blocks read_file on files > 500 LOC unless find_symbol was
      called for that file (or workspace-wide).
  G8. Plan validator rejects hedged confidence on localized
      deterministic fixes.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from phalanx.agents._plan_validator import (
    PlanValidationError,
    _is_localized_deterministic,
    validate_confidence_calibration,
)
from phalanx.agents.cifix_techlead import (
    _FIND_SYMBOL_REQUIRED_THRESHOLD,
    _file_line_count,
    _run_investigation_loop,
)
from phalanx.ci_fixer_v2.agent import LLMResponse, LLMToolUse


class _LoopCtx:
    def __init__(self, workspace: str) -> None:
        self.repo_workspace_path = workspace
        self.messages: list[dict] = []


def _resp_tool_use(name: str, tool_input: dict, *, use_id: str = "tu1") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        text="",
        tool_uses=[LLMToolUse(id=use_id, name=name, input=tool_input)],
    )


def _resp_end_with_fix_spec(confidence: float = 0.8) -> LLMResponse:
    fix_spec_json = (
        "```json\n"
        "{\n"
        '  "root_cause": "test",\n'
        '  "error_line_quote": "AssertionError: x",\n'
        '  "affected_files": ["a.py"],\n'
        '  "fix_spec": "do nothing",\n'
        '  "failing_command": "pytest",\n'
        '  "verify_command": "pytest",\n'
        '  "verify_success": {"exit_codes": [0], "stdout_contains": null, "stderr_excludes": null},\n'
        f'  "confidence": {confidence},\n'
        '  "open_questions": [],\n'
        '  "self_critique": {\n'
        '    "ci_log_addresses_root_cause": true,\n'
        '    "affected_files_exist_in_repo": true,\n'
        '    "verify_command_will_distinguish_success": true,\n'
        '    "notes": "ok"\n'
        "  },\n"
        '  "replan_reason": null\n'
        "}\n"
        "```"
    )
    return LLMResponse(stop_reason="end_turn", text=fix_spec_json, tool_uses=[])


class _FakeLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            return LLMResponse(stop_reason="end_turn", text="", tool_uses=[])
        return self._responses.pop(0)


class _NullLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _write_lines(d: str, name: str, n: int) -> None:
    with open(os.path.join(d, name), "w") as f:
        for i in range(n):
            f.write(f"x = {i}\n")


# ── G7: find_symbol prerequisite enforcement ──────────────────────────────


class TestFindSymbolEnforcement:
    def test_read_file_on_small_file_passes_without_find_symbol(self):
        """≤500 LOC files: no prerequisite."""
        with tempfile.TemporaryDirectory() as d:
            _write_lines(d, "small.py", 200)  # under threshold
            ctx = _LoopCtx(d)
            llm = _FakeLLM(
                [
                    _resp_tool_use("read_file", {"path": "small.py"}, use_id="t1"),
                    _resp_end_with_fix_spec(),
                ]
            )

            async def _run():
                return await _run_investigation_loop(
                    ctx=ctx,
                    llm_call=llm,
                    max_turns=5,
                    max_tool_calls=15,
                    logger=_NullLogger(),
                )

            spec, turns, calls = asyncio.run(_run())
            assert calls == 1
            tool_results = [
                m["content"][0]["content"]
                for m in ctx.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"][0].get("type") == "tool_result"
            ]
            assert tool_results[0].get("ok") is True

    def test_read_file_on_large_file_blocked_without_find_symbol(self):
        """>500 LOC + no prior find_symbol → rejected at dispatcher."""
        with tempfile.TemporaryDirectory() as d:
            _write_lines(d, "huge.py", 1000)  # > 500 threshold
            ctx = _LoopCtx(d)
            llm = _FakeLLM(
                [
                    _resp_tool_use(
                        "read_file",
                        {"path": "huge.py", "around_line": 100, "context": 10},
                        use_id="t1",
                    ),
                    _resp_end_with_fix_spec(),
                ]
            )

            async def _run():
                return await _run_investigation_loop(
                    ctx=ctx,
                    llm_call=llm,
                    max_turns=5,
                    max_tool_calls=15,
                    logger=_NullLogger(),
                )

            spec, turns, calls = asyncio.run(_run())
            tool_results = [
                m["content"][0]["content"]
                for m in ctx.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"][0].get("type") == "tool_result"
            ]
            assert len(tool_results) == 1
            blocked = tool_results[0]
            assert blocked.get("ok") is False
            assert "find_symbol_required" in blocked.get("error", "")

    def test_read_file_on_large_file_passes_after_find_symbol_for_same_file(self):
        with tempfile.TemporaryDirectory() as d:
            _write_lines(d, "huge.py", 1000)
            ctx = _LoopCtx(d)
            llm = _FakeLLM(
                [
                    _resp_tool_use(
                        "find_symbol",
                        {"name": "foo", "file": "huge.py"},
                        use_id="t1",
                    ),
                    _resp_tool_use(
                        "read_file",
                        {"path": "huge.py", "around_line": 100, "context": 5},
                        use_id="t2",
                    ),
                    _resp_end_with_fix_spec(),
                ]
            )

            async def _run():
                return await _run_investigation_loop(
                    ctx=ctx,
                    llm_call=llm,
                    max_turns=5,
                    max_tool_calls=15,
                    logger=_NullLogger(),
                )

            spec, turns, calls = asyncio.run(_run())
            tool_results = [
                m["content"][0]["content"]
                for m in ctx.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"][0].get("type") == "tool_result"
            ]
            assert len(tool_results) == 2
            # find_symbol result + read_file result (both ok)
            assert tool_results[1].get("ok") is True

    def test_read_file_on_large_file_passes_after_workspace_wide_find_symbol(self):
        """find_symbol with no `file` arg = workspace-wide; satisfies for any path."""
        with tempfile.TemporaryDirectory() as d:
            _write_lines(d, "huge.py", 1000)
            ctx = _LoopCtx(d)
            llm = _FakeLLM(
                [
                    _resp_tool_use("find_symbol", {"name": "foo"}, use_id="t1"),
                    _resp_tool_use(
                        "read_file",
                        {"path": "huge.py", "around_line": 100, "context": 5},
                        use_id="t2",
                    ),
                    _resp_end_with_fix_spec(),
                ]
            )

            async def _run():
                return await _run_investigation_loop(
                    ctx=ctx,
                    llm_call=llm,
                    max_turns=5,
                    max_tool_calls=15,
                    logger=_NullLogger(),
                )

            spec, turns, calls = asyncio.run(_run())
            tool_results = [
                m["content"][0]["content"]
                for m in ctx.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), list)
                and m["content"][0].get("type") == "tool_result"
            ]
            assert tool_results[1].get("ok") is True

    def test_threshold_constant_is_500(self):
        assert _FIND_SYMBOL_REQUIRED_THRESHOLD == 500


class TestFileLineCountHelper:
    def test_counts_lines_correctly(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "x.py"), "w") as f:
                f.write("a\nb\nc\n")
            assert _file_line_count(d, "x.py") == 3

    def test_returns_none_for_missing(self):
        with tempfile.TemporaryDirectory() as d:
            assert _file_line_count(d, "missing.py") is None


# ── G8: confidence calibration validator ──────────────────────────────────


def _make_localized_spec(confidence: float, *, root_cause: str = "regex anchor lets empty stem through") -> dict:
    return {
        "root_cause": root_cause,
        "affected_files": ["pkg/__init__.py"],
        "fix_spec": "tighten anchor",
        "confidence": confidence,
        "task_plan": [
            {"task_id": "t1", "agent_role": "engineer", "title": "patch"}
        ],
    }


class TestIsLocalizedDeterministic:
    def test_one_file_with_plan_no_flake_keywords_is_localized(self):
        assert _is_localized_deterministic(_make_localized_spec(0.9)) is True

    def test_three_files_is_not_localized(self):
        spec = _make_localized_spec(0.9)
        spec["affected_files"] = ["a.py", "b.py", "c.py"]
        assert _is_localized_deterministic(spec) is False

    def test_zero_files_is_not_localized(self):
        spec = _make_localized_spec(0.9)
        spec["affected_files"] = []
        assert _is_localized_deterministic(spec) is False

    def test_no_plan_is_not_localized(self):
        spec = _make_localized_spec(0.9)
        spec["task_plan"] = []
        assert _is_localized_deterministic(spec) is False

    def test_flake_keyword_in_root_cause_is_not_localized(self):
        spec = _make_localized_spec(0.9, root_cause="flaky network timeout")
        assert _is_localized_deterministic(spec) is False

    def test_timing_keyword_in_root_cause_is_not_localized(self):
        spec = _make_localized_spec(0.9, root_cause="race condition on timing")
        assert _is_localized_deterministic(spec) is False


class TestConfidenceCalibrationValidator:
    def test_localized_at_045_rejected(self):
        spec = _make_localized_spec(0.45)
        with pytest.raises(PlanValidationError) as exc:
            validate_confidence_calibration(spec)
        assert "confidence_calibration_failed" in str(exc.value)
        assert "0.45" in str(exc.value)
        assert "0.7" in str(exc.value)

    def test_localized_at_07_passes(self):
        validate_confidence_calibration(_make_localized_spec(0.7))

    def test_localized_at_09_passes(self):
        validate_confidence_calibration(_make_localized_spec(0.9))

    def test_localized_at_zero_passes_as_escalate_signal(self):
        """0.0 is the canonical ESCALATE confidence — must pass."""
        validate_confidence_calibration(_make_localized_spec(0.0))

    def test_flake_shape_at_045_passes(self):
        """Flake-shape diagnoses are exempt — uncertainty is legitimate."""
        spec = _make_localized_spec(0.45, root_cause="flaky timing race")
        validate_confidence_calibration(spec)

    def test_multi_file_at_045_passes(self):
        """≥3 affected_files is exempt — multi-component is legitimately uncertain."""
        spec = _make_localized_spec(0.45)
        spec["affected_files"] = ["a.py", "b.py", "c.py"]
        validate_confidence_calibration(spec)

    def test_no_plan_at_045_passes(self):
        spec = _make_localized_spec(0.45)
        spec["task_plan"] = []
        validate_confidence_calibration(spec)

    def test_string_confidence_handled(self):
        spec = _make_localized_spec(0.0)
        spec["confidence"] = "0.45"
        with pytest.raises(PlanValidationError):
            validate_confidence_calibration(spec)

    def test_missing_confidence_treated_as_zero_passes(self):
        spec = _make_localized_spec(0.0)
        del spec["confidence"]
        validate_confidence_calibration(spec)
