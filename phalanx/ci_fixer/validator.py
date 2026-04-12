"""
CI Fix Validator — re-runs the failing command in the workspace to verify the fix.

Deterministic: no LLM. Just runs the same tool that failed in CI and checks exit code.
Supports: ruff, mypy, pytest, tsc, eslint.

Used after the builder applies the fix — if validation fails, the pipeline
loops back to the analyst with the new output (max 2 iterations).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from phalanx.ci_fixer.log_parser import ParsedLog

log = structlog.get_logger(__name__)

# Timeout per validation run (seconds)
_VALIDATE_TIMEOUT = 120


@dataclass
class ValidationResult:
    passed: bool
    tool: str
    output: str  # stdout+stderr of the re-run
    error: str = ""


def validate_fix(parsed_log: ParsedLog, workspace: Path) -> ValidationResult:
    """
    Re-run the failing tool against the workspace to confirm the fix works.

    Returns ValidationResult.passed=True if the tool exits 0.
    """
    tool = parsed_log.tool
    files = parsed_log.all_files[:6]  # only check files that were failing

    if tool == "ruff":
        return _run_ruff(workspace, files)
    elif tool == "mypy":
        return _run_mypy(workspace, files)
    elif tool == "pytest":
        return _run_pytest(workspace, parsed_log)
    elif tool in ("tsc", "eslint"):
        return _run_node_linter(workspace, tool, files)
    else:
        # Unknown tool — skip validation, let it through
        log.info("ci_validator.skip", reason=f"no validator for tool={tool}")
        return ValidationResult(passed=True, tool=tool, output="(validation skipped — unknown tool)")


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command in the workspace, return (returncode, combined output)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_VALIDATE_TIMEOUT,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 1, f"(validation timed out after {_VALIDATE_TIMEOUT}s)"
    except FileNotFoundError:
        return 1, f"(tool not found: {cmd[0]})"
    except Exception as exc:
        return 1, f"(validation error: {exc})"


def _run_ruff(workspace: Path, files: list[str]) -> ValidationResult:
    targets = files if files else ["."]
    code, output = _run(["ruff", "check"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.ruff", passed=passed, files=files)
    return ValidationResult(passed=passed, tool="ruff", output=output)


def _run_mypy(workspace: Path, files: list[str]) -> ValidationResult:
    targets = files if files else ["."]
    code, output = _run(["mypy"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.mypy", passed=passed, files=files)
    return ValidationResult(passed=passed, tool="mypy", output=output)


def _run_pytest(workspace: Path, parsed_log: "ParsedLog") -> ValidationResult:
    # Run only the failing test files to keep it fast
    test_files = list({f.file for f in parsed_log.test_failures})
    targets = test_files if test_files else ["tests/"]
    code, output = _run(["python", "-m", "pytest", "-x", "-q"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.pytest", passed=passed, files=targets)
    return ValidationResult(passed=passed, tool="pytest", output=output)


def _run_node_linter(workspace: Path, tool: str, files: list[str]) -> ValidationResult:
    if tool == "tsc":
        code, output = _run(["npx", "tsc", "--noEmit"], workspace)
    else:
        targets = files if files else ["."]
        code, output = _run(["npx", "eslint"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.node", tool=tool, passed=passed)
    return ValidationResult(passed=passed, tool=tool, output=output)
