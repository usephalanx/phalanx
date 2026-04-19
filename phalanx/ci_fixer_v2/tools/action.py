"""Action tools — capabilities that cause real-world side effects.

Currently implemented in Week 1.5:
  - run_in_sandbox: execute a command inside the provisioned sandbox container

Pending (Week 1.6):
  - delegate_to_coder, commit_and_push, open_fix_pr_against_author_branch,
    comment_on_pr, escalate

Guarantees (spec §N3 — sandbox-only validation):
  - run_in_sandbox is the ONLY way to produce a sandbox_verified signal.
    There is NO local-subprocess fallback path. If the sandbox is not
    provisioned, this tool returns an error result and the agent must
    escalate.
  - On exit_code == 0 AND command_run covers context.original_failing_command,
    the verification gate flips. Any later patch write MUST invalidate the
    gate before commit_and_push can succeed.
"""

from __future__ import annotations

import asyncio
import time
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
# run_in_sandbox
# ─────────────────────────────────────────────────────────────────────────────

# Floor / ceiling for the command timeout so the LLM can't set absurd values.
_TIMEOUT_MIN_S: int = 5
_TIMEOUT_MAX_S: int = 600

RUN_IN_SANDBOX_SCHEMA = ToolSchema(
    name="run_in_sandbox",
    description=(
        "Execute a shell command inside the provisioned sandbox container. "
        "This is the ONLY trusted validation channel — commit_and_push is "
        "blocked until a run_in_sandbox call has executed the original "
        "failing CI command (or a strict superset of it) and exited 0. "
        "Commands run with the workspace bind-mounted at the container's "
        "working directory."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell command to execute (invoked as `sh -c <command>` "
                    "inside the container)."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Optional timeout in seconds (clamped to [5, 600]). "
                    "Default: 120."
                ),
                "minimum": _TIMEOUT_MIN_S,
                "maximum": _TIMEOUT_MAX_S,
            },
        },
        "required": ["command"],
    },
)


# Test seam: builds the docker-exec argv list. In production this calls
# the v1 helper; tests can patch this directly to avoid docker-cli.
def _build_exec_argv(container_id: str, shell_cmd: str) -> list[str]:
    from phalanx.ci_fixer.sandbox_pool import wrap_shell_cmd_for_container
    from phalanx.config.settings import get_settings

    docker_cmd = get_settings().sandbox_docker_cmd
    return wrap_shell_cmd_for_container(container_id, shell_cmd, docker_cmd=docker_cmd)


# Test seam: runs argv with a timeout, returns (exit_code, stdout, stderr,
# timed_out, elapsed_seconds). Tests patch this directly to return canned
# outcomes without spawning real subprocesses.
async def _exec_argv(
    argv: list[str], timeout_seconds: int
) -> tuple[int, str, str, bool, float]:
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        # docker binary missing — surface clearly, not as a subprocess error.
        raise RuntimeError(f"docker_binary_missing: {exc}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
        elapsed = time.monotonic() - start
        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
            False,
            elapsed,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # pragma: no cover — best-effort cleanup
            pass
        elapsed = time.monotonic() - start
        return (124, "", "timed_out", True, elapsed)


def _clamp_timeout(requested: Any) -> int:
    if not isinstance(requested, int):
        return 120
    if requested < _TIMEOUT_MIN_S:
        return _TIMEOUT_MIN_S
    if requested > _TIMEOUT_MAX_S:
        return _TIMEOUT_MAX_S
    return requested


async def _handle_run_in_sandbox(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    command = tool_input.get("command")
    if not command or not isinstance(command, str):
        return ToolResult(ok=False, error="command is required (non-empty string)")

    if not ctx.sandbox_container_id:
        return ToolResult(
            ok=False,
            error=(
                "sandbox_not_provisioned: no container available. "
                "Escalate infra_failure_out_of_scope."
            ),
        )

    timeout = _clamp_timeout(tool_input.get("timeout_seconds"))

    try:
        argv = _build_exec_argv(ctx.sandbox_container_id, command)
        exit_code, stdout, stderr, timed_out, elapsed = await _exec_argv(argv, timeout)
    except RuntimeError as exc:
        return ToolResult(ok=False, error=str(exc))

    ctx.cost.sandbox_runtime_seconds += elapsed

    # Verification gate: flip iff command covers original failing command
    # AND exit code is 0. Spec §6.
    verified = False
    if exit_code == 0:
        verified = ctx.mark_sandbox_verified(command)

    log.info(
        "v2.tools.run_in_sandbox.done",
        ci_fix_run_id=ctx.ci_fix_run_id,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=round(elapsed, 2),
        sandbox_verified=verified,
    )

    return ToolResult(
        ok=True,
        data={
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_seconds": round(elapsed, 2),
            "timed_out": timed_out,
            "sandbox_verified": verified,
        },
    )


class _RunInSandboxTool:
    schema = RUN_IN_SANDBOX_SCHEMA
    handler = staticmethod(_handle_run_in_sandbox)


_run_in_sandbox_tool = _RunInSandboxTool()
register(_run_in_sandbox_tool)


# ─────────────────────────────────────────────────────────────────────────────
# Shared GitHub-write seam  (Week 1.6c)
# ─────────────────────────────────────────────────────────────────────────────


async def _call_github_post(
    path: str, api_key: str, json_body: dict[str, Any]
) -> tuple[int, str, Any]:
    """Test seam for GitHub REST POSTs. Tests patch this symbol on the
    `action` module to intercept comment/PR writes without HTTP.
    """
    from phalanx.ci_fixer_v2.tools._github_api import github_post

    return await github_post(path, api_key, json_body)


# ─────────────────────────────────────────────────────────────────────────────
# comment_on_pr  (Week 1.6c)
# ─────────────────────────────────────────────────────────────────────────────

COMMENT_ON_PR_SCHEMA = ToolSchema(
    name="comment_on_pr",
    description=(
        "Post a markdown comment on a PR (under the Issues API, which is "
        "how GitHub exposes PR comments). Use this every time you commit "
        "or escalate — the author should understand your diagnosis and "
        "fix rationale, not just see a commit appear on their branch."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": (
                    "Markdown comment body. Must explain: diagnosis, what "
                    "you changed (or why you didn't change anything), and "
                    "how the author should proceed."
                ),
            },
            "pr_number": {
                "type": "integer",
                "description": (
                    "PR number. Defaults to AgentContext.pr_number if omitted."
                ),
            },
        },
        "required": ["body"],
    },
)


async def _handle_comment_on_pr(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    body = tool_input.get("body")
    if not body or not isinstance(body, str):
        return ToolResult(ok=False, error="body is required (non-empty string)")
    pr_number = tool_input.get("pr_number") or ctx.pr_number
    if not pr_number:
        return ToolResult(
            ok=False,
            error="pr_number is required (and AgentContext.pr_number is unset)",
        )
    if not ctx.ci_api_key:
        return ToolResult(ok=False, error="ci_api_key is not set on AgentContext")

    try:
        status, _text, parsed = await _call_github_post(
            f"/repos/{ctx.repo_full_name}/issues/{pr_number}/comments",
            ctx.ci_api_key,
            {"body": body},
        )
    except Exception as exc:
        log.warning("v2.tools.comment_on_pr.error", error=str(exc))
        return ToolResult(ok=False, error=f"github_call_failed: {exc}")

    if status not in (200, 201) or not isinstance(parsed, dict):
        return ToolResult(
            ok=False,
            error=f"github_api_error: status={status}",
        )

    return ToolResult(
        ok=True,
        data={
            "comment_id": parsed.get("id"),
            "url": parsed.get("html_url") or "",
            "pr_number": pr_number,
        },
    )


class _CommentOnPRTool:
    schema = COMMENT_ON_PR_SCHEMA
    handler = staticmethod(_handle_comment_on_pr)


_comment_on_pr_tool = _CommentOnPRTool()
register(_comment_on_pr_tool)


# ─────────────────────────────────────────────────────────────────────────────
# open_fix_pr_against_author_branch  (Week 1.6c)
# ─────────────────────────────────────────────────────────────────────────────
# Used when Phalanx does NOT have write permission to the author's PR branch.
# Phalanx pushed to its own fix branch (phalanx/ci-fix/{run_id}); this tool
# opens a PR whose `base` is the author's PR head branch and whose `head`
# is Phalanx's fix branch. Merging it brings the fix into the author's PR.

OPEN_FIX_PR_SCHEMA = ToolSchema(
    name="open_fix_pr_against_author_branch",
    description=(
        "Open a pull request proposing the CI fix against the author's PR "
        "branch. Use this when has_write_permission is False. `head_branch` "
        "should be the Phalanx fix branch (phalanx/ci-fix/{run_id}) that "
        "commit_and_push already pushed; `base_branch` is the author's PR "
        "head_branch (not the repo's default branch)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "PR title — short, describes the fix.",
            },
            "body": {
                "type": "string",
                "description": (
                    "PR body markdown — diagnosis, what was changed, how "
                    "to verify locally. Author reviews this before merging."
                ),
            },
            "head_branch": {
                "type": "string",
                "description": "Phalanx's fix branch (the commits to merge).",
            },
            "base_branch": {
                "type": "string",
                "description": "Author's PR head branch (the merge target).",
            },
        },
        "required": ["title", "body", "head_branch", "base_branch"],
    },
)


async def _handle_open_fix_pr_against_author_branch(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    title = tool_input.get("title")
    body = tool_input.get("body")
    head_branch = tool_input.get("head_branch")
    base_branch = tool_input.get("base_branch")
    for field_name, val in (
        ("title", title),
        ("body", body),
        ("head_branch", head_branch),
        ("base_branch", base_branch),
    ):
        if not val or not isinstance(val, str):
            return ToolResult(ok=False, error=f"{field_name} is required")
    if not ctx.ci_api_key:
        return ToolResult(ok=False, error="ci_api_key is not set on AgentContext")

    try:
        status, _text, parsed = await _call_github_post(
            f"/repos/{ctx.repo_full_name}/pulls",
            ctx.ci_api_key,
            {
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            },
        )
    except Exception as exc:
        log.warning("v2.tools.open_fix_pr.error", error=str(exc))
        return ToolResult(ok=False, error=f"github_call_failed: {exc}")

    if status not in (200, 201) or not isinstance(parsed, dict):
        return ToolResult(
            ok=False,
            error=f"github_api_error: status={status}",
        )

    return ToolResult(
        ok=True,
        data={
            "pr_number": parsed.get("number"),
            "pr_url": parsed.get("html_url") or "",
            "head_branch": head_branch,
            "base_branch": base_branch,
        },
    )


class _OpenFixPRTool:
    schema = OPEN_FIX_PR_SCHEMA
    handler = staticmethod(_handle_open_fix_pr_against_author_branch)


_open_fix_pr_tool = _OpenFixPRTool()
register(_open_fix_pr_tool)


# ─────────────────────────────────────────────────────────────────────────────
# escalate  (Week 1.6c)
# ─────────────────────────────────────────────────────────────────────────────
# The tool handler records the draft patch + reason on AgentContext so the
# loop's RunOutcome carries it to the finalization step. The loop itself
# terminates on `use.name == "escalate"` — this handler just validates input
# and stores artifacts.

ESCALATE_SCHEMA = ToolSchema(
    name="escalate",
    description=(
        "Clean terminal exit when you are not confident the fix is correct. "
        "Do NOT commit a speculative patch — escalate instead. The loop "
        "terminates immediately; the draft_patch + explanation are recorded "
        "on the run so humans can review what you considered."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": [
                    "low_confidence",
                    "turn_cap_reached",
                    "ambiguous_fix",
                    "preexisting_main_failure",
                    "infra_failure_out_of_scope",
                    "destructive_change_required",
                ],
                "description": "Why you are escalating (one of the listed reasons).",
            },
            "draft_patch": {
                "type": "string",
                "description": (
                    "Optional unified diff of the fix you considered but did "
                    "not commit. Empty when no patch was drafted."
                ),
            },
            "explanation": {
                "type": "string",
                "description": (
                    "Human-readable explanation — what you tried, what you "
                    "saw, and what you'd recommend the author do."
                ),
            },
        },
        "required": ["reason", "explanation"],
    },
)


_VALID_ESCALATION_REASONS = frozenset(
    {
        "low_confidence",
        "turn_cap_reached",
        "ambiguous_fix",
        "preexisting_main_failure",
        "infra_failure_out_of_scope",
        "destructive_change_required",
    }
)


async def _handle_escalate(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    reason = tool_input.get("reason")
    explanation = tool_input.get("explanation") or ""
    draft_patch = tool_input.get("draft_patch") or ""

    if reason not in _VALID_ESCALATION_REASONS:
        return ToolResult(
            ok=False,
            error=f"invalid_reason: {reason!r} (must be one of {sorted(_VALID_ESCALATION_REASONS)})",
        )
    if not explanation.strip():
        return ToolResult(
            ok=False,
            error="explanation is required (non-empty string)",
        )

    # Persist the drafted diff on the context so the loop's RunOutcome
    # can surface it in `last_attempted_diff`. The loop handles terminal
    # dispatch separately (sees name='escalate' and returns ESCALATED).
    if draft_patch:
        ctx.last_attempted_diff = draft_patch

    return ToolResult(
        ok=True,
        data={
            "acknowledged": True,
            "reason": reason,
            "explanation": explanation,
            "draft_patch_bytes": len(draft_patch),
        },
    )


class _EscalateTool:
    schema = ESCALATE_SCHEMA
    handler = staticmethod(_handle_escalate)


_escalate_tool = _EscalateTool()
register(_escalate_tool)


# ─────────────────────────────────────────────────────────────────────────────
# commit_and_push  (Week 1.6d)
# ─────────────────────────────────────────────────────────────────────────────
# Two strategies (spec §1 + §4.3):
#   - "author_branch": commit directly onto the author's PR head branch.
#     Requires AgentContext.has_write_permission AND author_head_branch set.
#   - "fix_branch":   commit onto a Phalanx-owned branch named
#     phalanx/ci-fix/{ci_fix_run_id}. Used when we do NOT have write
#     permission; paired with open_fix_pr_against_author_branch.
#
# This tool is gated by the loop: commit_and_push is blocked if
# last_sandbox_verified is False. Gate enforcement lives in agent.py, not here.


COMMIT_AND_PUSH_SCHEMA = ToolSchema(
    name="commit_and_push",
    description=(
        "Stage the given files, commit with the given message, and push to "
        "origin. Chooses a branch by strategy: 'author_branch' writes onto "
        "the author's PR branch (requires write permission); 'fix_branch' "
        "writes onto phalanx/ci-fix/{run_id} (used when no write permission "
        "— pair with open_fix_pr_against_author_branch). You MUST have run "
        "the original failing command in sandbox and seen it pass before "
        "calling this tool; the loop rejects unverified commits."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "branch_strategy": {
                "type": "string",
                "enum": ["author_branch", "fix_branch"],
                "description": (
                    "'author_branch' (requires has_write_permission=True) "
                    "or 'fix_branch' (Phalanx-owned fix branch)."
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Commit subject + body. Should explain the fix.",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Files to include in the commit, relative to workspace "
                    "root. Only these exact paths are staged."
                ),
            },
        },
        "required": ["branch_strategy", "commit_message", "files"],
    },
)


# Test seam for git subprocess calls. Returns (exit_code, stdout, stderr).
async def _run_git_command(
    workspace: str, args: list[str], timeout: int = 60
) -> tuple[int, str, str]:
    """Run `git -C {workspace} {args...}` with bounded timeout."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            workspace,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"git_binary_missing: {exc}") from exc

    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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


def _resolve_commit_branch(
    ctx: AgentContext, strategy: str
) -> tuple[str | None, str | None]:
    """Return (branch_name, error). Exactly one is non-None."""
    if strategy == "author_branch":
        if not ctx.has_write_permission:
            return (
                None,
                "author_branch_requires_write_permission: use "
                "fix_branch strategy + open_fix_pr_against_author_branch",
            )
        if not ctx.author_head_branch:
            return (
                None,
                "author_head_branch missing from AgentContext (run "
                "bootstrap must seed it from PR payload)",
            )
        return ctx.author_head_branch, None
    if strategy == "fix_branch":
        return f"phalanx/ci-fix/{ctx.ci_fix_run_id}", None
    return None, f"invalid_branch_strategy: {strategy!r}"


async def _handle_commit_and_push(
    ctx: AgentContext, tool_input: dict[str, Any]
) -> ToolResult:
    strategy = tool_input.get("branch_strategy")
    commit_message = tool_input.get("commit_message")
    files = tool_input.get("files")

    if strategy not in ("author_branch", "fix_branch"):
        return ToolResult(
            ok=False, error=f"branch_strategy must be author_branch|fix_branch"
        )
    if not commit_message or not isinstance(commit_message, str):
        return ToolResult(ok=False, error="commit_message is required")
    if not isinstance(files, list) or not files:
        return ToolResult(ok=False, error="files must be a non-empty list")
    if not all(isinstance(f, str) and f for f in files):
        return ToolResult(ok=False, error="every files entry must be a non-empty string")

    branch, err = _resolve_commit_branch(ctx, strategy)
    if err or not branch:
        return ToolResult(ok=False, error=err or "branch_resolution_failed")

    from phalanx.config.settings import get_settings

    _settings = get_settings()
    # Prefer v2-specific CI Fixer identity; fall back to the legacy
    # global identity if the v2 setting is empty. Keeps v1 agents'
    # commit attribution unchanged.
    author_name = _settings.git_author_name_ci_fixer or _settings.git_author_name
    author_email = _settings.git_author_email_ci_fixer or _settings.git_author_email

    # Shared git-config flags so we don't mutate the workspace's config.
    config_flags = [
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
    ]

    try:
        # 1. Ensure we're on the target branch. -B force-creates for fix_branch;
        #    for author_branch we expect the clone to already be on it but
        #    switching is idempotent.
        ec, _, err_out = await _run_git_command(
            ctx.repo_workspace_path,
            ["checkout", "-B", branch],
        )
        if ec != 0:
            return ToolResult(
                ok=False,
                error=f"git_checkout_failed: {err_out.strip() or f'exit={ec}'}",
            )

        # 2. Stage ONLY the listed files (-- to terminate options so file
        #    names starting with '-' don't get misparsed).
        ec, _, err_out = await _run_git_command(
            ctx.repo_workspace_path,
            ["add", "--", *files],
        )
        if ec != 0:
            return ToolResult(
                ok=False,
                error=f"git_add_failed: {err_out.strip() or f'exit={ec}'}",
            )

        # 3. Commit. If nothing is staged (agent requested a no-op commit),
        #    git returns non-zero; surface it as a clean error.
        ec, out, err_out = await _run_git_command(
            ctx.repo_workspace_path,
            [*config_flags, "commit", "-m", commit_message],
        )
        if ec != 0:
            msg = err_out.strip() or out.strip() or f"exit={ec}"
            return ToolResult(ok=False, error=f"git_commit_failed: {msg}")

        # 4. Capture the commit sha for return.
        ec, sha_out, _ = await _run_git_command(
            ctx.repo_workspace_path, ["rev-parse", "HEAD"]
        )
        if ec != 0:
            return ToolResult(
                ok=False,
                error="git_rev_parse_failed: could not read committed HEAD",
            )
        sha = sha_out.strip()

        # 5. Push to origin. --set-upstream so the fix branch tracks.
        ec, _, err_out = await _run_git_command(
            ctx.repo_workspace_path,
            ["push", "--set-upstream", "origin", branch],
            timeout=120,  # pushes are slower than local ops
        )
        if ec != 0:
            return ToolResult(
                ok=False,
                error=f"git_push_failed: {err_out.strip() or f'exit={ec}'}",
            )
    except RuntimeError as exc:
        return ToolResult(ok=False, error=str(exc))

    # Pushed successfully — invalidate the verification flag so a follow-up
    # workspace change requires re-verification before any further commit.
    ctx.invalidate_sandbox_verification()

    log.info(
        "v2.tools.commit_and_push.done",
        ci_fix_run_id=ctx.ci_fix_run_id,
        strategy=strategy,
        branch=branch,
        sha=sha,
    )
    return ToolResult(
        ok=True,
        data={
            "sha": sha,
            "branch": branch,
            "strategy": strategy,
            "pushed": True,
            "files_committed": list(files),
        },
    )


class _CommitAndPushTool:
    schema = COMMIT_AND_PUSH_SCHEMA
    handler = staticmethod(_handle_commit_and_push)


_commit_and_push_tool = _CommitAndPushTool()
register(_commit_and_push_tool)
