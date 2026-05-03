"""v1.7 Engineer step interpreter — deterministic execution of TL-emitted plans.

The architectural premise (per docs/v17-tl-as-planner.md): TL is the sole
LLM thinker. Engineer is a typist that walks TL's `steps` list and applies
each one verbatim. No invention, no judgment — if a step's preconditions
don't hold (e.g., `replace.old` text isn't in the target file), engineer
reports `step_precondition_violated` and TL re-plans with fresh evidence.

This file implements the deterministic dispatcher. Call sites (cifix_engineer
agent, tier-1 tests, future SRE verify) pass a Step dict + a workspace path
and get back a StepResult. No Celery, no DB, no LLM — just bytes in / bytes
out so it's trivial to unit-test.

Step actions (from _v17_types.Step):
  read         — read file (informational; verifies path exists)
  replace      — exact-string replace; `old` must appear ≥ once in `file`
  insert       — insert `content` after `after_line` in `file`
  delete_lines — delete inclusive line range in `file`
  apply_diff   — git apply a unified diff (handles new files, renames, etc.)
  run          — subprocess; check exit + optional stdout substring
  commit       — git add -A + git commit -m
  push         — git push
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_RUN_TIMEOUT_SECS = 60
_OUTPUT_BYTES_CAP = 4096


@dataclass
class StepResult:
    """Outcome of executing one step.

    `ok=False` → engineer aborts the task with the rest of the steps
    skipped. `error` is a short machine-readable code; `detail` is a
    human-readable message including any stdout/stderr tail.
    """
    ok: bool
    step_id: int
    action: str
    error: str | None = None
    detail: str | None = None
    output: dict[str, Any] = field(default_factory=dict)


# ─── Per-action handlers ─────────────────────────────────────────────────────


def _resolve_in_workspace(
    workspace: Path, file_rel: str
) -> tuple[Path | None, str | None]:
    """Resolve `file_rel` under `workspace`; reject path traversal.

    Both sides are .resolve()-d to handle symlinks consistently. macOS
    tempdirs (`/var/folders/...` → `/private/var/...`) bit us; same for
    any system where the workspace path passed in might not be canonical.
    """
    if not file_rel or not isinstance(file_rel, str):
        return None, "file path is empty or not a string"
    workspace_resolved = workspace.resolve()
    target = (workspace_resolved / file_rel).resolve()
    try:
        target.relative_to(workspace_resolved)
    except ValueError:
        return None, f"file {file_rel!r} resolves outside workspace"
    return target, None


def _do_read(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    file_rel = step.get("file") or ""
    target, err = _resolve_in_workspace(workspace, file_rel)
    if err:
        return StepResult(ok=False, step_id=sid, action="read", error="bad_file_path", detail=err)
    assert target is not None
    if not target.is_file():
        return StepResult(
            ok=False, step_id=sid, action="read",
            error="file_missing",
            detail=f"file {file_rel!r} does not exist in workspace",
        )
    try:
        content = target.read_text(errors="replace")
    except OSError as exc:
        return StepResult(
            ok=False, step_id=sid, action="read",
            error="read_failed", detail=str(exc),
        )
    return StepResult(
        ok=True, step_id=sid, action="read",
        output={"file": file_rel, "len_bytes": len(content), "len_lines": content.count("\n") + 1},
    )


def _do_replace(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    file_rel = step.get("file") or ""
    old = step.get("old")
    new = step.get("new")
    if not isinstance(old, str) or old == "":
        return StepResult(
            ok=False, step_id=sid, action="replace",
            error="bad_old", detail="`old` must be a non-empty string",
        )
    if not isinstance(new, str):
        return StepResult(
            ok=False, step_id=sid, action="replace",
            error="bad_new", detail="`new` must be a string",
        )
    target, err = _resolve_in_workspace(workspace, file_rel)
    if err:
        return StepResult(ok=False, step_id=sid, action="replace", error="bad_file_path", detail=err)
    assert target is not None
    if not target.is_file():
        return StepResult(
            ok=False, step_id=sid, action="replace",
            error="file_missing", detail=f"{file_rel!r} not in workspace",
        )
    try:
        content = target.read_text(errors="replace")
    except OSError as exc:
        return StepResult(
            ok=False, step_id=sid, action="replace",
            error="read_failed", detail=str(exc),
        )
    if old not in content:
        preview = old[:80] + ("..." if len(old) > 80 else "")
        return StepResult(
            ok=False, step_id=sid, action="replace",
            error="step_precondition_violated",
            detail=f"`old` substring not found in {file_rel!r}: {preview!r}",
        )
    # First-occurrence replacement (TL must emit unique `old` per file/step)
    new_content = content.replace(old, new, 1)
    try:
        target.write_text(new_content)
    except OSError as exc:
        return StepResult(
            ok=False, step_id=sid, action="replace",
            error="write_failed", detail=str(exc),
        )
    return StepResult(
        ok=True, step_id=sid, action="replace",
        output={"file": file_rel, "bytes_diff": len(new_content) - len(content)},
    )


def _do_insert(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    file_rel = step.get("file") or ""
    after_line = step.get("after_line")
    content_to_insert = step.get("content")
    if not isinstance(after_line, int) or after_line < 0:
        return StepResult(
            ok=False, step_id=sid, action="insert",
            error="bad_after_line",
            detail=f"`after_line` must be int ≥ 0; got {after_line!r}",
        )
    if not isinstance(content_to_insert, str):
        return StepResult(
            ok=False, step_id=sid, action="insert",
            error="bad_content", detail="`content` must be a string",
        )
    target, err = _resolve_in_workspace(workspace, file_rel)
    if err:
        return StepResult(ok=False, step_id=sid, action="insert", error="bad_file_path", detail=err)
    assert target is not None
    if not target.is_file():
        return StepResult(
            ok=False, step_id=sid, action="insert",
            error="file_missing", detail=f"{file_rel!r} not in workspace",
        )
    lines = target.read_text(errors="replace").splitlines(keepends=True)
    if after_line > len(lines):
        return StepResult(
            ok=False, step_id=sid, action="insert",
            error="step_precondition_violated",
            detail=f"after_line={after_line} > file length ({len(lines)})",
        )
    payload = content_to_insert if content_to_insert.endswith("\n") else content_to_insert + "\n"
    new_lines = lines[:after_line] + [payload] + lines[after_line:]
    target.write_text("".join(new_lines))
    return StepResult(
        ok=True, step_id=sid, action="insert",
        output={"file": file_rel, "lines_added": payload.count("\n")},
    )


def _do_delete_lines(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    file_rel = step.get("file") or ""
    line = step.get("line")
    end_line = step.get("end_line", line)
    if not isinstance(line, int) or line < 1:
        return StepResult(
            ok=False, step_id=sid, action="delete_lines",
            error="bad_line", detail=f"`line` must be int ≥ 1; got {line!r}",
        )
    if not isinstance(end_line, int) or end_line < line:
        return StepResult(
            ok=False, step_id=sid, action="delete_lines",
            error="bad_end_line",
            detail=f"`end_line` must be int ≥ line; got {end_line!r}",
        )
    target, err = _resolve_in_workspace(workspace, file_rel)
    if err:
        return StepResult(ok=False, step_id=sid, action="delete_lines", error="bad_file_path", detail=err)
    assert target is not None
    if not target.is_file():
        return StepResult(
            ok=False, step_id=sid, action="delete_lines",
            error="file_missing", detail=f"{file_rel!r} not in workspace",
        )
    lines = target.read_text(errors="replace").splitlines(keepends=True)
    if end_line > len(lines):
        return StepResult(
            ok=False, step_id=sid, action="delete_lines",
            error="step_precondition_violated",
            detail=f"end_line={end_line} > file length ({len(lines)})",
        )
    # 1-indexed inclusive: del lines[line-1 : end_line]
    del lines[line - 1 : end_line]
    target.write_text("".join(lines))
    return StepResult(
        ok=True, step_id=sid, action="delete_lines",
        output={"file": file_rel, "lines_deleted": end_line - line + 1},
    )


def _do_apply_diff(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    diff = step.get("diff") or ""
    if not isinstance(diff, str) or not diff.strip():
        return StepResult(
            ok=False, step_id=sid, action="apply_diff",
            error="bad_diff", detail="`diff` must be a non-empty string",
        )
    # `git apply --check` first to detect malformed diffs without mutating.
    check = subprocess.run(  # noqa: S603 — fixed argv
        ["git", "apply", "--check", "-"],
        cwd=str(workspace), input=diff, capture_output=True, text=True,
    )
    if check.returncode != 0:
        return StepResult(
            ok=False, step_id=sid, action="apply_diff",
            error="diff_apply_check_failed",
            detail=(check.stderr or check.stdout or "")[:_OUTPUT_BYTES_CAP],
        )
    apply = subprocess.run(  # noqa: S603 — fixed argv
        ["git", "apply", "-"],
        cwd=str(workspace), input=diff, capture_output=True, text=True,
    )
    if apply.returncode != 0:
        return StepResult(
            ok=False, step_id=sid, action="apply_diff",
            error="diff_apply_failed",
            detail=(apply.stderr or apply.stdout or "")[:_OUTPUT_BYTES_CAP],
        )
    return StepResult(
        ok=True, step_id=sid, action="apply_diff",
        output={"diff_lines": diff.count("\n") + 1},
    )


def _do_run(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    cmd = step.get("command") or ""
    if not isinstance(cmd, str) or not cmd.strip():
        return StepResult(
            ok=False, step_id=sid, action="run",
            error="bad_command", detail="`command` must be a non-empty string",
        )
    expect_exit = step.get("expect_exit", 0)
    expect_stdout = step.get("expect_stdout_contains")
    try:
        proc = subprocess.run(  # noqa: S603 — command from TL plan, scoped to workspace
            shlex.split(cmd),
            cwd=str(workspace),
            capture_output=True, text=True, timeout=_RUN_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return StepResult(
            ok=False, step_id=sid, action="run",
            error="timeout",
            detail=f"command did not return within {_RUN_TIMEOUT_SECS}s: {cmd!r}",
        )
    except FileNotFoundError as exc:
        return StepResult(
            ok=False, step_id=sid, action="run",
            error="command_not_found", detail=str(exc),
        )
    output = {
        "exit": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-_OUTPUT_BYTES_CAP:],
        "stderr_tail": (proc.stderr or "")[-_OUTPUT_BYTES_CAP:],
    }
    if proc.returncode != expect_exit:
        return StepResult(
            ok=False, step_id=sid, action="run",
            error="run_unexpected_exit",
            detail=(
                f"expected exit {expect_exit}, got {proc.returncode}. "
                f"stderr_tail: {output['stderr_tail'][-500:]}"
            ),
            output=output,
        )
    if expect_stdout and expect_stdout not in (proc.stdout or "") + (proc.stderr or ""):
        return StepResult(
            ok=False, step_id=sid, action="run",
            error="run_stdout_mismatch",
            detail=f"expected substring {expect_stdout!r} not in stdout/stderr",
            output=output,
        )
    return StepResult(ok=True, step_id=sid, action="run", output=output)


def _do_commit(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    msg = step.get("message") or ""
    if not isinstance(msg, str) or not msg.strip():
        return StepResult(
            ok=False, step_id=sid, action="commit",
            error="bad_message", detail="`message` must be non-empty",
        )
    add = subprocess.run(  # noqa: S603
        ["git", "add", "-A"], cwd=str(workspace), capture_output=True, text=True,
    )
    if add.returncode != 0:
        return StepResult(
            ok=False, step_id=sid, action="commit",
            error="git_add_failed", detail=add.stderr[:_OUTPUT_BYTES_CAP],
        )
    commit = subprocess.run(  # noqa: S603
        ["git", "-c", "user.email=cifix@phalanx.local",
         "-c", "user.name=phalanx-cifix",
         "commit", "-m", msg],
        cwd=str(workspace), capture_output=True, text=True,
    )
    if commit.returncode != 0:
        # `nothing to commit` is a soft-failure for engineer (TL's diff was
        # already applied or empty); surface it but don't fail the run.
        stderr = (commit.stderr or "") + (commit.stdout or "")
        if "nothing to commit" in stderr.lower():
            return StepResult(
                ok=True, step_id=sid, action="commit",
                output={"commit_sha": None, "note": "nothing_to_commit"},
            )
        return StepResult(
            ok=False, step_id=sid, action="commit",
            error="git_commit_failed", detail=stderr[:_OUTPUT_BYTES_CAP],
        )
    head = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True,
    )
    sha = (head.stdout or "").strip() or None
    return StepResult(
        ok=True, step_id=sid, action="commit",
        output={"commit_sha": sha, "message": msg},
    )


def _do_push(step: dict, workspace: Path) -> StepResult:
    sid = int(step.get("id", 0))
    push = subprocess.run(  # noqa: S603
        ["git", "push"], cwd=str(workspace), capture_output=True, text=True,
        timeout=_RUN_TIMEOUT_SECS,
    )
    if push.returncode != 0:
        return StepResult(
            ok=False, step_id=sid, action="push",
            error="git_push_failed",
            detail=(push.stderr or push.stdout or "")[:_OUTPUT_BYTES_CAP],
        )
    return StepResult(ok=True, step_id=sid, action="push")


# ─── Dispatch ────────────────────────────────────────────────────────────────


_HANDLERS: dict[str, Any] = {
    "read": _do_read,
    "replace": _do_replace,
    "insert": _do_insert,
    "delete_lines": _do_delete_lines,
    "apply_diff": _do_apply_diff,
    "run": _do_run,
    "commit": _do_commit,
    "push": _do_push,
}


def execute_step(step: dict, workspace: str | Path) -> StepResult:
    """Dispatch one Step. All-sync; no LLM; no async. Tests can call directly.

    Returns StepResult; never raises. Unknown action → ok=False with
    error='unknown_action'.
    """
    sid = int(step.get("id", 0))
    action = step.get("action")
    handler = _HANDLERS.get(action)
    if handler is None:
        return StepResult(
            ok=False, step_id=sid, action=action or "?",
            error="unknown_action",
            detail=f"unknown step action {action!r}; valid: {sorted(_HANDLERS)}",
        )
    workspace_path = Path(workspace)
    if not workspace_path.is_dir():
        return StepResult(
            ok=False, step_id=sid, action=action,
            error="bad_workspace",
            detail=f"workspace path is not a directory: {workspace}",
        )
    try:
        return handler(step, workspace_path)
    except Exception as exc:  # noqa: BLE001
        log.exception("v3.engineer.step.unhandled_exception", action=action)
        return StepResult(
            ok=False, step_id=sid, action=action,
            error="handler_crashed",
            detail=f"{type(exc).__name__}: {exc}",
        )


@dataclass
class TaskExecutionResult:
    """Aggregated result of executing all steps in an engineer task."""
    ok: bool
    completed_steps: list[int] = field(default_factory=list)
    failed_step: StepResult | None = None
    commit_sha: str | None = None
    narrow_verify_passed: bool | None = None
    narrow_verify_detail: str | None = None


def execute_task_steps(steps: list[dict], workspace: str | Path) -> TaskExecutionResult:
    """Walk all steps in order. Stop at first failure.

    On success, extracts commit_sha from the most recent commit step.
    The caller (cifix_engineer agent) handles narrow_verify separately
    after this returns ok=True.
    """
    result = TaskExecutionResult(ok=True)
    for step in steps:
        outcome = execute_step(step, workspace)
        if not outcome.ok:
            result.ok = False
            result.failed_step = outcome
            return result
        result.completed_steps.append(outcome.step_id)
        if outcome.action == "commit" and outcome.output.get("commit_sha"):
            result.commit_sha = outcome.output["commit_sha"]
    return result


async def execute_task_steps_async(
    steps: list[dict], workspace: str | Path
) -> TaskExecutionResult:
    """Async wrapper for the agent's async loop. The interpreter itself is
    sync because subprocess is sync; we just hop to a thread pool to avoid
    blocking the event loop on long `run` steps.
    """
    return await asyncio.to_thread(execute_task_steps, steps, workspace)


__all__ = [
    "StepResult",
    "TaskExecutionResult",
    "execute_step",
    "execute_task_steps",
    "execute_task_steps_async",
]
