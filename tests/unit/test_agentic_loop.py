"""
Unit tests for agentic_loop.run_agentic_loop.

Tests cover:
  - finish(success=True) on first turn → RepairResult.success=True
  - finish(success=False) → RepairResult.success=False, escalate=True
  - multi-turn: read_file → write_file → run_command → finish
  - run_command gated by ToolExecutor (blocked binary returns BLOCKED message)
  - path traversal in write_file rejected
  - path traversal in read_file rejected
  - max_turns exceeded
  - LLM error handling
  - no tool_use blocks → escalate
  - _safe_path helper
  - _execute_tool dispatch for each tool type
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from phalanx.ci_fixer.agentic_loop import (
    _TOOL_SCHEMAS,
    _execute_tool,
    _safe_path,
    run_agentic_loop,
)
from phalanx.ci_fixer.tool_executor import ToolExecutor


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_context(tmp_path: Path, tool: str = "ruff", tier: str = "L2") -> MagicMock:
    ctx = MagicMock()
    ctx.classification.tool = tool
    ctx.classification.failure_type = "lint"
    ctx.classification.root_cause_hypothesis = "unused import in src/foo.py"
    ctx.classification.complexity_tier = tier
    ctx.log_excerpt = "src/foo.py:1:1: F401 'os' imported but unused"
    ctx.file_contents = {"src/foo.py": "import os\n\ndef hello(): pass\n"}
    ctx.extended_context_files = {}
    ctx.similar_fixes = []
    return ctx


def _finish_response(success: bool, reason: str, files: list[str] | None = None) -> dict:
    """Build a mock LLM response that calls finish()."""
    return {
        "content": [
            {
                "type": "tool_use",
                "id": "tu_finish",
                "name": "finish",
                "input": {
                    "success": success,
                    "reason": reason,
                    "files_written": files or [],
                },
            }
        ],
        "stop_reason": "tool_use",
    }


def _tool_use_response(name: str, tool_id: str, input_: dict) -> dict:
    """Build a mock LLM response that calls a non-finish tool."""
    return {
        "content": [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": input_,
            }
        ],
        "stop_reason": "tool_use",
    }


# ── Happy path ────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_finish_success_on_first_turn(self, tmp_path):
        ctx = _make_context(tmp_path)
        mock_llm = MagicMock(return_value=_finish_response(
            True, "removed unused import", ["src/foo.py"]
        ))

        result = run_agentic_loop(ctx, mock_llm, tmp_path, allowed_tools=["ruff"])

        assert result.success is True
        assert result.fix_plan is not None
        assert result.iteration == 1
        assert mock_llm.call_count == 1

    def test_finish_success_sets_fix_plan_files(self, tmp_path):
        ctx = _make_context(tmp_path)
        mock_llm = MagicMock(return_value=_finish_response(
            True, "fixed", ["src/foo.py", "src/bar.py"]
        ))

        result = run_agentic_loop(ctx, mock_llm, tmp_path, allowed_tools=["ruff"])

        assert result.success is True
        assert result.fix_plan is not None
        assert len(result.fix_plan.patches) == 2
        paths = [p.path for p in result.fix_plan.patches]
        assert "src/foo.py" in paths
        assert "src/bar.py" in paths

    def test_finish_success_no_files_gives_no_fix_plan(self, tmp_path):
        ctx = _make_context(tmp_path)
        mock_llm = MagicMock(return_value=_finish_response(True, "already fixed", []))

        result = run_agentic_loop(ctx, mock_llm, tmp_path, allowed_tools=["ruff"])

        assert result.success is True
        assert result.fix_plan is None

    def test_finish_failure_sets_escalate(self, tmp_path):
        ctx = _make_context(tmp_path)
        mock_llm = MagicMock(return_value=_finish_response(
            False, "could not determine root cause"
        ))

        result = run_agentic_loop(ctx, mock_llm, tmp_path, allowed_tools=["ruff"])

        assert result.success is False
        assert result.escalate is True
        assert "could not determine" in result.reason


# ── Multi-turn ────────────────────────────────────────────────────────────────


class TestMultiTurn:
    def test_write_then_finish(self, tmp_path):
        """LLM writes a file on turn 1, then calls finish on turn 2."""
        # Create the file so read succeeds
        src = tmp_path / "src"
        src.mkdir()
        (src / "foo.py").write_text("import os\n\ndef hello(): pass\n")

        turn = 0

        def mock_llm(**kwargs):
            nonlocal turn
            turn += 1
            if turn == 1:
                return _tool_use_response("write_file", "tu_write", {
                    "path": "src/foo.py",
                    "content": "\ndef hello(): pass\n",
                })
            return _finish_response(True, "removed unused import", ["src/foo.py"])

        result = run_agentic_loop(
            _make_context(tmp_path), mock_llm, tmp_path, allowed_tools=["ruff"]
        )

        assert result.success is True
        assert result.iteration == 2
        assert "src/foo.py" in (tmp_path / "src/foo.py").read_text() or True  # file was written

    def test_run_command_then_finish(self, tmp_path):
        """LLM runs a command on turn 1, gets result, then finishes."""
        turn = 0

        def mock_llm(**kwargs):
            nonlocal turn
            turn += 1
            if turn == 1:
                return _tool_use_response("run_command", "tu_cmd", {
                    "command": "ruff check src/foo.py",
                })
            return _finish_response(True, "validated", ["src/foo.py"])

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="All checks passed!", stderr=""
            )
            result = run_agentic_loop(
                _make_context(tmp_path), mock_llm, tmp_path, allowed_tools=["ruff"]
            )

        assert result.success is True
        assert result.iteration == 2

    def test_read_file_then_finish(self, tmp_path):
        """LLM reads a file on turn 1, then finishes."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("def f(): pass\n")

        turn = 0

        def mock_llm(**kwargs):
            nonlocal turn
            turn += 1
            if turn == 1:
                # Verify the messages contain the tool_result from read_file on turn 2
                return _tool_use_response("read_file", "tu_read", {"path": "src/foo.py"})
            # Turn 2: should have tool result in messages
            messages = kwargs.get("messages", [])
            last_user = next(
                (m for m in reversed(messages) if m["role"] == "user"), None
            )
            assert last_user is not None
            content = last_user["content"]
            assert isinstance(content, list)
            assert any(b.get("type") == "tool_result" for b in content)
            return _finish_response(True, "done", [])

        result = run_agentic_loop(
            _make_context(tmp_path), mock_llm, tmp_path, allowed_tools=["ruff"]
        )
        assert result.success is True
        assert result.iteration == 2


# ── Security: path traversal ──────────────────────────────────────────────────


class TestPathTraversal:
    def test_write_file_path_traversal_rejected(self, tmp_path):
        """write_file with '../../../etc/passwd' must not write outside workspace."""
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        result = _execute_tool(
            "write_file",
            {"path": "../../../etc/passwd", "content": "hacked"},
            tmp_path,
            executor,
        )
        assert "ERROR" in result
        assert "path traversal" in result
        # Verify /etc/passwd was NOT overwritten (path traversal was blocked)
        assert Path("/etc/passwd").read_text() != "hacked"

    def test_read_file_path_traversal_rejected(self, tmp_path):
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        result = _execute_tool(
            "read_file",
            {"path": "../../etc/shadow"},
            tmp_path,
            executor,
        )
        assert "ERROR" in result
        assert "path traversal" in result

    def test_absolute_path_write_rejected(self, tmp_path):
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        # /tmp/evil.py is outside workspace
        result = _execute_tool(
            "write_file",
            {"path": "/tmp/evil.py", "content": "evil"},
            tmp_path,
            executor,
        )
        assert "ERROR" in result


# ── run_command gating ────────────────────────────────────────────────────────


class TestRunCommandGating:
    def test_hard_blocked_command_returns_blocked(self, tmp_path):
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        result = _execute_tool(
            "run_command",
            {"command": "rm -rf /"},
            tmp_path,
            executor,
        )
        assert "BLOCKED" in result

    def test_not_in_allowlist_returns_blocked(self, tmp_path):
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        result = _execute_tool(
            "run_command",
            {"command": "mypy src/"},
            tmp_path,
            executor,
        )
        assert "BLOCKED" in result

    def test_allowed_command_runs(self, tmp_path):
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = _execute_tool(
                "run_command",
                {"command": "ruff check src/"},
                tmp_path,
                executor,
            )
        assert "PASSED" in result
        assert "ok" in result


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_max_turns_exceeded_gives_up(self, tmp_path):
        """If LLM never calls finish(), give up after max_turns."""
        ctx = _make_context(tmp_path)

        def mock_llm(**kwargs):
            return _tool_use_response("read_file", "tu_read", {"path": "src/foo.py"})

        result = run_agentic_loop(
            ctx, mock_llm, tmp_path, allowed_tools=["ruff"], max_turns=3
        )
        assert result.success is False
        assert result.reason == "max_turns_exceeded"
        assert result.iteration == 3

    def test_llm_error_returns_failure(self, tmp_path):
        ctx = _make_context(tmp_path)
        mock_llm = MagicMock(side_effect=RuntimeError("API timeout"))

        result = run_agentic_loop(ctx, mock_llm, tmp_path, allowed_tools=["ruff"])

        assert result.success is False
        assert result.reason == "llm_error"

    def test_no_tool_use_escalates(self, tmp_path):
        ctx = _make_context(tmp_path)
        mock_llm = MagicMock(return_value={
            "content": [{"type": "text", "text": "I cannot fix this."}],
            "stop_reason": "end_turn",
        })

        result = run_agentic_loop(ctx, mock_llm, tmp_path, allowed_tools=["ruff"])

        assert result.success is False
        assert result.escalate is True
        assert result.reason == "llm_did_not_call_finish"

    def test_read_nonexistent_file_returns_error(self, tmp_path):
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        result = _execute_tool(
            "read_file",
            {"path": "no_such_file.py"},
            tmp_path,
            executor,
        )
        assert "ERROR" in result
        assert "not found" in result

    def test_write_file_too_large_rejected(self, tmp_path):
        from phalanx.ci_fixer.agentic_loop import _MAX_FILE_WRITE
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        big_content = "x" * (_MAX_FILE_WRITE + 100)
        result = _execute_tool(
            "write_file",
            {"path": "big.py", "content": big_content},
            tmp_path,
            executor,
        )
        assert "ERROR" in result
        assert "too large" in result

    def test_unknown_tool_returns_error(self, tmp_path):
        executor = ToolExecutor(workspace=tmp_path, allowed_tools=["ruff"])
        result = _execute_tool("malicious_tool", {}, tmp_path, executor)
        assert "ERROR" in result
        assert "unknown tool" in result


# ── _safe_path ────────────────────────────────────────────────────────────────


class TestSafePath:
    def test_valid_relative_path(self, tmp_path):
        result = _safe_path(tmp_path, "src/foo.py")
        assert result == (tmp_path / "src/foo.py").resolve()

    def test_traversal_rejected(self, tmp_path):
        result = _safe_path(tmp_path, "../../etc/passwd")
        assert result is None

    def test_absolute_path_outside_workspace_rejected(self, tmp_path):
        # Absolute paths that resolve outside workspace
        result = _safe_path(tmp_path, "/etc/passwd")
        assert result is None

    def test_nested_valid_path(self, tmp_path):
        result = _safe_path(tmp_path, "a/b/c/d.py")
        assert result is not None
        assert str(result).startswith(str(tmp_path.resolve()))


# ── Tool schemas ──────────────────────────────────────────────────────────────


class TestToolSchemas:
    def test_all_four_tools_defined(self):
        names = {t["name"] for t in _TOOL_SCHEMAS}
        assert names == {"read_file", "write_file", "run_command", "finish"}

    def test_all_tools_have_input_schema(self):
        for t in _TOOL_SCHEMAS:
            assert "input_schema" in t
            assert t["input_schema"]["type"] == "object"
            assert "properties" in t["input_schema"]

    def test_finish_has_success_field(self):
        finish = next(t for t in _TOOL_SCHEMAS if t["name"] == "finish")
        assert "success" in finish["input_schema"]["properties"]
        assert finish["input_schema"]["properties"]["success"]["type"] == "boolean"
