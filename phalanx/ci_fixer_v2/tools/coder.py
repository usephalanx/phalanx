"""Coder-scoped tools: apply_patch (subagent-only) + delegate_to_coder
(main-agent-only).

apply_patch is intentionally restricted to the coder subagent's tool
allow-list (see ci_fixer_v2.coder_subagent.ALLOWED_CODER_TOOLS). The main
agent must go through delegate_to_coder; it cannot mutate the workspace
directly. This keeps the main-agent's decision scope focused on
diagnosis + coordination while patch application + sandbox verification
happens inside a scoped, short-loop subagent.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from phalanx.ci_fixer_v2.context import AgentContext
from phalanx.ci_fixer_v2.tools.base import (
    ToolResult,
    ToolSchema,
    register,
)

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Workspace → sandbox sync
# ─────────────────────────────────────────────────────────────────────────────


async def _copy_file_into_sandbox(
    container_id: str, workspace_path: str, rel_path: str, timeout: int = 30
) -> tuple[bool, str]:
    """Copy a single file from the host workspace into the sandbox's
    `/workspace/` tree via `docker cp`.

    Why this exists: the v2 sandbox is provisioned with a one-shot
    `docker cp` of the whole workspace at checkout time — not a live
    bind mount. When apply_patch modifies a file on the host workspace,
    the sandbox still has the pre-patch copy, so `ruff check .` /
    `pytest` / etc. run against stale content and report the same
    failure forever. This helper keeps the two views in sync per-patch.

    Returns (ok, stderr). Tests patch this symbol directly.
    """
    import asyncio

    src = f"{workspace_path}/{rel_path}"
    dst = f"{container_id}:/workspace/{rel_path}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "cp",
            src,
            dst,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return False, f"docker_binary_missing: {exc}"
    try:
        _, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return False, f"docker cp timed out after {timeout}s"
    if proc.returncode != 0:
        return False, err_b.decode("utf-8", errors="replace")
    return True, ""


async def _sync_patched_files_to_sandbox(
    container_id: str, workspace_path: str, files: list[str]
) -> list[tuple[str, str]]:
    """Copy each file in `files` from host → sandbox. Returns a list of
    (file, error) for anything that failed; empty list means everything
    synced. Continues on per-file failure so a single bad file doesn't
    block the rest."""
    failures: list[tuple[str, str]] = []
    for rel in files:
        ok, err = await _copy_file_into_sandbox(container_id, workspace_path, rel)
        if not ok:
            failures.append((rel, err))
    return failures


# ─────────────────────────────────────────────────────────────────────────────
# Shared git-with-stdin seam
# ─────────────────────────────────────────────────────────────────────────────


async def _run_git_with_stdin(
    workspace: str, args: list[str], stdin_bytes: bytes, timeout: int = 60
) -> tuple[int, str, str]:
    """Run `git -C {workspace} {args...}` and pipe `stdin_bytes` to stdin."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            workspace,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"git_binary_missing: {exc}") from exc

    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin_bytes), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # pragma: no cover
            pass
        return (124, "", "git command timed out")
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# apply_patch  (subagent-only)
# ─────────────────────────────────────────────────────────────────────────────

APPLY_PATCH_SCHEMA = ToolSchema(
    name="apply_patch",
    description=(
        "Apply a unified diff to the workspace. The patch MUST touch only "
        "files listed in target_files — any other path is rejected as "
        "out-of-scope. `git apply --check` runs first; only clean patches "
        "are applied. After a successful apply, sandbox verification is "
        "invalidated: you must run_in_sandbox the original failing "
        "command before the main agent can commit."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "diff": {
                "type": "string",
                "description": "Unified diff (git-apply compatible).",
            },
            "target_files": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Files this patch is permitted to touch.",
            },
        },
        "required": ["diff", "target_files"],
    },
)


# Matches the `diff --git a/<path> b/<path>` header line that git emits.
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)$", re.MULTILINE)


def _extract_paths_from_diff(diff: str) -> list[str]:
    """Return the set of file paths touched by the unified diff."""
    paths: set[str] = set()
    for m in _DIFF_HEADER_RE.finditer(diff):
        paths.add(m.group("a"))
        paths.add(m.group("b"))
    # Fallback: some minimal diffs skip the git header and only have
    # `--- a/x` / `+++ b/x`. Parse those too.
    for line in diff.splitlines():
        if line.startswith("--- a/"):
            paths.add(line[len("--- a/") :].strip())
        elif line.startswith("+++ b/"):
            paths.add(line[len("+++ b/") :].strip())
    return sorted(paths)


async def _handle_apply_patch(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    diff = tool_input.get("diff")
    target_files = tool_input.get("target_files") or []
    if not diff or not isinstance(diff, str):
        return ToolResult(ok=False, error="diff is required (non-empty string)")
    if not isinstance(target_files, list) or not target_files:
        return ToolResult(
            ok=False, error="target_files must be a non-empty list"
        )
    if not all(isinstance(f, str) and f for f in target_files):
        return ToolResult(
            ok=False, error="every target_files entry must be a non-empty string"
        )

    # Reject patches that reach outside the declared scope.
    touched = _extract_paths_from_diff(diff)
    if not touched:
        return ToolResult(
            ok=False,
            error="patch_has_no_file_headers: git-apply requires `--- a/` and `+++ b/` lines",
        )
    allowed = set(target_files)
    out_of_scope = sorted(p for p in touched if p not in allowed)
    if out_of_scope:
        return ToolResult(
            ok=False,
            error=f"patch_touches_unlisted_files: {out_of_scope}",
        )

    stdin = diff.encode("utf-8")

    # 1. Dry-run validation with `git apply --check` so we fail loudly
    #    instead of leaving a half-applied workspace.
    try:
        ec, _, err_out = await _run_git_with_stdin(
            ctx.repo_workspace_path, ["apply", "--check"], stdin
        )
    except RuntimeError as exc:
        return ToolResult(ok=False, error=str(exc))
    if ec != 0:
        return ToolResult(
            ok=False,
            error=f"git_apply_check_failed: {err_out.strip() or f'exit={ec}'}",
        )

    # 2. Actually apply.
    try:
        ec, _, err_out = await _run_git_with_stdin(
            ctx.repo_workspace_path, ["apply"], stdin
        )
    except RuntimeError as exc:
        return ToolResult(ok=False, error=str(exc))
    if ec != 0:
        return ToolResult(
            ok=False,
            error=f"git_apply_failed: {err_out.strip() or f'exit={ec}'}",
        )

    # Patched files mean any prior sandbox verification is stale.
    ctx.invalidate_sandbox_verification()
    ctx.last_attempted_diff = diff

    # Propagate the change into the sandbox. Without this the sandbox
    # keeps its original (pre-patch) workspace copy and `run_in_sandbox`
    # never sees the fix — verification loops forever against stale
    # content. If sync fails for any file, surface it so the agent
    # doesn't declare success on a half-synced workspace.
    sync_failures: list[tuple[str, str]] = []
    if ctx.sandbox_container_id:
        sync_failures = await _sync_patched_files_to_sandbox(
            ctx.sandbox_container_id, ctx.repo_workspace_path, touched
        )
    if sync_failures:
        log.error(
            "v2.tools.apply_patch.sandbox_sync_failed",
            ci_fix_run_id=ctx.ci_fix_run_id,
            failures=sync_failures,
        )
        return ToolResult(
            ok=False,
            error=(
                "sandbox_sync_failed: patched on host but could not copy "
                "to sandbox — "
                + "; ".join(f"{f}: {e}" for f, e in sync_failures)
            ),
        )

    log.info(
        "v2.tools.apply_patch.applied",
        ci_fix_run_id=ctx.ci_fix_run_id,
        files=touched,
        synced_to_sandbox=bool(ctx.sandbox_container_id),
    )
    return ToolResult(
        ok=True,
        data={
            "applied_to": touched,
            "file_count": len(touched),
            "diff_bytes": len(stdin),
        },
    )


class _ApplyPatchTool:
    schema = APPLY_PATCH_SCHEMA
    handler = staticmethod(_handle_apply_patch)


_apply_patch_tool = _ApplyPatchTool()
register(_apply_patch_tool)


# ─────────────────────────────────────────────────────────────────────────────
# replace_in_file  (coder-subagent only)
# ─────────────────────────────────────────────────────────────────────────────
#
# Default edit primitive for the coder. Matches the industry-standard
# LLM-file-edit interface (Claude Code / Cursor / Aider) — find a
# literal old_string, replace with new_string, no line numbers, no
# hunk headers, no context-match rules.
#
# Why not unified-diff? LLMs generate unified diffs with ~10–30% silent
# failure rate on multi-line edits because the format requires
# byte-perfect recall of context lines + correct line numbers.
# replace_in_file uses plain-string addressing so the common case —
# "add a test block at the end", "swap + for *", "remove this
# describe(...) block" — never runs into diff-format brittleness.
#
# apply_patch stays for complex multi-site edits where a unique anchor
# is hard to find; the coder prompt pushes replace_in_file first.

REPLACE_IN_FILE_SCHEMA = ToolSchema(
    name="replace_in_file",
    description=(
        "Replace a literal substring in a workspace file. Preferred "
        "over apply_patch for most edits — find-and-replace avoids the "
        "brittleness of unified-diff context matching.\n\n"
        "Contract: `old_string` must appear in the file verbatim. If "
        "it appears 0 times, returns error='not_found'. If it appears "
        "more than once and occurrence='unique' (default), returns "
        "error='ambiguous' with line numbers — widen old_string so it "
        "matches exactly one location. Use occurrence='all' only when "
        "you intend to replace every occurrence.\n\n"
        "Common patterns:\n"
        "  - append block at EOF: old_string = last N bytes of file, "
        "    new_string = those bytes + your additions\n"
        "  - delete block: old_string = whole block, new_string = ''\n"
        "  - tweak line: old_string = exact line, new_string = new line\n\n"
        "path MUST be in target_files. After a successful replace, "
        "sandbox verification is invalidated: you must run_in_sandbox "
        "the original failing command before the main agent can commit."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Workspace-relative path to the file to modify. "
                    "MUST be listed in target_files."
                ),
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Literal substring to find. Empty string is rejected "
                    "— to create a new file, use apply_patch instead."
                ),
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement string. Can be empty (to delete the "
                    "matched region)."
                ),
            },
            "target_files": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Scope — path MUST be one of these.",
            },
            "occurrence": {
                "type": "string",
                "enum": ["unique", "all"],
                "default": "unique",
                "description": (
                    "`unique` requires old_string appear exactly once. "
                    "`all` replaces every occurrence. Default `unique` "
                    "because ambiguous replaces are usually bugs."
                ),
            },
        },
        "required": ["path", "old_string", "new_string", "target_files"],
    },
)


def _line_of_offset(text: str, offset: int) -> int:
    """1-based line number containing the byte offset. For error
    messages so the coder can see where its ambiguous matches landed."""
    return text[:offset].count("\n") + 1


async def _handle_replace_in_file(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    from pathlib import Path

    path = tool_input.get("path")
    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    target_files = tool_input.get("target_files") or []
    occurrence = tool_input.get("occurrence") or "unique"

    # ── input validation ────────────────────────────────────────────
    if not path or not isinstance(path, str):
        return ToolResult(ok=False, error="path is required (non-empty string)")
    if old_string is None or not isinstance(old_string, str):
        return ToolResult(ok=False, error="old_string is required (string)")
    if old_string == "":
        return ToolResult(
            ok=False,
            error=(
                "empty_old_string: use apply_patch to create new files; "
                "replace_in_file only mutates existing files"
            ),
        )
    if new_string is None or not isinstance(new_string, str):
        return ToolResult(ok=False, error="new_string is required (string, may be empty)")
    if not isinstance(target_files, list) or not target_files:
        return ToolResult(ok=False, error="target_files must be a non-empty list")
    if not all(isinstance(f, str) and f for f in target_files):
        return ToolResult(
            ok=False, error="every target_files entry must be a non-empty string"
        )
    if occurrence not in ("unique", "all"):
        return ToolResult(
            ok=False,
            error=f"occurrence must be 'unique' or 'all', got {occurrence!r}",
        )

    # ── scope + path safety ─────────────────────────────────────────
    if path not in set(target_files):
        return ToolResult(
            ok=False,
            error=f"path_not_in_target_files: {path!r} not in {sorted(target_files)}",
        )
    if ".." in Path(path).parts or Path(path).is_absolute():
        return ToolResult(
            ok=False,
            error=f"unsafe_path: {path!r} must be workspace-relative, no '..'",
        )

    file_path = Path(ctx.repo_workspace_path) / path
    if not file_path.exists():
        return ToolResult(
            ok=False,
            error=(
                f"file_not_found: {path!r} does not exist in workspace. "
                "Use apply_patch to create new files."
            ),
        )

    # ── read, find, write ───────────────────────────────────────────
    try:
        content = file_path.read_text()
    except UnicodeDecodeError as exc:
        return ToolResult(
            ok=False,
            error=f"non_text_file: {path!r} is not valid UTF-8 ({exc})",
        )

    count = content.count(old_string)
    if count == 0:
        return ToolResult(
            ok=False,
            error=(
                "not_found: old_string does not appear in the file. "
                "Re-read the file to get the exact current bytes "
                "(whitespace, trailing newlines matter) and retry."
            ),
        )
    if count > 1 and occurrence == "unique":
        # Locate every occurrence for the coder's diagnosis.
        positions: list[int] = []
        start = 0
        while True:
            idx = content.find(old_string, start)
            if idx == -1:
                break
            positions.append(_line_of_offset(content, idx))
            start = idx + 1
        return ToolResult(
            ok=False,
            error=(
                f"ambiguous: old_string matches {count} locations "
                f"(lines {positions}). Widen old_string with enough "
                "surrounding context to match exactly one site, or "
                "pass occurrence='all' if you intend to replace every "
                "occurrence."
            ),
        )

    # Perform the replacement — unique (count==1) or all.
    if occurrence == "all":
        new_content = content.replace(old_string, new_string)
        replacements = count
    else:
        new_content = content.replace(old_string, new_string, 1)
        replacements = 1

    try:
        file_path.write_text(new_content)
    except OSError as exc:
        return ToolResult(ok=False, error=f"write_failed: {exc}")

    # ── ctx mutations (mirror apply_patch contract) ─────────────────
    ctx.invalidate_sandbox_verification()

    # ── sandbox sync ────────────────────────────────────────────────
    sync_failures: list[tuple[str, str]] = []
    if ctx.sandbox_container_id:
        sync_failures = await _sync_patched_files_to_sandbox(
            ctx.sandbox_container_id, ctx.repo_workspace_path, [path]
        )
    if sync_failures:
        log.error(
            "v2.tools.replace_in_file.sandbox_sync_failed",
            ci_fix_run_id=ctx.ci_fix_run_id,
            failures=sync_failures,
        )
        return ToolResult(
            ok=False,
            error=(
                "sandbox_sync_failed: edited on host but could not copy "
                "to sandbox — "
                + "; ".join(f"{f}: {e}" for f, e in sync_failures)
            ),
        )

    log.info(
        "v2.tools.replace_in_file.applied",
        ci_fix_run_id=ctx.ci_fix_run_id,
        path=path,
        replacements=replacements,
        old_bytes=len(old_string),
        new_bytes=len(new_string),
        synced_to_sandbox=bool(ctx.sandbox_container_id),
    )
    return ToolResult(
        ok=True,
        data={
            "applied_to": [path],
            "replacements": replacements,
            "old_bytes": len(old_string),
            "new_bytes": len(new_string),
            "bytes_delta": len(new_string) - len(old_string),
        },
    )


class _ReplaceInFileTool:
    schema = REPLACE_IN_FILE_SCHEMA
    handler = staticmethod(_handle_replace_in_file)


_replace_in_file_tool = _ReplaceInFileTool()
register(_replace_in_file_tool)


# ─────────────────────────────────────────────────────────────────────────────
# delegate_to_coder  (main-agent-only)
# ─────────────────────────────────────────────────────────────────────────────

DELEGATE_TO_CODER_SCHEMA = ToolSchema(
    name="delegate_to_coder",
    description=(
        "Hand a bounded patch plan to the Sonnet coder subagent. The "
        "subagent applies the patch (within target_files only), re-runs "
        "the original failing command in sandbox, and returns a verified "
        "unified diff. Use this for every code change — the main agent "
        "does not mutate the workspace directly. The coder's sandbox "
        "verification flips the main agent's verification gate, "
        "unblocking commit_and_push."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": (
                    "Specific patch plan. Bad: 'fix the lint error.' "
                    "Good: 'Wrap the string literal on line 42 of app/api.py "
                    "across two lines to satisfy E501.'"
                ),
            },
            "target_files": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Files the coder is permitted to edit. The subagent "
                    "rejects any patch that touches paths outside this list."
                ),
            },
            "diagnosis_summary": {
                "type": "string",
                "description": (
                    "One-paragraph diagnosis summary the coder uses to "
                    "orient itself."
                ),
            },
            "failing_command": {
                "type": "string",
                "description": (
                    "Exact CI command the coder must run in sandbox and see "
                    "pass. Usually the value of AgentContext.original_failing_command."
                ),
            },
            "max_attempts": {
                "type": "integer",
                "description": "Soft hint to the subagent (default 3, max 5).",
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": [
            "task_description",
            "target_files",
            "diagnosis_summary",
            "failing_command",
        ],
    },
)


# Seam: computes the final unified diff from the workspace after the
# coder loop finishes. Tests patch this to return canned diff text.
async def _compute_final_diff(workspace: str) -> str:
    """Run `git diff HEAD` against the workspace; return the unified diff."""
    try:
        ec, out, _ = await _run_git_with_stdin(
            workspace, ["diff", "HEAD"], b""
        )
    except RuntimeError:
        return ""
    return out if ec == 0 else ""


async def _handle_delegate_to_coder(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    task_description = tool_input.get("task_description")
    target_files = tool_input.get("target_files") or []
    diagnosis_summary = tool_input.get("diagnosis_summary") or ""
    failing_command = tool_input.get("failing_command") or ctx.original_failing_command
    max_attempts_raw = tool_input.get("max_attempts")
    max_attempts = (
        max(1, min(int(max_attempts_raw), 5))
        if isinstance(max_attempts_raw, int)
        else 3
    )

    if not task_description or not isinstance(task_description, str):
        return ToolResult(ok=False, error="task_description is required")
    if not isinstance(target_files, list) or not target_files:
        return ToolResult(ok=False, error="target_files must be a non-empty list")
    if not all(isinstance(f, str) and f for f in target_files):
        return ToolResult(
            ok=False, error="every target_files entry must be a non-empty string"
        )
    if not failing_command:
        return ToolResult(
            ok=False,
            error="failing_command is required (tool input or AgentContext)",
        )

    # Import here to avoid a circular dep: coder_subagent imports from
    # tools (indirectly) for its type annotations.
    from phalanx.ci_fixer_v2.coder_subagent import run_coder_subagent

    coder_result = await run_coder_subagent(
        ctx=ctx,
        task_description=task_description,
        target_files=list(target_files),
        diagnosis_summary=diagnosis_summary,
        failing_command=failing_command,
        max_attempts=max_attempts,
    )

    final_diff = await _compute_final_diff(ctx.repo_workspace_path) if coder_result.success else ""
    if coder_result.success and final_diff:
        ctx.last_attempted_diff = final_diff

    return ToolResult(
        ok=True,
        data={
            "success": coder_result.success,
            "unified_diff": final_diff,
            "sandbox_exit_code": coder_result.sandbox_exit_code,
            "sandbox_stdout_tail": coder_result.sandbox_stdout_tail,
            "sandbox_stderr_tail": coder_result.sandbox_stderr_tail,
            "attempts_used": coder_result.attempts_used,
            "tokens_used": {
                "input": coder_result.sonnet_input_tokens,
                "output": coder_result.sonnet_output_tokens,
                "thinking": coder_result.sonnet_thinking_tokens,
            },
            "notes": coder_result.notes,
            "failing_command_matched": ctx.last_sandbox_verified,
        },
    )


class _DelegateToCoderTool:
    schema = DELEGATE_TO_CODER_SCHEMA
    handler = staticmethod(_handle_delegate_to_coder)


_delegate_to_coder_tool = _DelegateToCoderTool()
register(_delegate_to_coder_tool)
