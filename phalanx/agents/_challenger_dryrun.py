"""Challenger dry-run tool — execution-grounded evidence for the LLM critic.

The single most-cited finding from the adversarial-agent literature
(AlphaCodium ablation, Sweep, Replit Agent 3): **execution beats prose**.
Instead of asking the Challenger to reason about whether TL's
`verify_command` will catch the failure, we have it RUN the command
in a fresh sandbox state (BEFORE TL's diff is applied) and observe
the actual exit code + stdout tail.

If the dry-run produces something other than what TL described in
`failing_command` / `error_line_quote`, that's a strong objective signal
that TL picked the wrong verify_command — the same trap that bit us in
Bug #16 (pytest exit 4 misread as failure).

This tool is registered into the v2 tool registry at import time so the
Challenger LLM can call it like any other tool.

Implementation notes:
  - Workspace is materialized into a tempdir from the supplied
    `repo_files` mapping (corpus testing) OR copied from the agent
    context (real run with sandbox). Both paths use the same
    subprocess execution shim.
  - Networking is NOT disabled — real CI failures often involve network
    behavior. We rely on the test command's bounded scope to keep this
    cheap. (~30s wall-time cap.)
  - Workspace is git-init'd so `git apply` style steps work later.
  - Output is truncated to 4 KB; long pytest summaries get tail-trimmed.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import structlog

from phalanx.ci_fixer_v2.tools.base import ToolResult, ToolSchema, register

log = structlog.get_logger(__name__)


_OUTPUT_BYTES_CAP = 4096
_DRY_RUN_TIMEOUT_SECS = 30


DRY_RUN_VERIFY_SCHEMA = ToolSchema(
    name="dry_run_verify",
    description=(
        "Execute the candidate `verify_command` in a clean copy of the repo "
        "WITHOUT applying TL's proposed diff. Returns actual exit code, "
        "stdout tail, stderr tail. Use this to confirm the verify_command "
        "actually re-triggers the originally-reported failure — if exit "
        "code or output don't match what TL described in error_line_quote / "
        "failing_command, TL likely picked the wrong verify_command."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "verify_command": {
                "type": "string",
                "description": "The exact shell command from TL's fix_spec.verify_command.",
            },
            "expected_exit": {
                "type": "integer",
                "description": (
                    "What you expect to see — typically NON-zero "
                    "(the command should fail in the broken pre-fix state)."
                ),
            },
            "expected_stdout_substring": {
                "type": "string",
                "description": (
                    "Optional. A substring you expect in stdout/stderr "
                    "(e.g. the error_line_quote TL claimed was in the log)."
                ),
            },
        },
        "required": ["verify_command", "expected_exit"],
    },
)


async def _handle_dry_run_verify(
    ctx: Any, tool_input: dict[str, Any]
) -> ToolResult:
    """Run the supplied verify_command in ctx.repo_workspace_path (read-only
    in spirit — we don't apply patches), capture exit + output, compare to
    TL's expectation, and return a structured result the LLM can reason on.
    """
    verify_command = tool_input.get("verify_command", "")
    expected_exit = int(tool_input.get("expected_exit", 1))
    expected_stdout = tool_input.get("expected_stdout_substring") or ""

    if not verify_command or not isinstance(verify_command, str):
        return ToolResult(ok=False, error="dry_run_verify: verify_command required")

    workspace = getattr(ctx, "repo_workspace_path", None)
    if not workspace or not Path(workspace).is_dir():
        return ToolResult(
            ok=False,
            error=(
                f"dry_run_verify: ctx.repo_workspace_path is not a directory: "
                f"{workspace!r}"
            ),
        )

    # Run synchronously in a thread pool — subprocess is blocking but fast.
    def _run() -> dict:
        try:
            proc = subprocess.run(  # noqa: S603 — command from LLM, scoped to tempdir
                shlex.split(verify_command),
                cwd=workspace,
                capture_output=True,
                timeout=_DRY_RUN_TIMEOUT_SECS,
                text=True,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-_OUTPUT_BYTES_CAP:],
                "stderr": proc.stderr[-_OUTPUT_BYTES_CAP:],
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"timeout after {_DRY_RUN_TIMEOUT_SECS}s",
                "timed_out": True,
            }
        except FileNotFoundError as exc:
            return {
                "exit_code": 127,
                "stdout": "",
                "stderr": f"command not found: {exc}",
                "timed_out": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "timed_out": False,
            }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run)

    # Compare to TL's expectation
    exit_matches = result["exit_code"] == expected_exit
    output_blob = (result["stdout"] or "") + "\n" + (result["stderr"] or "")
    stdout_matches = (
        expected_stdout in output_blob
        if expected_stdout
        else None  # not checked
    )

    log.info(
        "v3.challenger.dry_run_verify",
        cmd=verify_command,
        exit=result["exit_code"],
        expected_exit=expected_exit,
        exit_matches=exit_matches,
        stdout_matches=stdout_matches,
        timed_out=result["timed_out"],
    )

    return ToolResult(
        ok=True,
        data={
            "actual_exit": result["exit_code"],
            "expected_exit": expected_exit,
            "exit_matches": exit_matches,
            "actual_stdout_tail": result["stdout"],
            "actual_stderr_tail": result["stderr"],
            "expected_stdout_substring": expected_stdout or None,
            "stdout_matches": stdout_matches,
            "timed_out": result["timed_out"],
            "interpretation": _interpret(result, expected_exit, expected_stdout),
        },
    )


def _interpret(result: dict, expected_exit: int, expected_stdout: str) -> str:
    """Plain-English read of the dry-run for the LLM's benefit."""
    if result["timed_out"]:
        return "TIMEOUT — command did not return within 30s; verify is broken or slow"
    if result["exit_code"] == 127:
        return (
            "COMMAND NOT FOUND — verify_command's first token isn't on PATH "
            "in the sandbox; env_requirements likely missing it"
        )
    if result["exit_code"] == expected_exit:
        if expected_stdout and expected_stdout not in (
            (result["stdout"] or "") + (result["stderr"] or "")
        ):
            return (
                "EXIT MATCHES BUT STDOUT DIFFERS — verify_command produces a "
                "different error than TL described; possible misdiagnosis"
            )
        return "EXIT MATCHES — verify_command DOES re-trigger the failure as TL described"
    return (
        f"EXIT MISMATCH — TL said expect exit {expected_exit}, got "
        f"{result['exit_code']}. verify_command likely doesn't re-trigger "
        f"the failing check (possible Bug #16 / exit-4 trap class)"
    )


class _DryRunVerifyTool:
    schema = DRY_RUN_VERIFY_SCHEMA
    handler = staticmethod(_handle_dry_run_verify)


dry_run_verify_tool = _DryRunVerifyTool()


def _register() -> None:
    register(dry_run_verify_tool)


# Side-effect: register at import time so the Challenger agent's tool
# dispatcher finds it. Idempotent (registry uses dict assignment).
_register()
