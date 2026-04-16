"""
ReproducerAgent — runs the CI reproducer command in the provisioned sandbox
and classifies the outcome.

Verdicts:
  confirmed    — sandbox reproduced the same failure (exit != 0, pattern match)
  flaky        — command passed in sandbox → CI failure was transient
  env_mismatch — command failed with a DIFFERENT error → wrong environment
  timeout      — reproducer command exceeded sandbox_timeout_seconds
  skipped      — no sandbox available (sandbox_enabled=False or provision failed)

Design:
  - Phase 2: runs the command as a local subprocess (no Docker yet).
    The sandbox_result is accepted for forward-compat with Phase 3's real
    container exec; in Phase 2 its presence simply gates whether to attempt.
  - asyncio.create_subprocess_shell is used because reproducer_cmd is a
    string that may contain flags, pipes, or argument lists the shell parses.
  - Timeout is enforced via asyncio.wait_for; the process is killed on breach.
  - Output matching is conservative: if tool name OR any error code appears
    in stdout/stderr we call it "confirmed".  False negatives (wrongly saying
    env_mismatch) are worse than false positives here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from phalanx.ci_fixer.context import ReproductionResult

if TYPE_CHECKING:
    from pathlib import Path

    from phalanx.ci_fixer.context import StructuredFailure
    from phalanx.ci_fixer.sandbox import SandboxResult

log = structlog.get_logger(__name__)


@dataclass
class ReproductionAttempt:
    """Raw result of a single subprocess execution — internal to this module."""

    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False


class ReproducerAgent:
    """
    Runs the reproducer command and classifies the CI failure.

    One instance per pipeline run; no shared state between calls.
    """

    def _output_matches_failure(
        self,
        output: str,
        structured_failure: StructuredFailure,
    ) -> bool:
        """
        Return True if stdout/stderr output looks like the original CI failure.

        Conservative check — matches if:
          1. The tool name appears anywhere in the output (e.g. "ruff"), OR
          2. Any structured error code appears (e.g. "F401", "E501", "TS2345").

        Lowercase comparison for tool name; error codes are case-sensitive.
        """
        lowered = output.lower()

        # Match 1: tool name anywhere in output
        if structured_failure.tool.lower() in lowered:
            return True

        # Match 2: any parsed error code in output
        errors: list[dict[str, Any]] = structured_failure.errors or []
        for err in errors:
            code = err.get("code", "")
            if code and code in output:
                return True

        return False

    async def reproduce(
        self,
        reproducer_cmd: str,
        workspace_path: Path,
        sandbox_result: SandboxResult | None,
        structured_failure: StructuredFailure,
        timeout_seconds: int = 120,
    ) -> ReproductionResult:
        """
        Execute reproducer_cmd and return a classified ReproductionResult.

        Args:
            reproducer_cmd:    The exact command CI ran (e.g. "ruff check .").
            workspace_path:    Working directory for the subprocess.
            sandbox_result:    From SandboxProvisioner; None or available=False → skip.
            structured_failure: Parsed failure context used for output matching.
            timeout_seconds:   Hard ceiling on subprocess wall time.

        Returns:
            ReproductionResult with verdict, exit_code, output, reproducer_cmd.
        """
        # ── Gate: no sandbox or sandbox unavailable ───────────────────────────
        if sandbox_result is None or not sandbox_result.available:
            log.info("ci_fixer.reproduce_skipped", reason="no_sandbox")
            return ReproductionResult(
                verdict="skipped",
                reproducer_cmd=reproducer_cmd,
            )

        # ── Gate: empty command ───────────────────────────────────────────────
        if not reproducer_cmd or not reproducer_cmd.strip():
            log.info("ci_fixer.reproduce_skipped", reason="empty_cmd")
            return ReproductionResult(
                verdict="skipped",
                reproducer_cmd=reproducer_cmd,
            )

        # ── Run subprocess ────────────────────────────────────────────────────
        attempt = await self._run_subprocess(
            cmd=reproducer_cmd,
            cwd=workspace_path,
            timeout_seconds=timeout_seconds,
        )

        combined_output = (attempt.stdout + "\n" + attempt.stderr).strip()

        log.info(
            "ci_fixer.reproduce_attempt",
            cmd=attempt.cmd,
            exit_code=attempt.exit_code,
            elapsed=round(attempt.elapsed_seconds, 2),
            timed_out=attempt.timed_out,
            output_chars=len(combined_output),
        )

        # ── Classify verdict ──────────────────────────────────────────────────
        if attempt.timed_out:
            verdict = "timeout"
        elif attempt.exit_code == 0:
            # Command passed in our environment → CI failure was transient/flaky
            verdict = "flaky"
        elif self._output_matches_failure(combined_output, structured_failure):
            # Same tool/error codes reproduced → confirmed
            verdict = "confirmed"
        else:
            # Failed differently → different environment / missing dependencies
            verdict = "env_mismatch"

        log.info(
            "ci_fixer.reproduced",
            verdict=verdict,
            exit_code=attempt.exit_code,
            cmd=reproducer_cmd,
        )

        return ReproductionResult(
            verdict=verdict,
            exit_code=attempt.exit_code,
            output=combined_output[:4000],  # cap stored output
            reproducer_cmd=reproducer_cmd,
        )

    async def _run_subprocess(
        self,
        cmd: str,
        cwd: Path,
        timeout_seconds: int,
    ) -> ReproductionAttempt:
        """
        Run cmd as a shell subprocess with a hard timeout.

        Uses asyncio.create_subprocess_shell so the full command string
        (flags, quoted args, etc.) is interpreted by the shell.
        """
        start = time.monotonic()

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
            elapsed = time.monotonic() - start
            return ReproductionAttempt(
                cmd=cmd,
                exit_code=proc.returncode or 0,
                stdout=stdout_b.decode(errors="replace"),
                stderr=stderr_b.decode(errors="replace"),
                elapsed_seconds=elapsed,
                timed_out=False,
            )

        except TimeoutError:
            elapsed = time.monotonic() - start
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return ReproductionAttempt(
                cmd=cmd,
                exit_code=-1,
                stdout="",
                stderr="",
                elapsed_seconds=elapsed,
                timed_out=True,
            )
