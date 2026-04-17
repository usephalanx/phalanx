"""
Tests for phalanx.ci_fixer.reproducer — ReproducerAgent.

Coverage targets:
  - reproduce(): all 5 verdicts (skipped, confirmed, flaky, env_mismatch, timeout)
  - reproduce(): skipped when sandbox unavailable (available=False)
  - reproduce(): skipped when reproducer_cmd is empty
  - _output_matches_failure(): tool name match, error code match, no match
  - _run_subprocess(): timeout path (process killed)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.ci_fixer.context import ReproductionResult, StructuredFailure
from phalanx.ci_fixer.reproducer import ReproducerAgent, ReproductionAttempt
from phalanx.ci_fixer.sandbox import SandboxResult

if TYPE_CHECKING:
    from pathlib import Path

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_sandbox(available: bool = True, container_id: str = "") -> SandboxResult:
    return SandboxResult(
        sandbox_id="phalanx-sandbox-test1234",
        stack="python",
        image="python:3.12-slim",
        workspace_path="/tmp/ws",
        available=available,
        container_id=container_id,
    )


def _make_sf(
    tool: str = "ruff",
    errors: list | None = None,
) -> StructuredFailure:
    return StructuredFailure(
        tool=tool,
        failure_type="lint",
        reproducer_cmd=f"{tool} check .",
        errors=errors or [],
    )


def _make_proc(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timeout: bool = False,
) -> AsyncMock:
    """Return a mock asyncio.Process suitable for create_subprocess_shell."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    if timeout:
        proc.communicate = AsyncMock(side_effect=TimeoutError())
    else:
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ── reproduce() — verdict classification ──────────────────────────────────────


class TestReproduceVerdicts:
    @pytest.mark.asyncio
    async def test_reproduce_skipped_when_no_sandbox(self, tmp_path: Path):
        """sandbox_result=None → verdict=skipped, no subprocess."""
        agent = ReproducerAgent()
        result = await agent.reproduce(
            reproducer_cmd="ruff check .",
            workspace_path=tmp_path,
            sandbox_result=None,
            structured_failure=_make_sf(),
            timeout_seconds=30,
        )
        assert result.verdict == "skipped"
        assert isinstance(result, ReproductionResult)

    @pytest.mark.asyncio
    async def test_reproduce_skipped_when_sandbox_unavailable(self, tmp_path: Path):
        """sandbox_result.available=False → verdict=skipped."""
        agent = ReproducerAgent()
        result = await agent.reproduce(
            reproducer_cmd="ruff check .",
            workspace_path=tmp_path,
            sandbox_result=_make_sandbox(available=False),
            structured_failure=_make_sf(),
            timeout_seconds=30,
        )
        assert result.verdict == "skipped"

    @pytest.mark.asyncio
    async def test_reproduce_skipped_when_empty_cmd(self, tmp_path: Path):
        """Empty reproducer_cmd → verdict=skipped."""
        agent = ReproducerAgent()
        result = await agent.reproduce(
            reproducer_cmd="",
            workspace_path=tmp_path,
            sandbox_result=_make_sandbox(),
            structured_failure=_make_sf(),
            timeout_seconds=30,
        )
        assert result.verdict == "skipped"

    @pytest.mark.asyncio
    async def test_reproduce_skipped_when_whitespace_cmd(self, tmp_path: Path):
        """Whitespace-only reproducer_cmd → verdict=skipped."""
        agent = ReproducerAgent()
        result = await agent.reproduce(
            reproducer_cmd="   ",
            workspace_path=tmp_path,
            sandbox_result=_make_sandbox(),
            structured_failure=_make_sf(),
            timeout_seconds=30,
        )
        assert result.verdict == "skipped"

    @pytest.mark.asyncio
    async def test_reproduce_flaky(self, tmp_path: Path):
        """exit_code=0 → command passed → CI failure was transient → flaky."""
        proc = _make_proc(returncode=0, stdout=b"All checks passed", stderr=b"")

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            agent = ReproducerAgent()
            result = await agent.reproduce(
                reproducer_cmd="ruff check .",
                workspace_path=tmp_path,
                sandbox_result=_make_sandbox(),
                structured_failure=_make_sf(),
                timeout_seconds=30,
            )

        assert result.verdict == "flaky"
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_reproduce_confirmed_by_tool_name(self, tmp_path: Path):
        """exit_code!=0, tool name in output → confirmed."""
        proc = _make_proc(
            returncode=1,
            stdout=b"ruff check failed: F401 unused import",
            stderr=b"",
        )

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            agent = ReproducerAgent()
            result = await agent.reproduce(
                reproducer_cmd="ruff check .",
                workspace_path=tmp_path,
                sandbox_result=_make_sandbox(),
                structured_failure=_make_sf(tool="ruff"),
                timeout_seconds=30,
            )

        assert result.verdict == "confirmed"
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_reproduce_confirmed_by_error_code(self, tmp_path: Path):
        """exit_code!=0, error code in output (no tool name) → confirmed."""
        proc = _make_proc(
            returncode=1,
            stdout=b"src/foo.py:1:1: F401 'os' imported but unused",
            stderr=b"",
        )
        sf = _make_sf(tool="ruff", errors=[{"file": "src/foo.py", "code": "F401"}])
        # Use a tool name that won't match the output to isolate error-code path
        sf.tool = "linter"

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            agent = ReproducerAgent()
            result = await agent.reproduce(
                reproducer_cmd="linter check .",
                workspace_path=tmp_path,
                sandbox_result=_make_sandbox(),
                structured_failure=sf,
                timeout_seconds=30,
            )

        assert result.verdict == "confirmed"

    @pytest.mark.asyncio
    async def test_reproduce_env_mismatch(self, tmp_path: Path):
        """exit_code!=0 but output unrelated to original failure → env_mismatch."""
        proc = _make_proc(
            returncode=1,
            stdout=b"command not found: ruff",
            stderr=b"bash: ruff: command not found",
        )
        # Use a structured failure whose tool name won't appear in the "not found" output
        sf = StructuredFailure(
            tool="mypy",
            failure_type="type_error",
            reproducer_cmd="mypy .",
            errors=[{"code": "E999"}],
        )

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            agent = ReproducerAgent()
            result = await agent.reproduce(
                reproducer_cmd="mypy .",
                workspace_path=tmp_path,
                sandbox_result=_make_sandbox(),
                structured_failure=sf,
                timeout_seconds=30,
            )

        assert result.verdict == "env_mismatch"

    @pytest.mark.asyncio
    async def test_reproduce_timeout(self, tmp_path: Path):
        """Process exceeds timeout → verdict=timeout, process killed."""
        proc = _make_proc(timeout=True)

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            agent = ReproducerAgent()
            result = await agent.reproduce(
                reproducer_cmd="ruff check .",
                workspace_path=tmp_path,
                sandbox_result=_make_sandbox(),
                structured_failure=_make_sf(),
                timeout_seconds=1,
            )

        assert result.verdict == "timeout"
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reproduce_result_fields(self, tmp_path: Path):
        """Result includes reproducer_cmd and truncated output."""
        long_output = b"F401 " * 1000  # > 4000 chars
        proc = _make_proc(returncode=1, stdout=long_output, stderr=b"")

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            agent = ReproducerAgent()
            result = await agent.reproduce(
                reproducer_cmd="ruff check .",
                workspace_path=tmp_path,
                sandbox_result=_make_sandbox(),
                structured_failure=_make_sf(tool="ruff"),
                timeout_seconds=30,
            )

        assert result.reproducer_cmd == "ruff check ."
        assert len(result.output) <= 4000


# ── _output_matches_failure ───────────────────────────────────────────────────


class TestOutputMatchesFailure:
    def test_matches_by_tool_name(self):
        agent = ReproducerAgent()
        sf = _make_sf(tool="ruff")
        assert agent._output_matches_failure("ruff check found 3 errors", sf) is True

    def test_matches_tool_name_case_insensitive(self):
        agent = ReproducerAgent()
        sf = _make_sf(tool="Ruff")
        assert agent._output_matches_failure("RUFF check: error F401", sf) is True

    def test_matches_by_error_code(self):
        agent = ReproducerAgent()
        sf = StructuredFailure(
            tool="nontool",  # won't match output
            failure_type="lint",
            reproducer_cmd="check .",
            errors=[{"code": "E501", "file": "foo.py"}],
        )
        assert agent._output_matches_failure("line too long E501 at 120 chars", sf) is True

    def test_no_match_unrelated_output(self):
        agent = ReproducerAgent()
        sf = StructuredFailure(
            tool="mypy",
            failure_type="type_error",
            reproducer_cmd="mypy .",
            errors=[{"code": "E999"}],
        )
        # Output has neither "mypy" nor "E999"
        assert agent._output_matches_failure("pip install failed: network error", sf) is False

    def test_no_match_empty_output(self):
        agent = ReproducerAgent()
        sf = _make_sf(tool="ruff")
        assert agent._output_matches_failure("", sf) is False

    def test_no_match_empty_errors_no_tool(self):
        agent = ReproducerAgent()
        sf = StructuredFailure(
            tool="pytest",
            failure_type="test_regression",
            reproducer_cmd="pytest .",
            errors=[],
        )
        # Output has no "pytest" in it
        assert agent._output_matches_failure("FAILED test_foo.py::test_bar", sf) is False

    def test_matches_with_no_code_in_error_dict(self):
        """Errors with no 'code' key should not raise."""
        agent = ReproducerAgent()
        sf = StructuredFailure(
            tool="ruff",
            failure_type="lint",
            reproducer_cmd="ruff check .",
            errors=[{"file": "foo.py", "line": 1}],  # no 'code' key
        )
        # Tool name match should still work
        assert agent._output_matches_failure("ruff: 1 error found", sf) is True


# ── Container exec path ───────────────────────────────────────────────────────


class TestReproducerContainerExec:
    """Tests for the docker exec path when sandbox_result.container_id is set."""

    def _make_sandbox_with_container(self, container_id: str = "ctr-abc123") -> object:
        return _make_sandbox(available=True, container_id=container_id)

    @pytest.mark.asyncio
    async def test_reproduce_uses_docker_exec_when_container_id_set(self, tmp_path):
        """When container_id is set, command is wrapped with docker exec."""
        proc = _make_proc(returncode=1, stdout=b"ruff: 1 error", stderr=b"")

        captured_args = []

        async def fake_exec(*args, **kwargs):
            captured_args.extend(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await ReproducerAgent().reproduce(
                reproducer_cmd="ruff check .",
                workspace_path=tmp_path,
                sandbox_result=self._make_sandbox_with_container("ctr-abc123"),
                structured_failure=_make_sf(tool="ruff"),
                timeout_seconds=30,
            )

        assert result.verdict == "confirmed"
        assert "docker" in captured_args
        assert "ctr-abc123" in captured_args

    @pytest.mark.asyncio
    async def test_reproduce_local_subprocess_when_no_container_id(self, tmp_path):
        """When container_id is empty, uses local subprocess shell."""
        proc = _make_proc(returncode=0, stdout=b"clean", stderr=b"")

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            result = await ReproducerAgent().reproduce(
                reproducer_cmd="ruff check .",
                workspace_path=tmp_path,
                sandbox_result=_make_sandbox(available=True),  # no container_id
                structured_failure=_make_sf(tool="ruff"),
                timeout_seconds=30,
            )

        assert result.verdict == "flaky"

    @pytest.mark.asyncio
    async def test_run_subprocess_with_container_id(self, tmp_path):
        """_run_subprocess with container_id uses create_subprocess_exec."""
        proc = _make_proc(returncode=0, stdout=b"ok", stderr=b"")

        captured = []

        async def fake_exec(*args, **kwargs):
            captured.extend(args)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            step = await ReproducerAgent()._run_subprocess(
                cmd="ruff check .",
                cwd=tmp_path,
                timeout_seconds=30,
                container_id="ctr-xyz",
            )

        assert step.exit_code == 0
        assert "ctr-xyz" in captured
        assert "sh" in captured

    @pytest.mark.asyncio
    async def test_run_subprocess_without_container_id(self, tmp_path):
        """_run_subprocess without container_id uses create_subprocess_shell."""
        proc = _make_proc(returncode=0, stdout=b"clean", stderr=b"")

        with patch("asyncio.create_subprocess_shell", return_value=proc):
            step = await ReproducerAgent()._run_subprocess(
                cmd="ruff check .",
                cwd=tmp_path,
                timeout_seconds=30,
                container_id="",
            )

        assert step.exit_code == 0


# ── ReproductionAttempt dataclass ─────────────────────────────────────────────


class TestReproductionAttempt:
    def test_defaults(self):
        a = ReproductionAttempt(
            cmd="ruff check .",
            exit_code=1,
            stdout="out",
            stderr="err",
            elapsed_seconds=0.5,
        )
        assert a.timed_out is False

    def test_timed_out_flag(self):
        a = ReproductionAttempt(
            cmd="ruff check .",
            exit_code=-1,
            stdout="",
            stderr="",
            elapsed_seconds=30.0,
            timed_out=True,
        )
        assert a.timed_out is True
