"""
Tests for phalanx.ci_fixer.verifier — VerifierAgent.

Coverage targets:
  - verify(): all 4 verdicts (passed, failed, skipped, timeout)
  - verify(): unknown stack → skipped (no profile)
  - verify(): python with pytest infrastructure → prepends pytest step
  - verify(): python without pytest → ruff only
  - verify(): first failing step short-circuits remaining steps
  - verify(): all steps timeout → verdict=timeout
  - _get_profile(): known and unknown stacks
  - _has_pytest(): detects pyproject.toml, pytest.ini, setup.cfg, absent
  - _run_cmd(): FileNotFoundError → VerificationStep with tool-not-found output
  - VerificationStep dataclass defaults
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.ci_fixer.context import VerificationResult
from phalanx.ci_fixer.verifier import VerificationStep, VerifierAgent

if TYPE_CHECKING:
    from pathlib import Path

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_proc(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timeout: bool = False,
    not_found: bool = False,
) -> MagicMock:
    """Return a mock asyncio.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    if timeout:
        proc.communicate = AsyncMock(side_effect=TimeoutError())
    elif not_found:
        proc.communicate = AsyncMock(side_effect=FileNotFoundError())
    else:
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _make_workspace(tmp_path: Path, *filenames: str) -> Path:
    for name in filenames:
        (tmp_path / name).touch()
    return tmp_path


# ── _has_pytest ───────────────────────────────────────────────────────────────


class TestHasPytest:
    def test_detects_pyproject_toml(self, tmp_path: Path):
        _make_workspace(tmp_path, "pyproject.toml")
        assert VerifierAgent()._has_pytest(tmp_path) is True

    def test_detects_pytest_ini(self, tmp_path: Path):
        _make_workspace(tmp_path, "pytest.ini")
        assert VerifierAgent()._has_pytest(tmp_path) is True

    def test_detects_setup_cfg(self, tmp_path: Path):
        _make_workspace(tmp_path, "setup.cfg")
        assert VerifierAgent()._has_pytest(tmp_path) is True

    def test_absent(self, tmp_path: Path):
        assert VerifierAgent()._has_pytest(tmp_path) is False


# ── _get_profile ──────────────────────────────────────────────────────────────


class TestGetProfile:
    def test_python_profile(self):
        profile = VerifierAgent()._get_profile("python")
        assert len(profile) >= 1
        labels = [label for label, _ in profile]
        assert "ruff_full" in labels

    def test_node_profile(self):
        profile = VerifierAgent()._get_profile("node")
        assert any("npm" in " ".join(cmd) for _, cmd in profile)

    def test_go_profile(self):
        profile = VerifierAgent()._get_profile("go")
        assert any("go" in cmd[0] for _, cmd in profile)

    def test_rust_profile(self):
        profile = VerifierAgent()._get_profile("rust")
        assert any("cargo" in cmd[0] for _, cmd in profile)

    def test_unknown_stack_empty_profile(self):
        assert VerifierAgent()._get_profile("unknown") == []


# ── verify() — core verdicts ──────────────────────────────────────────────────


class TestVerifyVerdicts:
    @pytest.mark.asyncio
    async def test_verify_skipped_unknown_stack(self, tmp_path: Path):
        """Unknown stack → no profile → verdict=skipped immediately."""
        result = await VerifierAgent().verify(
            workspace_path=tmp_path,
            stack="unknown",
            sandbox_result=None,
            timeout_seconds=30,
        )
        assert result.verdict == "skipped"
        assert isinstance(result, VerificationResult)

    @pytest.mark.asyncio
    async def test_verify_passed_python_no_pytest(self, tmp_path: Path):
        """Python workspace without pytest infra → ruff only → exit 0 → passed."""
        proc = _make_proc(returncode=0, stdout=b"All checks passed", stderr=b"")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="python",
                sandbox_result=None,
                timeout_seconds=30,
            )

        assert result.verdict == "passed"

    @pytest.mark.asyncio
    async def test_verify_passed_python_with_pytest(self, tmp_path: Path):
        """Python workspace with pyproject.toml → pytest + ruff → both pass."""
        _make_workspace(tmp_path, "pyproject.toml")
        proc = _make_proc(returncode=0, stdout=b"passed", stderr=b"")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="python",
                sandbox_result=None,
                timeout_seconds=30,
            )

        assert result.verdict == "passed"

    @pytest.mark.asyncio
    async def test_verify_failed_on_first_step(self, tmp_path: Path):
        """First step fails → verdict=failed, short-circuit."""
        proc = _make_proc(returncode=1, stdout=b"", stderr=b"FAILED test_foo.py")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="python",
                sandbox_result=None,
                timeout_seconds=30,
            )

        assert result.verdict == "failed"
        assert "FAILED" in result.output

    @pytest.mark.asyncio
    async def test_verify_timeout_single_step(self, tmp_path: Path):
        """Single step times out → all_timed_out → verdict=timeout."""
        proc = _make_proc(timeout=True)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="go",
                sandbox_result=None,
                timeout_seconds=1,
            )

        assert result.verdict == "timeout"

    @pytest.mark.asyncio
    async def test_verify_timeout_step_does_not_block_other_steps(self, tmp_path: Path):
        """Timeout on one step is skipped; if remaining steps pass → passed."""
        _make_workspace(tmp_path, "pyproject.toml")

        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call (pytest) times out
                return _make_proc(timeout=True)
            # Subsequent calls (ruff) pass
            return _make_proc(returncode=0, stdout=b"clean", stderr=b"")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="python",
                sandbox_result=None,
                timeout_seconds=1,
            )

        # ruff passed even though pytest timed out → overall passed
        assert result.verdict == "passed"

    @pytest.mark.asyncio
    async def test_verify_go_passed(self, tmp_path: Path):
        proc = _make_proc(returncode=0, stdout=b"ok  example.com/pkg", stderr=b"")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="go",
                sandbox_result=None,
                timeout_seconds=30,
            )

        assert result.verdict == "passed"

    @pytest.mark.asyncio
    async def test_verify_rust_failed(self, tmp_path: Path):
        proc = _make_proc(returncode=1, stdout=b"", stderr=b"error[E0308]: mismatched types")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="rust",
                sandbox_result=None,
                timeout_seconds=30,
            )

        assert result.verdict == "failed"

    @pytest.mark.asyncio
    async def test_verify_cmd_run_populated(self, tmp_path: Path):
        """cmd_run contains the command that was executed."""
        proc = _make_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await VerifierAgent().verify(
                workspace_path=tmp_path,
                stack="go",
                sandbox_result=None,
                timeout_seconds=30,
            )

        assert result.cmd_run != ""
        assert "go" in result.cmd_run


# ── _run_cmd ──────────────────────────────────────────────────────────────────


class TestRunCmd:
    @pytest.mark.asyncio
    async def test_run_cmd_tool_not_found(self, tmp_path: Path):
        """FileNotFoundError → VerificationStep with tool-not-found message, no raise."""
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("notool"),
        ):
            step = await VerifierAgent()._run_cmd(
                label="test_label",
                cmd_args=["notool", "--check"],
                cwd=tmp_path,
                timeout_seconds=30,
            )

        assert step.exit_code == -1
        assert "not found" in step.output
        assert step.timed_out is False

    @pytest.mark.asyncio
    async def test_run_cmd_success(self, tmp_path: Path):
        proc = _make_proc(returncode=0, stdout=b"clean", stderr=b"")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            step = await VerifierAgent()._run_cmd(
                label="ruff_full",
                cmd_args=["ruff", "check", "."],
                cwd=tmp_path,
                timeout_seconds=30,
            )

        assert step.exit_code == 0
        assert step.timed_out is False
        assert "clean" in step.output

    @pytest.mark.asyncio
    async def test_run_cmd_timeout(self, tmp_path: Path):
        proc = _make_proc(timeout=True)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            step = await VerifierAgent()._run_cmd(
                label="slow_check",
                cmd_args=["slow", "cmd"],
                cwd=tmp_path,
                timeout_seconds=1,
            )

        assert step.timed_out is True
        assert step.exit_code == -1
        proc.kill.assert_called_once()


# ── VerificationStep dataclass ────────────────────────────────────────────────


class TestVerificationStep:
    def test_defaults(self):
        step = VerificationStep(
            label="ruff",
            cmd="ruff check .",
            exit_code=0,
            output="clean",
            elapsed_seconds=1.2,
        )
        assert step.timed_out is False

    def test_timed_out_flag(self):
        step = VerificationStep(
            label="pytest",
            cmd="pytest",
            exit_code=-1,
            output="",
            elapsed_seconds=120.0,
            timed_out=True,
        )
        assert step.timed_out is True
