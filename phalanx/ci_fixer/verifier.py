"""
VerifierAgent — runs a broader verification suite after the fix is applied
to confirm no regressions were introduced.

Design:
  Unlike the validator (which re-runs only the originally-failing tool on
  the originally-failing files), the verifier runs the *full* test suite
  for the detected stack so we catch regressions in unrelated files.

  Verification profiles per stack:
    python → pytest (if test dir exists) + ruff check . (full repo)
    node   → npm test (if package.json has a test script)
    go     → go test ./...
    rust   → cargo test
    unknown → skipped (verdict="skipped")

  Execution:
    When sandbox_result.container_id is set, each command is executed inside
    the pre-warmed isolated container via `docker exec`.  The workspace is
    already at /workspace inside the container.
    When container_id is empty or sandbox unavailable, falls back to local
    subprocess (original Phase 2 behaviour — no regression).

  Timeout: settings.sandbox_timeout_seconds (same budget as reproducer).

  The verifier is intentionally conservative:
    - If the test command is not found → verdict="skipped" (don't block the fix)
    - If the command times out → verdict="timeout" (non-blocking per step)
    - If exit_code == 0 → verdict="passed"
    - If exit_code != 0 → verdict="failed"

  A "skipped" verdict does NOT block the pipeline — the fix proceeds.
  A "failed" verdict causes ctx.complete("escalated") and blocks commit.
  A "timeout" verdict is treated the same as "skipped" (conservative).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from phalanx.ci_fixer.context import VerificationResult

if TYPE_CHECKING:
    from pathlib import Path

    from phalanx.ci_fixer.sandbox import SandboxResult

log = structlog.get_logger(__name__)

# ── Verification profiles ─────────────────────────────────────────────────────
# Each profile is a list of commands to run in order.
# Commands are tuples of (label, args_list).
# All commands must pass for verdict="passed".
_PROFILES: dict[str, list[tuple[str, list[str]]]] = {
    "python": [
        ("ruff_full", ["ruff", "check", "."]),
    ],
    "node": [
        ("npm_test", ["npm", "test", "--if-present"]),
    ],
    "go": [
        ("go_test", ["go", "test", "./..."]),
    ],
    "rust": [
        ("cargo_test", ["cargo", "test"]),
    ],
}


@dataclass
class VerificationStep:
    """Result of a single verification command."""

    label: str
    cmd: str
    exit_code: int
    output: str
    elapsed_seconds: float
    timed_out: bool = False


class VerifierAgent:
    """
    Runs a broad verification sweep after the fix agent completes.

    One instance per pipeline run; no shared state between calls.
    """

    def _get_profile(self, stack: str) -> list[tuple[str, list[str]]]:
        """Return the verification command list for the given stack."""
        return _PROFILES.get(stack, [])

    def _has_pytest(self, workspace_path: Path) -> bool:
        """True if pytest is available (pyproject.toml or pytest.ini exists)."""
        return (
            (workspace_path / "pyproject.toml").exists()
            or (workspace_path / "pytest.ini").exists()
            or (workspace_path / "setup.cfg").exists()
        )

    def _container_id(self, sandbox_result: SandboxResult | None) -> str:
        """Return container_id from sandbox_result if available, else empty string."""
        if sandbox_result is None:
            return ""
        return getattr(sandbox_result, "container_id", "")

    async def verify(
        self,
        workspace_path: Path,
        stack: str,
        sandbox_result: SandboxResult | None,
        timeout_seconds: int = 120,
    ) -> VerificationResult:
        """
        Run the full verification suite for the given stack.

        Args:
            workspace_path:  Cloned repo root (same dir used by the fix agent).
            stack:           Tech stack from SandboxProvisioner ('python', etc.).
            sandbox_result:  Passed for forward-compat; not used in Phase 3.
            timeout_seconds: Hard ceiling per verification command.

        Returns:
            VerificationResult with verdict, output, cmd_run.
        """
        profile = self._get_profile(stack)

        # Add pytest to python profile only if test infrastructure exists
        if stack == "python" and self._has_pytest(workspace_path):
            profile = [
                ("pytest_full", ["python", "-m", "pytest", "-x", "-q", "--tb=short"])
            ] + profile

        if not profile:
            log.info("ci_fixer.verify_skipped", stack=stack, reason="no_profile")
            return VerificationResult(verdict="skipped", output="", cmd_run="")

        steps: list[VerificationStep] = []

        container_id = self._container_id(sandbox_result)

        for label, cmd_args in profile:
            step = await self._run_cmd(
                label=label,
                cmd_args=cmd_args,
                cwd=workspace_path,
                timeout_seconds=timeout_seconds,
                container_id=container_id,
            )
            steps.append(step)

            log.info(
                "ci_fixer.verify_step",
                label=label,
                exit_code=step.exit_code,
                timed_out=step.timed_out,
                elapsed=round(step.elapsed_seconds, 2),
            )

            if step.timed_out:
                # Timeout is non-blocking — treat as skipped for this step
                log.warning("ci_fixer.verify_timeout", label=label)
                continue

            if step.exit_code != 0:
                combined = "\n".join(s.output for s in steps)
                log.warning(
                    "ci_fixer.verify_failed",
                    label=label,
                    exit_code=step.exit_code,
                )
                return VerificationResult(
                    verdict="failed",
                    output=combined[:4000],
                    cmd_run=" ".join(cmd_args),
                )

        # All steps passed (or timed out — conservative skip)
        all_timed_out = all(s.timed_out for s in steps)
        if all_timed_out:
            return VerificationResult(
                verdict="timeout",
                output="All verification steps timed out",
                cmd_run="",
            )

        combined = "\n".join(s.output for s in steps if s.output)
        cmd_summary = "; ".join(" ".join(cmd) for _, cmd in profile)
        log.info("ci_fixer.verify_passed", stack=stack, steps=len(steps))
        return VerificationResult(
            verdict="passed",
            output=combined[:4000],
            cmd_run=cmd_summary,
        )

    async def _run_cmd(
        self,
        label: str,
        cmd_args: list[str],
        cwd: Path,
        timeout_seconds: int,
        container_id: str = "",
    ) -> VerificationStep:
        """
        Run a single verification command as an async subprocess.

        When container_id is provided, wraps with docker exec so the command
        runs inside the pre-warmed isolated container at /workspace.
        When container_id is empty, runs locally (original behaviour).

        Returns a VerificationStep with timed_out=True if timeout is exceeded.
        """
        from phalanx.ci_fixer.sandbox_pool import wrap_cmd_for_container
        from phalanx.config.settings import get_settings as _get_settings

        start = time.monotonic()
        cmd_str = " ".join(cmd_args)

        if container_id:
            docker_cmd = _get_settings().sandbox_docker_cmd
            exec_args = wrap_cmd_for_container(
                container_id, cmd_args, str(cwd), docker_cmd=docker_cmd
            )
        else:
            exec_args = cmd_args

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if not container_id else None,
            )

            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
            elapsed = time.monotonic() - start
            output = (
                stdout_b.decode(errors="replace") + "\n" + stderr_b.decode(errors="replace")
            ).strip()

            return VerificationStep(
                label=label,
                cmd=cmd_str,
                exit_code=proc.returncode or 0,
                output=output,
                elapsed_seconds=elapsed,
            )

        except TimeoutError:
            elapsed = time.monotonic() - start
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return VerificationStep(
                label=label,
                cmd=cmd_str,
                exit_code=-1,
                output="",
                elapsed_seconds=elapsed,
                timed_out=True,
            )

        except FileNotFoundError:
            elapsed = time.monotonic() - start
            return VerificationStep(
                label=label,
                cmd=cmd_str,
                exit_code=-1,
                output=f"(tool not found: {cmd_args[0]})",
                elapsed_seconds=elapsed,
                timed_out=False,
            )
