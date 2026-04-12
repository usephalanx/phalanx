"""
Unit tests for phalanx/ci_fixer/validator.py

Tests deterministic fix validation by mocking subprocess.run.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from phalanx.ci_fixer.log_parser import LintError, ParsedLog, TestFailure, TypeError
from phalanx.ci_fixer.validator import ValidationResult, validate_fix


def _parsed(tool: str, **kwargs) -> ParsedLog:
    return ParsedLog(tool=tool, **kwargs)


class TestValidateFix:
    def _mock_run(self, returncode: int, stdout: str = "", stderr: str = ""):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = stderr
        return result

    def test_ruff_pass(self, tmp_path):
        parsed = _parsed("ruff", lint_errors=[
            LintError(file="phalanx/foo.py", line=1, col=1, code="F401", message="unused")
        ])
        with patch("subprocess.run", return_value=self._mock_run(0, "All good")):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is True
        assert result.tool == "ruff"

    def test_ruff_fail(self, tmp_path):
        parsed = _parsed("ruff", lint_errors=[
            LintError(file="phalanx/foo.py", line=1, col=1, code="F401", message="unused")
        ])
        with patch("subprocess.run", return_value=self._mock_run(1, "", "phalanx/foo.py:1:1: F401")):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is False

    def test_mypy_pass(self, tmp_path):
        parsed = _parsed("mypy", type_errors=[
            TypeError(file="phalanx/foo.py", line=5, col=0, message="type error")
        ])
        with patch("subprocess.run", return_value=self._mock_run(0)):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is True
        assert result.tool == "mypy"

    def test_pytest_pass(self, tmp_path):
        parsed = _parsed("pytest", test_failures=[
            TestFailure(test_id="tests/unit/test_foo.py::test_bar", file="tests/unit/test_foo.py", message="")
        ])
        with patch("subprocess.run", return_value=self._mock_run(0)):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is True
        assert result.tool == "pytest"

    def test_pytest_fail(self, tmp_path):
        parsed = _parsed("pytest", test_failures=[
            TestFailure(test_id="tests/unit/test_foo.py::test_bar", file="tests/unit/test_foo.py", message="")
        ])
        with patch("subprocess.run", return_value=self._mock_run(1, "", "FAILED")):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is False

    def test_tsc_pass(self, tmp_path):
        parsed = _parsed("tsc", type_errors=[
            TypeError(file="src/foo.ts", line=1, col=1, message="TS2345: error")
        ])
        with patch("subprocess.run", return_value=self._mock_run(0)):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is True

    def test_eslint_pass(self, tmp_path):
        parsed = _parsed("eslint", lint_errors=[
            LintError(file="src/foo.js", line=1, col=1, code="eslint", message="no-unused-vars")
        ])
        with patch("subprocess.run", return_value=self._mock_run(0)):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is True

    def test_unknown_tool_skips_validation(self, tmp_path):
        parsed = _parsed("unknown")
        result = validate_fix(parsed, tmp_path)
        assert result.passed is True
        assert "skipped" in result.output

    def test_tool_not_found_returns_fail(self, tmp_path):
        parsed = _parsed("ruff", lint_errors=[
            LintError(file="phalanx/foo.py", line=1, col=1, code="F401", message="unused")
        ])
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is False
        assert "not found" in result.output

    def test_timeout_returns_fail(self, tmp_path):
        import subprocess
        parsed = _parsed("ruff", lint_errors=[
            LintError(file="phalanx/foo.py", line=1, col=1, code="F401", message="unused")
        ])
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ruff", timeout=120)):
            result = validate_fix(parsed, tmp_path)
        assert result.passed is False
        assert "timed out" in result.output
