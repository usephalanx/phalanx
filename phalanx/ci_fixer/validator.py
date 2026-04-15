"""
CI Fix Validator — re-runs the failing command in the workspace to verify the fix.

Phase 1 additions vs the original:
  1. Tool version capture — every ValidationResult carries tool_version ("ruff 0.4.1").
     Surfaced in the draft PR body so reviewers can verify env parity.
  2. Regression check — after the primary per-file check passes, the broader
     codebase is scanned for NEW errors introduced by the patch.
     A fix that breaks other files is treated as failed.

Supports: ruff, mypy, pytest, tsc, eslint.
Unknown tools → skipped (passed=True, explicit log).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from phalanx.ci_fixer.log_parser import ParsedLog

log = structlog.get_logger(__name__)

_VALIDATE_TIMEOUT = 120   # seconds per subprocess call
_VERSION_TIMEOUT = 5      # seconds for --version queries


@dataclass
class ValidationResult:
    passed: bool
    tool: str
    output: str
    tool_version: str = ""
    regressions: list = field(default_factory=list)
    error: str = ""


def validate_fix(
    parsed_log: ParsedLog,
    workspace: Path,
    original_parsed: ParsedLog | None = None,
) -> ValidationResult:
    """
    Re-run the failing tool against the workspace to confirm the fix.

    Steps:
      1. Primary check: run tool on only the files that were failing.
         If exit code != 0 → failed immediately.
      2. Regression check (ruff/mypy only): run tool on the whole workspace,
         compare against original_parsed errors.  Any NEW error that was not
         in original_parsed → treat as regression → failed.
      3. Capture tool version string for transparency.

    Returns ValidationResult.passed=True only when BOTH checks pass.
    """
    tool = parsed_log.tool
    files = parsed_log.all_files[:6]
    tool_version = _get_tool_version(tool)

    if tool == "ruff":
        result = _run_ruff(workspace, files, tool_version)
    elif tool == "mypy":
        result = _run_mypy(workspace, files, tool_version)
    elif tool == "pytest":
        result = _run_pytest(workspace, parsed_log, tool_version)
    elif tool in ("tsc", "eslint"):
        result = _run_node_linter(workspace, tool, files, tool_version)
    else:
        log.info("ci_validator.skip", reason=f"no validator for tool={tool}")
        return ValidationResult(
            passed=True,
            tool=tool,
            output="(validation skipped — unknown tool)",
            tool_version=tool_version,
        )

    # Short-circuit: primary check failed — no point running regression check
    if not result.passed:
        return result

    # Regression check for ruff and mypy (fast, deterministic)
    if tool in ("ruff", "mypy") and original_parsed is not None:
        regressions = _regression_check(tool, workspace, original_parsed, tool_version)
        if regressions:
            reg_summary = "; ".join(
                f"{getattr(e,'file','?')}:{getattr(e,'line','?')} {getattr(e,'code',getattr(e,'message',''))}"
                for e in regressions[:5]
            )
            log.warning(
                "ci_validator.regressions_found",
                count=len(regressions),
                summary=reg_summary,
            )
            return ValidationResult(
                passed=False,
                tool=tool,
                output=f"Fix introduced {len(regressions)} new error(s): {reg_summary}",
                tool_version=tool_version,
                regressions=regressions,
            )

    return result


# ── Tool runners ───────────────────────────────────────────────────────────────


def _run_ruff(workspace: Path, files: list[str], tool_version: str) -> ValidationResult:
    targets = files if files else ["."]
    code, output = _run(["ruff", "check"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.ruff", passed=passed, files=files, version=tool_version)
    return ValidationResult(passed=passed, tool="ruff", output=output, tool_version=tool_version)


def _run_mypy(workspace: Path, files: list[str], tool_version: str) -> ValidationResult:
    targets = files if files else ["."]
    code, output = _run(["mypy"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.mypy", passed=passed, files=files, version=tool_version)
    return ValidationResult(passed=passed, tool="mypy", output=output, tool_version=tool_version)


def _run_pytest(
    workspace: Path, parsed_log: ParsedLog, tool_version: str
) -> ValidationResult:
    test_files = list({f.file for f in parsed_log.test_failures})
    targets = test_files if test_files else ["tests/"]
    code, output = _run(["python", "-m", "pytest", "-x", "-q"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.pytest", passed=passed, files=targets, version=tool_version)
    return ValidationResult(passed=passed, tool="pytest", output=output, tool_version=tool_version)


def _run_node_linter(
    workspace: Path, tool: str, files: list[str], tool_version: str
) -> ValidationResult:
    if tool == "tsc":
        code, output = _run(["npx", "tsc", "--noEmit"], workspace)
    else:
        targets = files if files else ["."]
        code, output = _run(["npx", "eslint"] + targets, workspace)
    passed = code == 0
    log.info("ci_validator.node", tool=tool, passed=passed, version=tool_version)
    return ValidationResult(passed=passed, tool=tool, output=output, tool_version=tool_version)


# ── Regression check ───────────────────────────────────────────────────────────


def _regression_check(
    tool: str,
    workspace: Path,
    original_parsed: ParsedLog,
    tool_version: str,
) -> list:
    """
    Run the tool on the full workspace and return any errors that were NOT
    present in original_parsed (i.e., newly introduced by our patch).

    Returns a (possibly empty) list of LintError / TypeError objects.
    """
    from phalanx.ci_fixer.log_parser import parse_log  # noqa: PLC0415

    if tool == "ruff":
        code, output = _run(["ruff", "check", "."], workspace)
    elif tool == "mypy":
        code, output = _run(["mypy", "."], workspace)
    else:
        return []

    if code == 0:
        return []  # clean — no regressions possible

    new_parsed = parse_log(output)

    # Build set of pre-existing (file, code) pairs
    pre_existing: set[tuple[str, str]] = set()
    for e in original_parsed.lint_errors:
        pre_existing.add((e.file, e.code))
    for e in original_parsed.type_errors:
        pre_existing.add((e.file, getattr(e, "code", e.message[:30])))

    regressions = []
    for e in new_parsed.lint_errors:
        if (e.file, e.code) not in pre_existing:
            regressions.append(e)
    for e in new_parsed.type_errors:
        key = (e.file, getattr(e, "code", e.message[:30]))
        if key not in pre_existing:
            regressions.append(e)

    return regressions


# ── Subprocess helper ──────────────────────────────────────────────────────────


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


def _get_tool_version(tool: str) -> str:
    """
    Capture the installed version of the tool (e.g. "ruff 0.4.1").
    Used to surface env-parity info in the draft PR body.
    Never raises — returns empty string on any failure.
    """
    version_cmds: dict[str, list[str]] = {
        "ruff": ["ruff", "--version"],
        "mypy": ["mypy", "--version"],
        "pytest": ["python", "-m", "pytest", "--version"],
        "tsc": ["npx", "tsc", "--version"],
        "eslint": ["npx", "eslint", "--version"],
    }
    cmd = version_cmds.get(tool)
    if not cmd:
        return ""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT,
        )
        version_line = (result.stdout + result.stderr).strip().splitlines()[0]
        return version_line[:80]
    except Exception:
        return ""
