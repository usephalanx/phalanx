"""
Unit tests for pure helpers in phalanx/agents/ci_fixer.py.

Covers module-level helpers and CIFixerAgent._apply_patches without touching
the DB, Celery, or GitHub — those require integration tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from phalanx.agents.ci_fixer import (
    CIFixerAgent,
    _cleanup_workspace,
    _compute_fingerprint,
    _format_error_detail,
)
from phalanx.ci_fixer.analyst import FilePatch
from phalanx.ci_fixer.log_parser import LintError, ParsedLog, TestFailure, TypeError

if TYPE_CHECKING:
    from pathlib import Path

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_agent() -> CIFixerAgent:
    """Create a CIFixerAgent with a fake run_id (no DB needed for helper tests)."""
    with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
        agent = CIFixerAgent.__new__(CIFixerAgent)
        agent.ci_fix_run_id = "test-run-001"
        agent._log = MagicMock()
        return agent


def _lint_parsed(
    file: str = "phalanx/foo.py", code: str = "F401", msg: str = "unused import 'os'"
) -> ParsedLog:
    return ParsedLog(
        tool="ruff",
        lint_errors=[LintError(file=file, line=5, col=1, code=code, message=msg)],
    )


def _write(tmp_path: Path, rel: str, lines: list[str]) -> Path:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("".join(lines))
    return full


# ── _compute_fingerprint ────────────────────────────────────────────────────────


class TestComputeFingerprint:
    def test_returns_16_char_hex(self):
        parsed = _lint_parsed()
        h = _compute_fingerprint(parsed)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_error_class_same_hash(self):
        # Different line numbers → same hash (lines stripped)
        p1 = ParsedLog(
            tool="ruff",
            lint_errors=[
                LintError(
                    file="phalanx/foo.py", line=3, col=1, code="F401", message="unused import 'os'"
                ),
            ],
        )
        p2 = ParsedLog(
            tool="ruff",
            lint_errors=[
                LintError(
                    file="phalanx/foo.py", line=99, col=1, code="F401", message="unused import 'os'"
                ),
            ],
        )
        assert _compute_fingerprint(p1) == _compute_fingerprint(p2)

    def test_different_error_code_different_hash(self):
        p1 = _lint_parsed(code="F401")
        p2 = _lint_parsed(code="E501")
        assert _compute_fingerprint(p1) != _compute_fingerprint(p2)

    def test_different_file_different_hash(self):
        p1 = _lint_parsed(file="phalanx/foo.py")
        p2 = _lint_parsed(file="phalanx/bar.py")
        assert _compute_fingerprint(p1) != _compute_fingerprint(p2)

    def test_type_error_included(self):
        parsed = ParsedLog(
            tool="mypy",
            type_errors=[TypeError(file="src/foo.py", line=1, col=0, message="Incompatible type")],
        )
        h = _compute_fingerprint(parsed)
        assert len(h) == 16

    def test_test_failure_included(self):
        parsed = ParsedLog(
            tool="pytest",
            test_failures=[
                TestFailure(
                    test_id="tests/unit/test_foo.py::test_bar",
                    file="tests/unit/test_foo.py",
                    message="AssertionError",
                )
            ],
        )
        h = _compute_fingerprint(parsed)
        assert len(h) == 16

    def test_empty_log_has_stable_hash(self):
        parsed = ParsedLog(tool="unknown")
        h = _compute_fingerprint(parsed)
        assert len(h) == 16

    def test_parametrized_tests_normalized(self):
        """test[param1] and test[param2] should yield same fingerprint."""
        p1 = ParsedLog(
            tool="pytest",
            test_failures=[
                TestFailure(
                    test_id="tests/test_foo.py::test_bar[case1]",
                    file="tests/test_foo.py",
                    message="",
                ),
            ],
        )
        p2 = ParsedLog(
            tool="pytest",
            test_failures=[
                TestFailure(
                    test_id="tests/test_foo.py::test_bar[case2]",
                    file="tests/test_foo.py",
                    message="",
                ),
            ],
        )
        assert _compute_fingerprint(p1) == _compute_fingerprint(p2)

    def test_numbers_in_messages_normalized(self):
        """Actual values like '42' are replaced with N → same hash."""
        p1 = _lint_parsed(msg="expected 3 items")
        p2 = _lint_parsed(msg="expected 99 items")
        assert _compute_fingerprint(p1) == _compute_fingerprint(p2)


# ── _format_error_detail ───────────────────────────────────────────────────────


class TestFormatErrorDetail:
    def test_lint_errors_formatted(self):
        parsed = _lint_parsed(file="phalanx/foo.py", code="F401", msg="unused import 'os'")
        result = _format_error_detail(parsed)
        assert "F401" in result
        assert "phalanx/foo.py" in result

    def test_type_errors_formatted(self):
        parsed = ParsedLog(
            tool="mypy",
            type_errors=[TypeError(file="src/foo.py", line=5, col=0, message="str vs int")],
        )
        result = _format_error_detail(parsed)
        assert "str vs int" in result

    def test_test_failures_formatted(self):
        parsed = ParsedLog(
            tool="pytest",
            test_failures=[
                TestFailure(
                    test_id="tests/unit/test_foo.py::test_bar",
                    file="tests/unit/test_foo.py",
                    message="",
                )
            ],
        )
        result = _format_error_detail(parsed)
        assert "test_bar" in result

    def test_empty_log_returns_empty_string(self):
        parsed = ParsedLog(tool="unknown")
        assert _format_error_detail(parsed) == ""

    def test_truncated_to_5_errors(self):
        parsed = ParsedLog(
            tool="ruff",
            lint_errors=[
                LintError(file=f"src/f{i}.py", line=1, col=1, code="F401", message="x")
                for i in range(10)
            ],
        )
        result = _format_error_detail(parsed)
        # Only 5 errors should appear
        assert result.count("F401") == 5


# ── _cleanup_workspace ─────────────────────────────────────────────────────────


class TestCleanupWorkspace:
    def test_removes_existing_directory(self, tmp_path):
        d = tmp_path / "ws"
        d.mkdir()
        (d / "file.txt").write_text("content")
        _cleanup_workspace(d)
        assert not d.exists()

    def test_nonexistent_path_does_not_raise(self, tmp_path):
        _cleanup_workspace(tmp_path / "nonexistent")  # should not raise

    def test_nested_directories_removed(self, tmp_path):
        d = tmp_path / "ws" / "a" / "b"
        d.mkdir(parents=True)
        (d / "f.py").write_text("x")
        _cleanup_workspace(tmp_path / "ws")
        assert not (tmp_path / "ws").exists()


# ── CIFixerAgent._apply_patches ────────────────────────────────────────────────


class TestApplyPatches:
    def _agent(self) -> CIFixerAgent:
        return _make_agent()

    def test_applies_line_range_replacement(self, tmp_path):
        lines = ["line 1\n", "import os\n", "line 3\n", "line 4\n", "line 5\n"]
        _write(tmp_path, "src/foo.py", lines)

        patch = FilePatch(
            path="src/foo.py",
            start_line=2,
            end_line=2,
            corrected_lines=[],  # delete "import os" — delta = -1
        )
        # delta=-1 but _MAX_TOTAL_LINE_DELTA=30, so should pass
        # Wait: empty corrected_lines would make delta = -1
        patch = FilePatch(
            path="src/foo.py",
            start_line=2,
            end_line=2,
            corrected_lines=["# removed\n"],
        )
        agent = self._agent()
        written = agent._apply_patches(tmp_path, [patch])
        assert written == ["src/foo.py"]
        result = (tmp_path / "src/foo.py").read_text()
        assert "# removed" in result
        assert "import os" not in result

    def test_missing_file_skipped(self, tmp_path):
        patch = FilePatch(path="src/missing.py", start_line=1, end_line=1, corrected_lines=["x\n"])
        agent = self._agent()
        written = agent._apply_patches(tmp_path, [patch])
        assert written == []

    def test_bounds_out_of_range_skipped(self, tmp_path):
        lines = ["a\n", "b\n"]
        _write(tmp_path, "src/foo.py", lines)
        patch = FilePatch(path="src/foo.py", start_line=5, end_line=10, corrected_lines=["x\n"])
        agent = self._agent()
        written = agent._apply_patches(tmp_path, [patch])
        assert written == []

    def test_delta_too_large_skipped(self, tmp_path):
        lines = [f"line {i}\n" for i in range(5)]
        _write(tmp_path, "src/foo.py", lines)
        # Add 35 lines — exceeds _MAX_TOTAL_LINE_DELTA=30
        huge = [f"added {i}\n" for i in range(35)]
        patch = FilePatch(path="src/foo.py", start_line=1, end_line=1, corrected_lines=huge)
        agent = self._agent()
        written = agent._apply_patches(tmp_path, [patch])
        assert written == []

    def test_multiple_patches_applied_in_order(self, tmp_path):
        lines = ["line 1\n", "import os\n", "import sys\n", "line 4\n"]
        _write(tmp_path, "src/foo.py", lines)

        p1 = FilePatch(
            path="src/foo.py", start_line=2, end_line=2, corrected_lines=["# os removed\n"]
        )
        # After p1, file changes — p2 targets a different file
        _write(tmp_path, "src/bar.py", ["x = 1\n", "y = 2\n"])
        p2 = FilePatch(path="src/bar.py", start_line=1, end_line=1, corrected_lines=["x = 10\n"])

        agent = self._agent()
        written = agent._apply_patches(tmp_path, [p1, p2])
        assert "src/foo.py" in written
        assert "src/bar.py" in written

    def test_empty_patches_list_returns_empty(self, tmp_path):
        agent = self._agent()
        written = agent._apply_patches(tmp_path, [])
        assert written == []


# ── _commit_to_author_branch ───────────────────────────────────────────────────


class TestCommitToAuthorBranch:
    """Unit tests for the Tier 1 closed-loop git commit method."""

    def _make_agent(self) -> CIFixerAgent:
        with patch("phalanx.agents.base.BaseAgent.__init__", return_value=None):
            agent = CIFixerAgent.__new__(CIFixerAgent)
            agent.ci_fix_run_id = "test-run-cl-001"
            agent._log = MagicMock()
            return agent

    @pytest.mark.asyncio
    async def test_returns_error_when_not_a_git_repo(self, tmp_path):
        agent = self._make_agent()
        result = await agent._commit_to_author_branch(
            workspace=tmp_path,
            branch="feat/x",
            commit_message="fix: unused import",
            github_token="tok",
            repo_full_name="owner/repo",
        )
        assert result["sha"] is None
        assert "error" in result

    @pytest.mark.asyncio
    async def test_returns_error_on_branch_mismatch(self, tmp_path):
        """If workspace is on a different branch, return an error dict."""
        from unittest.mock import MagicMock, patch as _patch
        import subprocess

        # Init a real git repo on a branch named 'main'
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True, capture_output=True)
        (tmp_path / "f.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True)

        agent = self._make_agent()
        # Workspace is on 'master'/'main' but we pass branch='feat/different'
        result = await agent._commit_to_author_branch(
            workspace=tmp_path,
            branch="feat/different",
            commit_message="fix: lint",
            github_token="",
            repo_full_name="owner/repo",
        )
        assert result["sha"] is None
        assert "error" in result

    @pytest.mark.asyncio
    async def test_returns_no_changes_when_nothing_to_commit(self, tmp_path):
        import subprocess

        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True, capture_output=True)
        (tmp_path / "f.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True)

        # Determine actual branch name
        import subprocess as sp
        branch = sp.check_output(["git", "-C", str(tmp_path), "branch", "--show-current"]).decode().strip()

        agent = self._make_agent()
        with patch("phalanx.agents.ci_fixer.settings") as mock_s:
            mock_s.git_author_name = "FORGE"
            mock_s.git_author_email = "forge@example.com"
            result = await agent._commit_to_author_branch(
                workspace=tmp_path,
                branch=branch,
                commit_message="fix: nothing",
                github_token="",
                repo_full_name="owner/repo",
            )
        assert result.get("message") == "no_changes"
        assert result["sha"] is None
