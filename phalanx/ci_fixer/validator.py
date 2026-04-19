"""
CI Fix Validator — re-runs the failing command in the workspace to verify the fix.

Phase 1 additions vs the original:
  1. Tool version capture — every ValidationResult carries tool_version ("ruff 0.4.1").
     Surfaced in the draft PR body so reviewers can verify env parity.
  2. Regression check — after the primary per-file check passes, the broader
     codebase is scanned for NEW errors introduced by the patch.
     A fix that breaks other files is treated as failed.
  3. CI-parity discovery — reads .github/workflows/*.yml in the workspace to
     discover the exact commands the CI runs (e.g. ruff format --check, mypy flags,
     pytest --cov-fail-under). Falls back to sensible defaults when no CI config
     is found, so it works for any GitHub Actions repo generically.

Supports: ruff, mypy, pytest, tsc, eslint.
Unknown tools → skipped (passed=True, explicit log).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from phalanx.ci_fixer.log_parser import LintError, ParsedLog
    from phalanx.ci_fixer.log_parser import TypeError as TypeErr

# ── CI config discovery ────────────────────────────────────────────────────────

# Regexes to extract tool commands from CI YAML step `run:` blocks
_YAML_RUN_RE = re.compile(r"^\s*run:\s*[|>]?\s*(.+)$", re.MULTILINE)
# Multi-line run blocks (|- or |) — capture everything indented under `run:`
_YAML_RUN_BLOCK_RE = re.compile(r"run:\s*\|[-]?\n((?:[ \t]+.+\n?)*)", re.MULTILINE)


def _discover_ci_commands(tool: str, workspace: Path) -> dict:
    """
    Read .github/workflows/*.yml in the workspace and extract commands relevant
    to the given tool.

    Returns a dict with tool-specific flags discovered from CI, e.g.:
      ruff  → {"run_format_check": True}
      mypy  → {"extra_flags": ["--ignore-missing-imports"]}
      pytest → {"cov_fail_under": 70, "extra_flags": ["-x"]}

    Falls back to empty/False defaults when CI YAML is absent or tool not found.
    This ensures the validator stays generic across any GitHub Actions repo.
    """
    workflows_dir = workspace / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return {}

    all_run_lines: list[str] = []
    for yml_file in workflows_dir.glob("*.yml"):
        try:
            text = yml_file.read_text(errors="replace")
            # Extract inline run: value lines
            for m in _YAML_RUN_RE.finditer(text):
                all_run_lines.append(m.group(1).strip())
            # Extract multi-line run block lines
            for m in _YAML_RUN_BLOCK_RE.finditer(text):
                for line in m.group(1).splitlines():
                    stripped = line.strip()
                    if stripped:
                        all_run_lines.append(stripped)
        except Exception:
            continue

    if tool == "ruff":
        return _discover_ruff_config(all_run_lines)
    if tool == "mypy":
        return _discover_mypy_config(all_run_lines)
    if tool == "pytest":
        return _discover_pytest_config(all_run_lines)
    return {}


def _discover_ruff_config(run_lines: list[str]) -> dict:
    """Detect whether CI runs `ruff format --check` in addition to `ruff check`."""
    run_format_check = any(
        "ruff" in line
        and "format" in line
        and ("--check" in line or "check" in line.split("format")[-1])
        for line in run_lines
    )
    return {"run_format_check": run_format_check}


def _discover_mypy_config(run_lines: list[str]) -> dict:
    """Extract extra mypy flags used in CI (e.g. --ignore-missing-imports)."""
    extra_flags: list[str] = []
    known_flags = [
        "--ignore-missing-imports",
        "--strict",
        "--disallow-untyped-defs",
        "--no-implicit-optional",
        "--warn-return-any",
        "--warn-unused-ignores",
        "--check-untyped-defs",
    ]
    for line in run_lines:
        if "mypy" not in line:
            continue
        for flag in known_flags:
            if flag in line and flag not in extra_flags:
                extra_flags.append(flag)
    return {"extra_flags": extra_flags}


def _discover_pytest_config(run_lines: list[str]) -> dict:
    """Extract pytest --cov-fail-under threshold and common flags from CI."""
    cov_fail_under: int | None = None
    extra_flags: list[str] = []
    for line in run_lines:
        if "pytest" not in line:
            continue
        m = re.search(r"--cov-fail-under[=\s]+(\d+)", line)
        if m:
            cov_fail_under = int(m.group(1))
        for flag in ("-x", "--tb=short", "--tb=long", "--tb=no", "-q", "-v"):
            if flag in line.split() and flag not in extra_flags:
                extra_flags.append(flag)
    return {"cov_fail_under": cov_fail_under, "extra_flags": extra_flags}


log = structlog.get_logger(__name__)

_VALIDATE_TIMEOUT = 120  # seconds per subprocess call
_VERSION_TIMEOUT = 5  # seconds for --version queries


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

    ci_config = _discover_ci_commands(tool, workspace)

    if tool == "ruff":
        result = _run_ruff(workspace, files, tool_version, ci_config)
    elif tool == "mypy":
        result = _run_mypy(workspace, files, tool_version, ci_config)
    elif tool == "pytest":
        result = _run_pytest(workspace, parsed_log, tool_version, ci_config)
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
                f"{getattr(e, 'file', '?')}:{getattr(e, 'line', '?')} {getattr(e, 'code', getattr(e, 'message', ''))}"
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


def _run_ruff(
    workspace: Path, files: list[str], tool_version: str, ci_config: dict | None = None
) -> ValidationResult:
    targets = files if files else ["."]
    ci_config = ci_config or {}

    # Step 1: ruff check (lint)
    code, output = _run(["ruff", "check"] + targets, workspace)
    if code != 0:
        log.info("ci_validator.ruff_check", passed=False, files=files, version=tool_version)
        return ValidationResult(passed=False, tool="ruff", output=output, tool_version=tool_version)

    # Step 2: ruff format --check — only if CI actually runs it
    if ci_config.get("run_format_check"):
        fmt_code, fmt_output = _run(["ruff", "format", "--check"] + targets, workspace)
        combined = (output + "\n" + fmt_output).strip()
        passed = fmt_code == 0
        log.info("ci_validator.ruff_format_check", passed=passed, files=files, version=tool_version)
        return ValidationResult(
            passed=passed, tool="ruff", output=combined, tool_version=tool_version
        )

    log.info("ci_validator.ruff", passed=True, files=files, version=tool_version)
    return ValidationResult(passed=True, tool="ruff", output=output, tool_version=tool_version)


def _run_mypy(
    workspace: Path, files: list[str], tool_version: str, ci_config: dict | None = None
) -> ValidationResult:
    targets = files if files else ["."]
    ci_config = ci_config or {}
    extra_flags: list[str] = ci_config.get("extra_flags", [])
    code, output = _run(["mypy"] + extra_flags + targets, workspace)
    passed = code == 0
    log.info(
        "ci_validator.mypy", passed=passed, files=files, flags=extra_flags, version=tool_version
    )
    return ValidationResult(passed=passed, tool="mypy", output=output, tool_version=tool_version)


def _run_pytest(
    workspace: Path, parsed_log: ParsedLog, tool_version: str, ci_config: dict | None = None
) -> ValidationResult:
    ci_config = ci_config or {}
    test_files = list({f.file for f in parsed_log.test_failures})
    targets = test_files if test_files else ["tests/"]

    base_flags = ["-x", "-q"]
    # Apply extra CI flags discovered (e.g. --tb=short), avoiding duplicates
    for flag in ci_config.get("extra_flags", []):
        if flag not in base_flags:
            base_flags.append(flag)

    # Apply coverage threshold if CI enforces one and we're running the full suite
    cov_fail_under: int | None = ci_config.get("cov_fail_under")
    cov_flags: list[str] = []
    if cov_fail_under is not None and not test_files:
        cov_flags = [f"--cov-fail-under={cov_fail_under}"]

    cmd = ["python", "-m", "pytest"] + base_flags + cov_flags + targets
    code, output = _run(cmd, workspace)
    passed = code == 0
    log.info(
        "ci_validator.pytest",
        passed=passed,
        files=targets,
        cov_threshold=cov_fail_under,
        version=tool_version,
    )
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
    for le in original_parsed.lint_errors:
        pre_existing.add((le.file, le.code))
    for te in original_parsed.type_errors:
        pre_existing.add((te.file, getattr(te, "code", te.message[:30])))

    regressions: list[LintError | TypeErr] = []
    for le in new_parsed.lint_errors:
        if (le.file, le.code) not in pre_existing:
            regressions.append(le)
    for te in new_parsed.type_errors:
        key = (te.file, getattr(te, "code", te.message[:30]))
        if key not in pre_existing:
            regressions.append(te)

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
